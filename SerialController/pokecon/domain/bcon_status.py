"""BCON ステータス・PLAYER_INFO・取込秒数・本体色取得の純粋デコード補助。"""

from __future__ import annotations

from typing import Any

from pokecon.domain.procon_color import describe_bcon_errcode

# 取込秒数の受付域。Pico 側は秒 1-60、範囲外は ERRCODE 0x10 で拒否する。
# 範囲外は送らず注意だけ出す（送って拒否させる運用にしない）。
CAPTURE_MIN_S = 1
CAPTURE_MAX_S = 60

# COLOR_SET の 12B は RGB×4（本体・本体・左・右）の順。画面もこの順で 4 欄置く。
COLOR_SLOT_NAMES = ("本体", "本体2", "左", "右")
COLOR_DEFAULTS = ("000000", "000000", "000000", "000000")

# 色問合せの読取失敗時に出す一文。試験盤と一字一句同じこと。
COLOR_READ_FALLBACK_NOTE = "色の読み取りに失敗しました。000000で表示します。"

# STATUS flags（SSOT は Switch-bcon の spec/protocol_v3.md §5.5）。
# bit0 USB mounted・bit1 Switch ready・bit2 timeout-neutral 中・
# bit3 WDT recovered・bit4 UART overrun・bit5 wired mode・
# bit6 BT connected・bit7 RUMBLE 見た（前回 STATUS 以降の Switch 振動出力）。
STATUS_FLAG_NAMES = (
    "USB接続中",
    "Switch準備OK",
    "無操作中立",
    "WDTから復帰",
    "UART overrun",
    "有線",
    "BT接続中",
    "RUMBLEあり",
)
STATUS_FLAG_WIRED = 0x20

# PLAYER_INFO flags bit2 の保存証拠マスク（BEACON 再生可）。
PLAYER_FLAG_CAP_SAVED = 0x04

# 取込期限の余白秒。期限は要求秒数＋この秒数の壁時計で切る。
CAPTURE_DEADLINE_MARGIN_S = 5

# 取込期限の表示文。成功文は単一定数で一字一句固定する。
SAVE_SUCCESS_TEXT = "取込を保存しました。"
SAVE_TIMEOUT_TEXT = "取込期間が終了しましたが保存を確認できませんでした。"

# PLAYER_INFO flags bit3-4/bit5-6 は Switch が 0x41 で要求した IMU レンジ。
# FW が ACK 時に保持した値を可視化用に載せている（旧 FW は 0 のまま）。
GYRO_RANGE_LABELS = ("±250dps", "±500dps", "±1000dps", "±2000dps")
ACCEL_RANGE_LABELS = ("±8G", "±4G", "±2G", "±16G")


def parse_capture_seconds(raw: Any) -> int | None:
    """取込秒数を読む。1-60 の外は None（送らず注意を出す側で使う）。"""
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if CAPTURE_MIN_S <= seconds <= CAPTURE_MAX_S:
        return seconds
    return None


def request_color(transport: Any, timeout: float = 1.0) -> bytes | None:
    """本体色の 12B を問う。成功は 12B、来なければ None（例外なし）。

    送受の実体は運搬器の `request_color`（COLOR_GET 送出＋COLOR_INFO 待ち）。
    ここは委譲と形の確認だけ受け持つ。口が無い・壊れた応答でも落とさない。
    """
    try:
        try:
            limit = float(timeout)
        except (TypeError, ValueError):
            limit = 1.0
        method = getattr(transport, "request_color", None)
        if not callable(method):
            return None
        try:
            result = method(timeout=limit)
        except TypeError:
            try:
                result = method(limit)
            except Exception:
                return None
        if result is None:
            return None
        payload = bytes(result)
    except Exception:
        return None
    if len(payload) != 12:
        return None
    return payload


def format_status_flags(flags: Any) -> str:
    """STATUS flags を日本語の並びへ。D/M 表示切替の相当情報はこれで見る。"""
    try:
        value = int(flags) & 0xFF
    except (TypeError, ValueError):
        return f"flags=不明({flags!r})"
    names = [name for bit, name in enumerate(STATUS_FLAG_NAMES) if value & (1 << bit)]
    if not names:
        return f"flags=0x{value:02X}（なし）"
    return f"flags=0x{value:02X}（{'・'.join(names)}）"


def format_status_block(status: Any) -> str:
    """STATUS_REQ の応答を 1 画面分にまとめる。読めなければ理由だけ出す。"""
    try:
        flags = int(status["flags"]) & 0xFF
        last_seq = int(status["last_seq"]) & 0xFF
        err_crc = int(status["err_crc"]) & 0xFFFF
        err_drop = int(status["err_drop"]) & 0xFFFF
        errcode = int(status["errcode"]) & 0xFF
    except (TypeError, ValueError, KeyError, AttributeError):
        return f"STATUSの読み取りに失敗しました: {status!r}"
    return (
        f"{format_status_flags(flags)} last_seq={last_seq} "
        f"err_crc={err_crc} err_drop={err_drop} "
        f"errcode=0x{errcode:02X}（{describe_bcon_errcode(errcode)}）"
    )


def decode_player_lamp(byte: int) -> tuple[bool, bool, bool, bool]:
    """PLAYER_INFO の LED バイトを下位 4bit そのまま 4 つの真偽へ。

    bit0=LED1, bit1=LED2, bit2=LED3, bit3=LED4。並べ替えはしない。
    窓口（_update_player_lamps）で据え置き/全消灯を決める手前の復号段。
    """
    value = int(byte) & 0x0F
    return (
        bool((value >> 0) & 1),
        bool((value >> 1) & 1),
        bool((value >> 2) & 1),
        bool((value >> 3) & 1),
    )


def decode_player_flags(byte: int) -> tuple[bool, bool]:
    """PLAYER_INFO の flags を (IMU, 振動) へ。予約の上位 bit は見ない。"""
    value = int(byte) & 0xFF
    return (bool((value >> 0) & 1), bool((value >> 1) & 1))


def decode_player_save(byte: int) -> bool:
    """PLAYER_INFO の flags bit2 を保存済みへ。予約 bit3-7 は見ない。"""
    value = int(byte) & 0xFF
    return bool(value & PLAYER_FLAG_CAP_SAVED)


def decode_player_imu_range(byte: int) -> tuple[str, str]:
    """PLAYER_INFO flags の IMU 要求レンジを表示名へ。未知 bit は見ない。"""
    value = int(byte) & 0xFF
    return (
        GYRO_RANGE_LABELS[(value >> 3) & 0x3],
        ACCEL_RANGE_LABELS[(value >> 5) & 0x3],
    )


def pre_status_snapshot(transport: Any) -> dict[str, Any] | None:
    """送出前の STATUS 写しを取る。線に触れず、口が無ければ None。

    旧運搬器・偽物に last_status が無くても落とさない。
    """
    try:
        current = getattr(transport, "last_status", None)
        if not callable(current):
            return None
        snapshot = current()
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    try:
        return dict(snapshot)
    except (TypeError, ValueError):
        return None
