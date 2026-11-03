"""PythonCommand の実行制御まわりの振る舞い（実機不要）。

対象は次の 2 ファイルの未実行だった経路。
  ・Commands/PythonCommandBase.py … togglePause / paused_total / _pausedSeconds /
    direct_serial / _serialConfig / print2 / _subLogQueue / log2
  ・pokecon/domain/CommandOperate.py … _deadline / _runElapsed

時間は time.perf_counter を手で進める時計に差し替え、実時間は待たない。
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from Commands import PythonCommandBase
from Commands.PythonCommandBase import PythonCommand, StopThread
from pokecon.domain import CommandOperate
from pokecon.ui import LogPane


class _MinimalCommand(PythonCommand):
    """状態だけを持つ最小のコマンド。do は呼ばない。"""

    NAME = "_MinimalCommand"

    # 実行時に Window が差し込む任意属性。テストも同じ経路で差し込む。
    sub_log_queue: Any = None

    def do(self) -> None:
        """何もしない。pause などの部品だけを直接呼ぶ。"""


class _FakeKeys:
    """direct_serial が触る範囲だけを持つ偽物。"""

    def __init__(self, log: list[str] | None = None) -> None:
        self.ser: Any = None
        self.source = "script"
        self.sent: list[tuple[list[str], list[float]]] = []
        self.ended = 0
        self._log = log

    def serialcommand_direct_send(
        self, commands: list[str], waittime: list[float]
    ) -> None:
        self.sent.append((list(commands), list(waittime)))
        if self._log is not None:
            self._log.append(f"send:{commands[0]}")

    def end(self) -> None:
        self.ended += 1


class _ManualClock:
    """手で進める時計。呼ばれただけでは進まない。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _ManualClock:
    """perf_counter を決定的な時計へ差し替える。

    pause / resume / _pausedSeconds は PythonCommandBase 側で、
    _deadline / _runElapsed は CommandOperate 側で perf_counter を引く。
    両モジュールの time 参照だけを差し替え、プロセス全体の時計は汚さない。
    """
    clock = _ManualClock()
    fake_time = SimpleNamespace(perf_counter=clock)
    monkeypatch.setattr(PythonCommandBase, "time", fake_time)
    monkeypatch.setattr(CommandOperate, "time", fake_time)
    return clock


def _make_command(keys: _FakeKeys | None = None) -> _MinimalCommand:
    """install 相当の状態を明示的にそろえたコマンドを作る。"""
    cmd = _MinimalCommand()
    cmd.keys = _FakeKeys() if keys is None else keys
    cmd.alive = True
    cmd._stop_event = threading.Event()
    cmd._resume_event = threading.Event()
    cmd._resume_event.set()  # 既定は実行中
    cmd._paused_total = 0.0
    cmd._pause_started = 0.0
    return cmd


def test_paused_total_includes_time_while_paused_and_accumulates(
    clock: _ManualClock,
) -> None:
    # Given: 走っているコマンド
    cmd = _make_command()
    assert cmd.paused_total == 0.0

    # When: 一時停止して時計だけを 5 秒進める
    cmd.pause()
    assert cmd.isPaused()
    clock.now = 5.0

    # Then: 停止の最中でも進行分が返る（待ちの締切補正が参照する値）
    assert cmd.paused_total == pytest.approx(5.0)
    assert cmd._pausedSeconds() == pytest.approx(5.0)

    # When: 再開して 4 秒走らせ、もう一度 3 秒止める
    cmd.resume()
    assert not cmd.isPaused()
    clock.now = 9.0
    assert cmd.paused_total == pytest.approx(5.0)  # 走っていた分は加算しない
    cmd.pause()
    clock.now = 12.0
    cmd.resume()

    # Then: 通算は単調に増え、止めていた分だけを数える
    assert cmd.paused_total == pytest.approx(8.0)


def test_toggle_pause_returns_the_state_after_switching(clock: _ManualClock) -> None:
    # Given: 走っているコマンド
    cmd = _make_command()

    # When: 1 回切り替える
    switched = cmd.togglePause()

    # Then: 戻り値は「切り替えた後」の状態で、元に戻すともう一度切り替わる
    assert switched is True
    assert cmd.isPaused()
    clock.now = 3.0
    assert cmd.togglePause() is False
    assert not cmd.isPaused()
    assert cmd.paused_total == pytest.approx(3.0)


