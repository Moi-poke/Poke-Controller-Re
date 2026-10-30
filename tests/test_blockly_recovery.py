"""Blockly 試し実行でも、回復可能エラーは本体の再試行に任せる。"""

from __future__ import annotations

import threading

from core import Sender
from fakes import FakeTransport
from services import blockly_run

_CODE = """
from Commands.PythonCommandBase import PythonCommand, RecoverableError


class TrialRecovery(PythonCommand):
    NAME = "TrialRecovery"

    def do(self) -> None:
        if not getattr(self, "retried", False):
            self.retried = True
            raise RecoverableError("a.png がありません", hint="置いてください")
"""


def test_trial_recoverable_abort_marks_error_with_reason() -> None:
    # Given: 何度でも回復可能エラーを出す試し実行で「中止」を選ぶ
    session = blockly_run.TrialSession(run_id=2)
    cls, errors = blockly_run.build_trial_class(_CODE, session)
    assert errors == [] and cls is not None
    cmd = cls()
    cmd.ask_recovery = lambda _advice: False

    done = threading.Event()
    sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    assert cmd.start(sender, done.set) is True
    assert done.wait(5.0), "試し実行が終わらない"

    # Then: 編集画面が理由なしの「エラー」だけにならない
    assert session.result == "エラー"
    assert session.error == "実行を中止しました"


def test_trial_recoverable_error_can_retry_without_ending_early() -> None:
    # Given: 1回目だけ回復可能エラーを出す試し実行
    session = blockly_run.TrialSession(run_id=1)
    cls, errors = blockly_run.build_trial_class(_CODE, session)
    assert errors == [] and cls is not None
    cmd = cls()
    asked: list[object] = []

    def answer(advice: object) -> bool:
        asked.append(advice)
        return True

    cmd.ask_recovery = answer

    # When: 本体と同じく再試行する
    done = threading.Event()
    sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    assert cmd.start(sender, done.set) is True
    assert done.wait(5.0), "試し実行が終わらない"

    # Then: 途中で「エラー」に確定せず、最後まで走って「完了」になる
    assert len(asked) == 1
    assert session.error == ""
    assert session.result == "完了"
