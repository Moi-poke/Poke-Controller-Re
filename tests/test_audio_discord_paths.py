"""AudioMixin の待ち・録音・通知と Discord 通知経路の検証。

実機もネットワークも使わない。待ちは期限関数を、音声源は既知の波形を
差し替えて、実際に何回照合したか・何秒待ったかまで見る。
"""

from __future__ import annotations

import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from Commands import PythonCommandBase as pb
from Commands.PythonCommandBase import ImageProcPythonCommand, PythonCommand
from loguru import logger
from pokecon.domain.CommandAudio import AudioMixin

RATE = 44100


def sine(freq: float = 440.0, seconds: float = 0.25) -> np.ndarray:
    t = np.arange(int(seconds * RATE), dtype=np.float64) / RATE
    return (0.5 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


class FakeAudio:
    """readWindow だけを持つ音声源。要求された秒数を記録する。"""

    def __init__(self, window: np.ndarray) -> None:
        self.window = window
        self.reads: list[float] = []

    def readWindow(self, seconds: float) -> np.ndarray:
        self.reads.append(seconds)
        return self.window.copy()


class Probe(AudioMixin):
    """待ちと期限だけを差し替えた AudioMixin 単体。

    Discord 属性は作らない（「通知先が無い」経路をそのまま通すため）。
    """

    def __init__(self, window: np.ndarray | None = None) -> None:
        self._initAudio(None if window is None else FakeAudio(window))
        self.waits: list[float] = []
        # None なら期限切れしない。n なら expired() の n 回目で True。
        self.expire_after: int | None = None

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def _deadline(self, timeout: float) -> Callable[[], bool]:
        _ = timeout
        calls = 0

        def expired() -> bool:
            nonlocal calls
            calls += 1
            return self.expire_after is not None and calls >= self.expire_after

        return expired


@contextmanager
def logs() -> Iterator[list[str]]:
    """loguru のメッセージ本文だけを集める（他のテストと混ざらないよう外す）。"""
    messages: list[str] = []
    handler = logger.add(messages.append, level="INFO", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(handler)


class FakeNotify:
    """Discord_Notify の代役。作られた個体と送信内容を覚える。"""

    instances: list[FakeNotify] = []

    def __init__(self, camera: Any = None) -> None:
        self.camera = camera
        self.calls: list[dict[str, Any]] = []
        FakeNotify.instances.append(self)

    def send_message(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FailingNotify:
    """送信で必ず落ちる代役。"""

    def __init__(self, camera: Any = None) -> None:
        _ = camera

    def send_message(self, **kwargs: Any) -> None:
        raise RuntimeError(f"webhook down: {kwargs}")


class FakeCamera:
    """画像通知の __post_init__ が受け取るカメラの目印。"""


class TextCmd(PythonCommand):
    def do(self) -> None:
        pass


class ImageCmd(ImageProcPythonCommand):
    def do(self) -> None:
        pass


def test_record_clip_writes_known_wav(tmp_path: Path) -> None:
    data = sine(seconds=0.25)
    probe = Probe(data)

    out = probe.recordClip(0.25, name="my clip/1", clip_dir=str(tmp_path))

    # 音声源には録音秒数がそのまま渡る。
    assert probe.audio.reads == [0.25]
    target = Path(out)
    assert target.parent == tmp_path
    assert target.name.endswith("_my_clip_1.wav")
    with wave.open(str(target), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == RATE
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    expected = (np.clip(data.astype(np.float64), -1.0, 1.0) * 32767.0).astype(np.int16)
    np.testing.assert_array_equal(pcm, expected)


def test_record_clip_returns_empty_when_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = Probe(sine(seconds=0.1))

    def broken_open(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(wave, "open", broken_open)
    assert probe.recordClip(0.1, clip_dir=str(tmp_path)) == ""


def test_wait_sound_hits_on_second_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = Probe(np.zeros(64, dtype=np.float32))
    # 非 ASCII パスでもテンプレートを読めること（実ファイルで1回通す）。
    template = str(tmp_path / "日本語" / "bell.wav")
    probe.recordClipTo(template, source=sine(seconds=0.1), rate=RATE)
    scores = iter([0.10, 0.90])
    seen: list[int] = []

    def fake_score(window: np.ndarray, tpl: np.ndarray, rate: int) -> float:
        _ = (window, tpl)
        seen.append(rate)
        return next(scores)

    monkeypatch.setattr(probe, "_scoreSound", fake_score)

    assert (
        probe.waitSound(
            template, threshold=0.8, window_s=0.5, timeout=10.0, interval=0.01
        )
        is True
    )
    # 1回目は外れ、interval だけ待って2回目で当たる。
    assert seen == [RATE, RATE]
    assert probe.waits == [0.01]
    assert probe.audio.reads == [0.5, 0.5]


def test_wait_sound_timeout_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = Probe(np.zeros(64, dtype=np.float32))
    probe.expire_after = 1
    template = str(tmp_path / "bell.wav")
    probe.recordClipTo(template, source=sine(seconds=0.1), rate=RATE)
    monkeypatch.setattr(probe, "_scoreSound", lambda *args: 0.0)

    assert probe.waitSound(template, threshold=0.9, timeout=0.0, interval=0.01) is False
    # 期限切れなら追加の待ちも読み直しもしない。
    assert probe.waits == []
    assert probe.audio.reads == [3.0]


def test_notify_sound_without_discord_only_logs() -> None:
    probe = Probe(np.zeros(8, dtype=np.float32))
    assert not hasattr(probe, "Discord")

    with logs() as messages:
        probe._notifySound("しんじゅ")

    assert any("しんじゅ" in message for message in messages)


def test_notify_sound_sends_without_image() -> None:
    probe = Probe(np.zeros(8, dtype=np.float32))
    probe.Discord = FakeNotify()

    probe._notifySound("ぴかぴか")

    assert probe.Discord.calls == [
        {"index": 0, "content": "ぴかぴか", "without_image": True}
    ]


def test_notify_sound_swallows_send_failure() -> None:
    probe = Probe(np.zeros(8, dtype=np.float32))
    probe.Discord = FailingNotify()

    with logs() as messages:
        probe._notifySound("こおり")

    assert any("通知に失敗" in message for message in messages)


def test_match_trigger_tone_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Probe(np.zeros(8, dtype=np.float32))
    seen: list[tuple[list[tuple[float, float]], list[float], float]] = []

    def fake_tone(
        bands: list[tuple[float, float]],
        thresholds: list[float],
        window_s: float,
    ) -> bool:
        seen.append((bands, thresholds, window_s))
        return True

    monkeypatch.setattr(probe, "isTonePresent", fake_tone)
    trigger = {
        "kind": "tone",
        "params": {
            "bands": [(3000.0, 3200.0)],
            "thresholds": [0.25],
            "window_s": 2.0,
        },
    }

    assert probe._matchTrigger(trigger) is True
    assert seen == [([(3000.0, 3200.0)], [0.25], 2.0)]


def test_match_trigger_sound_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Probe(np.zeros(8, dtype=np.float32))
    seen: list[tuple[str, float, float]] = []

    def fake_sound(template_wav: str, threshold: float, window_s: float) -> bool:
        seen.append((template_wav, threshold, window_s))
        return False

    monkeypatch.setattr(probe, "isSoundPresent", fake_sound)
    trigger = {
        "kind": "sound",
        "params": {"template_wav": "bell.wav", "threshold": 0.7, "window_s": 1.5},
    }

    assert probe._matchTrigger(trigger) is False
    assert seen == [("bell.wav", 0.7, 1.5)]


def test_discord_text_forwards_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeNotify.instances.clear()
    monkeypatch.setattr(pb, "Discord_Notify", FakeNotify)
    cmd = TextCmd()
    cmd.__post_init__()
    notify = FakeNotify.instances[0]

    assert cmd.discord_text("hello", index=2) is True
    assert notify.calls[-1] == {
        "index": 2,
        "content": "hello",
        "without_image": True,
    }

    # name 指定では index ごと渡す（送信先の解決は Discord_Notify 側）。
    assert cmd.discord_text("named", index=3, name="alerts") is True
    assert notify.calls[-1] == {
        "index": 3,
        "content": "named",
        "name": "alerts",
        "without_image": True,
    }


def test_discord_text_returns_false_on_error() -> None:
    cmd = TextCmd()
    cmd.Discord = FailingNotify()

    assert cmd.discord_text("hello") is False


def test_image_post_init_builds_notify_with_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeNotify.instances.clear()
    monkeypatch.setattr(pb, "Discord_Notify", FakeNotify)
    camera = FakeCamera()
    cmd = ImageCmd(camera)

    cmd.__post_init__()

    assert len(FakeNotify.instances) == 1
    made = FakeNotify.instances[0]
    assert made.camera is camera
    assert cmd.Discord is made


def test_discord_image_forwards_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeNotify.instances.clear()
    monkeypatch.setattr(pb, "Discord_Notify", FakeNotify)
    cmd = ImageCmd(FakeCamera())
    cmd.__post_init__()
    notify = FakeNotify.instances[0]

    assert cmd.discord_image("shot", index=1) is True
    assert notify.calls[-1] == {"index": 1, "content": "shot"}

    assert cmd.discord_image("named", index=2, name="alerts") is True
    assert notify.calls[-1] == {"index": 2, "content": "named", "name": "alerts"}


def test_discord_image_returns_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pb, "Discord_Notify", FailingNotify)
    cmd = ImageCmd(FakeCamera())
    cmd.__post_init__()

    assert cmd.discord_image("shot") is False
