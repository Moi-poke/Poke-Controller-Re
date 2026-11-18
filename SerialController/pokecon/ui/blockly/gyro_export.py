"""Blockly 記録で採取した IMU サンプルの .pgyro 書き出し補助。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

#: Blockly の記録で作るジャイロ（.pgyro）の置き場。保存される `.py` と同じ
#: フォルダに置き、コマンド一式を複写すればジャイロも付いてくるようにする。
BLOCKLY_GYRO_REL = Path("Commands") / "PythonCommands"
BLOCKLY_GYRO_PERIOD_US = 5000


def save_blockly_gyro(
    app_dir: str | Path,
    take: tuple[float, list[tuple[float, tuple[int, int, int, int, int, int]]]],
    origin: float | None,
    end: float | None = None,
) -> dict[str, Any] | None:
    """記録中の IMU サンプルを .pgyro に書く。失敗は例外（呼び出し側が拾う）。

    end はマクロの終わり（最後の操作＋余韻）。最後のサンプルより後ろも
    そこで静止させ、マクロの残り時間ぶん最後の角速度を積分し続けない。
    """
    from pokecon.domain.motion_track import MotionRecorder, resample_to_grid

    t0, events = take
    offset_us, samples = resample_to_grid(
        events,
        origin if origin is not None else t0,
        end_s=(end + 0.2) if end is not None else None,
    )
    if not samples:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    millis = int(time.time() * 1000) % 1000
    name = f"blockly_{stamp}_{millis:03d}.pgyro"
    rel = BLOCKLY_GYRO_REL / name
    path = Path(app_dir) / rel
    with MotionRecorder(str(path), start_offset_us=offset_us) as recorder:
        for sample in samples:
            recorder.write(sample)
    return {
        "file": rel.as_posix(),
        "count": len(samples),
        "offset_us": offset_us,
        "duration_ms": len(samples) * (BLOCKLY_GYRO_PERIOD_US // 1000),
    }
