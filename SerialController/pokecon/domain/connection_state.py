"""接続状態の不変な写し。GUI や通信処理には依存しない。"""

from dataclasses import dataclass
from typing import Literal

type ConnectionPhase = Literal[
    "disconnected",
    "opening",
    "hello",
    "ready",
    "failed",
    "closing",
    "restarting",
    "bootloader",
]


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    generation: int = 0
    phase: ConnectionPhase = "disconnected"
    transport_name: str = ""
    port_name: str = ""
    baud: int | None = None
    opened: bool = False
    operation: str | None = None
    message: str = "未接続"
    hello_ack: tuple[int, int, int, int] | None = None
    status: tuple[int, int, int, int, int] | None = None
    status_received_at: float | None = None
    player_info: bytes = b""
    rumble: bytes = b""
