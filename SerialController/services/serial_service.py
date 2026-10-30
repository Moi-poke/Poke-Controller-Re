#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serial_service.py - シリアル通信の所有と接続手順を1つに持つ層。

Window から「Sender の生成・通信方式の切替・接続／切断・キーボードの
寿命管理」の手順をここへ移している。Window に残すのは tk 変数の読み書き・
確認ダイアログ・見た目の反映だけ。

なぜ分けるか:
  ・接続手順（止める→閉じる→開く→作り直す）は入口が増えるたびに
    片方だけ直す事故が起きる。手順はここだけに置く。
  ・tkinter を触らない。設定値は通常の Python 値で受け取り、
    接続ログは subscribe_connection_log、その他の通知は notify_user へ出す。
    ヘッドレスの検証でそのまま動く。

キーボード操作の寿命もここが持つ。開き直すと KeyPress を作り直すが、
Keyboard は生成時に渡された古い KeyPress を持ち続けるため、
接続の切り替えと同時に作り直さないと押しっぱなしの記録が残る。
"""

from __future__ import annotations

import csv
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from Gamepad import SwitchGamepadController
from Keyboard import SwitchKeyboardController
from core import transport
from core.Keys import KeyPress
from core.connection_state import ConnectionPhase, ConnectionSnapshot
from core.procon_color import is_bcon_transport
from core.serial.arbitration import list_arbitration_modes, resolve_arbitration_mode
from core.serial.sender import Sender
from core.transport.bcon_protocol import (
    BAUD_TABLE,
    T_HELLO_ACK,
    T_PLAYER_INFO,
    T_RUMBLE,
    T_STATUS,
)
from loguru import logger
from services import run_journal
from services.bcon_connect import connect_bcon
from services.run_journal import read_m_counts

# 開閉判定の使い回し猶予(秒)。GuiAssets.SER_OPENED_TTL_S と同じ値に揃える。
# is_open は表示ポンプ（200ms）や題名更新など Tk スレッドから高頻度で
# 呼ばれる。switch-bcon-proc では子プロセス往復（最大で約8秒級）のため、
# Tk スレッドで同期問い合わせすると1回の停滞がポンプ間隔の崩れとして
# そのまま出る。TTL 内は前回の判定を使い回し、TTL 切れの問い直しは
# 裏スレッドへ回してその間も古い値を返す（stale-while-revalidate）。
# TTL だけだと切れるたび Tk が8秒止まるため、裏へ回す方を必須とする。
# 初回（未照会）は閉じているものとして返す。後方互換のため値は
# monkeypatch で変えられる。
IS_OPEN_TTL_S = 0.5


@dataclass
class SenderSpec:
    """Sender 生成に要る設定値一式（tk 変数ではなく通常の値）。"""

    transport_name: str
    is_show_serial: bool
    arbitration_mode: str
    arbitration_cooldown: float
    input_log_format: str
    input_log_actions: str
    input_log_enabled: bool
    input_log_stick_change: bool
    # ライブ入力の最低保持ミリ秒（8〜64。範囲外はSender側で無視する）
    live_min_dwell_ms: int


class SerialService:
    """送り先（Sender）とその周辺の所有者。"""

    def __init__(
        self,
        notify_user: Callable[[str], None],
        base_dir: str,
        input_log_emit: Callable[[str], None] | None,
        keyboard_active: Callable[[], bool] | None = None,
        gamepad_active: Callable[[], bool] | None = None,
        log_dir: str | None = None,
    ) -> None:
        """notify_user は接続ログ以外の1行通知（Window は print を渡す）。

        接続ログは subscribe_connection_log で受け取る。GUI は世代付きで
        inbox に積み、表示時に現在世代と一致するものだけを表示する。

        base_dir は transport プラグインフォルダの相対解決用。
        input_log_emit は入力ログの送り先（LogPane.emitInputLog）。
        keyboard_active は押下を受け付けるかの判定（Window は窓の
        フォーカスを渡す）。省略時は常時受け付ける。
        gamepad_active はゲームパッド読み取りの門（有効化中は開ける）。
        キーボードと違いフォーカスに連動させない。
        log_dir は走行記録CSVの置き場。省略時は base_dir/log。
        """
        self._notify = notify_user
        self._base_dir = base_dir
        self._emit = input_log_emit
        self._keyboard_active = keyboard_active
        self._gamepad_active = gamepad_active
        self._log_dir = log_dir or os.path.join(base_dir, "log")
        self.sender: Sender | None = None
        self.key_press: KeyPress | None = None
        self.keyboard: SwitchKeyboardController | None = None
        self.gamepad: SwitchGamepadController | None = None
        # ゲームパッド入力の表示先（仮想パッドの鏡）。Tk スレッド制約の
        # ため、呼び出し側がスレッドの受け渡しまで持つ。None なら送らない。
        self._gamepad_display: Callable[[dict[str, Any]], None] | None = None
        # 走行記録の進行中状態。begin/end 以外は触らない。
        self._run_active = False
        self._run_name = ""
        self._run_t0 = 0.0
        self._run_stats_before: dict[str, Any] = {}
        self._run_csv_path = ""
        self._run_file: Any = None
        self._run_writer: Any = None
        self._run_rows = 0
        self._run_transport: Any = None
        # 走行 CSV の書き込みと close を直列化する（送出スレッドと終了処理が
        # 競合すると行が混ざる）。RLock なのは、書き込み失敗時に同じ錠の中で
        # _close_run_file を呼ぶ経路があるため。
        self._run_write_lock = threading.RLock()
        # 前走終了時のM計数。次走の差分の基準にする（初走は基準なし）。
        self.last_m: dict[str, int] | None = None
        # コード版の写し（走行開始行に付ける）。初回だけgitへ聞く。
        self._code_version_cache: str | None = None
        # 起動引数で指定された通信方式。設定より優先する（一時的な指定で、
        # 画面から選び直したら効かせない）。
        self.transport_override = ""
        # 開閉判定の写し。Tk スレッドは RPC を待たず、ここだけ見る。
        # 実体が差し替わったら古い写しは捨てる（id 使い回しの取り違え防止）。
        self._open_lock = threading.Lock()
        self._open_sender: Any = None
        self._open_snapshot = False
        self._open_snapshot_at = 0.0
        self._open_refreshing = False
        # この錠は受付と写しだけを守る。通信待ちや callback 中には保持しない。
        self._connection_lock = threading.RLock()
        self._connection = ConnectionSnapshot()
        self._connection_callbacks: list[Callable[[ConnectionSnapshot], None]] = []
        self._connection_logs: list[Callable[[int, str], None]] = []
        self._connection_cancel = threading.Event()
        self._connection_busy = False
        self._connection_completion: dict[str, Any] = {}
        self._connection_shutdown = False
        self._connection_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="SerialConnection"
        )
        self._connection_worker_id: int | None = None
        self._connection_rx_unsub: Callable[[], None] | None = None
        self._last_connect: tuple[int, str, int, bool, str] | None = None
        self._shutdown_future: Future[bool] | None = None
        self._connection_jobs: list[Future[Any]] = []

    def set_log_dir(self, path: str) -> None:
        """走行記録CSVの置き場を変える（検証用）。"""
        self._log_dir = str(path)

    def code_version(self) -> str:
        """コード版（git短縮ハッシュ）。取れなければ空文字。

        ビルド違いの混線防止用。重いので初回だけ聞いて覚える。
        失敗は握る（記録の欠落は実行を止めない）。
        """
        if self._code_version_cache is not None:
            return self._code_version_cache
        version = ""
        try:
            import subprocess

            repo = os.path.dirname(os.path.abspath(self._base_dir))
            done = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if done.returncode == 0:
                version = done.stdout.strip()
        except Exception:
            version = ""
        self._code_version_cache = version
        return version

    # -- 選択の解決（純粋な手続き） ------------------------------------------

    @staticmethod
    def resolve_transport(name: str | None) -> str:
        """通信方式名を実際に使える名前へ直す（未知は既定へ理由つきで落とす）。"""
        return transport.resolve_transport_name(name)

    @staticmethod
    def list_transports() -> list[str]:
        """登録されている通信方式名の一覧（選択欄の候補に使う）。"""
        return transport.list_transports()

    @staticmethod
    def uses_bcon(name: str | None) -> bool:
        """その通信方式が bcon 系（能力 BCON_STATE）か。

        名前の直書きではなく能力で見る。別プロセス版（switch-bcon-proc）や
        利用者定義の bcon 互換も同じ口で拾える。
        """
        return transport.transport_capability(name) == transport.BCON_STATE

    def selected_transport_name(self, configured: str) -> str:
        """これから使う通信方式の名前を決める。

        優先順は 起動引数 > 設定ファイル。引数を上に置くのは、
        設定を書き換えずにその場で試せるようにするため。
        """
        name = self.transport_override or configured
        return transport.resolve_transport_name(name)

    def clear_override(self) -> None:
        """起動引数の指定を捨てる。画面から選び直したら呼ぶ。"""
        self.transport_override = ""

    @staticmethod
    def arbitration_modes() -> tuple[str, ...]:
        """選べる入力調停の名前（選択欄の候補に使う）。"""
        return list_arbitration_modes()

    @staticmethod
    def resolve_arbitration(mode: str | None, logger: Any = None) -> str:
        """入力調停名を実際に使える名前へ直す（未知は既定へ理由つきで落とす）。"""
        return resolve_arbitration_mode(mode, logger=logger)

    # -- 構築 -----------------------------------------------------------------

    def load_plugins(self, plugin_dir: str) -> None:
        """利用者が置いた自作の Transport を読み込む。

        フォルダ指定が空なら何もしない（既定）。読めたものは名前を
        出す。黙って足すと、候補が増えた理由が分からなくなる。
        """
        directory = (plugin_dir or "").strip()
        if not directory:
            return
        if not os.path.isabs(directory):
            directory = os.path.join(self._base_dir, directory)
        added = transport.load_transport_plugins(directory)
        if added:
            message = "通信方式を読み込みました: " + ", ".join(added)
            self._notify(message)
            logger.info(message)

    def build_sender(self, spec: SenderSpec, transport_logger: Any = None) -> None:
        """Sender を作り、調停と入力ログを反映する。

        運び方は登録簿から名前で作る。作れなければ Transport 側が
        理由を出して既定へ戻すので None にはならない。
        """
        # 入力ログは print と混ぜず、専用のキューへ流す。同じ経路だと
        # 入力ログが上限を食い尽くしてコマンドの出力が捨てられる。
        self.sender = Sender(
            spec.is_show_serial,
            input_log_emit=self._emit,
            transport=transport.create_transport(
                spec.transport_name, logger=transport_logger
            ),
        )
        # 設定の入力の優先付けを反映する。送信側を作り直しても
        # 画面の選択が効いたままになるよう、生成のたびに適用する。
        self.sender.setArbitration(
            mode=spec.arbitration_mode, cooldown=spec.arbitration_cooldown
        )
        # ライブ入力の最低保持を反映する（範囲外はSender側で無視する）
        self.sender.setLiveMinDwell(spec.live_min_dwell_ms)
        # 作り直した送り先は閉じている。古い写しを使い回さない。
        self._note_open(False)
        self.apply_input_log(
            spec.input_log_format,
            spec.input_log_actions,
            spec.input_log_enabled,
            spec.input_log_stick_change,
        )

    def set_show_serial(self, value: bool) -> None:
        """送信内容を表示するかの切替を送信側へ押し込む（GUI スレッドから呼ぶ）。

        Tk の変数を送信スレッドに読ませないための入口である。Tk の
        変数は GUI スレッドのもので、worker から読むと GUI が忙しい間
        （Tcl が回っていない間）そこで待たされる。送信側がまだ無い
        とき（起動直後）は何もしない。
        """
        if self.sender is None:
            return
        self.sender.setShowSerial(bool(value))

    def apply_input_log(
        self, format: str, actions: str, enabled: bool, stick_change: bool
    ) -> None:
        """入力ログの設定を Sender へ反映する。

        書式はプリセット名でもテンプレート文字列そのものでもよい。
        誤った書式でも起動は止めない（ログは補助機能なので、
        ここで落ちるほうが困る）。
        """
        if self.sender is None:
            return
        try:
            # 記録する操作の絞り込み。空なら書式ごとの既定に任せる
            self.sender.setInputLogFormat(format, actions.strip() or None)
            self.sender.setInputLogEnabled(enabled)
            self.sender.setInputLogStickChange(stick_change)
        except Exception as e:
            message = f"入力ログの設定を適用できませんでした: {e}"
            self._notify(message)
            logger.warning(message)

    def set_input_log_enabled(self, enabled: bool) -> None:
        """入力ログの出力を止める・再開する（表示切替用）。"""
        if self.sender is not None:
            self.sender.setInputLogEnabled(enabled)

    def set_arbitration(self, mode: str, cooldown: float) -> None:
        """入力調停を Sender へ反映する。Sender が無くてもよい。"""
        if self.sender is not None:
            self.sender.setArbitration(mode=mode, cooldown=cooldown)

    def _switch_transport_body(
        self, name: str, transport_logger: Any, generation: int
    ) -> tuple[bool, bool]:
        """選択された通信方式へ差し替える。

        開いている線は Sender.setTransport が閉じる。
        戻り値は (差し替えたか, 入力ログが繋がったか)。繋がらない方式も
        あるのでここで理由を出す（黙ると1行も出ない理由が分からない）。
        呼び出し側は差し替えたら開き直す（従来どおり繋がった状態に戻す）。
        """
        if self.sender is None:
            return (False, False)
        # 表示名と実体名は一致しない（BconTransport.name == "bcon" だが
        # プリセット名は "switch-bcon"）。別名で押し直しただけで作り直すと
        # 線を閉じて HELLO からやり直しになるため、正規化してから比べる。
        # 未登録名を既定へ落とす resolve_transport_name は使わない
        # （未知名どうしが legacy_text に潰れて別方式を取り違える）。
        requested = transport.canonical_transport_name(name)
        current = transport.canonical_transport_name(self.sender.getTransportName())
        if requested is not None and requested == current:
            return (False, True)
        new_transport = transport.create_transport(name, logger=transport_logger)
        linked = self.sender.setTransport(new_transport)
        # setTransportのFalseは2通りある。入力ログの無い方式への切替
        # （運び物は替わる）と、worker停止失敗での不変である。見分けは
        # 実体の同一性で行い、替わっていなければ失敗として扱う。
        if not linked and self.sender.transport is not new_transport:
            message = f"通信方式を {name} に切り替えられませんでした。"
            self._connection_log(generation, message)
            return (False, False)
        message = f"通信方式を {name} に切り替えました。"
        self._connection_log(generation, message)
        if not linked:
            self._connection_log(generation, "この方式では入力ログを出せません。")
        return (True, linked)

    # -- 接続の受付と不変な写し -----------------------------------------------

    def connection_snapshot(self) -> ConnectionSnapshot:
        with self._connection_lock:
            return self._connection

    def subscribe_connection(
        self, callback: Callable[[ConnectionSnapshot], None]
    ) -> Callable[[], None]:
        with self._connection_lock:
            self._connection_callbacks.append(callback)
            snapshot = self._connection
        self._call_connection(callback, snapshot)

        def unsubscribe() -> None:
            with self._connection_lock:
                if callback in self._connection_callbacks:
                    self._connection_callbacks.remove(callback)

        return unsubscribe

    def subscribe_connection_log(
        self, callback: Callable[[int, str], None]
    ) -> Callable[[], None]:
        """接続ログを世代付きで通知する。ヘッドレスの購読にも共通の口。"""
        with self._connection_lock:
            self._connection_logs.append(callback)

        def unsubscribe() -> None:
            with self._connection_lock:
                if callback in self._connection_logs:
                    self._connection_logs.remove(callback)

        return unsubscribe

    @staticmethod
    def _call_connection(callback: Callable[..., None], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:
            logger.exception("接続状態の通知に失敗しました")

    def _current(self, generation: int) -> bool:
        with self._connection_lock:
            return (
                self._connection.generation == generation
                and not self._connection_cancel.is_set()
            )

    def _publish(
        self, generation: int, *, _receiving: bool = False, **changes: Any
    ) -> None:
        with self._connection_lock:
            if _receiving and self._connection.phase not in {
                "opening",
                "hello",
                "ready",
            }:
                return
            if not self._current(generation):
                return
            # ready の確定は後始末が終わった受付解放と同じ錠の中で行う。
            if not _receiving and self._connection_busy:
                if changes.get("phase") == "ready":
                    self._connection_completion.update(changes)
                    return
                # 失敗・切断・再起動の進捗も、後始末中は操作名を残す。
                if changes.get("operation", "") is None:
                    changes.pop("operation")
            self._connection = replace(self._connection, **changes)
            snapshot = self._connection
            callbacks = tuple(self._connection_callbacks)
        for callback in callbacks:
            if not self._current(generation):
                break
            self._call_connection(callback, snapshot)

    def _connection_log(self, generation: int, message: str) -> None:
        with self._connection_lock:
            if not self._current(generation):
                return
            callbacks = tuple(self._connection_logs)
        logger.info(message)
        for callback in callbacks:
            if not self._current(generation):
                break
            self._call_connection(callback, generation, message)

    def _advance_locked(self, phase: ConnectionPhase, message: str) -> int:
        """受付の錠を保持した呼出元だけが世代を変更する。"""
        self._connection_completion = {}
        self._connection_cancel.set()
        generation = self._connection.generation + 1
        self._connection_cancel = threading.Event()
        self._connection = replace(
            self._connection,
            generation=generation,
            phase=phase,
            opened=False,
            operation=message,
            message=message,
            baud=None,
            hello_ack=None,
            status=None,
            status_received_at=None,
            player_info=b"",
            rumble=b"",
        )
        return generation

    def _begin_connection(
        self,
        phase: ConnectionPhase | None = None,
        message: str = "",
    ) -> tuple[int, ConnectionSnapshot] | None:
        # sync/async とも同じ受付錠を使い、実処理は一つの executor が直列化する。
        with self._connection_lock:
            previous = self._connection
            accepted = not self._connection_shutdown and not self._connection_busy
            generation = previous.generation
            if accepted:
                self._connection_busy = True
                self._connection_completion = {}
                if phase is not None:
                    generation = self._advance_locked(phase, message)
                else:
                    self._connection = replace(self._connection, operation=message)
        if not accepted:
            self._connection_log(generation, "別の操作が実行中です。")
            return None
        if phase is not None:
            self._note_open(False)
            self._publish(generation)
        return generation, previous

    def _submit_connection(
        self, generation: int, work: Callable[[], Any]
    ) -> Future[Any]:
        def job() -> Any:
            self._connection_worker_id = threading.get_ident()
            try:
                if self._current(generation):
                    return work()
                return None
            except Exception as exc:
                opened = False
                if self._current(generation):
                    try:
                        opened = bool(
                            self.sender is not None and self.sender.isOpened()
                        )
                    except Exception:
                        pass
                self._clear_connection(generation, "failed", str(exc), opened=opened)
                self._connection_log(generation, f"接続操作に失敗しました: {exc}")
                return None
            finally:
                self._finish_connection_job(generation)

        with self._connection_lock:
            if not self._current(generation) or self._connection_shutdown:
                skipped: Future[Any] = Future()
                skipped.set_result(None)
                return skipped
            future = self._connection_executor.submit(job)
            self._connection_jobs = [
                item for item in self._connection_jobs if not item.done()
            ]
            self._connection_jobs.append(future)
        return future

    def _finish_connection_job(self, generation: int) -> None:
        with self._connection_lock:
            if not self._current(generation):
                return
            switching = self._connection.operation == "通信方式切替"
            failed = self._connection.phase == "failed"
            changes = dict(self._connection_completion)
            if switching and not failed:
                changes = dict(
                    phase="disconnected",
                    message="未接続",
                    opened=False,
                    baud=None,
                    hello_ack=None,
                    status=None,
                    status_received_at=None,
                    player_info=b"",
                    rumble=b"",
                )
            changes["operation"] = None
            self._connection = replace(self._connection, **changes)
            self._connection_completion = {}
            self._connection_busy = False
            snapshot = self._connection
            callbacks = tuple(self._connection_callbacks)
        # この後は旧ジョブの状態変更をしない。通知から同世代の操作も受付可能。
        for callback in callbacks:
            with self._connection_lock:
                if self._connection is not snapshot:
                    break
            self._call_connection(callback, snapshot)

    def _clear_connection(
        self,
        generation: int,
        phase: ConnectionPhase,
        message: str,
        *,
        opened: bool = False,
    ) -> None:
        with self._connection_lock:
            if self._current(generation):
                self._connection_completion = {}
        self._publish(
            generation,
            phase=phase,
            message=message,
            opened=opened,
            operation=None,
            baud=None,
            hello_ack=None,
            status=None,
            status_received_at=None,
            player_info=b"",
            rumble=b"",
        )

    def _subscribe_connection_rx(self, target: Any, generation: int) -> None:
        if self._connection_rx_unsub is not None:
            self._connection_rx_unsub()
            self._connection_rx_unsub = None
        if not is_bcon_transport(target):
            return

        def receive(frame: Any) -> None:
            if not self._current(generation):
                return
            if self.connection_snapshot().phase not in {"opening", "hello", "ready"}:
                return
            try:
                kind, payload = int(frame[0]), bytes(frame[1])
                if kind == T_HELLO_ACK and len(payload) == 4:
                    self._publish(generation, _receiving=True, hello_ack=tuple(payload))
                elif kind == T_STATUS and len(payload) == 7:
                    self._publish(
                        generation,
                        _receiving=True,
                        status=(
                            payload[0],
                            payload[1],
                            int.from_bytes(payload[2:4], "little"),
                            int.from_bytes(payload[4:6], "little"),
                            payload[6],
                        ),
                        status_received_at=time.monotonic(),
                    )
                elif kind == T_PLAYER_INFO:
                    self._publish(generation, _receiving=True, player_info=payload)
                elif kind == T_RUMBLE:
                    self._publish(generation, _receiving=True, rumble=payload)
            except (TypeError, ValueError, IndexError):
                return

        self._connection_rx_unsub = target.subscribe_rx(receive)

    def request_connect(
        self,
        port_num: int,
        port_name: str,
        baud: int,
        keyboard_enabled: bool,
        setting_path: str,
    ) -> bool:
        accepted = self._begin_connection("opening", "接続しています。")
        if accepted is None:
            return False
        generation, _ = accepted
        args = (port_num, port_name, baud, keyboard_enabled, setting_path)
        self._submit_connection(
            generation, lambda: self._connect_body(generation, *args)
        )
        return True

    def connect(
        self,
        port_num: int,
        port_name: str,
        baud: int,
        keyboard_enabled: bool,
        setting_path: str,
    ) -> tuple[bool, bool]:
        accepted = self._begin_connection("opening", "接続しています。")
        if accepted is None:
            return False, False
        generation, _ = accepted
        args = (port_num, port_name, baud, keyboard_enabled, setting_path)
        if threading.get_ident() == self._connection_worker_id:
            try:
                return self._connect_body(generation, *args)
            finally:
                self._finish_connection_job(generation)
        result = self._submit_connection(
            generation, lambda: self._connect_body(generation, *args)
        ).result()
        return result if isinstance(result, tuple) else (False, False)

    def _connect_body(
        self,
        generation: int,
        port_num: int,
        port_name: str,
        baud: int,
        keyboard_enabled: bool,
        setting_path: str,
    ) -> tuple[bool, bool]:
        sender = self.sender
        if sender is None:
            self._clear_connection(generation, "failed", "通信方式が未設定です。")
            return False, False
        target = sender.transport
        self.stop_keyboard()
        self.stop_gamepad()
        self.key_press = None
        if sender.isOpened():
            self._connection_log(generation, "既存の接続を閉じて接続し直します。")
            sender.closeSerial()
            if sender.isOpened():
                self._clear_connection(
                    generation, "failed", "既存の接続を閉じられませんでした。"
                )
                return False, False
        if not self._current(generation):
            return False, False
        name = sender.getTransportName()
        self._publish(
            generation, transport_name=name, port_name=port_name or str(port_num)
        )
        self._subscribe_connection_rx(target, generation)
        self._last_connect = (port_num, port_name, baud, keyboard_enabled, setting_path)
        opened = bool(sender.openSerial(port_num, port_name, baud))
        if not self._current(generation):
            return False, False
        self._note_open(opened)
        if not opened:
            reason_getter = getattr(target, "last_open_error", None)
            reason = reason_getter() if callable(reason_getter) else None
            message = (
                f"{port_name or port_num} を開けませんでした。USB 接続と、"
                "他のアプリが使用中でないか確認してください。"
            )
            if reason:
                message += f" {reason}"
            self._clear_connection(generation, "failed", message)
            self._connection_log(generation, message)
            return False, False
        self._publish(generation, opened=True)
        if is_bcon_transport(target):
            if not self._establish_bcon(generation, target, sender):
                return False, False
        else:
            if not self._prepare_connection_inputs(generation, sender):
                return False, False
            self._publish(
                generation, phase="ready", baud=baud, message="接続済み（HELLO 対象外）"
            )
            self._connection_log(
                generation,
                f"{port_name or port_num} に接続しました（{name} / {baud}bps）。",
            )
        if not self._current(generation):
            return False, False
        return True, bool(keyboard_enabled and self.keyboard is not None)

    def _prepare_connection_inputs(self, generation: int, sender: Any) -> bool:
        """ready 通知の前に入力の受け先を用意し、再試行では重複生成しない。"""
        if not self._current(generation):
            return False
        if self.key_press is None:
            self.key_press = KeyPress(sender, source="keyboard")
        if self._last_connect is not None and self.keyboard is None:
            _, _, _, keyboard_enabled, setting_path = self._last_connect
            if keyboard_enabled:
                self.set_keyboard_enabled(True, setting_path)
        return self._current(generation)

    def _establish_bcon(self, generation: int, target: Any, sender: Any) -> bool:
        if not all(
            callable(getattr(target, name, None))
            for name in (
                "hello",
                "hold_input",
                "stop_live_loop",
                "start_live_loop",
                "current_baud",
            )
        ):
            if self.connection_snapshot().phase != "ready":
                self._clear_connection(
                    generation,
                    "failed",
                    "この通信方式はこの操作に対応していません。",
                    opened=bool(target.is_open()),
                )
            self._connection_log(
                generation, "この通信方式はこの操作に対応していません。"
            )
            return False
        self._publish(
            generation,
            phase="hello",
            message="HELLO を確認しています。",
            baud=None,
            hello_ack=None,
            status=None,
            status_received_at=None,
            player_info=b"",
            rumble=b"",
        )
        ok = connect_bcon(
            target,
            lambda text: self._connection_log(generation, text),
            sender,
            is_current=lambda: self._current(generation),
        )
        if not self._current(generation):
            return False
        if not ok:
            self._clear_connection(
                generation,
                "failed",
                "HELLO を確認できませんでした。",
                opened=bool(target.is_open()),
            )
            return False
        rate_getter = getattr(target, "current_baud", None)
        rate = rate_getter() if callable(rate_getter) else None
        if not self._current(generation):
            return False
        if rate is not None and self._last_connect is not None:
            port_num, port_name, _, keyboard_enabled, setting_path = self._last_connect
            self._last_connect = (
                port_num,
                port_name,
                rate,
                keyboard_enabled,
                setting_path,
            )
        if not self._prepare_connection_inputs(generation, sender):
            return False
        snapshot = self.connection_snapshot()
        message = (
            f"BCON {snapshot.port_name} に接続しました（{snapshot.transport_name} / "
            f"{rate if rate is not None else '不明'}bps、HELLO 確認済み）。"
        )
        self._publish(
            generation,
            phase="ready",
            opened=True,
            baud=rate,
            operation=None,
            message=message,
        )
        self._connection_log(generation, message)
        return True

    def request_switch_transport(self, name: str, transport_logger: Any = None) -> bool:
        accepted = self._begin_connection("opening", "通信方式を切り替えています。")
        if accepted is None:
            return False
        generation, previous = accepted
        self._publish(generation, operation="通信方式切替")
        self._submit_connection(
            generation,
            lambda: self._switch_connection(
                generation, name, transport_logger, previous
            ),
        )
        return True

    def switch_transport(
        self, name: str, transport_logger: Any = None
    ) -> tuple[bool, bool]:
        accepted = self._begin_connection("opening", "通信方式を切り替えています。")
        if accepted is None:
            return False, False
        generation, previous = accepted
        self._publish(generation, operation="通信方式切替")
        if threading.get_ident() == self._connection_worker_id:
            try:
                return self._switch_connection(
                    generation, name, transport_logger, previous
                )
            finally:
                self._finish_connection_job(generation)
        result = self._submit_connection(
            generation,
            lambda: self._switch_connection(
                generation, name, transport_logger, previous
            ),
        ).result()
        return result if isinstance(result, tuple) else (False, False)

    def _switch_connection(
        self,
        generation: int,
        name: str,
        transport_logger: Any,
        previous: ConnectionSnapshot,
    ) -> tuple[bool, bool]:
        self.stop_keyboard()
        self.stop_gamepad()
        changed, linked = self._switch_transport_body(
            name, transport_logger, generation
        )
        if not self._current(generation):
            return False, False
        sender = self.sender
        if sender is None:
            return False, False
        self._note_open(False)
        self._publish(generation, transport_name=sender.getTransportName())
        if not changed and not linked:
            self._publish(
                generation,
                phase="failed",
                message="通信方式を切り替えられませんでした。",
                opened=bool(sender.isOpened()),
            )
            return False, False
        # 方式の切替だけを行う。GUI は完了後、移動先の baud で接続を依頼する。
        return changed, linked

    def disconnect(self) -> None:
        with self._connection_lock:
            if self._connection_shutdown:
                return
            self._connection_busy = True
            self._connection_completion = {}
            generation = self._advance_locked("closing", "切断しています。")
        self._publish(generation)
        self._note_open(False)
        self._submit_connection(generation, lambda: self._disconnect_body(generation))

    def _disconnect_body(self, generation: int) -> bool:
        self.stop_keyboard()
        self.stop_gamepad()
        sender = self.sender
        was_open = bool(sender is not None and sender.isOpened())
        if self._connection_rx_unsub is not None:
            self._connection_rx_unsub()
            self._connection_rx_unsub = None
        if sender is not None:
            sender.closeSerial()
        self.key_press = None
        self._note_open(False)
        closed = not (sender is not None and sender.isOpened())
        port = self.connection_snapshot().port_name
        self._clear_connection(
            generation,
            "disconnected" if closed else "failed",
            "未接続" if closed else "切断できませんでした。",
        )
        if was_open and closed:
            self._connection_log(generation, f"{port} を切断しました。")
        return was_open and closed

    def _bcon_request(self, label: str, work: Callable[[int, Any, Any], Any]) -> bool:
        accepted = self._begin_connection(message=label)
        if accepted is None:
            return False
        generation, _ = accepted
        self._publish(generation, operation=label)

        def job() -> None:
            sender = self.sender
            target = getattr(sender, "transport", None)
            if target is None or not is_bcon_transport(target):
                self._connection_log(
                    generation, "この通信方式はこの操作に対応していません。"
                )
            elif not target.is_open():
                self._clear_connection(
                    generation, "failed", "シリアルが開いていません。"
                )
            else:
                work(generation, target, sender)

        self._submit_connection(generation, job)
        return True

    def retry_bcon_hello(self) -> bool:
        return self._bcon_request("HELLO", self._establish_bcon)

    def change_bcon_baud(self, index: int) -> bool:
        def work(generation: int, target: Any, sender: Any) -> None:
            method = getattr(target, "set_baud_index", None)
            if (
                not callable(method)
                or index not in BAUD_TABLE
                or not all(
                    callable(getattr(target, name, None))
                    for name in ("hold_input", "current_baud")
                )
            ):
                self._connection_log(
                    generation, "この通信方式はこの操作に対応していません。"
                )
                return
            old = self.connection_snapshot().baud
            self._publish(
                generation,
                phase="hello",
                baud=None,
                hello_ack=None,
                status=None,
                status_received_at=None,
                player_info=b"",
                rumble=b"",
            )
            if not target.hold_input(True):
                raise RuntimeError("入力を停止できませんでした。")
            changed = bool(method(index))
            if not self._current(generation):
                return
            current_baud = target.current_baud()
            # 失敗時も旧速度で HELLO が通るまでは復帰と見なさない。
            if not changed and (old is None or current_baud != old):
                self._clear_connection(
                    generation,
                    "failed",
                    "通信速度を確認できませんでした。",
                    opened=True,
                )
                return
            if self._establish_bcon(generation, target, sender) and not changed:
                self._connection_log(
                    generation,
                    "速度変更に失敗しました。元の通信速度で HELLO を確認しました。",
                )

        return self._bcon_request("通信速度変更", work)

    def hunt_bcon_baud(self) -> bool:
        def work(generation: int, target: Any, sender: Any) -> None:
            method = getattr(target, "baud_hunt", None)
            if not callable(method) or not all(
                callable(getattr(target, name, None))
                for name in ("hold_input", "current_baud")
            ):
                self._connection_log(
                    generation, "この通信方式はこの操作に対応していません。"
                )
                return
            self._publish(
                generation,
                phase="hello",
                baud=None,
                hello_ack=None,
                status=None,
                status_received_at=None,
                player_info=b"",
                rumble=b"",
            )
            if not target.hold_input(True):
                raise RuntimeError("入力を停止できませんでした。")
            rate = method(candidates=list(BAUD_TABLE.values()))
            if not self._current(generation):
                return
            if rate is None:
                self._clear_connection(
                    generation,
                    "failed",
                    "通信速度を検出できませんでした。",
                    opened=True,
                )
                return
            self._establish_bcon(generation, target, sender)

        return self._bcon_request("通信速度検出", work)

    def run_bcon_operation(
        self,
        label: str,
        work: Callable[[Any], bool],
        *,
        restarts_device: bool = False,
        bootloader: bool = False,
    ) -> bool:
        def operation(generation: int, target: Any, sender: Any) -> None:
            restart = restarts_device or bootloader
            if restart:
                if not all(
                    callable(getattr(target, name, None))
                    for name in ("hold_input", "stop_live_loop")
                ):
                    self._connection_log(
                        generation, "この通信方式はこの操作に対応していません。"
                    )
                    return
                if (
                    not target.hold_input(True)
                    or not target.stop_live_loop()
                    or not sender.stopLiveWorker()
                ):
                    raise RuntimeError("入力送信を停止できませんでした。")
                sender.releaseAll()
                sender.discardLive()
                self._clear_connection(
                    generation,
                    "bootloader" if bootloader else "restarting",
                    "操作しています。",
                    opened=True,
                )
                self._publish(generation, operation=label)
            try:
                ok = bool(work(target))
            except Exception as exc:
                self._connection_log(
                    generation, f"{label} を完了できませんでした: {exc}"
                )
                ok = False
            if not self._current(generation):
                return
            if restart and ok:
                message = (
                    "ファームウェア書込み後に『更新』→『接続』を行ってください。"
                    if bootloader
                    else "Pico が再起動します。ポートが戻ったら『更新』→『接続』で接続してください。"
                )
                self._clear_connection(
                    generation, "bootloader" if bootloader else "restarting", message
                )
                self._note_open(False)
                self._connection_log(generation, message)
            elif not ok:
                self._connection_log(generation, f"{label} に失敗しました。")
                if restart and target.is_open():
                    self._establish_bcon(generation, target, sender)
                elif not target.is_open():
                    self._clear_connection(
                        generation, "failed", "接続が閉じられました。"
                    )

        return self._bcon_request(label, operation)

    # -- キーボード -------------------------------------------------------------

    def set_keyboard_enabled(self, enabled: bool, setting_path: str) -> str | None:
        """キーボード操作の有効・無効を切り替える。

        成功したら None、失敗したら理由を返す（呼び出し側は画面の
        チェックを戻す）。理由はここで利用者とファイルの両方へ出す。
        """
        if not enabled:
            self.stop_keyboard()
            return None
        # keyPress が無いまま生成すると、キーを押した瞬間に
        # AttributeError になる（Keyboard 側に None チェックはあるが、
        # 生成自体は通ってしまうため先に断つ）。
        if self.key_press is None:
            message = "シリアル未接続のためキーボード操作を有効にできません"
            self._notify(message)  # チェックが勝手に外れる理由を画面にも出す
            logger.warning(message)
            return message
        if self.keyboard is None:
            try:
                self.keyboard = SwitchKeyboardController(
                    self.key_press,
                    setting_path=setting_path,
                    is_active=self._keyboard_active,
                )
                self.keyboard.listen()
            except Exception as e:
                message = f"キーボード操作を開始できませんでした: {e}"
                self._notify(message)
                logger.warning(message)
                self.keyboard = None
                return message
        return None

    def stop_keyboard(self) -> None:
        """キーボード操作を止める。止まっていれば何もしない。

        停止処理は切断・再接続・終了の3か所から呼ばれる。同じ手順を
        3回書くと、片方だけ直したときに挙動が食い違う。
        """
        if self.keyboard is not None:
            try:
                self.keyboard.stop()
            except Exception as e:
                logger.warning(f"キーボードの停止で例外: {e}")
            self.keyboard = None

    def reload_keyboard_map(self) -> None:
        """生きている実体のキー割り当てを読み直す。

        キーコンフィグ保存直後に呼ぶ。Listener は作り直さない。
        実体が無ければ何もしない（次回の有効化で新割当を読む）。
        """
        if self.keyboard is None:
            return
        try:
            self.keyboard.reload_key_map()
        except Exception as e:
            logger.warning(f"キー割り当ての再読込で例外: {e}")

    # -- ゲームパッド -----------------------------------------------------------

    def set_gamepad_enabled(
        self, enabled: bool, pad_index: int = 0, deadzone: int = 1638
    ) -> str | None:
        """ゲームパッド操作の有効・無効を切り替える。

        成功したら None、失敗したら理由を返す（呼び出し側は画面の
        チェックを戻す）。理由はここで利用者とファイルの両方へ出す。
        キーボード操作と同じ作法である。
        """
        if not enabled:
            self.stop_gamepad()
            return None
        if self.sender is None:
            message = "シリアル未接続のためゲームパッド操作を有効にできません"
            self._notify(message)
            logger.warning(message)
            return message
        if self.gamepad is None:
            try:
                from core.hidhide import hidhide_ready
                from core.pad_source import PadSource, sdl_debug_enabled

                self.gamepad = SwitchGamepadController(
                    self.sender,
                    pad_index=pad_index,
                    is_active=self._gamepad_active,
                    on_display=self._gamepad_display,
                    deadzone=deadzone,
                    reader=PadSource(
                        allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready
                    ),
                )
                self.gamepad.listen()
            except Exception as e:
                message = f"ゲームパッド操作を開始できませんでした: {e}"
                self._notify(message)
                logger.warning(message)
                self.gamepad = None
                return message
        return None

    def stop_gamepad(self) -> None:
        """ゲームパッド操作を止める。止まっていれば何もしない。

        停止処理は切断・再接続・終了の3か所から呼ばれる。同じ手順を
        3回書くと、片方だけ直したときに挙動が食い違う。
        """
        if self.gamepad is not None:
            try:
                self.gamepad.stop()
            except Exception as e:
                logger.warning(f"ゲームパッドの停止で例外: {e}")
            self.gamepad = None

    def invalidate_gamepad(self) -> bool:
        """生きている読取器のキャッシュを捨てる（手動再接続用）。

        止めも作り直しもしない。捨てた（実体があった）ら True。
        実体が無いときは False を返し、呼び出し側が列挙用の
        読み取り口だけを破棄できるようにする。
        """
        gamepad = self.gamepad
        if gamepad is None:
            return False
        invalidate = getattr(gamepad, "invalidate_reader", None)
        if not callable(invalidate):
            return False
        try:
            invalidate()
            return True
        except Exception as e:
            logger.warning(f"ゲームパッドの読み取り口を破棄できません: {e}")
            return False

    def set_gamepad_display(self, fn: Callable[[dict[str, Any]], None] | None) -> None:
        """ゲームパッド入力の表示先を差し替える（仮想パッドの鏡用）。

        生きている実体にもそのまま付ける。Tk スレッドへの受け渡しは
        呼び出し側が行う（ここでは呼ばない。読取スレッドから来る）。
        """
        self._gamepad_display = fn
        gamepad = self.gamepad
        if gamepad is not None:
            try:
                gamepad.on_display = fn
            except Exception:
                pass

    def list_gamepads(self) -> list[int]:
        """繋がっているパッド番号の一覧。読めなければ空リスト。"""
        try:
            from core.hidhide import hidhide_ready
            from core.pad_source import PadSource, sdl_debug_enabled

            return PadSource(
                allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready
            ).scan()
        except Exception:
            return []

    # -- 状態・終了 ---------------------------------------------------------------

    def is_open(self) -> bool:
        """回線が開いているかを返す。Tk から呼ぶため RPC を待たない。

        写しが新しければそのまま返し、古ければ裏スレッドで問い直して
        その間も古い値を返す。死んだ運搬層は閉じているものとして扱い、
        例外は Tk へ逃がさない。
        """
        try:
            return self._is_open_cached()
        except Exception:
            return False

    def _is_open_cached(self) -> bool:
        """写しを読む。古ければ裏での問い直しを1件だけ予約する。"""
        sender = self.sender
        if sender is None:
            return False
        if self.connection_snapshot().phase in {
            "disconnected",
            "closing",
            "restarting",
            "bootloader",
        }:
            return False
        now = time.monotonic()
        with self._open_lock:
            same = self._open_sender is sender
            fresh = same and (now - self._open_snapshot_at) < IS_OPEN_TTL_S
            value = self._open_snapshot if same else False
            need = not fresh and not self._open_refreshing
            if need:
                self._open_refreshing = True
        if need:
            self._start_open_refresh(sender)
        return value

    def _start_open_refresh(self, sender: Any) -> None:
        """開閉の再確認も接続 worker 上で行う。"""
        generation = self.connection_snapshot().generation

        def job() -> None:
            try:
                if not self._current(generation):
                    return
                value = bool(sender.isOpened())
                if not self._current(generation):
                    return
                self._note_open(value)
                snapshot = self.connection_snapshot()
                if not value and snapshot.opened:
                    self._clear_connection(
                        generation, "failed", "接続が閉じられました。"
                    )
            except Exception:
                if self._current(generation):
                    self._note_open(False)
                    self._clear_connection(
                        generation, "failed", "接続状態を確認できませんでした。"
                    )
            finally:
                with self._open_lock:
                    self._open_refreshing = False

        try:
            with self._connection_lock:
                if self._connection_shutdown:
                    raise RuntimeError("終了処理中です。")
                future = self._connection_executor.submit(job)
                self._connection_jobs = [
                    item for item in self._connection_jobs if not item.done()
                ]
                self._connection_jobs.append(future)
        except RuntimeError:
            with self._open_lock:
                self._open_refreshing = False

    def _note_open(self, value: bool) -> None:
        """開閉の確定を写しへ直書きする。開く・閉じる手順の直後に呼ぶ。

        結果が分かっているため RPC はしない。送り先の差し替え直後にも
        呼ぶ（古い写しの取り違え防止）。
        """
        try:
            with self._open_lock:
                self._open_sender = self.sender
                self._open_snapshot = bool(value)
                self._open_snapshot_at = time.monotonic()
        except Exception:
            pass

    def flush_input_log(self) -> None:
        """溜まった入力ログを送り先へ流す（表示ポンプから呼ぶ）。"""
        if self.sender is not None:
            self.sender.flushInputLog()

    def shutdown(self) -> bool:
        """世代を無効化し、未開始ジョブを取消して一度だけ閉じる。"""
        with self._connection_lock:
            first = self._shutdown_future is None
            if first:
                self._connection_shutdown = True
                self._connection_busy = True
                self._shutdown_future = Future()
                generation = self._advance_locked("closing", "終了しています。")
            else:
                generation = self._connection.generation
            result = self._shutdown_future
            pending = tuple(self._connection_jobs) if first else ()
        assert result is not None
        own_worker = threading.get_ident() == self._connection_worker_id
        if first:
            self._note_open(False)
            self._publish(generation)
            for future in pending:
                future.cancel()

            def close() -> None:
                self._connection_worker_id = threading.get_ident()
                try:
                    closed = self._disconnect_body(generation)
                except Exception as exc:
                    logger.warning(f"終了時の切断に失敗しました: {exc}")
                    closed = False
                self._finish_connection_job(generation)
                result.set_result(closed)

            if own_worker:
                close()
                self._connection_executor.shutdown(wait=False, cancel_futures=True)
            else:
                self._connection_executor.submit(close)
                self._connection_executor.shutdown(wait=False)
        if own_worker and not result.done():
            return False
        return bool(result.result())

    # -- 走行記録（コマンドの開始・終了に付随する診断） ----------------------
    # 10走0成功のような切り分けに使う。何が・どの条件で走り、PC側で
    # 落としたか（dropped）・Picoが拒否したか（ng）・線に何が出たか
    # （CSV）をファイルログへ残す。記録の失敗で実行は壊さない。

    # 1走のワイヤ記録の上限行数。125Hzで約13分ぶん。超えたら記録だけ止める。
    MAX_RUN_ROWS = 100000

    @staticmethod
    def _ms(value: Any) -> Any:
        """秒→ミリ秒整数。取れなければ "—"。"""
        try:
            return int(round(float(value) * 1000.0))
        except (TypeError, ValueError):
            return "—"

    def _live_stats(self) -> dict[str, Any]:
        """いまのlive統計の写し。取れなければ空辞書。"""
        try:
            sender = self.sender
            if sender is None:
                return {}
            stats = sender.getLiveStats()
            return dict(stats) if isinstance(stats, dict) else {}
        except Exception:
            return {}

    def _unwrap_run_rows(self) -> None:
        """ワイヤ記録の包みを外す。無ければ何もしない。"""
        transport = self._run_transport
        self._run_transport = None
        if transport is None:
            return
        try:
            instance = getattr(transport, "__dict__", None)
            if isinstance(instance, dict):
                instance.pop("send_row", None)
        except Exception:
            logger.debug("ワイヤ記録の解除に失敗", exc_info=True)

    def _close_run_file(self) -> None:
        """記録CSVを閉じる。無ければ何もしない。"""
        with self._run_write_lock:
            try:
                if self._run_file is not None:
                    self._run_file.close()
            except Exception:
                logger.debug("記録CSVの close に失敗", exc_info=True)
            finally:
                self._run_file = None
                self._run_writer = None

    def begin_command_run(self, cmd_name: Any, profile: str = "") -> None:
        """走行記録を始める。runner が開始確定後に呼ぶ。

        live統計の基準・ワイヤ記録CSV・開始1行を残す。線が無くても
        開始行だけは残す（記録の欠落と実行の失敗を区別するため）。
        """
        try:
            if self._run_active:
                self._finish_run_rows("上書き終了")
            name = str(cmd_name)
            sender = self.sender
            transport = getattr(sender, "transport", None) if sender else None
            tname = getattr(transport, "name", "?") if transport else "?"
            dwell = self._ms(getattr(sender, "_live_min_dwell_s", None))
            repeat = self._ms(getattr(type(sender), "LIVE_REPEAT_S", None))
            self._run_stats_before = self._live_stats()
            self._run_name = name
            self._run_t0 = time.perf_counter()
            self._run_rows = 0
            self._run_csv_path = ""
            csv_path = ""
            if transport is not None:
                # 途中で失敗しても必ず閉じるため、開く前から名前を用意する。
                handle = None
                try:
                    os.makedirs(self._log_dir, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    csv_path = os.path.join(
                        self._log_dir,
                        run_journal.run_csv_name(stamp, profile, name),
                    )
                    handle = open(csv_path, "w", newline="", encoding="utf-8")
                    writer = csv.writer(handle)
                    writer.writerow(["t", "line"])
                    orig = transport.send_row

                    def _rec(
                        row: str, measure_perf: bool = True, _o: Any = orig
                    ) -> Any:
                        started = time.perf_counter()
                        try:
                            return _o(row, measure_perf)
                        finally:
                            self._record_wire_row(writer, handle, started, row)

                    transport.send_row = _rec  # type: ignore[method-assign]
                    self._run_file = handle
                    self._run_writer = writer
                    self._run_transport = transport
                    self._run_csv_path = csv_path
                except Exception:
                    # 記録が始まらなかったことは走行日誌側で分かるが、黙って
                    # いると CSV が無い理由が追えない。警告として残す。
                    logger.warning(
                        "ワイヤ記録の開始に失敗。記録なしで続行します", exc_info=True
                    )
                    self._run_csv_path = ""
                    # 開いていて所有権を渡していない handle は、ここで閉じる
                    # （_close_run_file は self._run_file しか見ない）。
                    if handle is not None:
                        try:
                            handle.close()
                        except Exception:
                            logger.debug("記録ファイルを閉じられません", exc_info=True)
                        self._run_file = None
                        self._run_writer = None
                    # 差し替えだけ済んでいた場合は包みを外す（走行のたびに
                    # 包みが重なるのを防ぐ）。
                    instance = getattr(transport, "__dict__", None)
                    if isinstance(instance, dict):
                        instance.pop("send_row", None)
            self._run_active = True
            logger.info(
                run_journal.format_run_start(
                    cmd_name=name,
                    profile=profile,
                    transport=tname,
                    dwell_ms=dwell,
                    repeat_ms=repeat,
                    version=self.code_version(),
                )
            )
        except Exception:
            logger.debug("走行記録の開始に失敗", exc_info=True)
            self._run_active = False

    def _record_wire_row(self, writer: Any, handle: Any, at: float, row: Any) -> None:
        """送出1行をCSVへ写す。上限を超えたら記録だけ止める。"""
        try:
            # 同一性の確認も書き込みも同じ錠の中で行う（close と競合させない）。
            with self._run_write_lock:
                if (
                    self._run_writer is not writer
                    or self._run_rows >= self.MAX_RUN_ROWS
                ):
                    if (
                        self._run_rows >= self.MAX_RUN_ROWS
                        and self._run_writer is writer
                    ):
                        self._run_writer = None
                        logger.warning("ワイヤ記録が上限に達したため記録を止めます")
                    return
                writer.writerow([f"{at:.6f}", str(row)])
                self._run_rows += 1
                if self._run_rows % 200 == 0:
                    handle.flush()
        except Exception:
            logger.debug("ワイヤ記録の書き出しに失敗", exc_info=True)

    def _finish_run_rows(self, reason: Any) -> dict[str, Any]:
        """記録の包み外しと集計。end_command_run の本体（同期部）。"""
        seconds = 0.0
        try:
            seconds = max(0.0, time.perf_counter() - self._run_t0)
        except Exception:
            seconds = 0.0
        self._unwrap_run_rows()
        self._close_run_file()
        after = self._live_stats()
        delta = run_journal.live_delta(self._run_stats_before, after)
        summary: dict[str, Any] = {
            "reason": str(reason),
            "seconds": seconds,
            "rows": int(self._run_rows),
            "csv_path": self._run_csv_path,
            "live_delta": delta,
            "m_thread": None,
        }
        self._run_active = False
        return summary

    def _finish_m_counts(self, name: str) -> None:
        """走行終了時のM計数を裏で読み、差分を残す。失敗は握る。"""
        try:
            sender = self.sender
            transport = getattr(sender, "transport", None) if sender else None
            if transport is None:
                logger.debug("M取得なし（transportが無い）")
                return
            # 送信方式で切ってから生シリアルを取る（bcon には M を混ぜない）。
            if run_journal.raw_serial_for_m(transport) is None:
                logger.debug("M取得なし（生シリアルを持たない方式）")
                return
            now = read_m_counts(transport)
            text = run_journal.format_m_delta(self.last_m, now if now else None)
            if now:
                self.last_m = now
            logger.info(f"M確定[{name}]: {text}")
        except Exception:
            logger.debug("M確定の取得に失敗", exc_info=True)

    def end_command_run(self, reason: Any = "完了") -> dict[str, Any]:
        """走行記録を閉じる。runner が後始末確定時に呼ぶ。

        終了1行（理由・秒数・live差分・M差分・CSV）を残す。M計数は
        裏スレッドで読む（表を固めない）。戻り値は集計辞書。
        """
        try:
            if not self._run_active:
                return {
                    "reason": str(reason),
                    "seconds": 0.0,
                    "rows": 0,
                    "csv_path": "",
                    "live_delta": {},
                    "m_thread": None,
                }
            name = self._run_name
            summary = self._finish_run_rows(reason)
            live_text = run_journal.format_live_delta(summary["live_delta"])
            if self.last_m is None:
                m_text = "M基準なし（裏で取得中）"
            else:
                m_text = "前走基準あり（裏で取得中。確定行を待つこと）"
            csv_path = summary["csv_path"] or "記録なし"
            logger.info(
                run_journal.format_run_end(
                    cmd_name=name,
                    reason=summary["reason"],
                    seconds=summary["seconds"],
                    live_text=live_text,
                    m_text=m_text,
                    csv_path=csv_path,
                    rows=summary["rows"],
                )
            )
            try:
                thread = threading.Thread(
                    target=self._finish_m_counts,
                    args=(name,),
                    name="PokeConRunM",
                    daemon=True,
                )
                thread.start()
                summary["m_thread"] = thread
            except Exception:
                logger.debug("M取得スレッドの起動に失敗", exc_info=True)
            return summary
        except Exception:
            logger.debug("走行記録の終了に失敗", exc_info=True)
            self._run_active = False
            return {
                "reason": str(reason),
                "seconds": 0.0,
                "rows": 0,
                "csv_path": "",
                "live_delta": {},
                "m_thread": None,
            }
