"""BCON 設定。接続の写しを表示し、サービスの直列操作へ要求を渡す。

Tk の値は操作要求前に読み、受信コールバックは UiInbox とログキューへ積む。
ポートと HELLO、入力送信、受信の所有者は SerialService とする。
"""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
import tkinter.messagebox as tkmsg
import tkinter.ttk as ttk
from collections.abc import Callable
from typing import Any

from LogPane import DropOldestQueue
from core.connection_state import ConnectionSnapshot
from core.procon_color import (
    _CONFIG_TYPE_NAMES as _CONFIG_TYPE_NAMES,
    build_color_payload as build_color_payload,
    config_reject_type as config_reject_type,
    describe_bcon_errcode as describe_bcon_errcode,
    is_bcon_transport as is_bcon_transport,
    parse_color_hex as parse_color_hex,
    send_config_frame as send_config_frame,
)
from core.transport.bcon import decode_rumble
from core.transport.bcon_protocol import (
    T_BEACON_START,
    T_CAPTURE_START,
    T_COLOR_SET,
    T_KEY_DELETE,
)
from core.ui_inbox import UiInbox, post_latest_to_gui
from services.serial_service import SerialService
from ui.tooltip import Tooltip

BCON_WINDOW_TITLE = "BCON 設定"
_LOGGER = logging.getLogger(__name__)

# 計測の間引き幅。debugへの書き出しはこの秒数に1件までにする。
# per-frameのI/Oにしないための枠で、数え自体は毎回行う。
COUNTERS_LOG_INTERVAL_S = 5.0

# RX→UIの洪水対策（LogPaneのflushQueueと同一規律）。
# 未処理の溜めに上限を設け、超えた分は古い方から捨てる。捨てた件数は
# 「... N行省略 ...」の1行で可視化する（黙って捨てない）。
BCON_QUEUE_MAX = 1024
# 1回の_pollで取り出す上限。洪水でも1 tickで抱え込まない。
# LogPaneのFLUSH_MAX_LINESと同値。rumble/player_infoの連打程度
# （～200件）は1 tickで畳み切れ、溢れは次tickへ残る。
BCON_FLUSH_MAX = 512
# ログ欄に残す行数。Textは行数に比例して重くなる。
BCON_MAX_LINES = 1000


# 取込秒数の受付域。Pico側は秒1-60、範囲外は ERRCODE 0x10 で拒否する。
# 範囲外は送らず注意だけ出す（送って拒否させる運用にしない）。
CAPTURE_MIN_S = 1
CAPTURE_MAX_S = 60

# COLOR_SET の12Bは RGB×4（本体・本体・左・右）の順。画面もこの順で4欄置く。
COLOR_SLOT_NAMES = ("本体", "本体2", "左", "右")
COLOR_DEFAULTS = ("000000", "000000", "000000", "000000")

# プレイヤーLEDの色（PLAYER_INFOのランプ表示）。点灯=黄緑・消灯=灰。
PLAYER_LAMP_ON_COLOR = "green yellow"
PLAYER_LAMP_OFF_COLOR = "gray"

# STATUS flags（SSOTは Switch-bcon の spec/protocol_v3.md §5.5）。
# bit0 USB mounted・bit1 Switch ready・bit2 timeout-neutral中・
# bit3 WDT recovered・bit4 UART overrun・bit5 wired mode・
# bit6 BT connected・bit7 RUMBLE見た（前回STATUS以降のSwitch振動出力）。
STATUS_FLAG_NAMES = (
    "USB接続中",
    "Switch準備OK",
    "無操作中立",
    "WDTから復帰",
    "UART overrun",
    "有線",
    "BT接続中",
    "RUMBLEあり",
)
STATUS_FLAG_WIRED = 0x20

# 色の読みと荷造り・errcode説明の実体は core/procon_color.py にある。
# ここでは旧名のまま読めるよう再公開だけする（利用者台本は
# Commands/CommandColor.py から読む）。


def parse_capture_seconds(raw: Any) -> int | None:
    """取込秒数を読む。1-60の外は None（送らず注意を出す側で使う）。"""
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if CAPTURE_MIN_S <= seconds <= CAPTURE_MAX_S:
        return seconds
    return None


# 色問合せの読取失敗時に出す一文。試験盤と一字一句同じこと。
COLOR_READ_FALLBACK_NOTE = "色の読み取りに失敗しました。000000で表示します。"


def request_color(transport: Any, timeout: float = 1.0) -> bytes | None:
    """本体色の12Bを問う。成功は12B、来なければNone（例外なし）。

    送受の実体は運搬器の `request_color`（COLOR_GET送出＋COLOR_INFO待ち）。
    ここは委譲と形の確認だけ受け持つ。口が無い・壊れた応答でも落とさない。
    """
    try:
        try:
            limit = float(timeout)
        except (TypeError, ValueError):
            limit = 1.0
        method = getattr(transport, "request_color", None)
        if not callable(method):
            return None
        try:
            result = method(timeout=limit)
        except TypeError:
            try:
                result = method(limit)
            except Exception:
                return None
        if result is None:
            return None
        payload = bytes(result)
    except Exception:
        return None
    if len(payload) != 12:
        return None
    return payload


def prefill_color_entries(transport: Any, entries: Any, notify: Any = None) -> None:
    """開いた直後の4欄へ問合せ結果を前埋めする。失敗は000000＋注意。

    entries は `.get()`/`.set()` を持つ4つ組（StringVar相当）。
    notify は注意文を受ける口（`notes.append` 相当）。例外は投げない。
    """
    try:
        items = list(entries)
    except TypeError:
        return
    if len(items) != 4:
        return
    try:
        payload = request_color(transport)
    except Exception:
        payload = None
    if payload is not None and len(payload) == 12:
        for index, item in enumerate(items):
            try:
                red = int(payload[index * 3]) & 0xFF
                green = int(payload[index * 3 + 1]) & 0xFF
                blue = int(payload[index * 3 + 2]) & 0xFF
                item.set(f"{red:02x}{green:02x}{blue:02x}")
            except Exception:
                try:
                    item.set("000000")
                except Exception:
                    pass
        return
    for item in items:
        try:
            item.set("000000")
        except Exception:
            pass
    if callable(notify):
        try:
            notify(COLOR_READ_FALLBACK_NOTE)
        except Exception:
            pass


def update_color_preview(entries: Any, canvas: Any, items: Any) -> None:
    """Entry文字列→Canvasのfillへ映す（打鍵のたびの見た目だけ）。

    不正な欄は前を残す（送らない・落とさない）。例外は投げない。
    """
    try:
        entry_list = list(entries)
        item_list = list(items)
    except TypeError:
        return
    for entry, item in zip(entry_list, item_list):
        try:
            raw = entry.get()
        except Exception:
            continue
        slot = parse_color_hex(raw)
        if slot is None:
            continue
        try:
            red, green, blue = slot
            canvas.itemconfigure(item, fill=f"#{red:02x}{green:02x}{blue:02x}")
        except Exception:
            continue


def format_status_flags(flags: Any) -> str:
    """STATUS flagsを日本語の並びへ。D/M表示切替の相当情報はこれで見る。"""
    try:
        value = int(flags) & 0xFF
    except (TypeError, ValueError):
        return f"flags=不明({flags!r})"
    names = [name for bit, name in enumerate(STATUS_FLAG_NAMES) if value & (1 << bit)]
    if not names:
        return f"flags=0x{value:02X}（なし）"
    return f"flags=0x{value:02X}（{'・'.join(names)}）"


