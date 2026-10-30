"""回復可能エラーの分類と再試行の契約（実機なし）。"""

from __future__ import annotations

from typing import Any

import pytest
from core.recovery import (
    CameraUnavailableError,
    RecoverableError,
    RecoveryAborted,
    RecoveryAdvice,
    TemplateImageError,
    classify,
    retry_with_recovery,
)


def test_template_error_is_file_not_found_and_recoverable() -> None:
    # Given/When: テンプレート欠如の専用例外
    exc = TemplateImageError("a.png がありません", hint="Template/ に置いてください")
    # Then: 既存の FileNotFoundError 受けも壊さず、回復案内を持つ
    assert isinstance(exc, FileNotFoundError)
    assert isinstance(exc, RecoverableError)
    advice = classify(exc)
    assert advice is not None
    assert advice.title == "テンプレート画像を読み込めません"
    assert "Template/ に置いてください" in advice.message()
    assert advice.auto_attempts == 0


def test_serial_exception_is_recoverable_but_not_auto_retried() -> None:
    # Given/When: pyserial の通信失敗
    from serial import SerialException

    advice = classify(SerialException("write failed"))
    # Then: 人に尋ねる対象。ただし送信は冪等でないので自動では繰り返さない
    assert advice is not None
    assert advice.title == "シリアル通信に失敗しました"
    assert advice.auto_attempts == 0


def test_generic_file_not_found_is_not_recoverable() -> None:
    # Given/When: 利用者スクリプト自身のファイル読み込み
    # Then: テンプレート欠如と混同しない（勝手に再試行しない）
    assert classify(FileNotFoundError("user-data.csv")) is None


@pytest.mark.parametrize(
    "exc",
    [SyntaxError("bad"), NameError("x"), TypeError("x"), ValueError("x")],
)
def test_programmer_errors_are_not_recoverable(exc: BaseException) -> None:
    assert classify(exc) is None


def test_camera_error_retries_automatically_before_prompt() -> None:
    # Given: 2回失敗して3回目に成功するカメラ
    calls = 0
    sleeps: list[float] = []
    asked: list[RecoveryAdvice] = []

    def read() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CameraUnavailableError("切れた")
        return "frame"

    def ask(_exc: BaseException, advice: RecoveryAdvice) -> bool | None:
        asked.append(advice)
        return True

    # When
    result = retry_with_recovery(read, ask=ask, sleep=sleeps.append)

    # Then: 自動の2回を使い切り、人には尋ねない
    assert result == "frame"
    assert calls == 3
    assert asked == []
    assert sleeps == [0.25, 0.5]


def test_manual_retry_repeats_the_same_operation() -> None:
    # Given: 最初はテンプレートが無く、利用者が置いてから再試行する
    state = {"exists": False}
    asked: list[RecoveryAdvice] = []

    def read() -> str:
        if not state["exists"]:
            raise TemplateImageError("a.png がありません")
        return "image"

    def ask(_exc: BaseException, advice: RecoveryAdvice) -> bool | None:
        asked.append(advice)
        state["exists"] = True
        return True

    # When
    result = retry_with_recovery(read, ask=ask)

    # Then
    assert result == "image"
    assert len(asked) == 1
    assert asked[0].title == "テンプレート画像を読み込めません"


def test_abort_raises_recovery_aborted_with_cause() -> None:
    # Given: 利用者が「中止」を選ぶ
    cause = TemplateImageError("a.png がありません")

    def read() -> str:
        raise cause

    # When/Then
    with pytest.raises(RecoveryAborted) as info:
        retry_with_recovery(read, ask=lambda _exc, _advice: False)
    assert info.value.cause is cause


def test_cannot_prompt_reraises_original_error() -> None:
    # Given: GUI が無く、人に尋ねられない
    cause = TemplateImageError("a.png がありません")

    def read() -> str:
        raise cause

    # When/Then: 元の例外をそのまま出す（診断情報を落とさない）
    with pytest.raises(TemplateImageError) as info:
        retry_with_recovery(read, ask=None)
    assert info.value is cause


def test_fatal_error_is_not_retried_even_if_ask_exists() -> None:
    calls = 0
    asked: list[Any] = []

    def read() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("programmer error")

    def ask(_exc: BaseException, advice: RecoveryAdvice) -> bool | None:
        asked.append(advice)
        return True

    with pytest.raises(ValueError):
        retry_with_recovery(read, ask=ask)
    assert calls == 1
    assert asked == []


def test_backoff_is_capped() -> None:
    advice = RecoveryAdvice(
        title="t", initial_delay=0.25, multiplier=2.0, max_delay=1.0
    )
    assert [advice.delay(i) for i in range(5)] == [0.25, 0.5, 1.0, 1.0, 1.0]
