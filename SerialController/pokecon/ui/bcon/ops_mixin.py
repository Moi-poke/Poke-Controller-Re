"""BCON 設定 UI のボタン操作・RPC 実行ハンドラ。"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
import tkinter.messagebox as tkmsg
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pokecon.domain.bcon_status import (
    STATUS_FLAG_WIRED,
    decode_player_save,
    format_status_block,
    parse_capture_seconds,
    pre_status_snapshot,
)
from pokecon.domain.procon_color import (
    build_color_payload,
    describe_bcon_errcode,
    is_bcon_transport,
    parse_color_hex,
    send_config_frame,
)
from pokecon.domain.serial import bcon_keyboard
from pokecon.domain.transport.bcon_protocol import (
    T_BEACON_START,
    T_CAPTURE_START,
    T_COLOR_SET,
    T_KEY_DELETE,
)

_LOGGER = logging.getLogger("pokecon.ui.BconSetup")


class BconOpsMixin:
    """BconSetup の RPC 送出・確認ダイアログ・各種操作ハンドラを担当する。"""

    if TYPE_CHECKING:
        window: Any
        _service: Any
        _closed: bool
        _queue: Any
        _visibility_revision: int
        _capture_saved_seen: bool
        _seconds: Any
        _colors: list[Any]
        _kb_text: Any
        _kb_interval: Any
        _kb_cancel: threading.Event
        _kb_pending: threading.Event
        _on_emulate_role: Callable[[int], None] | None
        _success_dispatch: Callable[[Callable[[], None]], None] | None

        def _admit(self, *, recovery: bool = False) -> bool: ...
        def _set_busy(self) -> None: ...
        def _arm_capture_deadline(self, seconds: int) -> None: ...

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
        """STATUS の前後差分で拒否を判定し、粘着 errcode を失敗にしない。"""
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
            pre = pre_status_snapshot(transport)
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
        on_success: Callable[[], None] | None = None,
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
            elif on_success is not None:
                # ワーカーから Tk を触らない。GUI スレッドへ渡す。
                self._dispatch_success(on_success)
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
            on_success=lambda: self._notify_emulate_role(role),
        )

    def _dispatch_success(self, callback: Callable[[], None]) -> None:
        """成功後の処理を GUI スレッドへ渡す。

        success_dispatch があればそれを使う（タブが見えていない間も届く）。
        無ければ自分の _poll の queue へ積む（テスト・単体利用の退避）。
        """
        dispatch = getattr(self, "_success_dispatch", None)
        if callable(dispatch):
            try:
                dispatch(callback)
                return
            except Exception:
                _LOGGER.exception("BCON操作の成功後処理を渡せませんでした")
        self._post_result("success", callback)

    def _notify_emulate_role(self, role: int) -> None:
        """種別切替の成功を画面へ伝える（GUI スレッド専用）。"""
        if self._closed:
            return
        callback = getattr(self, "_on_emulate_role", None)
        if not callable(callback):
            return
        try:
            callback(int(role))
        except Exception:
            _LOGGER.exception("種別切替の成功後処理に失敗しました")

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

    def _on_delete_keys(self) -> None:
        if not self._admit():
            return
        snapshot = self._service.connection_snapshot()
        if snapshot.status and (int(snapshot.status[0]) & STATUS_FLAG_WIRED):
            self._queue.put(
                ("log", "有線モードでは鍵を削除できません。無線で起動してください。")
            )
            return
        if (
            tkmsg.askquestion(
                "確認",
                "Pico の Classic リンク鍵と保存ホストをすべて削除します。取込は残ります。取り消せません。Switch側の登録解除も必要です。続けますか？",
                parent=self.window,
            )
            == "yes"
        ):
            self._send_config(T_KEY_DELETE, b"", "ペア鍵削除中", "ペア鍵削除")

    def _keyboard_sender(
        self, transport: Any, log: Callable[[str], None]
    ) -> Callable[[bytes], bool] | None:
        if not is_bcon_transport(transport) or not callable(
            getattr(transport, "send_keyboard", None)
        ):
            log("この通信方式はキーボード入力に対応していません。")
            return None
        snapshot = self._service.connection_snapshot()
        if not transport.hello_established() or snapshot.hello_ack is None:
            log("先に接続（HELLO→開始）を実行してください")
            return None
        minor = snapshot.hello_ack[2]
        if minor < 13:
            log("PicoのFWが古い（0.13未満）。書き込んでください")
            return None
        if minor < 15:
            log("FW15未満はキーボード取りこぼし対策前です。")
        return lambda body: bool(transport.send_keyboard(body))

    def _on_keyboard_send(self) -> None:
        if not self._admit():
            return
        # Entry の読取りと操作世代の固定は GUI スレッドで済ませる。
        text = self._kb_text.get()
        interval_s = self._keyboard_interval_s()
        if interval_s is None:
            return
        generation = self._service.connection_snapshot().generation
        log = self._operation_log()
        cancel = threading.Event()
        self._kb_cancel = cancel

        def cancelled() -> bool:
            snapshot = self._service.connection_snapshot()
            return (
                cancel.is_set()
                or snapshot.generation != generation
                or not snapshot.opened
                or snapshot.phase != "ready"
            )

        def work(transport: Any) -> bool:
            try:
                send = self._keyboard_sender(transport, log)
                if send is None:
                    return False
                try:
                    result = bcon_keyboard.send_text(
                        send, text, interval_s=interval_s, cancelled=cancelled
                    )
                except ValueError as exc:
                    log(str(exc))
                    return False
                log(
                    f"キーボード送信: {result.sent}/{len(text)}文字を通信へ書き込みました。"
                    + (result.error or "")
                )
                return result.error is None
            finally:
                self._kb_pending.clear()

        self._kb_pending.set()
        if not self._run(work, "キーボード送信"):
            self._kb_pending.clear()

    def _release_keyboard(
        self, transport: Any, interval_s: float = bcon_keyboard.KEY_INTERVAL_S
    ) -> bool:
        log = self._operation_log()
        send = self._keyboard_sender(transport, log)
        if send is None:
            return False
        error = bcon_keyboard.release_keys(send, interval_s=interval_s)
        log(
            f"キー解放に失敗しました: {error}"
            if error
            else "キー解放を通信へ書き込みました。"
        )
        return error is None

    def _keyboard_interval_s(self) -> float | None:
        try:
            interval_ms = self._kb_interval.get()
            if 20 <= interval_ms <= 200:
                return interval_ms / 1000
        except (tk.TclError, ValueError):
            pass
        self._queue.put(("log", "間隔は20〜200msで入力してください。"))
        return None

    def _on_keyboard_release(self) -> None:
        if not self._admit():
            return
        interval_s = self._keyboard_interval_s()
        if interval_s is not None:
            self._run(
                lambda transport: self._release_keyboard(transport, interval_s),
                "キー解放",
            )

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