def format_status_block(status: Any) -> str:
    """STATUS_REQ の応答を1画面分にまとめる。読めなければ理由だけ出す。"""
    try:
        flags = int(status["flags"]) & 0xFF
        last_seq = int(status["last_seq"]) & 0xFF
        err_crc = int(status["err_crc"]) & 0xFFFF
        err_drop = int(status["err_drop"]) & 0xFFFF
        errcode = int(status["errcode"]) & 0xFF
    except (TypeError, ValueError, KeyError, AttributeError):
        return f"STATUSの読み取りに失敗しました: {status!r}"
    return (
        f"{format_status_flags(flags)} last_seq={last_seq} "
        f"err_crc={err_crc} err_drop={err_drop} "
        f"errcode=0x{errcode:02X}（{describe_bcon_errcode(errcode)}）"
    )


# PLAYER_INFO の LED / flags は Switch-bcon の spec/protocol_v3.md に従う。
# LEDは下位4bitをそのまま LED1-4 へ（並べ替え無し）。flagsは
# bit0=IMU・bit1=振動・bit2=保存済みで、予約の上位bitは見ない。
# bit2は取込済みwakeのFlash保存ありの写し（BEACON再生可）。

# PLAYER_INFO flags bit2の保存証拠マスク。
PLAYER_FLAG_CAP_SAVED = 0x04

# 取込期限の余白秒。期限は要求秒数＋この秒数の壁時計で切る。
CAPTURE_DEADLINE_MARGIN_S = 5

# 取込期限の表示文。成功文は単一定数で一字一句固定する。
SAVE_SUCCESS_TEXT = "取込を保存しました。"
SAVE_TIMEOUT_TEXT = "取込期間が終了しましたが保存を確認できませんでした。"


def decode_player_lamp(byte: int) -> tuple[bool, bool, bool, bool]:
    """PLAYER_INFO の LED バイトを下位4bitそのまま4つの真偽へ。

    bit0=LED1, bit1=LED2, bit2=LED3, bit3=LED4。並べ替えはしない。
    窓口（_update_player_lamps）で据え置き/全消灯を決める手前の復号段。
    """
    value = int(byte) & 0x0F
    return (
        bool((value >> 0) & 1),
        bool((value >> 1) & 1),
        bool((value >> 2) & 1),
        bool((value >> 3) & 1),
    )


def decode_player_flags(byte: int) -> tuple[bool, bool]:
    """PLAYER_INFO の flags を (IMU, 振動) へ。予約の上位bitは見ない。"""
    value = int(byte) & 0xFF
    return (bool((value >> 0) & 1), bool((value >> 1) & 1))


def decode_player_save(byte: int) -> bool:
    """PLAYER_INFO の flags bit2を保存済みへ。予約bit3-7は見ない。"""
    value = int(byte) & 0xFF
    return bool(value & PLAYER_FLAG_CAP_SAVED)


# PLAYER_INFO flags bit3-4/bit5-6 は Switch が 0x41 で要求した IMU レンジ。
# FW が ACK 時に保持した値を可視化用に載せている（旧FWは0のまま）。
GYRO_RANGE_LABELS = ("±250dps", "±500dps", "±1000dps", "±2000dps")
ACCEL_RANGE_LABELS = ("±8G", "±4G", "±2G", "±16G")


def decode_player_imu_range(byte: int) -> tuple[str, str]:
    """PLAYER_INFO flags の IMU要求レンジを表示名へ。未知bitは見ない。"""
    value = int(byte) & 0xFF
    return (
        GYRO_RANGE_LABELS[(value >> 3) & 0x3],
        ACCEL_RANGE_LABELS[(value >> 5) & 0x3],
    )


def _pre_status_snapshot(transport: Any) -> dict[str, Any] | None:
    """送出前のSTATUS写しを取る。線に触れず、口が無ければNone。

    旧運搬器・偽物に last_status が無くても落とさない。
    """
    try:
        current = getattr(transport, "last_status", None)
        if not callable(current):
            return None
        snapshot = current()
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    try:
        return dict(snapshot)
    except (TypeError, ValueError):
        return None


