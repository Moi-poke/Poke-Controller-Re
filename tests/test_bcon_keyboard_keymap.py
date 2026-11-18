"""ASCII 全文検証と、押下・離鍵・中止の送信契約。実時間は進めない。"""

import pytest
from pokecon.domain.serial.bcon_keyboard import (
    NEUTRAL,
    prepare_text,
    release_keys,
    report,
    send_text,
)


@pytest.mark.parametrize(
    ("text", "mod", "keys"),
    [
        ("abcdefghijklmnopqrstuvwxyz", 0, list(range(0x04, 0x1E))),
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 2, list(range(0x04, 0x1E))),
        ("1234567890", 0, list(range(0x1E, 0x28))),
        ("!@#$%^&*()", 2, list(range(0x1E, 0x28))),
        (
            "-=[]\\;'`,./",
            0,
            [0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38],
        ),
        (
            '_+{}|:"~<>?',
            2,
            [0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38],
        ),
        ("\n\r\x1b\b\t ", 0, [0x28, 0x28, 0x29, 0x2A, 0x2B, 0x2C]),
    ],
)
def test_us_keymap(text: str, mod: int, keys: list[int]) -> None:
    assert prepare_text(text) == tuple(
        bytes([mod, 0, key, 0, 0, 0, 0, 0]) for key in keys
    )


@pytest.mark.parametrize("text", ["ok日本語", "ok€", "ok\x00", "ok\x7f"])
def test_invalid_text_sends_no_frames_even_neutral(text: str) -> None:
    sent: list[bytes] = []

    def send(body: bytes) -> bool:
        sent.append(body)
        return True

    with pytest.raises(ValueError, match="3文字目"):
        send_text(send, text)
    assert sent == []


def test_press_release_order_and_final_duplicate_release() -> None:
    events: list[bytes | float] = []

    def send(body: bytes) -> bool:
        events.append(body)
        return True

    result = send_text(send, "aa", sleep=events.append)
    a = bytes.fromhex("00 00 04 00 00 00 00 00")
    assert events == [
        a,
        0.05,
        NEUTRAL,
        0.05,
        a,
        0.05,
        NEUTRAL,
        0.05,
        NEUTRAL,
        0.05,
        NEUTRAL,
    ]
    assert result.sent == 2
    assert result.error is None


def test_long_interval_refreshes_non_neutral_within_100ms() -> None:
    events: list[bytes | float] = []

    def send(body: bytes) -> bool:
        events.append(body)
        return True

    def sleep(seconds: float) -> None:
        events.append(round(seconds, 6))

    result = send_text(send, "a", interval_s=0.2, sleep=sleep)
    a = bytes.fromhex("00 00 04 00 00 00 00 00")
    assert events == [
        a,
        0.08,
        a,
        0.08,
        a,
        0.04,
        NEUTRAL,
        0.2,
        NEUTRAL,
        0.2,
        NEUTRAL,
    ]
    assert result.sent == 1
    assert result.error is None


def test_refresh_period_is_clamped_to_100ms() -> None:
    events: list[bytes | float] = []

    def send(body: bytes) -> bool:
        events.append(body)
        return True

    result = send_text(send, "a", interval_s=0.2, refresh_s=0.5, sleep=events.append)
    a = bytes.fromhex("00 00 04 00 00 00 00 00")
    assert events[:5] == [a, 0.1, a, 0.1, NEUTRAL]
    assert result.sent == 1
    assert result.error is None


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_failed_press_or_release_stops_and_attempts_two_neutrals(
    raises: bool,
    fail_at: int,
) -> None:
    sent: list[bytes] = []

    def send(body: bytes) -> bool:
        sent.append(body)
        if len(sent) == fail_at:
            if raises:
                raise OSError("切断")
            return False
        return True

    result = send_text(send, "abc", sleep=lambda _: None)
    assert sent[-2:] == [NEUTRAL, NEUTRAL]
    assert len(sent) == fail_at + 2
    assert result.sent == (0 if fail_at == 1 else 1)
    assert result.error


def test_cancel_after_press_never_presses_next_character() -> None:
    sent: list[bytes] = []

    def send(body: bytes) -> bool:
        sent.append(body)
        return True

    result = send_text(
        send,
        "ab",
        cancelled=lambda: bool(sent),
        sleep=lambda _: None,
    )
    assert sent == [report(0, 4), NEUTRAL, NEUTRAL, NEUTRAL]
    assert result.sent == 1
    assert result.error and "中止" in result.error


def test_wait_exception_still_releases_keys() -> None:
    sent: list[bytes] = []

    def send(body: bytes) -> bool:
        sent.append(body)
        return True

    def fail_wait(_: float) -> None:
        raise RuntimeError("待機失敗")

    result = send_text(send, "ab", sleep=fail_wait)
    assert sent == [report(0, 4), NEUTRAL, NEUTRAL]
    assert result.sent == 1 and result.error


def test_explicit_release_attempts_second_frame_even_after_exception() -> None:
    sent: list[bytes] = []

    def send(body: bytes) -> bool:
        sent.append(body)
        raise OSError("切断")

    assert release_keys(send, sleep=lambda _: None)
    assert sent == [NEUTRAL, NEUTRAL]


def test_custom_interval_applies_to_press_release_and_cleanup() -> None:
    events: list[bytes | float] = []

    def send(body: bytes) -> bool:
        events.append(body)
        return True

    result = send_text(send, "a", interval_s=0.08, sleep=events.append)
    assert result.error is None
    assert events == [
        report(0, 4),
        0.08,
        NEUTRAL,
        0.08,
        NEUTRAL,
        0.08,
        NEUTRAL,
    ]
    events.clear()
    assert release_keys(send, interval_s=0.12, sleep=events.append) is None
    assert events == [NEUTRAL, 0.12, NEUTRAL]
