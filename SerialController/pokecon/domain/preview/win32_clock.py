# noqa: E501  # noqa: SIZE_OK - approved Task 4 single-module Win32 runtime and facade contract.
"""High-resolution preview clock with an exclusive Tk ``after`` fallback."""

from __future__ import annotations

import ctypes
import math
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

_SUPPORTED_FPS: Final[tuple[int, ...]] = (5, 15, 30, 45, 60)
_WM_APP: Final[int] = 0x8000
_WM_NCCREATE: Final[int] = 0x0081
_PREVIEW_WAKE_MESSAGE: Final[int] = _WM_APP + 0xC1
_HWND_MESSAGE: Final[int] = -3
_INFINITE: Final[int] = 0xFFFFFFFF
_WAIT_OBJECT_0: Final[int] = 0
_WAIT_FAILED: Final[int] = 0xFFFFFFFF
_CREATE_WAITABLE_TIMER_HIGH_RESOLUTION: Final[int] = 0x00000002
_TIMER_MODIFY_STATE: Final[int] = 0x00000002
_SYNCHRONIZE: Final[int] = 0x00100000
_ZERO: Final[int] = 0
_NATIVE_WAKE_POLL_MS: Final[int] = 1
# poll の自己再予約の下限。after_idle 経由の再予約は Tk の idle 列だけが消化するため
# 同じ Interpreter の after(ms) タイマーが発火せず、after() 駆動の処理（ログ・Bcon タブ・
# 統計）が全て止まってフリーズに見える（実測: 3 秒で心拍 0 回）。0 ms で
# after() を再予約しても idle 処理と再描画を飢えさせるので、必ず 1 ms 以上で予約する。
_NATIVE_WAKE_MIN_POLL_MS: Final[int] = 1
_NATIVE_WAKE_IDLE_POLL_MS: Final[int] = 1
_NATIVE_WAKE_DRAIN_BUDGET_S: Final[float] = 0.004
# evidence は production では drain_evidence() されない（drain は health/調査経路のみ）。
# 上限なしだと 45-60fps の長時間プレビューで 1 tick あたり 1 行が際限なく積まれ、
# セッション時間に比例してメモリが増える。直近 EVIDENCE_MAX_ROWS 行だけを保持し、
# 古い行から捨てる。60fps・1 tick あたり 2 行でも 60 秒の窓を十分に覆う（約 136 秒分）。
EVIDENCE_MAX_ROWS: Final[int] = 16384


class StopResult(StrEnum):
    """Result of a main-thread stop request."""

    STOPPED = "stopped"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Result returned by one main-thread preview tick."""

    schedule: Literal["active", "idle"]


@dataclass(frozen=True, slots=True)
class PreviewTick:
    """Immutable QPC-domain tick published by the native worker."""

    epoch: int
    generation: int
    configured_fps: int
    sequence: int
    deadline_qpc: int
    emitted_qpc: int


@dataclass(frozen=True, slots=True)
class PreviewWake:
    """Immutable public wake record used by the native runtime boundary."""

    kind: Literal["clock_ready", "tick", "error"]
    epoch: int
    generation: int
    configured_fps: int
    sequence: int = 0
    qpc_frequency_hz: int = 0
    error_name: str = ""


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    """Immutable view of the worker's published health state."""

    worker_thread_native_id: int
    last_sequence: int
    last_wake_qpc: int
    post_failure_count: int
    last_post_error: int
    running: bool


class _PreviewClockError(RuntimeError):
    """Raised when native preview-clock setup cannot be completed."""


class _ControlKind(StrEnum):
    START = "start"
    SET_FPS = "set_fps"
    ACTIVE = "active"
    IDLE = "idle"
    REALIGN = "realign"


@dataclass(frozen=True, slots=True)
class _ControlState:
    kind: _ControlKind
    epoch: int
    generation: int
    configured_fps: int
    active: bool


class _ClockState(StrEnum):
    STOPPED = "stopped"
    STARTING_NATIVE = "starting_native"
    HIGH_RESOLUTION = "high_resolution"
    AFTER = "after"
    STOPPING = "stopping"
    TEARDOWN_PENDING = "teardown_pending"
    FAILED = "failed"


class _StaleTickDrop(StrEnum):
    """Sentinel outcome for a mailbox tick dropped as a superseded generation.

    ``consume_tick`` returns ``None`` for two unrelated reasons, and the main
    thread must be able to tell them apart: an empty latest-wins mailbox (the
    blit had no new frame to draw) versus a tick whose epoch/generation no
    longer matches (already counted by ``stale_tick_dropped_count``). Counting
    both in one place would conflate pacer phase offsets with stale drops and
    destroy the discrimination, so the stale case is returned explicitly.
    """

    STALE = "stale_tick_drop"


class _NativeRuntime(Protocol):
    def bind_wake_callback(self, callback: Callable[[], None]) -> None: ...

    def start(self) -> None: ...

    def request_control(self, control: _ControlState) -> None: ...

    def request_stop(self) -> None: ...

    def join(self, timeout_s: float) -> bool: ...

    def destroy(self) -> None: ...

    def consume_tick(self) -> PreviewTick | _StaleTickDrop | None: ...

    def snapshot(self) -> Mapping[str, int | str | bool]: ...

    def begin_accounting_window(self) -> None: ...

    def end_accounting_window(self) -> Mapping[str, int | str | bool]: ...

    def drain_evidence(self) -> tuple[Mapping[str, object], ...]:  # noqa: E501  # noqa: OBJECT_OK
        ...