class BconSetup:
    """bcon設定の小窓。1つだけ開く前提で使う。"""

    def __init__(
        self,
        master: Any,
        service: SerialService,
        *,
        embedded: bool = False,
        command_busy: Callable[[], bool] | None = None,
    ) -> None:
        """接続と会話の所有者はサービス。画面は写しと操作要求だけを扱う。"""
        self._service = service
        self._command_busy = command_busy or (lambda: False)
        self._snapshot = service.connection_snapshot()
        self._unsupported: set[str] = set()
        self._color_loaded = False
        self.inbox = UiInbox()
        self._visible = False
        self._visibility_revision = 0
        self._poll_id: Any = None
        self._connection_unsub: Callable[[], None] | None = None
        self._log_unsub: Callable[[], None] | None = None
        self._embedded = embedded
        self._queue: DropOldestQueue = DropOldestQueue(maxsize=BCON_QUEUE_MAX)
        self._queue_dropped_total = 0
        self._busy = False
        self._stop = False
        self._closed = False
        # 常時計測の素朴な数え（H1/H2/H3切り分け用・永置）。
        # int/float/perf_counterだけで、per-frameのI/Oはしない。
        self._poll_ticks = 0
        self._poll_drained_total = 0
        self._poll_last_drained = 0
        self._poll_last_ms = 0.0
        self._poll_max_ms = 0.0
        self._queue_max_qsize = 0
        self._rx_counts: dict[int, int] = {}
        self._rx_total = 0
        self._rx_start_t = 0.0
        self._counters_log_t = 0.0
        # 取込期限の保存証拠とafter予約。期限は要求秒数＋余白の壁時計。
        self._capture_saved_seen = False
        self._capture_deadline_id: Any = None

        if embedded:
            # タブ枠は Notebook が持つ。close で枠ごと壊さず中身（_body）だけ壊す
            self.window = master
        else:
            self.window = tk.Toplevel(master)
            self.window.title(BCON_WINDOW_TITLE)
            self.window.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self.window, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self._body = body

        # タブでは操作列と情報列を、幅が足りれば左右に、足りなければ縦に並べる
        # （_reflow_columns）。常に左右だと 800px 前後を要し、タブ欄の要求幅が
        # 一番広いタブで決まるため、他のタブを開いていても横スクロールが出た。
        # 別窓は従来どおり縦積み（同じ親へ積む＝列を分けない）。
        self._two_columns: bool | None = None
        if embedded:
            col_ops = ttk.Frame(body)
            col_info = ttk.Frame(body)
            # 要求幅は狭い方（縦積み）の幅に固定し、実際の幅で並べ方を決める。
            # 中身から要求幅を決めさせると、左右に並べた広さが要求として残り、
            # 窓を狭めても縦積みへ戻れない。
            body.grid_propagate(False)
            body.columnconfigure(1, weight=1)
            body.bind("<Configure>", lambda _e: self._reflow_columns(), add="+")
        else:
            col_ops = col_info = body
        self._col_ops = col_ops
        self._col_info = col_info

        # 実行中は全ボタンを止める。止めた旨の表示だけ別に置く。
        self._busy_label = ttk.Label(col_ops, text="")
        self._busy_label.pack(anchor=tk.W)
        self._summary = ttk.Label(
            col_ops, text="未接続", wraplength=330, justify=tk.LEFT
        )
        self._summary.pack(fill=tk.X, pady=(0, 8))

        grp_connect = ttk.LabelFrame(col_ops, text="接続", padding=8)
        grp_connect.pack(fill=tk.X, pady=(0, 8))
        grp_connect.columnconfigure(0, weight=1, uniform="btn")
        grp_connect.columnconfigure(1, weight=1, uniform="btn")
        self._btn_connect = ttk.Button(
            grp_connect,
            text="HELLO をやり直す",
            command=self._on_connect,
        )
        self._btn_connect.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            padx=2,
            pady=2,
        )
        self._btn_status = ttk.Button(
            grp_connect,
            text="状態確認",
            command=self._on_status,
        )
        self._btn_status.grid(row=1, column=0, sticky=tk.EW, padx=2, pady=2)
        self._btn_ping = ttk.Button(
            grp_connect,
            text="疎通確認（PING）",
            command=self._on_ping,
        )
        self._btn_ping.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)

        grp_pair = ttk.LabelFrame(
            col_ops,
            text="無線ペアリング（無線モードのみ）",
            padding=8,
        )
        grp_pair.pack(fill=tk.X, pady=(0, 8))
        for column in range(4):
            grp_pair.columnconfigure(column, weight=1, uniform="btn")
        ttk.Label(grp_pair, text="取込").grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        self._seconds = tk.IntVar(value=15)
        self._seconds_input = ttk.Spinbox(
            grp_pair,
            from_=1,
            to=60,
            width=4,
            textvariable=self._seconds,
        )
        self._seconds_input.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        ttk.Label(grp_pair, text="秒").grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        self._btn_cap = ttk.Button(
            grp_pair,
            text="取込開始",
            command=self._on_capture,
        )
        self._btn_cap.grid(row=0, column=3, sticky=tk.EW, padx=2, pady=2)
        self._btn_beacon = ttk.Button(
            grp_pair,
            text="保存した入力を再生",
            command=self._on_beacon,
        )
        self._btn_beacon.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            padx=2,
            pady=2,
        )
        self._btn_reconnect = ttk.Button(
            grp_pair,
            text="再接続を試す",
            command=self._on_reconnect,
        )
        self._btn_reconnect.grid(
            row=1,
            column=2,
            columnspan=2,
            sticky=tk.EW,
            padx=2,
            pady=2,
        )

        ttk.Label(
            grp_pair,
            text="無線モード・Pico と Switch が未接続・取込時間内に Joy-Con の HOME を長押し。PC と Pico の接続は維持",
            wraplength=310,
        ).grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=4)

        grp_mode = ttk.LabelFrame(
            col_ops,
            text="動作モード（変更すると再起動）",
            padding=8,
        )
        grp_mode.pack(fill=tk.X, pady=(0, 8))
        for column in (1, 2, 3):
            grp_mode.columnconfigure(column, weight=1, uniform="btn")
        ttk.Label(grp_mode, text="接続方式").grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        self._btn_w1 = ttk.Button(
            grp_mode,
            text="有線にする",
            command=self._on_wired_on,
        )
        self._btn_w1.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._btn_w0 = ttk.Button(
            grp_mode,
            text="無線にする",
            command=self._on_wired_off,
        )
        self._btn_w0.grid(row=0, column=2, sticky=tk.EW, padx=2, pady=2)
        self._btn_winfo = ttk.Button(
            grp_mode,
            text="現在の方式",
            command=self._on_wired_show,
        )
        self._btn_winfo.grid(row=0, column=3, sticky=tk.EW, padx=2, pady=2)
        ttk.Label(grp_mode, text="種別").grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        self._btn_e0 = ttk.Button(
            grp_mode,
            text="Pro Con",
            command=lambda: self._on_emulate_set(0),
        )
        self._btn_e0.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)
        self._btn_e1 = ttk.Button(
            grp_mode,
            text="Joy-Con L",
            command=lambda: self._on_emulate_set(1),
        )
        self._btn_e1.grid(row=1, column=2, sticky=tk.EW, padx=2, pady=2)
        self._btn_e2 = ttk.Button(
            grp_mode,
            text="Joy-Con R",
            command=lambda: self._on_emulate_set(2),
        )
        self._btn_e2.grid(row=1, column=3, sticky=tk.EW, padx=2, pady=2)

        # 取り消せない操作だけ集める。他のボタンはここへ置かない
        # （押し間違いを防ぐための隔離であり、処理は何も変えない）。
        grp_danger = ttk.LabelFrame(
            col_ops,
            text="保守（取り消せない操作）",
            padding=8,
        )
        grp_danger.pack(fill=tk.X, pady=(0, 8))
        grp_danger.columnconfigure(0, weight=1, uniform="btn")
        grp_danger.columnconfigure(1, weight=1, uniform="btn")
        self._btn_bootsel = ttk.Button(
            grp_danger,
            text="BOOTSEL再起動",
            command=self._on_bootsel,
        )
        self._btn_bootsel.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            padx=2,
            pady=2,
        )
        self._btn_clear = ttk.Button(
            grp_danger,
            text="取込を破棄",
            command=self._on_clear,
        )
        self._btn_clear.grid(row=1, column=0, sticky=tk.EW, padx=2, pady=2)
        self._btn_keys = ttk.Button(
            grp_danger,
            text="ペア鍵を全削除",
            command=self._on_delete_keys,
        )
        self._btn_keys.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)

        grp_state = ttk.LabelFrame(
            col_info,
            text="コントローラ状態",
            padding=8,
        )
        grp_state.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(grp_state, text="プレイヤーLED").grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        # PLAYER_INFO のLEDはbit0=LED1..bit3=LED4。実機は縦4灯で
        # LED1が最下段・LED4が最上段。ここは左からLED1..LED4の横並びで出す。
        self._lamp_canvas = tk.Canvas(
            grp_state,
            width=120,
            height=30,
            highlightthickness=0,
        )
        self._lamp_canvas.grid(row=0, column=1, sticky=tk.W, padx=2, pady=2)
        self._lamp_items: list[int] = []
        for index in range(4):
            left = 4 + index * 30
            self._lamp_items.append(
                self._lamp_canvas.create_rectangle(
                    left,
                    4,
                    left + 22,
                    26,
                    fill=PLAYER_LAMP_OFF_COLOR,
                    outline="#4D4D4D",
                )
            )
        self._imu_label = ttk.Label(grp_state, text="ジャイロ: なし")
        self._imu_label.grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self._vib_label = ttk.Label(grp_state, text="振動: 未受信")
        self._vib_label.grid(row=1, column=1, sticky=tk.W, padx=2, pady=2)

        grp_color = ttk.LabelFrame(
            col_info,
            text="本体色（RRGGBB）",
            padding=8,
        )
        grp_color.pack(fill=tk.X, pady=(0, 8))
        self._colors: list[Any] = []
        color_entries: list[Any] = []
        # タブでは 4 欄を 2x2 に並べる（横 1 列だと情報列で最も幅を取る）。
        slots = ttk.Frame(grp_color)
        slots.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=2, pady=2)
        per_row = 2 if embedded else len(COLOR_SLOT_NAMES)
        for index, (slot, default) in enumerate(zip(COLOR_SLOT_NAMES, COLOR_DEFAULTS)):
            r, c = divmod(index, per_row)
            ttk.Label(slots, text=slot).grid(
                row=r,
                column=c * 2,
                padx=(6 if c == 0 else 2, 0),
                sticky=tk.W,
            )
            var = tk.StringVar(value=default)
            entry = ttk.Entry(slots, width=8, textvariable=var)
            entry.grid(row=r, column=c * 2 + 1, padx=2, pady=1)
            self._colors.append(var)
            color_entries.append(entry)
        self._color_canvas = tk.Canvas(
            grp_color,
            width=96,
            height=20,
            highlightthickness=0,
        )
        self._color_canvas.grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self._color_items: list[int] = []
        for index in range(4):
            left = 2 + index * 24
            self._color_items.append(
                self._color_canvas.create_rectangle(
                    left, 2, left + 20, 18, fill="#000000", outline="#4D4D4D"
                )
            )
        for entry, var in zip(color_entries, self._colors):
            entry.bind("<KeyRelease>", lambda _e: self._refresh_color_preview())
            var.trace_add("write", lambda *_a: self._refresh_color_preview())
        self._btn_color = ttk.Button(
            grp_color,
            text="色を送る",
            command=self._on_color,
        )
        self._btn_color.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)
        self._color_entries = color_entries
        self._refresh_color_preview()
        grp_baud = ttk.LabelFrame(col_info, text="通信速度", padding=8)
        grp_baud.pack(fill=tk.X, pady=(0, 8))
        self._actual_baud = ttk.Label(grp_baud, text="実際の速度: 不明")
        self._actual_baud.pack(anchor=tk.W)
        self._btn_hunt = ttk.Button(grp_baud, text="速度を探す", command=self._on_hunt)
        self._btn_hunt.pack(fill=tk.X)

        grp_log = ttk.LabelFrame(col_info, text="ログ", padding=8)
        grp_log.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        log_frame = ttk.Frame(grp_log)
        log_frame.pack(fill=tk.BOTH, expand=True)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self._log = tk.Text(
            log_frame,
            height=8 if embedded else 16,
            # タブでは列の幅に合わせて伸びるので、要求幅は小さくしておく。
            width=36 if embedded else 72,
            state=tk.DISABLED,
        )
        self._log.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self._log.yview
        )
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self._log.configure(yscrollcommand=log_scroll.set)

        # ボタン面から外した用語と注意は吹き出しへ移す（面の文言を短く保つ）。
        Tooltip(
            self._btn_connect,
            "Pico と接続し、Switch を操作できる状態にする。",
        )
        Tooltip(
            self._btn_status,
            "Pico の状態と、接続中のコントローラ情報を表示する。",
        )
        Tooltip(self._btn_ping, "Pico との通信が通るか確認し、応答時間を表示する。")
        Tooltip(
            self._btn_cap,
            "指定した秒数だけ、実際のコントローラ操作を記録する。",
        )
        Tooltip(
            self._btn_beacon,
            "保存した操作を Pico から再生する。",
        )
        Tooltip(
            self._btn_reconnect,
            "Switch との接続をやり直す。",
        )
        Tooltip(
            self._btn_w1,
            "USB 接続に切り替える（Bluetooth は停止。約0.5秒後に再起動）。",
        )
        Tooltip(
            self._btn_w0,
            "Bluetooth 接続に切り替える（約0.5秒後に再起動）。",
        )
        Tooltip(
            self._btn_winfo,
            "いま USB 接続か Bluetooth 接続かを表示する。",
        )
        Tooltip(
            self._btn_e0,
            "Switch に Pro Controller として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_e1,
            "Switch に Joy-Con (L) として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_e2,
            "Switch に Joy-Con (R) として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_bootsel,
            "ファームウェア書き込み用のモードで再起動する（COM ポートが外れる）。",
        )
        Tooltip(
            self._btn_clear,
            "Pico に取り込んだ入力を破棄する。Switch 側のペアリング解除も別途必要。",
        )
        Tooltip(
            self._btn_keys,
            "Pico に保存したペアリング情報をすべて削除する。Switch 側の登録解除も別途必要。",
        )
        Tooltip(
            self._btn_color,
            "コントローラ本体の色を Pico に送る（プレイヤーLEDの色ではありません）。",
        )
        if embedded:
            self._reflow_columns()
        self.set_visible(True)

    def _reflow_columns(self) -> None:
        """タブの幅に合わせて、操作列と情報列を左右に並べるか縦に積むか決める。"""
        body, ops, info = self._body, self._col_ops, self._col_info
        if ops is info:
            return
        try:
            gap = 16
            ow, oh = ops.winfo_reqwidth(), ops.winfo_reqheight()
            iw, ih = info.winfo_reqwidth(), info.winfo_reqheight()
            two = body.winfo_width() >= ow + gap + iw + 16
            if two != self._two_columns:
                self._two_columns = two
                if two:
                    # grid は前の指定（columnspan）を引き継ぐので明示して戻す。
                    ops.grid(row=0, column=0, columnspan=1, sticky=tk.NW)
                    info.grid(
                        row=0, column=1, columnspan=1, sticky=tk.NSEW,
                        padx=(gap, 0), pady=0,
                    )  # fmt: skip
                    body.rowconfigure(0, weight=1)
                    body.rowconfigure(1, weight=0)
                else:
                    ops.grid(row=0, column=0, columnspan=2, sticky=tk.NW)
                    info.grid(
                        row=1, column=0, columnspan=2, sticky=tk.NSEW, padx=0,
                        pady=(gap, 0),
                    )  # fmt: skip
                    body.rowconfigure(0, weight=0)
                    body.rowconfigure(1, weight=1)
            # padding=8 の上下左右の分を足す。
            height = max(oh, ih) if two else oh + gap + ih
            width, height = max(ow, iw) + 16, height + 16
            if (int(str(body.cget("width"))), int(str(body.cget("height")))) != (
                width,
                height,
            ):
                body.configure(width=width, height=height)
        except tk.TclError:
            pass

    # -- 画面まわり ------------------------------------------------------
    # 通信はサービスへ渡し、画面の更新はGUI側のpollだけで行う。

    def close(self) -> None:
        """窓を閉じる。二重に呼ばれても安全にする。"""
        if getattr(self, "_closed", False):
            return
        self.set_visible(False)
        self.inbox.close()
        self._closed = True
        self._stop = True
        # 取込期限の予約を消す。閉じた後の期限表示は出さない。
        self._cancel_capture_deadline()
        # 溜まった届けは捨てる。_pollは止まるため残すと1件漏れる。
        # 使用中も戻す。残したまま閉じると次に開いた窓が塞がったままになる。
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            pass
        self._busy = False
        # 埋め込みは枠（タブ）を残して中身だけ壊す。別窓は窓ごと壊す。
        target = self._body if getattr(self, "_embedded", False) else self.window
        try:
            if target.winfo_exists():
                target.destroy()
        except Exception:
            pass

    @staticmethod
    def _apply_text(widget: Any, text: str) -> None:
        """文が変わるときだけ configure する（理由は _apply_state と同じ）。"""
        try:
            if str(widget.cget("text")) == text:
                return
        except Exception:
            pass
        widget.configure(text=text)

    @staticmethod
    def _apply_state(widget: Any, state: str) -> None:
        """状態が変わるときだけ configure する。

        ttk/tk の configure は同じ値でも再描画を積む。この画面は接続の写しが
        届くたび（PLAYER_INFO は 60Hz）に可否を見直すので、同じ値の入れ直し
        だけで描画が積み上がり、表示中のプレビューの tick が落ちる（実測
        60→39fps）。cget を持たない差し替え画面では従来どおり入れる。
        """
        try:
            if str(widget.cget("state")) == state:
                return
        except Exception:
            pass
        widget.configure(state=state)

    def _set_busy(self) -> None:
        snapshot = self._service.connection_snapshot()
        busy = bool(snapshot.operation) or self._command_busy()
        self._busy = busy
        ready = snapshot.opened and snapshot.phase == "ready" and not busy
        recovery = (
            snapshot.opened and snapshot.phase in {"ready", "failed"} and not busy
        )
        for name in (
            "_btn_cap",
            "_btn_reconnect",
            "_btn_status",
            "_btn_ping",
            "_btn_winfo",
            "_btn_w0",
            "_btn_w1",
            "_btn_e0",
            "_btn_e1",
            "_btn_e2",
            "_btn_bootsel",
            "_btn_clear",
            "_btn_keys",
            "_btn_color",
        ):
            self._apply_state(getattr(self, name), tk.NORMAL if ready else tk.DISABLED)
        saved = len(snapshot.player_info) >= 2 and decode_player_save(
            snapshot.player_info[1]
        )
        self._apply_state(
            self._btn_beacon, tk.NORMAL if ready and saved else tk.DISABLED
        )
        for widget in [self._seconds_input, *self._color_entries]:
            self._apply_state(widget, tk.NORMAL if ready else tk.DISABLED)
        for widget in (self._btn_connect, self._btn_hunt):
            self._apply_state(widget, tk.NORMAL if recovery else tk.DISABLED)
        optional = {
            "状態確認中": (self._btn_status, self._btn_winfo),
            "疎通確認中": (self._btn_ping,),
            "BOOTSEL移行中": (self._btn_bootsel,),
            "無線切替中": (self._btn_w0,),
            "有線切替中": (self._btn_w1,),
            "種別切替中": (self._btn_e0, self._btn_e1, self._btn_e2),
            "Switch再接続要求中": (self._btn_reconnect,),
        }
        for action in self._unsupported:
            for widget in optional.get(action, ()):
                self._apply_state(widget, tk.DISABLED)
        self._apply_text(
            self._busy_label,
            f"{snapshot.operation}…" if snapshot.operation else "",
        )

    def _admit(self, *, recovery: bool = False) -> bool:
        if self._command_busy():
            self._queue.put(("log", "コマンドを停止してから変更してください。"))
            return False
        snapshot = self._service.connection_snapshot()
        if not snapshot.opened:
            self._queue.put(("log", "未接続です。シリアルタブで接続してください。"))
            return False
        if snapshot.operation:
            return False
        if snapshot.phase != "ready" and not (recovery and snapshot.phase == "failed"):
            self._queue.put(("log", "HELLO をやり直すか、速度を探してください。"))
            return False
        return True

    def _append(self, text: str) -> None:
        try:
            at_bottom = self._log.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._trim_log()
        if at_bottom:
            try:
                self._log.see(tk.END)
            except Exception:
                pass
        self._log.configure(state=tk.DISABLED)

    def _trim_log(self) -> None:
        log = getattr(self, "_log", None)
        if log is None:
            return
        try:
            total = int(log.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > BCON_MAX_LINES:
            try:
                log.delete("1.0", f"end-{BCON_MAX_LINES}l")
            except Exception:
                pass

    def _update_player_lamps(self, lamp: Any, flags: Any = None) -> None:
        """PLAYER_INFO のランプとIMU/振動を画面へ反映する。

        引数は復号前の生値でよい（lamp・flagsの2引数、または2byteの
        payload 1つ）。b"" や1byteの短い payload・予約値は全消灯へ倒し、
        例外は投げない。mainloopの無いヘッドレス検査（__new__＋stub）
        からも同じ口で呼べる。作業スレッドから直接触らず、after() 還流の
        先で呼ぶこと。
        """
        if flags is None and isinstance(lamp, (bytes, bytearray, memoryview)):
            payload = bytes(lamp)
            if len(payload) >= 2:
                lamp, flags = payload[0], payload[1]
            else:
                lamp, flags = None, None
        try:
            lamps = decode_player_lamp(lamp)
            imu, vibration = decode_player_flags(flags)
        except (TypeError, ValueError):
            # 読めない値は全消灯にする（前回の点灯を残すと紛らわしい）。
            lamps = (False, False, False, False)
            imu = vibration = False
        items = getattr(self, "_lamp_items", None)
        canvas = getattr(self, "_lamp_canvas", None)
        # 同じ色を入れ直すと Canvas の再描画が積まれる。点灯が変わったときだけ。
        if items is not None and canvas is not None:
            if getattr(self, "_lamps_shown", None) != lamps:
                try:
                    for item, is_on in zip(items, lamps):
                        color = PLAYER_LAMP_ON_COLOR if is_on else PLAYER_LAMP_OFF_COLOR
                        canvas.itemconfigure(item, fill=color)
                    self._lamps_shown = lamps
                except Exception:
                    # 閉じた後・stubなど、描けない時は色だけ諦める。
                    pass
        for name, label, detected in (
            ("_imu_label", "ジャイロ", imu),
            ("_vib_label", "振動", vibration),
        ):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            try:
                self._apply_text(widget, f"{label}: {'あり' if detected else 'なし'}")
            except Exception:
                continue
        # 保存証拠は表示と別に覚える。BEACONでは立てない。
        self._note_player_imu_range(flags)

    def _note_player_imu_range(self, flags: Any) -> None:
        """Switchが要求したIMUレンジ(0x41)を変化時だけログへ1行出す。

        bit7は「0x41を実際に受信したか」。未受信のときの値は既定
        (±2000dps/±8G) なので、要求と取り違えないよう明示する。
        """
        try:
            value = int(flags) & 0xFF
            gyro, accel = decode_player_imu_range(value)
        except (TypeError, ValueError):
            return
        seen = bool(value & 0x80)
        # bit0=0x40(IMU有効化)受信。0x41未着でも、こちらが立っていれば
        # USB OUTのサブコマンド経路自体は生きていると判断できる。
        imu_on = bool(value & 0x01)
        shown = (gyro, accel, seen, imu_on)
        if shown == getattr(self, "_imu_range_shown", None):
            return
        self._imu_range_shown = shown
        if seen:
            note = ""
        elif imu_on:
            note = "（未指定のため既定値）"
        else:
            note = "（未接続）"
        try:
            self._append(f"ジャイロ感度: {gyro} / {accel}{note}")
        except Exception:
            pass

    def _note_player_save(self, flags: Any) -> None:
        """PLAYER_INFO flags bit2を見たら保存済みを覚える。例外は出さない。"""
        try:
            if self._capture_deadline_id is not None and decode_player_save(
                int(flags) & 0xFF
            ):
                self._capture_saved_seen = True
        except (TypeError, ValueError):
            pass

    def _refresh_color_preview(self) -> None:
        """色4欄の見た目を小矩形へ映す。打鍵のたびの表示だけ（送らない）。"""
        try:
            update_color_preview(
                getattr(self, "_colors", []),
                getattr(self, "_color_canvas", None),
                getattr(self, "_color_items", []),
            )
        except Exception:
            pass

    def set_visible(self, visible: bool) -> None:
        """非表示時は購読と予約を外し、下書きと表示済みログを保持する。"""
        if self._closed or self._visible == visible:
            return
        self._visible = visible
        self._visibility_revision += 1
        if not visible:
            for name in ("_connection_unsub", "_log_unsub"):
                unsubscribe = getattr(self, name)
                setattr(self, name, None)
                if unsubscribe is not None:
                    unsubscribe()
            self._cancel_capture_deadline()
            if self._poll_id is not None:
                self.window.after_cancel(self._poll_id)
                self._poll_id = None
            return
        self._connection_unsub = self._service.subscribe_connection(
            self._receive_snapshot
        )
        self._log_unsub = self._service.subscribe_connection_log(self._receive_log)
        self._poll()

    def _receive_snapshot(self, _snapshot: ConnectionSnapshot) -> None:
        if self._visible and not self._closed:
            post_latest_to_gui(
                self,
                "connection",
                lambda: self._apply_snapshot(self._service.connection_snapshot()),
            )

    def _receive_log(self, generation: int, text: str) -> None:
        if self._visible and not self._closed:
            self._queue.put(("generation_log", (generation, text)))

    def _apply_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        if not self._visible or self._closed:
            return
        if snapshot.generation != self._service.connection_snapshot().generation:
            return
        previous = self._snapshot
        if snapshot.generation != previous.generation or snapshot.phase in {
            "disconnected",
            "opening",
            "failed",
            "closing",
            "restarting",
            "bootloader",
        }:
            self._cancel_capture_deadline()
            self._capture_saved_seen = False
            self._unsupported.clear()
        self._snapshot = snapshot
        self._update_player_lamps(snapshot.player_info)
        if (
            snapshot.player_info != previous.player_info
            and len(snapshot.player_info) >= 2
        ):
            self._note_player_save(snapshot.player_info[1])
        if snapshot.rumble:
            try:
                left, right = decode_rumble(snapshot.rumble)
                self._apply_text(self._vib_label, f"振動: L={left} R={right}")
            except (TypeError, ValueError):
                self._apply_text(self._vib_label, "振動: 不明")
        else:
            self._apply_text(self._vib_label, "振動: 未受信")
        self._render_summary()
        self._set_busy()
        if (
            snapshot.phase == "ready"
            and not snapshot.operation
            and not self._color_loaded
        ):
            self._load_initial_color()

    def _load_initial_color(self) -> None:
        if not self._colors or self._command_busy():
            return
        draft = tuple(var.get() for var in self._colors)
        if draft != COLOR_DEFAULTS:
            self._color_loaded = True
            return
        generation = self._service.connection_snapshot().generation

        def work(transport: Any) -> bool:
            payload = request_color(transport)
            self._post_result("color_prefill", (generation, draft, payload))
            return payload is not None

        self._color_loaded = self._service.run_bcon_operation("本体色読取中", work)

    def _render_summary(self) -> None:
        snapshot = self._snapshot
        phases = {
            "disconnected": "未接続",
            "opening": "ポートを開いています",
            "hello": "HELLO 確認中",
            "ready": "接続済み",
            "failed": "接続失敗",
            "closing": "切断中",
            "restarting": "再起動待ち",
            "bootloader": "書込み待ち",
        }
        hello = "未確認"
        if snapshot.hello_ack is not None:
            adopted, major, minor, result = snapshot.hello_ack
            hello = f"採用 {adopted} / FW {major}.{minor} / 結果 {result}"
        status = "未受信（不明）"
        if snapshot.status_received_at is not None:
            age = max(0.0, time.monotonic() - snapshot.status_received_at)
            # 小数まで出すと表示が 0.1 秒ごとに変わり、そのたびにラベルの
            # 折り返しが計算し直される。1 秒刻みで十分（3 秒で古い情報扱い）。
            status = f"{age:.0f}秒前" + ("（古い情報）" if age > 3 else "")
        mode = "不明"
        if snapshot.status is not None:
            mode = "有線" if snapshot.status[0] & STATUS_FLAG_WIRED else "無線"
            if (
                snapshot.status_received_at is None
                or time.monotonic() - snapshot.status_received_at > 3
            ):
                mode += "（古い情報）"
        baud = f"{snapshot.baud}bps" if snapshot.baud is not None else "不明"
        # 同じ文を入れ直すと ttk ラベル（wraplength 付き）が折り返しを
        # 計算し直し、そのたびに画面全体の描画が積まれる。変わったときだけ。
        self._apply_text(
            self._summary,
            f"{phases[snapshot.phase]}: {snapshot.message}\n"
            f"ポート: {snapshot.port_name or '未選択'} / {baud}\n"
            f"HELLO: {hello}\nSwitch 接続方式: {mode} / STATUS: {status}",
        )
        self._apply_text(self._actual_baud, f"実際の速度: {baud}")

    def _arm_capture_deadline(self, seconds: int) -> None:
        """取込期限を張る。要求秒数＋余白の壁時計で1発だけ鳴らす。"""
        try:
            wanted = int(seconds)
        except (TypeError, ValueError):
            return
        if wanted < CAPTURE_MIN_S or wanted > CAPTURE_MAX_S:
            return
        self._cancel_capture_deadline()
        self._capture_saved_seen = False
        try:
            delay_ms = (wanted + CAPTURE_DEADLINE_MARGIN_S) * 1000
            self._capture_deadline_id = self.window.after(
                delay_ms, self._on_capture_deadline
            )
        except Exception:
            self._capture_deadline_id = None

    def _cancel_capture_deadline(self) -> None:
        """取込期限の予約を消す。二重・予約なしでも落とさない。"""
        pending = getattr(self, "_capture_deadline_id", None)
        self._capture_deadline_id = None
        if pending is None:
            return
        try:
            cancel = getattr(self.window, "after_cancel", None)
            if callable(cancel):
                cancel(pending)
        except Exception:
            pass

    def _on_capture_deadline(self) -> None:
        """取込期限時にPLAYER_INFOの保存証拠を報告し、期限だけ解除する。"""
        self._capture_deadline_id = None
        if getattr(self, "_closed", False):
            # 閉じ競合でも購読だけは外す（持ち込まない）。
            self._teardown_capture()
            return
        try:
            seen = bool(getattr(self, "_capture_saved_seen", False))
        except Exception:
            seen = False
        # 先に外して残りカスを捨て、完了文だけ残す。
        self._teardown_capture()
        try:
            if seen:
                self._queue.put(("log", SAVE_SUCCESS_TEXT))
            else:
                self._queue.put(("log", SAVE_TIMEOUT_TEXT))
        except Exception:
            pass

    def _drain_stale_capture_entries(self) -> int:
        """取込の残りカスだけ捨てる。log/doneは残す。二重安全。

        player_info/rumble/capture_armは取込期限後の古い届けであり、
        次の取込へ持ち込まない。log/doneは画面の履歴のため残す。
        捨てた件数を返す（T1数えのqsize証跡の補助）。
        """
        try:
            q = getattr(self, "_queue", None)
            if q is None:
                return 0
        except Exception:
            return 0
        stale = 0
        keep: list[Any] = []
        try:
            while True:
                item = q.get_nowait()
                try:
                    kind, _ = item
                except (TypeError, ValueError):
                    stale += 1
                    continue
                if kind in ("player_info", "rumble", "capture_arm"):
                    stale += 1
                    continue
                keep.append(item)
        except queue.Empty:
            pass
        except Exception:
            pass
        for item in keep:
            try:
                q.put(item)
            except Exception:
                pass
        return stale

    def _teardown_capture(self) -> None:
        self._cancel_capture_deadline()
        self._drain_stale_capture_entries()

    def _ensure_counters(self) -> None:
        # __new__素体の検査体でも数えが動くよう欠品分だけ足す。
        if getattr(self, "_rx_counts", None) is None:
            self._rx_counts = {}
        for name, fresh in (
            ("_poll_ticks", 0),
            ("_poll_drained_total", 0),
            ("_poll_last_drained", 0),
            ("_poll_last_ms", 0.0),
            ("_poll_max_ms", 0.0),
            ("_queue_max_qsize", 0),
            ("_queue_dropped_total", 0),
            ("_rx_total", 0),
            ("_rx_start_t", 0.0),
            ("_counters_log_t", 0.0),
        ):
            if not hasattr(self, name):
                setattr(self, name, fresh)

    def counters_snapshot(self) -> dict[str, Any]:
        # H1/H2/H3切り分け用の写し。呼ぶだけで線・画面に触れない。
        self._ensure_counters()
        now = time.perf_counter()
        start = float(self._rx_start_t)
        span = now - start if start > 0.0 else 0.0
        by_ftype = dict(self._rx_counts)
        per_sec = (
            {ftype: count / span for ftype, count in by_ftype.items()}
            if span > 0.0
            else dict.fromkeys(by_ftype, 0.0)
        )
        return {
            "poll_ticks": int(self._poll_ticks),
            "poll_drained_total": int(self._poll_drained_total),
            "poll_last_drained": int(self._poll_last_drained),
            "poll_last_ms": float(self._poll_last_ms),
            "poll_max_ms": float(self._poll_max_ms),
            "queue_max_qsize": int(self._queue_max_qsize),
            "queue_dropped_total": int(self._queue_dropped_total),
            "rx_total": int(self._rx_total),
            "rx_by_ftype": by_ftype,
            "rx_per_sec_by_ftype": per_sec,
        }

    def _maybe_log_counters(self) -> None:
        # debugへの書き出しは間引く（既定では出ない・出しても5秒に1件）。
        try:
            now = time.perf_counter()
            if now - float(self._counters_log_t) < COUNTERS_LOG_INTERVAL_S:
                return
            self._counters_log_t = now
            _LOGGER.debug(
                "bcon計測 tick=%d drain=%d/%d poll_ms=%.3f(max %.3f) qmax=%d rx=%s",
                int(self._poll_ticks),
                int(self._poll_last_drained),
                int(self._poll_drained_total),
                float(self._poll_last_ms),
                float(self._poll_max_ms),
                int(self._queue_max_qsize),
                dict(self._rx_counts),
            )
        except Exception:
            pass

    def _poll(self) -> None:
        self._poll_id = None
        if self._closed or not self._visible:
            return
        self.inbox.drain()
        start = time.perf_counter()
        self._ensure_counters()
        self._queue_max_qsize = max(self._queue_max_qsize, self._queue.qsize())
        current = self._service.connection_snapshot().generation
        drained = 0
        for _ in range(BCON_FLUSH_MAX):
            try:
                kind, value = self._queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "generation_log":
                generation, text = value
                if generation == current:
                    self._append(text)
            elif kind == "unsupported":
                generation, action = value
                if generation == current:
                    self._unsupported.add(action)
            elif kind == "color_prefill":
                generation, draft, payload = value
                if (
                    generation == current
                    and tuple(var.get() for var in self._colors) == draft
                ):
                    if payload is None:
                        self._append(COLOR_READ_FALLBACK_NOTE)
                    else:
                        for index, var in enumerate(self._colors):
                            var.set(bytes(payload[index * 3 : index * 3 + 3]).hex())
                        self._refresh_color_preview()
            elif kind == "log":
                self._append(str(value))
            elif kind == "capture_cancel":
                generation, revision = value
                if generation == current and revision == self._visibility_revision:
                    self._cancel_capture_deadline()
            elif kind == "capture_arm":
                generation, revision, seconds, sent_at = value
                if generation == current and revision == self._visibility_revision:
                    remaining = max(
                        0,
                        seconds
                        + CAPTURE_DEADLINE_MARGIN_S
                        - (time.monotonic() - sent_at),
                    )
                    self._cancel_capture_deadline()
                    self._capture_deadline_id = self.window.after(
                        int(remaining * 1000), self._on_capture_deadline
                    )
        dropped = self._queue.take_dropped()
        if dropped:
            self._queue_dropped_total += dropped
            self._append(f"... {dropped}行省略 ...")
        self._render_summary()
        self._set_busy()
        if self._embedded:
            self._reflow_columns()
        self._note_poll_tick(start, drained)
        self._poll_id = self.window.after(120, self._poll)

    def _note_poll_tick(self, tick_start: float, drained: int) -> None:
        # 1 tickの吐き出し数と壁msを残す（H2切り分け用・GUI側のみ）。
        try:
            elapsed_ms = (time.perf_counter() - tick_start) * 1000.0
            self._poll_ticks += 1
            self._poll_drained_total += drained
            self._poll_last_drained = drained
            self._poll_last_ms = elapsed_ms
            if elapsed_ms > self._poll_max_ms:
                self._poll_max_ms = elapsed_ms
        except Exception:
            pass
        self._maybe_log_counters()

    def _run(
        self,
        work: Callable[[Any], bool],
        label: str,
        *,
        restarts_device: bool = False,
        bootloader: bool = False,
    ) -> bool:
        if not self._admit():
            return False
        if self._service.run_bcon_operation(
            label, work, restarts_device=restarts_device, bootloader=bootloader
        ):
            self._set_busy()
            return True
        return False

    def _post_result(self, kind: str, value: Any) -> None:
        if not self._closed:
            self._queue.put((kind, value))

    def _operation_log(self, action: str | None = None) -> Callable[[str], None]:
        generation = self._service.connection_snapshot().generation

        def log(text: str) -> None:
            self._post_result("generation_log", (generation, text))
            if action and text.startswith("この通信方式は"):
                self._post_result("unsupported", (generation, action))

        return log

    def _config_result(
        self,
        transport: Any,
        what: str,
        pre_status: Any = None,
        *,
        log: Callable[[str], None] | None = None,
    ) -> bool:
        """STATUSの前後差分で拒否を判定し、粘着errcodeを失敗にしない。"""
        emit = log or self._operation_log()
        status = self._request_status(transport, emit)
        if status is None:
            return False
        errcode = int(status.get("errcode", 0)) & 0xFF
        unchanged = isinstance(pre_status, dict) and all(
            int(status.get(key, 0)) == int(pre_status.get(key, 0))
            for key in ("errcode", "err_drop", "err_crc")
        )
        if errcode == 0 or unchanged:
            sticky = "（直近errcodeは過去の記録です）" if errcode else ""
            emit(f"{what}を受け付けました。{sticky}{format_status_block(status)}")
            return True
        note = f"{what}は拒否されました（{describe_bcon_errcode(errcode)}）。"
        if int(status.get("flags", 0)) & STATUS_FLAG_WIRED and errcode in (0x10, 0x11):
            note += "先に「無線にする」で切り替え、再起動後にシリアルタブで接続してください。"
        emit(note + format_status_block(status))
        return False

    def _request_status(
        self, transport: Any, log: Callable[[str], None]
    ) -> dict[str, Any] | None:
        request = getattr(transport, "request_status", None)
        if not callable(request):
            log("この通信方式は状態確認に対応していません。")
            return None
        try:
            status = request(timeout=1.0)
        except Exception as exc:
            log(f"状態を取得できませんでした: {exc}")
            return None
        if not isinstance(status, dict):
            log("Pico から応答がありません。ポート・通信速度を確認してください。")
            return None
        try:
            for key in ("flags", "last_seq", "err_crc", "err_drop", "errcode"):
                int(status[key])
        except (KeyError, TypeError, ValueError):
            log("STATUS の応答形式が不正です。以前の状態は更新しません。")
            return None
        return status

    def _on_connect(self) -> None:
        if self._admit(recovery=True):
            self._service.retry_bcon_hello()
            self._set_busy()

    def _on_hunt(self) -> None:
        if self._admit(recovery=True):
            self._service.hunt_bcon_baud()
            self._set_busy()

    def _on_capture(self) -> None:
        if not self._admit():
            return
        try:
            seconds = parse_capture_seconds(self._seconds.get())
        except (tk.TclError, ValueError, TypeError):
            seconds = None
        if seconds is None:
            self._queue.put(("log", "取込時間は1～60秒で指定してください。"))
            return
        snapshot = self._service.connection_snapshot()
        if snapshot.status is None or snapshot.status[0] & (STATUS_FLAG_WIRED | 0x40):
            self._queue.put(
                (
                    "log",
                    "無線モードで Pico と Switch が未接続の状態を確認してください。",
                )
            )
            return
        self._capture_saved_seen = False
        generation = snapshot.generation
        revision = self._visibility_revision
        log = self._operation_log()

        def work(transport: Any) -> bool:
            pre = self._request_status(transport, log)
            if pre is None or int(pre["flags"]) & (STATUS_FLAG_WIRED | 0x40):
                log("無線モードで Pico と Switch が未接続の状態を確認してください。")
                self._post_result("capture_cancel", (generation, revision))
                return False
            sent_at = time.monotonic()
            if not send_config_frame(transport, T_CAPTURE_START, bytes([seconds])):
                log("送信に失敗しました。")
                self._post_result("capture_cancel", (generation, revision))
                return False
            if not self._config_result(transport, "取込", pre, log=log):
                self._post_result("capture_cancel", (generation, revision))
                return False
            self._post_result("capture_arm", (generation, revision, seconds, sent_at))
            return True

        if self._run(work, "取込開始中"):
            self._arm_capture_deadline(seconds)

    def _on_beacon(self) -> None:
        if not self._admit():
            return
        snapshot = self._service.connection_snapshot()
        if len(snapshot.player_info) < 2 or not decode_player_save(
            snapshot.player_info[1]
        ):
            self._queue.put(("log", "保存した入力をまだ確認できていません。"))
            return
        self._send_config(T_BEACON_START, b"", "再生開始中", "BEACON")

    def _send_config(
        self, frame_type: int, payload: bytes, label: str, what: str
    ) -> None:
        log = self._operation_log()

        def work(transport: Any) -> bool:
            pre = _pre_status_snapshot(transport)
            if not send_config_frame(transport, frame_type, payload):
                log("送信に失敗しました。")
                return False
            return self._config_result(transport, what, pre, log=log)

        self._run(work, label)

    def _on_status(self) -> None:
        log = self._operation_log("状態確認中")

        def work(transport: Any) -> bool:
            status = self._request_status(transport, log)
            if status is None:
                return False
            log(format_status_block(status))
            return True

        self._run(work, "状態確認中")

    def _on_ping(self) -> None:
        log = self._operation_log("疎通確認中")

        def work(transport: Any) -> bool:
            ping = getattr(transport, "ping", None)
            if not callable(ping):
                log("この通信方式は疎通確認に対応していません。")
                return False
            rtt = ping(timeout=1.0)
            if rtt is None:
                log("Pico から応答がありません。ポート・通信速度を確認してください。")
                return False
            log(f"PONG: 疎通OK（往復 {float(rtt) * 1000:.1f}ms）")
            return True

        self._run(work, "疎通確認中")

    def _on_wired_show(self) -> None:
        log = self._operation_log("状態確認中")

        def work(transport: Any) -> bool:
            status = self._request_status(transport, log)
            if status is None:
                return False
            mode = "有線" if int(status.get("flags", 0)) & STATUS_FLAG_WIRED else "無線"
            log(f"現在は{mode}です。{format_status_block(status)}")
            return True

        self._run(work, "接続方式確認中")

    def _confirmed_method(
        self,
        method_name: str,
        args: tuple[Any, ...],
        label: str,
        confirmation: str,
        *,
        restarts_device: bool = False,
        bootloader: bool = False,
    ) -> None:
        if not self._admit():
            return
        if tkmsg.askquestion("確認", confirmation, parent=self.window) != "yes":
            return
        log = self._operation_log(label)

        def work(transport: Any) -> bool:
            method = getattr(transport, method_name, None)
            if not callable(method):
                log("この通信方式はこの操作に対応していません。")
                return False
            ok = method(*args, timeout=1.0) is True
            if not ok:
                log("Pico から応答がありません。ポート・通信速度を確認してください。")
            return ok

        self._run(work, label, restarts_device=restarts_device, bootloader=bootloader)

    def _on_wired_off(self) -> None:
        self._confirmed_method(
            "set_wired_mode",
            (False,),
            "無線切替中",
            "無線に切り替えます。保存後、約0.5秒で再起動します。続けますか？",
            restarts_device=True,
        )

    def _on_wired_on(self) -> None:
        self._confirmed_method(
            "set_wired_mode",
            (True,),
            "有線切替中",
            "有線（USB直結・Bluetooth停止）に切り替えます。保存後、約0.5秒で再起動します。続けますか？",
            restarts_device=True,
        )

    def _on_emulate_set(self, role: int) -> None:
        names = ("Pro Controller", "Joy-Con (L)", "Joy-Con (R)")
        if role not in range(len(names)):
            return
        self._confirmed_method(
            "set_emulate_mode",
            (role,),
            "種別切替中",
            f"{names[role]}に切り替えます。保存後、約0.5秒で再起動します。続けますか？",
            restarts_device=True,
        )

    def _on_bootsel(self) -> None:
        self._confirmed_method(
            "request_bootsel",
            (),
            "BOOTSEL移行中",
            "ファームウェア書込み用に再起動します。約0.5秒後にポートが外れます。続けますか？",
            bootloader=True,
        )

    def _on_reconnect(self) -> None:
        self._confirmed_method(
            "request_reconnect",
            (),
            "Switch再接続要求中",
            "Switch がコントローラを受け付ける状態で再接続を試します。続けますか？",
        )

    def _on_clear(self) -> None:
        if not self._admit():
            return
        if (
            tkmsg.askquestion(
                "確認",
                "取込とClassic鍵を破棄します。取り消せません。Switch側の登録解除も必要です。続けますか？",
                parent=self.window,
            )
            == "yes"
        ):
            self._send_config(T_KEY_DELETE, b"", "取込破棄中", "取込破棄")

    def _on_delete_keys(self) -> None:
        if not self._admit():
            return
        if (
            tkmsg.askquestion(
                "確認",
                "Pico のペア鍵をすべて削除します。取り消せません。Switch側の登録解除も必要です。続けますか？",
                parent=self.window,
            )
            == "yes"
        ):
            self._send_config(T_KEY_DELETE, b"", "ペア鍵削除中", "ペア鍵削除")

    def _on_color(self) -> None:
        if not self._admit():
            return
        slots = [parse_color_hex(var.get()) for var in self._colors]
        try:
            payload = build_color_payload(slots)
        except ValueError:
            self._queue.put(("log", "色はRRGGBBの6桁で4つ入れてください。"))
            return
        self._send_config(T_COLOR_SET, payload, "色変更中", "色変更")
