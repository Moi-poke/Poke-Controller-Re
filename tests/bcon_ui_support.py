"""BCON画面のサービス境界を調べる、Tk不要の検査体。"""

from dataclasses import replace
from typing import Any

from BconSetup import BCON_QUEUE_MAX, BconSetup
from LogPane import DropOldestQueue
from core.connection_state import ConnectionSnapshot
from core.ui_inbox import UiInbox


class Widget:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.text = ""
        self.destroyed = False
        self.pending: dict[int, Any] = {}
        self.serial = 0

    def configure(self, **values: Any) -> None:
        self.values.update(values)

    def config(self, **values: Any) -> None:
        self.configure(**values)

    def insert(self, _index: Any, text: str) -> None:
        self.text += text

    def yview(self) -> tuple[float, float]:
        return (0.0, 1.0)

    def see(self, _index: Any) -> None:
        pass

    def index(self, _value: str) -> str:
        return "1.0"

    def delete(self, *_args: Any) -> None:
        pass

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True

    def after(self, _delay: int, callback: Any) -> int:
        self.serial += 1
        self.pending[self.serial] = callback
        return self.serial

    def after_cancel(self, identifier: int) -> None:
        self.pending.pop(identifier, None)

    def itemconfigure(self, item: int, **values: Any) -> None:
        self.values[str(item)] = values


class Service:
    def __init__(self, transport: Any = None, **changes: Any) -> None:
        self.snapshot = replace(ConnectionSnapshot(), **changes)
        self.transport = transport
        self.callbacks: list[Any] = []
        self.logs: list[Any] = []
        self.operations: list[tuple[str, Any, dict[str, Any]]] = []
        self.retry = 0
        self.hunt = 0

    @staticmethod
    def uses_bcon(name: str) -> bool:
        return name in {"bcon", "switch-bcon", "switch-bcon-proc"}

    def connection_snapshot(self) -> ConnectionSnapshot:
        return self.snapshot

    def subscribe_connection(self, callback: Any) -> Any:
        self.callbacks.append(callback)
        callback(self.snapshot)
        return (
            lambda: self.callbacks.remove(callback)
            if callback in self.callbacks
            else None
        )

    def subscribe_connection_log(self, callback: Any) -> Any:
        self.logs.append(callback)
        return lambda: self.logs.remove(callback) if callback in self.logs else None

    def publish(self, **changes: Any) -> None:
        self.snapshot = replace(self.snapshot, **changes)
        for callback in tuple(self.callbacks):
            callback(self.snapshot)

    def run_bcon_operation(self, label: str, work: Any, **options: Any) -> bool:
        self.operations.append((label, work, options))
        return True

    def retry_bcon_hello(self) -> bool:
        self.retry += 1
        return True

    def hunt_bcon_baud(self) -> bool:
        self.hunt += 1
        return True


def setup(service: Service | None = None) -> Any:
    obj: Any = BconSetup.__new__(BconSetup)
    obj._service = service or Service()
    obj._snapshot = obj._service.snapshot
    obj._unsupported = set()
    obj._visibility_revision = 0
    obj._color_loaded = False
    obj._command_busy = lambda: False
    obj.inbox = UiInbox()
    obj._queue = DropOldestQueue(maxsize=BCON_QUEUE_MAX)
    obj._queue_dropped_total = 0
    obj._closed = obj._stop = obj._visible = obj._busy = False
    obj._embedded = False
    obj._poll_id = obj._capture_deadline_id = None
    obj._capture_saved_seen = False
    obj._connection_unsub = obj._log_unsub = None
    obj._color_entries = []
    obj._colors = []
    for name in (
        "window",
        "_body",
        "_summary",
        "_actual_baud",
        "_busy_label",
        "_log",
        "_imu_label",
        "_vib_label",
        "_seconds_input",
        "_btn_connect",
        "_btn_hunt",
        "_btn_cap",
        "_btn_beacon",
        "_btn_reconnect",
        "_btn_status",
        "_btn_ping",
        "_btn_winfo",
        "_btn_w0",
        "_btn_w1",
        "_btn_e0",
        "_btn_e1",
        "_btn_e2",
        "_btn_bootsel",
        "_btn_clear",
        "_btn_keys",
        "_btn_color",
        "_lamp_canvas",
    ):
        setattr(obj, name, Widget())
    obj._lamp_items = [0, 1, 2, 3]
    return obj
