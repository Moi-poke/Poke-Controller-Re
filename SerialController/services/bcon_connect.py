"""BCON の入力を止め、HELLO を確認してから再開する。"""

from collections.abc import Callable
from typing import Any

from core.procon_color import is_bcon_transport


def connect_bcon(
    transport: Any,
    log: Callable[[str], None],
    sender: Any = None,
    hello_timeout: float = 3.0,
    *,
    is_current: Callable[[], bool] = lambda: True,
) -> bool:
    """SerialService の直列 worker で実行する。失敗時は入力を保留する。"""
    held = False
    success = False
    try:
        if not is_current() or not is_bcon_transport(transport):
            return False
        if not transport.is_open() or not is_current():
            log("シリアルが開いていません。接続し直してください。")
            return False
        if not transport.hold_input(True) or not is_current():
            log("入力を停止できませんでした。")
            return False
        held = True
        if not transport.stop_live_loop() or not is_current():
            log("周期送信を停止できませんでした。")
            return False
        if sender is not None:
            if not sender.stopLiveWorker() or not is_current():
                log("入力送信を停止できませんでした。")
                return False
            sender.discardLive()
            sender.releaseAll()
            sender.discardLive()
        if not is_current():
            return False
        if not transport.hello(timeout=hello_timeout, quiet=True) or not is_current():
            log("HELLO を確認できませんでした。版・配線・通信速度を確認してください。")
            return False
        if sender is not None:
            sender.discardLive()
            sender.releaseAll()
            sender.discardLive()
        if not is_current() or not transport.hold_input(False):
            return False
        if not is_current():
            return False
        try:
            loop_ok = bool(transport.start_live_loop())
        except Exception:
            loop_ok = False
        if not is_current():
            return False
        if not loop_ok:
            log("周期送信を開始できないため通常送信で動作します。")
        if sender is not None:
            if not sender.startLiveWorker() or not is_current():
                log("入力送信を再開できませんでした。")
                return False
        success = is_current()
        return success
    except Exception as exc:
        log(f"接続手順を完了できませんでした: {exc}")
        return False
    finally:
        # 次世代の open は同じ worker 上でこの後始末を待つ。
        if held and not success:
            try:
                transport.hold_input(True)
                transport.stop_live_loop()
                if sender is not None:
                    sender.stopLiveWorker()
                    sender.discardLive()
            except Exception:
                pass