def _validate_configured_fps(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("configured FPS must be an integer")  # noqa: E501  # noqa: GENERIC_ERR_OK
    if value not in _SUPPORTED_FPS:
        raise ValueError("configured FPS must be one of 5, 15, 30, 45, 60")  # noqa: E501  # noqa: GENERIC_ERR_OK
    return value


def _validate_idle_interval(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("idle interval must be a positive integer")  # noqa: E501  # noqa: GENERIC_ERR_OK
    return value


def _qpc_interval(frequency_hz: int, rate_hz: float) -> int:
    return max(1, math.ceil(frequency_hz / rate_hz))


def _negative_due_100ns(deadline_qpc: int, now_qpc: int, frequency_hz: int) -> int:
    delta_qpc = max(0, deadline_qpc - now_qpc)
    due_100ns = max(1, math.ceil(delta_qpc * 10_000_000 / frequency_hz))
    return -due_100ns


def _advance_deadline(
    deadline_qpc: int, now_qpc: int, interval_qpc: int
) -> tuple[int, int]:
    """Re-anchor the deadline and report how many whole periods were jumped.

    ``periods_skipped`` is ``0`` while the deadline is still in the future and
    ``(now_qpc - deadline_qpc) // interval_qpc`` once it is due, so an on-time
    tick reports nothing. The returned deadline keeps the legacy
    ``skipped = periods_skipped + 1`` grid advance, so the firing schedule of the
    pacer is unchanged; only what is reported is new.
    """
    if deadline_qpc > now_qpc:
        return deadline_qpc, 0
    periods_skipped = (now_qpc - deadline_qpc) // interval_qpc
    return deadline_qpc + (periods_skipped + 1) * interval_qpc, periods_skipped


def _validate_diagnostic_rate(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("diagnostic rate must be finite and positive")  # noqa: E501  # noqa: GENERIC_ERR_OK
    return value


if os.name == "nt":
    _WNDPROC_FACTORY = ctypes.WINFUNCTYPE
else:
    _WNDPROC_FACTORY = ctypes.CFUNCTYPE

_WNDPROC_TYPE = _WNDPROC_FACTORY(
    ctypes.c_ssize_t,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
)
type _WndProc = Any


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


class _RuntimeApi(Protocol):
    def query_performance_frequency(self) -> int: ...

    def query_performance_counter(self) -> int: ...

    def create_waitable_timer_ex(
        self,
        attributes: int | None,
        name: str | None,
        flags: int,
        desired_access: int,
    ) -> int: ...

    def create_event(
        self,
        attributes: int | None,
        manual_reset: bool,
        initial_state: bool,
        name: str | None,
    ) -> int: ...

    def register_class_ex(
        self,
        class_name: str,
        instance: int,
        window_proc: _WndProc,
    ) -> int: ...

    def create_window_ex(
        self,
        class_name: str,
        instance: int,
        parent: int,
    ) -> int: ...

    def set_waitable_timer(
        self,
        handle: int,
        due_100ns: int,
        period_ms: int,
        completion_routine: None,
        completion_argument: None,
        resume: bool,
    ) -> bool: ...

    def cancel_waitable_timer(self, handle: int) -> bool: ...

    def set_event(self, handle: int) -> bool: ...

    def event_is_set(self, handle: int) -> bool: ...

    def wait_for_multiple_objects(
        self,
        handles: Sequence[int],
        wait_all: bool,
        timeout_ms: int,
    ) -> int: ...

    def post_message(
        self, hwnd: int, message: int, wparam: int, lparam: int
    ) -> bool: ...

    def destroy_window(self, hwnd: int) -> bool: ...

    def unregister_class(self, class_name: str, instance: int) -> bool: ...

    def close_handle(self, handle: int) -> bool: ...

    def get_last_error(self) -> int: ...

    def get_module_handle(self, module_name: str | None) -> int: ...

    def native_window_proc(self) -> _WndProc: ...


def _as_handle(value: int | ctypes.c_void_p | None) -> int:
    if value is None:
        return _ZERO
    if isinstance(value, ctypes.c_void_p):
        return int(value.value or _ZERO)
    return int(value)


class _CtypesWin32Api:
    """Small explicit-signature adapter around kernel32."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise _PreviewClockError("Win32 API is only available on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._query_frequency = self._kernel32.QueryPerformanceFrequency
        self._query_counter = self._kernel32.QueryPerformanceCounter
        self._create_timer = self._kernel32.CreateWaitableTimerExW
        self._create_event = self._kernel32.CreateEventW
        self._register_class = self._user32.RegisterClassExW
        self._create_window = self._user32.CreateWindowExW
        self._set_timer = self._kernel32.SetWaitableTimer
        self._cancel_timer = self._kernel32.CancelWaitableTimer
        self._set_event = self._kernel32.SetEvent
        self._wait = self._kernel32.WaitForMultipleObjects
        self._post_message = self._user32.PostMessageW
        self._destroy_window = self._user32.DestroyWindow
        self._unregister_class = self._user32.UnregisterClassW
        self._close_handle = self._kernel32.CloseHandle
        self._get_last_error = self._kernel32.GetLastError
        self._get_module_handle = self._kernel32.GetModuleHandleW
        self._native_wndproc = self._user32.DefWindowProcW
        self._native_wndproc.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self._native_wndproc.restype = ctypes.c_ssize_t
        self._query_frequency.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
        self._query_frequency.restype = ctypes.c_int
        self._query_counter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
        self._query_counter.restype = ctypes.c_int
        self._create_timer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._create_timer.restype = ctypes.c_void_p
        self._create_event.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._create_event.restype = ctypes.c_void_p
        self._register_class.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        self._register_class.restype = ctypes.c_ushort
        self._create_window.argtypes = [
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._create_window.restype = ctypes.c_void_p
        self._set_timer.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._set_timer.restype = ctypes.c_int
        self._cancel_timer.argtypes = [ctypes.c_void_p]
        self._cancel_timer.restype = ctypes.c_int
        self._set_event.argtypes = [ctypes.c_void_p]
        self._set_event.restype = ctypes.c_int
        self._wait.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self._wait.restype = ctypes.c_uint32
        self._post_message.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self._post_message.restype = ctypes.c_int
        self._destroy_window.argtypes = [ctypes.c_void_p]
        self._destroy_window.restype = ctypes.c_int
        self._unregister_class.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        self._unregister_class.restype = ctypes.c_int
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int
        self._get_last_error.argtypes = []
        self._get_last_error.restype = ctypes.c_uint32
        self._get_module_handle.argtypes = [ctypes.c_wchar_p]
        self._get_module_handle.restype = ctypes.c_void_p
        self._last_wndclass: _WNDCLASSEXW | None = None

    def query_performance_frequency(self) -> int:
        value = ctypes.c_longlong()
        if not self._query_frequency(ctypes.byref(value)):
            raise OSError(self.get_last_error(), "QueryPerformanceFrequency failed")
        return int(value.value)

    def query_performance_counter(self) -> int:
        value = ctypes.c_longlong()
        if not self._query_counter(ctypes.byref(value)):
            raise OSError(self.get_last_error(), "QueryPerformanceCounter failed")
        return int(value.value)

    def create_waitable_timer_ex(
        self,
        attributes: int | None,
        name: str | None,
        flags: int,
        desired_access: int,
    ) -> int:
        return _as_handle(self._create_timer(attributes, name, flags, desired_access))

    def create_event(
        self,
        attributes: int | None,
        manual_reset: bool,
        initial_state: bool,
        name: str | None,
    ) -> int:
        return _as_handle(
            self._create_event(
                attributes,
                int(manual_reset),
                int(initial_state),
                name,
            )
        )

    def register_class_ex(
        self,
        class_name: str,
        instance: int,
        window_proc: _WndProc,
    ) -> int:
        window_class = _WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        window_class.lpfnWndProc = ctypes.cast(window_proc, ctypes.c_void_p)
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        self._last_wndclass = window_class
        return int(self._register_class(ctypes.byref(window_class)))

    def create_window_ex(
        self,
        class_name: str,
        instance: int,
        parent: int,
    ) -> int:
        return _as_handle(
            self._create_window(
                0,
                class_name,
                "PokeConPreviewClock",
                0,
                0,
                0,
                0,
                0,
                parent,
                None,
                instance,
                None,
            )
        )

    def set_waitable_timer(
        self,
        handle: int,
        due_100ns: int,
        period_ms: int,
        completion_routine: None,
        completion_argument: None,
        resume: bool,
    ) -> bool:
        due_time = ctypes.c_longlong(due_100ns)
        return bool(
            self._set_timer(
                handle,
                ctypes.byref(due_time),
                period_ms,
                completion_routine,
                completion_argument,
                int(resume),
            )
        )

    def cancel_waitable_timer(self, handle: int) -> bool:
        return bool(self._cancel_timer(handle))

    def set_event(self, handle: int) -> bool:
        return bool(self._set_event(handle))

    def event_is_set(self, handle: int) -> bool:
        return False

    def wait_for_multiple_objects(
        self,
        handles: Sequence[int],
        wait_all: bool,
        timeout_ms: int,
    ) -> int:
        if not handles:
            return _WAIT_FAILED
        array_type = ctypes.c_void_p * len(handles)
        handle_array = array_type(*handles)
        return int(
            self._wait(
                len(handles),
                handle_array,
                int(wait_all),
                timeout_ms,
            )
        )

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        return bool(self._post_message(hwnd, message, wparam, lparam))

    def destroy_window(self, hwnd: int) -> bool:
        return bool(self._destroy_window(hwnd))

    def unregister_class(self, class_name: str, instance: int) -> bool:
        return bool(self._unregister_class(class_name, instance))

    def close_handle(self, handle: int) -> bool:
        return bool(self._close_handle(handle))

    def get_last_error(self) -> int:
        return int(self._get_last_error())

    def get_module_handle(self, module_name: str | None) -> int:
        return _as_handle(self._get_module_handle(module_name))

    def native_window_proc(self) -> _WndProc:
        return self._native_wndproc

    @property
    def last_wndclass(self) -> _WNDCLASSEXW | None:
        return self._last_wndclass


@dataclass(frozen=True, slots=True)
class _WorkerConfig:
    epoch: int
    generation: int
    configured_fps: int
    diagnostic_rate_hz: float | None
    idle_interval_ms: int
    deadline_qpc: int
    interval_qpc: int


@dataclass(frozen=True, slots=True)
class _WorkerHandles:
    stop_event: int
    control_event: int
    timer_handle: int


@dataclass(frozen=True, slots=True)
class _CleanupEvidence:
    window_class_destroyed: bool
    window_class_unregistered: bool
    worker_running: bool
    qpc_frequency_hz: int
    worker_thread_native_id: int
    last_sequence: int
    worker_tick_published_count: int
    main_dispatch_count: int


@dataclass(frozen=True, slots=True)
class _RuntimeWake:
    reason: Literal["clock_ready", "tick", "control", "error"]
    epoch: int
    generation: int
    configured_fps: int
    sequence: int = 0
    qpc_frequency_hz: int = 0
    error_name: str = ""
    error_value: int = 0
    qpc: int = 0


class _SharedState:
    """Lock-protected mailbox, health, wake, and accounting state."""

    def __init__(self, configured_fps: int, diagnostic_rate_hz: float | None) -> None:
        self.lock = threading.Lock()
        self.configured_fps = configured_fps
        self.diagnostic_rate_hz = diagnostic_rate_hz
        self.control: _ControlState | None = None
        self.current_control: _ControlState | None = None
        self.pending: PreviewTick | None = None
        self.wakes: deque[_RuntimeWake] = deque()
        self.evidence: deque[Mapping[str, object]] = deque(maxlen=EVIDENCE_MAX_ROWS)
        self.qpc_frequency_hz = 0
        self.worker_thread_native_id = 0
        self.last_sequence = 0
        self.last_delivered_sequence = 0
        self.last_wake_qpc = 0
        self.post_failure_count = 0
        self.last_post_error = 0
        self.last_worker_error_name = ""
        self.last_worker_error_value = 0
        self.stop_signal_succeeded = True
        self.stop_signal_failure_count = 0
        self.last_stop_signal_error_name = ""
        self.last_stop_signal_error_value = 0
        self.running = False
        self.worker_tick_published_count = 0
        self.period_skipped_count = 0
        self.pending_tick_superseded_count = 0
        self.main_dispatch_count = 0
        self.stale_tick_dropped_count = 0
        self.dispatch_in_flight = False
        self.reentrant_wake_count = 0
        self.last_health_callback_result = 0
        self.window_class_destroyed = False
        self.window_class_unregistered = False
        self._accounting_events: list[tuple[str, int]] = []
        self._accounting_active = False
        self._accounting_start_index: int | None = None
        self._accounting_start_pending = False
        self._accounting_start_period_skipped = 0

    def set_qpc_frequency(self, frequency_hz: int) -> None:
        with self.lock:
            self.qpc_frequency_hz = frequency_hz

    def mark_worker_id(self, worker_thread_native_id: int) -> None:
        with self.lock:
            self.worker_thread_native_id = worker_thread_native_id
            self.running = True

    def mark_stopped(self) -> None:
        with self.lock:
            self.running = False

    def begin_stop_signal_attempt(self) -> None:
        with self.lock:
            self.stop_signal_succeeded = True

    def record_stop_signal_failure(self, error_name: str, error_value: int) -> None:
        with self.lock:
            self.stop_signal_succeeded = False
            self.stop_signal_failure_count += 1
            self.last_stop_signal_error_name = error_name
            self.last_stop_signal_error_value = error_value
            self.last_worker_error_name = error_name
            self.last_worker_error_value = error_value
            self._append_evidence_locked(
                {
                    "record_type": "stop_signal_failure",
                    "error_name": error_name,
                    "error_value": error_value,
                    "stop_signal_failure_count": self.stop_signal_failure_count,
                }
            )

    def set_control(self, control: _ControlState) -> None:
        with self.lock:
            previous = self.current_control
            self.control = control
            self.current_control = control
            if previous is not None and (
                previous.epoch != control.epoch
                or previous.generation != control.generation
            ):
                self._drop_pending_locked()

    def worker_control(self, fallback: _ControlState) -> _ControlState:
        with self.lock:
            control = self.current_control or fallback
            previous = self.current_control
            self.control = None
            self.current_control = control
            if previous is not None and (
                previous.epoch != control.epoch
                or previous.generation != control.generation
            ):
                self._drop_pending_locked()
            return control

    def take_control(self) -> _ControlState | None:
        with self.lock:
            control = self.control
            self.control = None
            return control

    def invalidate_pending(self) -> None:
        with self.lock:
            self._drop_pending_locked()

    def _drop_pending_locked(self) -> None:
        if self.pending is not None:
            if self._accounting_active:
                self._accounting_events.append(("stale", self.pending.sequence))
            self.pending = None
            self.stale_tick_dropped_count += 1

    def publish_tick(
        self,
        control: _ControlState,
        deadline_qpc: int,
        emitted_qpc: int,
        qpc_frequency_hz: int,
    ) -> PreviewTick:
        with self.lock:
            if self.pending is not None:
                if self._accounting_active:
                    self._accounting_events.append(
                        ("superseded", self.pending.sequence)
                    )
                self.pending_tick_superseded_count += 1
            sequence = self.last_sequence + 1
            if self._accounting_active:
                self._accounting_events.append(("published", sequence))
            tick = PreviewTick(
                epoch=control.epoch,
                generation=control.generation,
                configured_fps=control.configured_fps,
                sequence=sequence,
                deadline_qpc=deadline_qpc,
                emitted_qpc=emitted_qpc,
            )
            self.pending = tick
            self.last_sequence = sequence
            self.last_wake_qpc = emitted_qpc
            self.worker_tick_published_count += 1
            self._append_evidence_locked(
                {
                    "record_type": "worker_tick",
                    "epoch": tick.epoch,
                    "generation": tick.generation,
                    "configured_fps": tick.configured_fps,
                    "sequence": tick.sequence,
                    "deadline_qpc": tick.deadline_qpc,
                    "emitted_qpc": tick.emitted_qpc,
                    "qpc_frequency_hz": qpc_frequency_hz,
                }
            )
            return tick

    def consume_tick(
        self, epoch: int, generation: int
    ) -> PreviewTick | _StaleTickDrop | None:
        with self.lock:
            tick = self.pending
            self.pending = None
            if tick is None:
                return None
            if tick.epoch != epoch or tick.generation != generation:
                if self._accounting_active:
                    self._accounting_events.append(("stale", tick.sequence))
                self.stale_tick_dropped_count += 1
                self._append_evidence_locked(
                    {
                        "record_type": "stale_drop",
                        "epoch": tick.epoch,
                        "generation": tick.generation,
                        "sequence": tick.sequence,
                    }
                )
                return _StaleTickDrop.STALE
            if self._accounting_active:
                self._accounting_events.append(("dispatched", tick.sequence))
            self.main_dispatch_count += 1
            self.last_delivered_sequence = tick.sequence
            return tick

    def record_periods_skipped(self, periods_skipped: int) -> None:
        """Accumulate whole pacer periods the worker jumped over this tick."""
        with self.lock:
            self.period_skipped_count += periods_skipped

    def enqueue_wake(self, wake: _RuntimeWake) -> None:
        with self.lock:
            self.wakes.append(wake)
            if wake.qpc:
                self.last_wake_qpc = max(self.last_wake_qpc, wake.qpc)

    def take_wake(self) -> _RuntimeWake | None:
        with self.lock:
            if not self.wakes:
                return None
            return self.wakes.popleft()

    def record_control_wake(
        self,
        control: _ControlState,
        qpc_frequency_hz: int,
        now_qpc: int,
    ) -> None:
        with self.lock:
            self.last_wake_qpc = now_qpc
            self._append_evidence_locked(
                {
                    "record_type": "control_wake",
                    "reason": control.kind.value,
                    "epoch": control.epoch,
                    "generation": control.generation,
                    "configured_fps": control.configured_fps,
                    "qpc_frequency_hz": qpc_frequency_hz,
                    "wake_qpc": now_qpc,
                }
            )

    def record_post_failure(self, error_value: int) -> None:
        with self.lock:
            self.post_failure_count += 1
            self.last_post_error = error_value
            self.running = False
            self._append_evidence_locked(
                {
                    "record_type": "post_failure",
                    "error_value": error_value,
                    "post_failure_count": self.post_failure_count,
                }
            )

    def record_worker_error(self, error_name: str, error_value: int) -> None:
        with self.lock:
            self.last_worker_error_name = error_name
            self.last_worker_error_value = error_value
            self.running = False
            self._append_evidence_locked(
                {
                    "record_type": "worker_error",
                    "error_name": error_name,
                    "error_value": error_value,
                }
            )

    def record_reentrant_wake(self) -> None:
        with self.lock:
            self.reentrant_wake_count += 1
            self._append_evidence_locked(
                {
                    "record_type": "reentrant_wake",
                    "reentrant_wake_count": self.reentrant_wake_count,
                }
            )

    def set_dispatch_in_flight(self, value: bool) -> None:
        with self.lock:
            self.dispatch_in_flight = value

    def set_health_result(self, value: int) -> None:
        with self.lock:
            self.last_health_callback_result = value
            self._append_evidence_locked(
                {
                    "record_type": "health_callback",
                    "result": value,
                }
            )

    def record_health_callback(
        self,
        snapshot: Mapping[str, int | str | bool],
        result: int,
    ) -> None:
        with self.lock:
            self.last_health_callback_result = result
            self._append_evidence_locked(
                {
                    "record_type": "health_callback",
                    "result": result,
                    "running": bool(snapshot.get("running", False)),
                    "worker_thread_native_id": int(
                        snapshot.get("worker_thread_native_id", 0)
                    ),
                    "last_sequence": int(snapshot.get("last_sequence", 0)),
                    "last_delivered_sequence": int(
                        snapshot.get("last_delivered_sequence", 0)
                    ),
                    "qpc_frequency_hz": int(snapshot.get("qpc_frequency_hz", 0)),
                    "post_failure_count": int(snapshot.get("post_failure_count", 0)),
                    "last_post_error": int(snapshot.get("last_post_error", 0)),
                }
            )

    def begin_accounting(self) -> None:
        with self.lock:
            # 前回の窓が閉じ切っていない場合の取りこぼしを避けるため、先に空にする。
            self._accounting_events.clear()
            self._accounting_active = True
            self._accounting_start_index = len(self._accounting_events)
            self._accounting_start_pending = self.pending is not None
            self._accounting_start_period_skipped = self.period_skipped_count

    def end_accounting(self) -> dict[str, int | bool]:
        with self.lock:
            start_index = self._accounting_start_index or 0
            events = self._accounting_events[start_index:]
            counts = {
                "published": sum(kind == "published" for kind, _ in events)
                + int(self._accounting_start_pending),
                "dispatched": sum(kind == "dispatched" for kind, _ in events),
                "superseded": sum(kind == "superseded" for kind, _ in events),
                "stale": sum(kind == "stale" for kind, _ in events),
            }
            result: dict[str, int | bool] = {
                "worker_tick_published_count": counts["published"],
                "main_dispatch_count": counts["dispatched"],
                "pending_tick_superseded_count": counts["superseded"],
                "stale_tick_dropped_count": counts["stale"],
                "pending_tick_present_at_window_end": int(self.pending is not None),
                "period_skipped_count": max(
                    0, self.period_skipped_count - self._accounting_start_period_skipped
                ),
            }
            result["latest_only_accounting_ok"] = self._accounting_balanced_locked(
                result
            )
            self._accounting_start_index = None
            self._accounting_start_pending = False
            self._accounting_start_period_skipped = 0
            self._accounting_events.clear()
            self._accounting_active = False
            return result

    @staticmethod
    def _accounting_balanced_locked(values: Mapping[str, int | bool]) -> bool:
        published = values["worker_tick_published_count"]
        dispatched = values["main_dispatch_count"]
        superseded = values["pending_tick_superseded_count"]
        stale = values["stale_tick_dropped_count"]
        pending = values["pending_tick_present_at_window_end"]
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (published, dispatched, superseded, stale, pending)
        ):
            return False
        return published == dispatched + superseded + stale + pending

    def _append_evidence_locked(self, row: Mapping[str, object]) -> None:  # noqa: E501  # noqa: OBJECT_OK
        self.evidence.append(dict(row))

    def snapshot(self) -> dict[str, int | str | bool]:
        with self.lock:
            return {
                "qpc_frequency_hz": self.qpc_frequency_hz,
                "worker_thread_native_id": self.worker_thread_native_id,
                "last_sequence": self.last_sequence,
                "last_delivered_sequence": self.last_delivered_sequence,
                "last_wake_qpc": self.last_wake_qpc,
                "post_failure_count": self.post_failure_count,
                "last_post_error": self.last_post_error,
                "last_worker_error_name": self.last_worker_error_name,
                "last_worker_error_value": self.last_worker_error_value,
                "stop_signal_succeeded": self.stop_signal_succeeded,
                "stop_signal_failure_count": self.stop_signal_failure_count,
                "last_stop_signal_error_name": self.last_stop_signal_error_name,
                "last_stop_signal_error_value": self.last_stop_signal_error_value,
                "running": self.running,
                "worker_tick_published_count": self.worker_tick_published_count,
                "period_skipped_count": self.period_skipped_count,
                "pending_tick_superseded_count": self.pending_tick_superseded_count,
                "main_dispatch_count": self.main_dispatch_count,
                "stale_tick_dropped_count": self.stale_tick_dropped_count,
                "pending_tick_present": self.pending is not None,
                "dispatch_in_flight": self.dispatch_in_flight,
                "reentrant_wake_count": self.reentrant_wake_count,
                "last_health_callback_result": self.last_health_callback_result,
                "window_class_destroyed": self.window_class_destroyed,
                "window_class_unregistered": self.window_class_unregistered,
            }

    def drain_evidence(self) -> tuple[Mapping[str, object], ...]:  # noqa: E501  # noqa: OBJECT_OK
        with self.lock:
            rows = tuple(dict(row) for row in self.evidence)
            self.evidence.clear()
            return rows


@dataclass(frozen=True, slots=True)
class _WorkerContext:
    api: _RuntimeApi
    handles: _WorkerHandles
    hwnd: int
    message_id: int
    mailbox: _SharedState
    health: _SharedState
    config: _WorkerConfig


def _worker_signal_from_public(wake: _RuntimeWake) -> PreviewWake:
    if wake.reason == "error":
        return PreviewWake(
            kind="error",
            epoch=wake.epoch,
            generation=wake.generation,
            configured_fps=wake.configured_fps,
            qpc_frequency_hz=wake.qpc_frequency_hz,
            error_name=wake.error_name,
        )
    if wake.reason == "clock_ready":
        return PreviewWake(
            kind="clock_ready",
            epoch=wake.epoch,
            generation=wake.generation,
            configured_fps=wake.configured_fps,
            qpc_frequency_hz=wake.qpc_frequency_hz,
        )
    return PreviewWake(
        kind="tick",
        epoch=wake.epoch,
        generation=wake.generation,
        configured_fps=wake.configured_fps,
        sequence=wake.sequence,
        qpc_frequency_hz=wake.qpc_frequency_hz,
    )


def _worker_post_wake(context: _WorkerContext, wake: _RuntimeWake) -> bool:
    context.mailbox.enqueue_wake(wake)
    if context.api.post_message(context.hwnd, context.message_id, 0, 0):
        return True
    context.mailbox.record_post_failure(context.api.get_last_error())
    return False


def _worker_arm_timer(context: _WorkerContext, deadline_qpc: int, now_qpc: int) -> bool:
    due_100ns = _negative_due_100ns(
        deadline_qpc,
        now_qpc,
        context.mailbox.qpc_frequency_hz,
    )
    if context.api.set_waitable_timer(
        context.handles.timer_handle,
        due_100ns,
        0,
        None,
        None,
        False,
    ):
        return True
    _worker_post_error(context, "SetWaitableTimer")
    return False


def _worker_close_handles(context: _WorkerContext) -> None:
    handles = (
        context.handles.timer_handle,
        context.handles.control_event,
        context.handles.stop_event,
    )
    for handle in handles:
        if handle:
            try:
                context.api.close_handle(handle)
            except (OSError, RuntimeError, ValueError, AttributeError):
                context.mailbox.record_worker_error("CloseHandle", 0)


def _worker_post_error(context: _WorkerContext, error_name: str) -> None:
    error_value = context.api.get_last_error()
    context.mailbox.record_worker_error(error_name, error_value)
    current_control = context.mailbox.current_control
    if current_control is None:
        current_control = _ControlState(
            _ControlKind.START,
            context.config.epoch,
            context.config.generation,
            context.config.configured_fps,
            True,
        )
    _worker_post_wake(
        context,
        _RuntimeWake(
            "error",
            current_control.epoch,
            current_control.generation,
            current_control.configured_fps,
            qpc_frequency_hz=context.mailbox.qpc_frequency_hz,
            error_name=error_name,
            error_value=error_value,
        ),
    )


def _worker_main(context: _WorkerContext) -> None:
    """Run only timer, event, QPC, mailbox, health, and message operations."""
    api = context.api
    state = context.mailbox
    handles = context.handles
    config = context.config
    control = state.worker_control(
        _ControlState(
            _ControlKind.START,
            config.epoch,
            config.generation,
            config.configured_fps,
            True,
        )
    )
    interval_qpc = config.interval_qpc
    deadline_qpc = config.deadline_qpc
    state.mark_worker_id(threading.get_native_id())
    try:
        if not _worker_arm_timer(
            context, deadline_qpc, api.query_performance_counter()
        ):
            return
        if not _worker_post_wake(
            context,
            _RuntimeWake(
                "clock_ready",
                control.epoch,
                control.generation,
                control.configured_fps,
                qpc_frequency_hz=state.qpc_frequency_hz,
                qpc=deadline_qpc - config.interval_qpc,
            ),
        ):
            return
        while True:
            wait_result = api.wait_for_multiple_objects(
                [handles.stop_event, handles.control_event, handles.timer_handle],
                False,
                _INFINITE,
            )
            if wait_result == _WAIT_FAILED:
                _worker_post_error(context, "WaitForMultipleObjects")
                return
            stop_signaled = wait_result == _WAIT_OBJECT_0 or api.event_is_set(
                handles.stop_event
            )
            if stop_signaled:
                return
            control_signaled = wait_result == _WAIT_OBJECT_0 + 1 or api.event_is_set(
                handles.control_event
            )
            if control_signaled:
                next_control = state.take_control()
                if next_control is None:
                    continue
                control = next_control
                rate_hz = (
                    state.diagnostic_rate_hz
                    if state.diagnostic_rate_hz is not None
                    else float(control.configured_fps)
                )
                interval_qpc = _qpc_interval(state.qpc_frequency_hz, rate_hz)
                deadline_qpc = api.query_performance_counter() + interval_qpc
                state.record_control_wake(
                    control,
                    state.qpc_frequency_hz,
                    deadline_qpc - interval_qpc,
                )
                if not _worker_arm_timer(
                    context,
                    deadline_qpc,
                    deadline_qpc - interval_qpc,
                ):
                    return
                if not _worker_post_wake(
                    context,
                    _RuntimeWake(
                        "control",
                        control.epoch,
                        control.generation,
                        control.configured_fps,
                        qpc_frequency_hz=state.qpc_frequency_hz,
                        qpc=deadline_qpc - interval_qpc,
                    ),
                ):
                    return
                continue
            if wait_result != _WAIT_OBJECT_0 + 2:
                continue
            now_qpc = api.query_performance_counter()
            if now_qpc < deadline_qpc:
                if not _worker_arm_timer(context, deadline_qpc, now_qpc):
                    return
                continue
            tick = state.publish_tick(
                control,
                deadline_qpc,
                now_qpc,
                state.qpc_frequency_hz,
            )
            if not _worker_post_wake(
                context,
                _RuntimeWake(
                    "tick",
                    control.epoch,
                    control.generation,
                    control.configured_fps,
                    sequence=tick.sequence,
                    qpc_frequency_hz=state.qpc_frequency_hz,
                    qpc=now_qpc,
                ),
            ):
                return
            deadline_qpc, periods_skipped = _advance_deadline(
                deadline_qpc, now_qpc, interval_qpc
            )
            state.record_periods_skipped(periods_skipped)
            if not _worker_arm_timer(context, deadline_qpc, now_qpc):
                return
    except Exception as exc:  # noqa: E501  # noqa: BROAD_EXCEPT_OK - worker boundary must remain alive.
        _worker_post_error(context, type(exc).__name__)
    finally:
        state.mark_stopped()
        _worker_close_handles(context)


class _WindowsNativeRuntime:
    """Own all Win32 resources and the sole native worker thread."""

    def __init__(
        self,
        *,
        owner_thread_id: int,
        configured_fps: int,
        idle_interval_ms: int,
        diagnostic_rate_hz: float | None = None,
        api: _RuntimeApi | None = None,
    ) -> None:
        self._assert_owner(owner_thread_id)
        self._configured_fps = _validate_configured_fps(configured_fps)
        self._idle_interval_ms = _validate_idle_interval(idle_interval_ms)
        self._diagnostic_rate_hz = _validate_diagnostic_rate(diagnostic_rate_hz)
        self._owner_thread_id = owner_thread_id
        self._api = api if api is not None else _CtypesWin32Api()
        self._state = _SharedState(self._configured_fps, self._diagnostic_rate_hz)
        self._callback: Callable[[], None] | None = None
        self._wake_signal = threading.Event()
        self._thread: threading.Thread | None = None
        self._context: _WorkerContext | None = None
        self._initial_control: _ControlState | None = None
        self._timer_handle = 0
        self._stop_event = 0
        self._control_event = 0
        self._class_name = f"PokeConPreviewClock_{os.getpid()}_{id(self):x}"
        self._class_atom = 0
        self._class_instance = self._api.get_module_handle(None)
        self._hwnd = 0
        self._wndproc_ref: _WndProc | None = None
        self._wndclass_ref: _WNDCLASSEXW | None = None
        self._handles_closed = False
        self._initialized = False
        self._initialize()

    def _assert_owner(self, owner_thread_id: int | None = None) -> None:
        expected = self._owner_thread_id if owner_thread_id is None else owner_thread_id
        if threading.get_native_id() != expected:
            raise _PreviewClockError("preview clock operation crossed the owner thread")

    def _initialize(self) -> None:
        try:
            frequency_hz = self._api.query_performance_frequency()
            if frequency_hz <= 0:
                raise _PreviewClockError("QPC frequency is not positive")
            self._api.query_performance_counter()
            self._state.set_qpc_frequency(frequency_hz)
            self._timer_handle = self._api.create_waitable_timer_ex(
                None,
                None,
                _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                _TIMER_MODIFY_STATE | _SYNCHRONIZE,
            )
            self._require_handle(self._timer_handle, "CreateWaitableTimerExW")
            self._stop_event = self._api.create_event(None, False, False, None)
            self._require_handle(self._stop_event, "CreateEventW(stop)")
            self._control_event = self._api.create_event(None, False, False, None)
            self._require_handle(self._control_event, "CreateEventW(control)")
            self._wndproc_ref = self._api.native_window_proc()
            self._class_atom = self._api.register_class_ex(
                self._class_name,
                self._class_instance,
                self._wndproc_ref,
            )
            if self._class_atom == 0:
                raise _PreviewClockError("RegisterClassExW failed")
            self._wndclass_ref = getattr(self._api, "last_wndclass", None)
            self._hwnd = self._api.create_window_ex(
                self._class_name,
                self._class_instance,
                _HWND_MESSAGE,
            )
            if self._hwnd == 0:
                raise _PreviewClockError("CreateWindowExW failed")
            self._initialized = True
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._cleanup_initialization()
            raise

    @staticmethod
    def _require_handle(handle: int, operation: str) -> None:
        if not handle:
            raise _PreviewClockError(f"{operation} failed")

    def bind_wake_callback(self, callback: Callable[[], None]) -> None:
        self._assert_owner()
        self._callback = callback

    def consume_wake_signal(self) -> bool:
        self._assert_owner()
        with self._state.lock:
            pending = bool(self._state.wakes)
        if not pending and not self._wake_signal.is_set():
            return False
        self._wake_signal.clear()
        callback = self._callback
        if callback is not None:
            try:
                callback()
            except BaseException:  # noqa: E501  # noqa: BROAD_EXCEPT_OK - native wake boundary.
                self._state.record_worker_error("WNDPROC", 0)
        return True

    def set_initial_control(self, control: _ControlState) -> None:
        self._assert_owner()
        if self._thread is not None:
            raise _PreviewClockError("cannot set initial control after worker start")
        self._initial_control = control
        self._state.set_control(control)

    def start(self) -> None:
        self._assert_owner()
        if not self._initialized:
            raise _PreviewClockError("native runtime is not initialized")
        if self._thread is not None:
            return
        frequency_hz = self._state.snapshot()["qpc_frequency_hz"]
        if not isinstance(frequency_hz, int):
            raise _PreviewClockError("native runtime has no QPC frequency")
        initial_control = self._initial_control or _ControlState(
            _ControlKind.START,
            1,
            1,
            self._configured_fps,
            True,
        )
        if self._state.current_control is None:
            self._state.set_control(initial_control)
        anchor_qpc = self._api.query_performance_counter()
        initial_rate = self._diagnostic_rate_hz or float(initial_control.configured_fps)
        interval_qpc = _qpc_interval(frequency_hz, initial_rate)
        context = _WorkerContext(
            api=self._api,
            handles=_WorkerHandles(
                stop_event=self._stop_event,
                control_event=self._control_event,
                timer_handle=self._timer_handle,
            ),
            hwnd=self._hwnd,
            message_id=_PREVIEW_WAKE_MESSAGE,
            mailbox=self._state,
            health=self._state,
            config=_WorkerConfig(
                epoch=initial_control.epoch,
                generation=initial_control.generation,
                configured_fps=initial_control.configured_fps,
                diagnostic_rate_hz=self._diagnostic_rate_hz,
                idle_interval_ms=self._idle_interval_ms,
                deadline_qpc=anchor_qpc + interval_qpc,
                interval_qpc=interval_qpc,
            ),
        )
        self._context = context
        self._thread = threading.Thread(
            target=_worker_main,
            args=(context,),
            name="PreviewClockQpcWorker",
            daemon=True,
        )
        self._thread.start()

    def request_control(self, control: _ControlState) -> None:
        self._assert_owner()
        if self._thread is None:
            self._state.set_control(control)
            return
        self._state.set_control(control)
        if not self._api.set_event(self._control_event):
            self._state.record_worker_error("SetEvent", self._api.get_last_error())

    def request_stop(self) -> None:
        self._assert_owner()
        self._state.begin_stop_signal_attempt()
        if self._stop_event:
            try:
                if not self._api.set_event(self._stop_event):
                    self._state.record_stop_signal_failure(
                        "SetEvent", self._api.get_last_error()
                    )
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._state.record_stop_signal_failure("SetEvent", 0)
        if self._timer_handle:
            try:
                if not self._api.cancel_waitable_timer(self._timer_handle):
                    self._state.record_stop_signal_failure(
                        "CancelWaitableTimer", self._api.get_last_error()
                    )
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._state.record_stop_signal_failure("CancelWaitableTimer", 0)

    def join(self, timeout_s: float) -> bool:
        self._assert_owner()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    def destroy(self) -> None:
        self._assert_owner()
        if self._thread is not None and self._thread.is_alive():
            raise _PreviewClockError("cannot destroy a live native worker")
        if self._hwnd:
            if not self._api.destroy_window(self._hwnd):
                raise _PreviewClockError("DestroyWindow failed")
            self._state.window_class_destroyed = True
            self._hwnd = 0
        if self._class_atom:
            if not self._api.unregister_class(self._class_name, self._class_instance):
                raise _PreviewClockError("UnregisterClass failed")
            self._state.window_class_unregistered = True
            self._class_atom = 0
        if not self._handles_closed:
            if self._thread is None:
                for handle in (
                    self._timer_handle,
                    self._stop_event,
                    self._control_event,
                ):
                    if handle:
                        self._api.close_handle(handle)
            self._handles_closed = True
        self._callback = None
        self._wake_signal.clear()
        self._wndproc_ref = None
        self._wndclass_ref = None
        self._context = None

    def has_live_resources(self) -> bool:
        thread = self._thread
        return bool(
            (thread is not None and thread.is_alive())
            or self._hwnd
            or self._class_atom
            or not self._handles_closed
        )

    def consume_tick(self) -> PreviewTick | _StaleTickDrop | None:
        control = self._state.current_control
        if control is None:
            return self._state.consume_tick(1, 1)
        return self._state.consume_tick(control.epoch, control.generation)

    def consume_wake(self) -> _RuntimeWake | None:
        return self._state.take_wake()

    def record_reentrant_wake(self) -> None:
        self._state.record_reentrant_wake()

    def record_health_callback(
        self,
        snapshot: Mapping[str, int | str | bool],
        result: int,
    ) -> None:
        self._state.record_health_callback(snapshot, result)

    def set_dispatch_gate(self, value: bool) -> None:
        self._state.set_dispatch_in_flight(value)

    def invalidate_pending(self) -> None:
        self._state.invalidate_pending()

    def snapshot(self) -> dict[str, int | str | bool]:
        snapshot = self._state.snapshot()
        thread = self._thread
        snapshot["running"] = bool(thread is not None and thread.is_alive())
        snapshot["worker_thread_native_id"] = int(
            snapshot.get("worker_thread_native_id", 0)
        )
        return snapshot

    def begin_accounting_window(self) -> None:
        self._state.begin_accounting()

    def end_accounting_window(self) -> dict[str, int | bool]:
        return self._state.end_accounting()

    def drain_evidence(self) -> tuple[Mapping[str, object], ...]:  # noqa: E501  # noqa: OBJECT_OK
        return self._state.drain_evidence()

    def _wndproc(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        del hwnd, wparam, lparam
        if message == _WM_NCCREATE:
            return 1
        if message != _PREVIEW_WAKE_MESSAGE:
            return 0
        self._wake_signal.set()
        return 0

    def _cleanup_initialization(self) -> None:
        if self._hwnd:
            try:
                self._api.destroy_window(self._hwnd)
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._state.record_worker_error("initialization_cleanup", 0)
            self._hwnd = 0
        if self._class_atom:
            try:
                self._api.unregister_class(self._class_name, self._class_instance)
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._state.record_worker_error("initialization_cleanup", 0)
            self._class_atom = 0
        for handle in (
            self._timer_handle,
            self._stop_event,
            self._control_event,
        ):
            if handle:
                try:
                    self._api.close_handle(handle)
                except (OSError, RuntimeError, ValueError, AttributeError):
                    self._state.record_worker_error("initialization_cleanup", 0)
        self._timer_handle = 0
        self._stop_event = 0
        self._control_event = 0
        self._wndproc_ref = None
        self._wndclass_ref = None
        self._callback = None
        self._wake_signal.clear()
        self._handles_closed = True


def _worker_context_for_test(api: _RuntimeApi) -> _WorkerContext:
    state = _SharedState(60, None)
    state.set_qpc_frequency(10_000_000)
    return _WorkerContext(
        api=api,
        handles=_WorkerHandles(stop_event=101, control_event=102, timer_handle=103),
        hwnd=104,
        message_id=_PREVIEW_WAKE_MESSAGE,
        mailbox=state,
        health=state,
        config=_WorkerConfig(
            epoch=1,
            generation=1,
            configured_fps=60,
            diagnostic_rate_hz=None,
            idle_interval_ms=200,
            deadline_qpc=1_000_000,
            interval_qpc=166_667,
        ),
    )