@pytest.mark.parametrize(
    "config",
    [
        None,  # 未設定
        ["COM3", 9600],  # 辞書でない
        {"com_port": 3, "com_port_name": "COM3"},  # baud_rate 欠落
        {"com_port": "COM3", "com_port_name": "COM3", "baud_rate": 9600},  # 変換不能
        {"com_port": 3, "com_port_name": "COM3", "baud_rate": [9600]},  # 型不正
        {"com_port": 0, "com_port_name": "", "baud_rate": 9600},  # 空
        {"com_port": -1, "com_port_name": "", "baud_rate": 9600},  # 空（負の番号）
    ],
)
def test_serial_config_returns_none_for_missing_or_invalid(config: Any) -> None:
    cmd = _make_command()
    cmd.serial_config = config

    assert cmd._serialConfig() is None


def test_serial_config_normalizes_numeric_text_to_int() -> None:
    # Given: settings.ini 由来の値が文字列で入っている
    cmd = _make_command()
    cmd.serial_config = {
        "com_port": "7",
        "com_port_name": "COM7",
        "baud_rate": "19200",
    }

    # Then: 開き直しで使える型にそろえて返す
    assert cmd._serialConfig() == {
        "com_port": 7,
        "com_port_name": "COM7",
        "baud_rate": 19200,
    }


def test_serial_config_accepts_port_zero_when_a_name_is_given() -> None:
    # 「空」と見なすのは番号も名前も空のときだけ。名前があるなら開き先は決まる。
    cmd = _make_command()
    cmd.serial_config = {"com_port": 0, "com_port_name": "Bcon", "baud_rate": 1000000}

    assert cmd._serialConfig() == {
        "com_port": 0,
        "com_port_name": "Bcon",
        "baud_rate": 1000000,
    }


def test_direct_serial_rejects_count_mismatch() -> None:
    # Given: 命令と待ち時間の数がずれている
    keys = _FakeKeys()
    cmd = _make_command(keys)

    # Then: 黙って短い方に合わせず、送る前に落ちる
    with pytest.raises(ValueError, match="数が違います"):
        cmd.direct_serial(["A", "B"], [0.0])

    assert keys.sent == []


def test_direct_serial_strips_line_breaks_before_sending() -> None:
    # Given: CR/LF 付きの生文字列
    keys = _FakeKeys()
    cmd = _make_command(keys)

    # When: 付いたまま送ると壊れるので落として送る
    cmd.direct_serial(["A\r\n", "B\n", "C"], [0.0, 0.0, 0.0])

    # Then: 改行は消え、待ちはこちら側で済ませたので Keys には 0 秒を渡す
    assert keys.sent == [(["A"], [0.0]), (["B"], [0.0]), (["C"], [0.0])]


def test_direct_serial_gates_and_waits_before_each_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: 1 件ずつ関所と待ちを通ることを記録できるコマンド
    log: list[str] = []
    keys = _FakeKeys(log)
    cmd = _make_command(keys)
    monkeypatch.setattr(cmd, "_gate", lambda: log.append("gate"))
    monkeypatch.setattr(cmd, "wait", lambda seconds: log.append(f"wait:{seconds}"))

    # When: 待ち時間付きで 2 件送る
    cmd.direct_serial(["A", "B"], [0.5, 0.25])

    # Then: 1 件ごとに 関所 → 待ち → 送信 の順で通る
    assert log == ["gate", "wait:0.5", "send:A", "gate", "wait:0.25", "send:B"]


def test_direct_serial_stops_before_sending_when_command_is_stopped() -> None:
    # Given: Stop 済み（alive=False）のコマンド
    keys = _FakeKeys()
    cmd = _make_command(keys)
    cmd.alive = False

    # Then: 1 件目を送る前に関所で降り、押下も解放される
    with pytest.raises(StopThread):
        cmd.direct_serial(["A", "B"], [0.0, 0.0])

    assert keys.sent == []
    assert keys.ended == 1
    assert cmd.keys is None


