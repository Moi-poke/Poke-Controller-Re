"""有線 BCON 用の US 配列 ASCII 写像と、離鍵を伴う文字列送信。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

NEUTRAL = bytes(8)
LSHIFT = 0x02
KEY_INTERVAL_S = 0.050
# spec §5.14: 非中立の間は100ms以内で同じ全状態を再送する。
# UIの間隔が100msを超えてもpress保持中にこの周期で再送する。
KEY_REFRESH_S = 0.080


def report(mod: int, key: int) -> bytes:
    """修飾キーと1キーを boot keyboard の全状態8Bへ組む。"""
    return bytes((mod, 0, key, 0, 0, 0, 0, 0))


_KEYS = {chr(ord("a") + i): (0, 0x04 + i) for i in range(26)}
_KEYS.update({chr(ord("A") + i): (LSHIFT, 0x04 + i) for i in range(26)})
_KEYS.update({char: (0, 0x1E + i) for i, char in enumerate("1234567890")})
_KEYS.update({char: (LSHIFT, 0x1E + i) for i, char in enumerate("!@#$%^&*()")})
for _plain, _shifted, _usage in (
    ("-", "_", 0x2D),
    ("=", "+", 0x2E),
    ("[", "{", 0x2F),
    ("]", "}", 0x30),
    ("\\", "|", 0x31),
    (";", ":", 0x33),
    ("'", '"', 0x34),
    ("`", "~", 0x35),
    (",", "<", 0x36),
    (".", ">", 0x37),
    ("/", "?", 0x38),
):
    _KEYS[_plain] = (0, _usage)
    _KEYS[_shifted] = (LSHIFT, _usage)
_KEYS.update(
    {
        "\n": (0, 0x28),
        "\r": (0, 0x28),
        "\x1b": (0, 0x29),
        "\b": (0, 0x2A),
        "\t": (0, 0x2B),
        " ": (0, 0x2C),
    }
)


def prepare_text(text: str) -> tuple[bytes, ...]:
    """全文を検証する。未対応文字があれば位置と文字を示して拒否する。"""
    reports = []
    for index, char in enumerate(text):
        if char not in _KEYS:
            raise ValueError(
                f"{index + 1}文字目 {char!r} は未対応です。US 配列の ASCII を入力してください。"
            )
        reports.append(report(*_KEYS[char]))
    return tuple(reports)


@dataclass(frozen=True)
class SendResult:
    """sent は押下を受け付けた文字数。Switch での入力完了を保証しない。"""

    sent: int
    error: str | None = None


def _send(send: Callable[[bytes], bool], body: bytes) -> str | None:
    try:
        if send(body):
            return None
        return "送信を受け付けませんでした。"
    except Exception as exc:
        return f"送信中に例外が発生しました: {exc}"


def release_keys(
    send: Callable[[bytes], bool],
    *,
    interval_s: float = KEY_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """指定間隔で neutral を2回試みる。1回目の失敗でも2回目は省かない。"""
    error = _send(send, NEUTRAL)
    try:
        sleep(interval_s)
    except Exception as exc:
        error = error or f"キー解放の待機に失敗しました: {exc}"
    second_error = _send(send, NEUTRAL)
    return error or second_error


def send_text(
    send: Callable[[bytes], bool],
    text: str,
    *,
    interval_s: float = KEY_INTERVAL_S,
    refresh_s: float = KEY_REFRESH_S,
    cancelled: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> SendResult:
    """全文検証後、押下と離鍵を別送する。終了・中止・例外時は離鍵を二重送信。

    interval_s は press 保持と離鍵後の待ち。refresh_s は非中立の再送周期で、
    100msを超えないよう 0.01..0.1 に丸める。interval_s が refresh_s より
    長い場合は保持中に同じpress全状態を再送する（UART/CDC欠落対策）。
    """
    # finally より前で拒否し、未対応文字を含む場合は neutral も送らない。
    reports = prepare_text(text)
    try:
        interval = float(interval_s)
        refresh = float(refresh_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"キーボード間隔が不正です: {exc}") from None
    if interval < 0.0:
        raise ValueError("キーボード間隔は0以上にしてください。")
    refresh = min(max(refresh, 0.01), 0.1)
    sent = 0
    error = None
    try:
        for body in reports:
            if cancelled():
                error = "送信を中止しました。"
                break
            error = _send(send, body)
            if error:
                break
            sent += 1
            held = 0.0
            while held + refresh < interval:
                sleep(refresh)
                held += refresh
                if cancelled():
                    error = "送信を中止しました。"
                    break
                # 非中立の全状態を再送（変化ではなく保持のリフレッシュ）。
                error = _send(send, body)
                if error:
                    break
            if error:
                break
            remain = interval - held
            if remain > 0.0:
                sleep(remain)
            error = _send(send, NEUTRAL)
            if error:
                break
            sleep(interval)
    except Exception as exc:
        error = f"送信を中止しました: {exc}"
    finally:
        release_error = release_keys(send, interval_s=interval, sleep=sleep)
        if release_error:
            error = f"{error or ''}キー解放に失敗しました: {release_error}"
    return SendResult(sent, error)
