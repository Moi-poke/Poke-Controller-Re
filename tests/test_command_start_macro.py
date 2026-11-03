"""実行開始の入口（start）と、時計送りのマクロ（timeLeap）の検証。

start は「二重起動を防ぐ」「スレッド生成に失敗したら状態を残さない」
「GUI の委譲輪を回し始める」の3点が利用者に見える差になる。
timeLeap は Switch の時計を進める手順そのものなので、押す順番が
変われば別の画面を操作してしまう。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from Commands.Keys import Button, Direction
from Commands.PythonCommandBase import PythonCommand
from pokecon.domain.CommandOperate import OperateMixin


class _Cmd(PythonCommand):
    """スレッド本体を走らせない最小のコマンド。"""

    def do(self) -> None:
        pass

    def do_safe(self, ser: Any) -> None:
        """開始・停止の検査だけしたいので、本物の実行ループは使わない。"""


def _new() -> _Cmd:
    cmd = _Cmd()
    cmd.alive = True
    return cmd


def test_start_refuses_a_second_run() -> None:
    cmd = _new()
    cmd._running = True

    assert cmd.start(None) is False


def test_start_allows_a_new_run_after_the_previous_thread_ended() -> None:
    cmd = _new()
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()

    cmd._running = False
    cmd.thread = finished
    assert cmd.start(None) is True


def test_start_refuses_while_the_previous_thread_is_alive() -> None:
    cmd = _new()
    hold = threading.Event()
    live = threading.Thread(target=hold.wait)
    live.start()
    try:
        cmd._running = False
        cmd.thread = live
        assert cmd.start(None) is False
    finally:
        hold.set()
        live.join()


def test_start_clears_state_when_the_thread_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = _new()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no thread")

    monkeypatch.setattr("Commands.PythonCommandBase.threading.Thread", boom)

    assert cmd.start(None) is False
    # 失敗したまま _running を残すと、以後このコマンドは二度と起動できない
    assert cmd._running is False
    assert cmd.thread is None
    assert cmd.postProcess is None


def test_start_hands_the_gui_poll_a_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from Commands import CommandDialog

    scheduled: list[Any] = []
    monkeypatch.setattr(
        CommandDialog, "_ensure_gui_poll", lambda root: scheduled.append(root)
    )

    class _Root:
        def after(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    cmd = _new()
    root = _Root()
    cmd.gui_root = root

    assert cmd.start(None) is True
    assert scheduled == [root]


class _Macro:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def press(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def test_time_leap_goes_back_and_forth() -> None:
    macro = _Macro()

    OperateMixin.timeLeap(macro, True)  # type: ignore[arg-type]

    pressed = [args[0] for args, _ in macro.calls]
    # 設定画面を開き、年月を増やして戻す（LEFT を含む）
    assert Direction.LEFT in pressed
    assert Button.HOME in pressed
    assert pressed[-2:] == [Button.HOME, Button.HOME]
    assert (Direction.UP in pressed) and (Direction.RIGHT in pressed)


def test_time_leap_one_way_skips_the_rollback() -> None:
    back = _Macro()
    forward = _Macro()

    OperateMixin.timeLeap(back, True)  # type: ignore[arg-type]
    OperateMixin.timeLeap(forward, False)  # type: ignore[arg-type]

    back_pressed = [args[0] for args, _ in back.calls]
    forward_pressed = [args[0] for args, _ in forward.calls]
    # 片道版は「増やす」だけなので、戻す操作（LEFT）が無い
    assert Direction.LEFT not in forward_pressed
    assert Direction.LEFT in back_pressed
    assert len(forward_pressed) < len(back_pressed)
    assert forward_pressed[-2:] == [Button.HOME, Button.HOME]