def test_print2_and_log2_prefer_the_sub_log_queue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: 副ログの受け皿を持つコマンド
    sink: queue.Queue[str] = queue.Queue()
    cmd = _make_command()
    cmd.sub_log_queue = sink

    # When: print と同じ書き方で出す
    cmd.print2("a", "b")
    cmd.log2("c")

    # Then: 標準出力ではなく受け皿へ、sep / end を反映して入る
    assert sink.get_nowait() == "a b\n"
    assert sink.get_nowait() == "c\n"
    assert sink.empty()
    assert capsys.readouterr().out == ""


def test_print2_and_log2_fall_back_to_stdout_without_any_queue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: 副ログの受け皿がどこにも無い（CUI 実行・GUI 前）
    monkeypatch.setattr(LogPane, "sub_log_queue", None)
    cmd = _make_command()

    # When: sep / end も指定して出す
    cmd.print2("x", "y", sep="|", end="!")
    cmd.log2("z")

    # Then: 失われないよう通常の print へ落ちる
    assert capsys.readouterr().out == "x|y!z\n"


def test_sub_log_queue_prefers_the_attribute_then_log_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: LogPane 側にも受け皿がある状態
    shared: queue.Queue[str] = queue.Queue()
    own: queue.Queue[str] = queue.Queue()
    monkeypatch.setattr(LogPane, "sub_log_queue", shared)
    cmd = _make_command()

    # Then: 属性が無ければ LogPane の受け皿
    assert cmd._subLogQueue() is shared

    # When: 明示的に差し込むとそちらが優先される
    cmd.sub_log_queue = own
    assert cmd._subLogQueue() is own

    # Then: None は「無い」と同じ扱いで LogPane へ戻る
    cmd.sub_log_queue = None
    assert cmd._subLogQueue() is shared


def test_deadline_is_already_expired_for_zero_and_negative_timeouts(
    clock: _ManualClock,
) -> None:
    # Given: 経過 0 の時計
    cmd = _make_command()

    # Then: 0 は「その場で期限」、負はさらに前なので即座に時間切れ
    assert cmd._deadline(0.0)() is True
    assert cmd._deadline(-1.0)() is True


def test_deadline_expires_exactly_at_the_timeout_boundary(
    clock: _ManualClock,
) -> None:
    # Given: 期限 2 秒
    cmd = _make_command()
    expired = cmd._deadline(2.0)

    # Then: 2 秒ちょうどで切り替わる（1.999 秒ではまだ）
    clock.now = 1.999
    assert expired() is False
    clock.now = 2.0
    assert expired() is True


def test_deadline_does_not_count_paused_time(clock: _ManualClock) -> None:
    # Given: 期限 2 秒で始めた待ち
    cmd = _make_command()
    expired = cmd._deadline(2.0)
    clock.now = 1.0
    assert expired() is False

    # When: そのまま 10 秒間一時停止する
    cmd.pause()
    clock.now = 11.0

    # Then: 止めている間は期限が進まず、再開後に残り 1 秒を数える
    assert expired() is False
    cmd.resume()
    clock.now = 11.5
    assert expired() is False
    clock.now = 12.0
    assert expired() is True


def test_run_elapsed_freezes_while_paused_and_skips_the_paused_span(
    clock: _ManualClock,
) -> None:
    # Given: 走り始めの目盛り
    cmd = _make_command()
    start = cmd._runElapsed()
    assert start == 0.0

    # When: 5 秒走ってから 3 秒止め、再開して 2 秒走る
    clock.now = 5.0
    before_pause = cmd._runElapsed()
    cmd.pause()
    clock.now = 8.0
    during_pause = cmd._runElapsed()
    cmd.resume()
    clock.now = 10.0

    # Then: 停止中は止まったまま、差分は走っていた 2 秒だけになる
    assert before_pause == pytest.approx(5.0)
    assert during_pause == pytest.approx(5.0)
    assert cmd._runElapsed() == pytest.approx(7.0)
    assert cmd._runElapsed() - before_pause == pytest.approx(2.0)
