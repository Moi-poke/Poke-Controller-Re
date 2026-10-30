"""コマンド全体の回復境界（do_safe）の契約。"""

from __future__ import annotations

from typing import Any

from Commands.PythonCommandBase import (
    PythonCommand,
    RecoverableError,
    RecoveryAborted,
    RecoveryAdvice,
)
from core.recovery import TemplateImageError


class Recovering(PythonCommand):
    NAME = "Recovering"

    def __init__(self, outcomes: list[BaseException | None], decisions: list[Any]):
        super().__init__()
        self.outcomes = list(outcomes)
        self.decisions = list(decisions)
        self.calls = 0
        self.asked: list[RecoveryAdvice] = []
        self.cleaned = 0

    def __post_init__(self) -> None:
        pass

    def do(self) -> None:
        self.calls += 1
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome

    def ask_recovery(self, advice: RecoveryAdvice) -> bool | None:
        self.asked.append(advice)
        return self.decisions.pop(0) if self.decisions else None

    def _cleanup(self, ser: Any = None) -> None:
        self.cleaned += 1


def test_recoverable_error_can_retry_from_the_start() -> None:
    # Given: 1回目はテンプレート欠如、利用者が直して再試行を選ぶ
    cmd = Recovering(
        [RecoverableError("a.png がありません", hint="置いてください"), None],
        [True],
    )

    # When
    cmd.do_safe(None)

    # Then: 同じコマンドが最初から2回走り、履歴は失敗にしない
    assert cmd.calls == 2
    assert len(cmd.asked) == 1
    # コマンド全体の再試行は副作用が繰り返されることを必ず伝える
    assert "最初から" in cmd.asked[0].hint
    assert cmd._history_failed is False
    assert cmd.cleaned >= 1


def test_abort_marks_error_and_does_not_retry(capsys: Any) -> None:
    cmd = Recovering([TemplateImageError("a.png がありません")], [False])

    cmd.do_safe(None)

    out = capsys.readouterr().out
    assert "例外が発生しました" not in out
    assert "再試行を中止しました" in out
    assert cmd.calls == 1
    assert cmd._history_failed is True
    assert cmd.cleaned >= 1


def test_no_gui_keeps_the_original_error_path() -> None:
    # Given: 人に尋ねられない（ask が None）
    cmd = Recovering([RecoverableError("直せません")], [None])

    cmd.do_safe(None)

    assert cmd.calls == 1
    assert cmd._history_failed is True
    assert cmd.cleaned >= 1


def test_serial_error_can_retry_from_the_start() -> None:
    # Given: 通信断で落ち、利用者がケーブルを直して再試行を選ぶ
    from serial import SerialException

    cmd = Recovering([SerialException("write failed"), None], [True])

    # When
    cmd.do_safe(None)

    # Then: 送信は自動で繰り返さず、人の選択でだけ最初からやり直す
    assert cmd.calls == 2
    assert len(cmd.asked) == 1
    assert cmd.asked[0].title == "シリアル通信に失敗しました"


def test_fatal_error_is_reported_without_asking() -> None:
    cmd = Recovering([ValueError("programmer error")], [True])

    cmd.do_safe(None)

    assert cmd.calls == 1
    assert cmd.asked == []
    assert cmd._history_failed is True


def test_recovery_aborted_from_an_operation_is_not_a_traceback_path() -> None:
    # Given: 画像読み込みの途中で利用者が「中止」を選んだ
    cmd = Recovering(
        [RecoveryAborted(TemplateImageError("a.png がありません"))],
        [True],
    )

    # When
    cmd.do_safe(None)

    # Then: 再試行せず、エラーとして閉じる
    assert cmd.calls == 1
    assert cmd.asked == []
    assert cmd._history_failed is True
