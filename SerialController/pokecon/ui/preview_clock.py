"""High-resolution preview clock with an exclusive Tk ``after`` fallback."""

from __future__ import annotations

import ctypes as ctypes
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, assert_never

from loguru import logger
from pokecon.domain.preview.win32_clock import (
    EVIDENCE_MAX_ROWS as EVIDENCE_MAX_ROWS,
    _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION as _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
    _HWND_MESSAGE as _HWND_MESSAGE,
    _INFINITE as _INFINITE,
    _NATIVE_WAKE_DRAIN_BUDGET_S as _NATIVE_WAKE_DRAIN_BUDGET_S,
    _NATIVE_WAKE_IDLE_POLL_MS as _NATIVE_WAKE_IDLE_POLL_MS,
    _NATIVE_WAKE_MIN_POLL_MS as _NATIVE_WAKE_MIN_POLL_MS,
    _NATIVE_WAKE_POLL_MS as _NATIVE_WAKE_POLL_MS,
    _PREVIEW_WAKE_MESSAGE as _PREVIEW_WAKE_MESSAGE,
    _SUPPORTED_FPS as _SUPPORTED_FPS,
    _SYNCHRONIZE as _SYNCHRONIZE,
    _TIMER_MODIFY_STATE as _TIMER_MODIFY_STATE,
    _WAIT_FAILED as _WAIT_FAILED,
    _WAIT_OBJECT_0 as _WAIT_OBJECT_0,
    _WM_APP as _WM_APP,
    _WM_NCCREATE as _WM_NCCREATE,
    _WNDCLASSEXW as _WNDCLASSEXW,
    _WNDPROC_TYPE as _WNDPROC_TYPE,
    _ZERO as _ZERO,
    DispatchResult as DispatchResult,
    PreviewTick as PreviewTick,
    PreviewWake as PreviewWake,
    StopResult as StopResult,
    WorkerHealth as WorkerHealth,
    _CleanupEvidence as _CleanupEvidence,
    _ClockState as _ClockState,
    _ControlKind as _ControlKind,
    _ControlState as _ControlState,
    _CtypesWin32Api as _CtypesWin32Api,
    _NativeRuntime as _NativeRuntime,
    _PreviewClockError as _PreviewClockError,
    _RuntimeApi as _RuntimeApi,
    _RuntimeWake as _RuntimeWake,
    _SharedState as _SharedState,
    _StaleTickDrop as _StaleTickDrop,
    _WindowsNativeRuntime as _WindowsNativeRuntime,
    _WndProc as _WndProc,
    _WorkerConfig as _WorkerConfig,
    _WorkerContext as _WorkerContext,
    _WorkerHandles as _WorkerHandles,
    _advance_deadline as _advance_deadline,
    _as_handle as _as_handle,
    _negative_due_100ns as _negative_due_100ns,
    _qpc_interval as _qpc_interval,
    _validate_configured_fps as _validate_configured_fps,
    _validate_diagnostic_rate as _validate_diagnostic_rate,
    _validate_idle_interval as _validate_idle_interval,
    _worker_arm_timer as _worker_arm_timer,
    _worker_close_handles as _worker_close_handles,
    _worker_context_for_test as _worker_context_for_test,
    _worker_main as _worker_main,
    _worker_post_error as _worker_post_error,
    _worker_post_wake as _worker_post_wake,
    _worker_signal_from_public as _worker_signal_from_public,
)


def _create_native_runtime(
    *,
    owner_thread_id: int,
    configured_fps: int,
    idle_interval_ms: int,
    diagnostic_rate_hz: float | None = None,
) -> _NativeRuntime:
    if os.name != "nt":
        raise _PreviewClockError("native preview clock requires Windows")
    return _WindowsNativeRuntime(
        owner_thread_id=owner_thread_id,
        configured_fps=configured_fps,
        idle_interval_ms=idle_interval_ms,
        diagnostic_rate_hz=diagnostic_rate_hz,
    )


class PreviewClock:
    """Main-thread facade for native scheduling and exclusive fallback."""

    def __init__(
        self,
        root: Any,
        dispatch_tick: Callable[[], DispatchResult],
        configured_fps: int,
        idle_interval_ms: int,
    ) -> None:
        self._root = root
        self._dispatch_tick = dispatch_tick
        #: 1ms の見張り（native）／周期の刻み（after）で呼ぶ追加処理。
        #: 映像の「新しい1枚を描く」を、自前の after(1) 連鎖を増やさずに
        #: 済ませるための口。例外は握りつぶさず dispatch 側の失敗として扱う。
        self._poll_hook: Callable[[], object] | None = None
        self._configured_fps = _validate_configured_fps(configured_fps)
        self._idle_interval_ms = _validate_idle_interval(idle_interval_ms)
        self._owner_thread_id = threading.get_native_id()
        self._state = _ClockState.STOPPED
        self._epoch = 0
        self._generation = 0
        self._runtime: _NativeRuntime | None = None
        self._cleanup_evidence: _CleanupEvidence | None = None
        self._archived_evidence: tuple[Mapping[str, object], ...] = ()  # noqa: E501  # noqa: OBJECT_OK
        self._native_stop_signal_succeeded: bool | None = None
        self._last_schedule: Literal["active", "idle"] = "active"
        self._fallback_deadline_s: float | None = None
        self._fallback_after_id: str | None = None
        self._health_after_id: str | None = None
        self._health_arm_in_progress = False
        self._native_wake_after_id: str | None = None
        self._teardown_after_id: str | None = None
        self._teardown_target: Literal["stopped", "after"] | None = None
        self._native_stop_requested = False
        self._last_post_failure_count = 0
        self._last_valid_wake_monotonic: float | None = None
        self._last_health_callback_result = 0
        self._dispatch_in_flight = False
        self._reentrant_wake_count = 0
        self._blit_skipped_no_new_frame = 0
        self._blit_skipped_at_window_start = 0
        self._accounting_window_started = False

    def _assert_owner(self) -> None:
        if threading.get_native_id() != self._owner_thread_id:
            raise _PreviewClockError("preview clock operation crossed the owner thread")

    def start(self) -> None:
        self._assert_owner()
        match self._state:
            case _ClockState.STOPPED:
                pass
            case _ClockState.TEARDOWN_PENDING:
                raise _PreviewClockError("preview clock teardown is pending")
            case (
                _ClockState.STARTING_NATIVE
                | _ClockState.HIGH_RESOLUTION
                | _ClockState.AFTER
                | _ClockState.STOPPING
                | _ClockState.FAILED
            ):
                return
            case unreachable:
                assert_never(unreachable)
        self._state = _ClockState.STARTING_NATIVE
        self._epoch += 1
        self._generation += 1
        self._native_stop_signal_succeeded = None
        initial_result = self._invoke_dispatch()
        self._last_schedule = initial_result.schedule
        initial_control = _ControlState(
            _ControlKind.START,
            self._epoch,
            self._generation,
            self._configured_fps,
            self._last_schedule == "active",
        )
        runtime: _NativeRuntime | None = None
        # Tcl async wake は意図的に使わない。ctypes の Tcl_AsyncMark トランポリンは
        # ワーカースレッドから Tcl インタプリタへ再入し、プロセスを fatal 終了させた
        # 実測があるため。起床は PostMessageW + after(ms) poll 経路に一本化する。
        try:
            runtime = _create_native_runtime(
                owner_thread_id=self._owner_thread_id,
                configured_fps=self._configured_fps,
                idle_interval_ms=self._idle_interval_ms,
            )
            self._runtime = runtime
            set_initial_control = getattr(runtime, "set_initial_control", None)
            if callable(set_initial_control):
                set_initial_control(initial_control)
            runtime.bind_wake_callback(self._on_native_wake)
            runtime.start()
            self._native_stop_requested = False
            self._last_post_failure_count = 0
            self._last_valid_wake_monotonic = time.monotonic()
            self._state = _ClockState.HIGH_RESOLUTION
            self._send_control(_ControlKind.START, self._last_schedule == "active")
            self._arm_health_callback()
            self._schedule_native_wake_poll(_NATIVE_WAKE_IDLE_POLL_MS)
        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
            self._generation += 1
            cleaned = True
            if runtime is not None:
                cleaned = self._dispose_failed_native_runtime(runtime)
            if not cleaned:
                self._state = _ClockState.TEARDOWN_PENDING
                self._teardown_target = "after"
                self._schedule_teardown_retry()
                return
            self._enter_after_fallback()
            if not isinstance(exc, _PreviewClockError):
                logger.warning(
                    "preview native clock unavailable: {}", type(exc).__name__
                )

    def _dispose_failed_native_runtime(self, runtime: _NativeRuntime) -> bool:
        self._request_native_stop(runtime)
        try:
            joined = runtime.join(1.0)
        except (OSError, RuntimeError, ValueError, AttributeError):
            joined = False
            self._last_health_callback_result = -1
        if not joined:
            return False
        if self._native_worker_live() and not bool(self._native_stop_signal_succeeded):
            return False
        try:
            runtime.destroy()
            self._archive_runtime_evidence(runtime)
            self._archive_cleanup_evidence(runtime)
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            return False
        if self._runtime is runtime:
            self._runtime = None
        self._native_stop_signal_succeeded = None
        return True

    def stop(self) -> StopResult:
        self._assert_owner()
        match self._state:
            case _ClockState.STOPPED:
                self._state = _ClockState.STOPPED
                return StopResult.STOPPED
            case _ClockState.FAILED:
                if self._native_resources_live():
                    self._state = _ClockState.TEARDOWN_PENDING
                    self._teardown_target = "stopped"
                    return self._finish_native_teardown(
                        StopResult.PENDING, retry_stop_signal=True
                    )
                self._state = _ClockState.STOPPED
                return StopResult.STOPPED
            case _ClockState.AFTER:
                self._cancel_fallback()
                self._generation += 1
                self._state = _ClockState.STOPPED
                return StopResult.STOPPED
            case _ClockState.HIGH_RESOLUTION | _ClockState.STARTING_NATIVE:
                self._state = _ClockState.STOPPING
                self._close_dispatch_gate()
                self._invalidate_generation()
                self._cancel_health_callback()
                self._cancel_native_wake_poll()
                self._native_stop_requested = False
                self._native_stop_signal_succeeded = None
                self._teardown_target = "stopped"
                if self._runtime is not None:
                    self._request_native_stop(self._runtime)
                    self._native_stop_requested = True
                return self._finish_native_teardown(StopResult.STOPPED)
            case _ClockState.STOPPING | _ClockState.TEARDOWN_PENDING:
                if self._teardown_target == "after":
                    self._teardown_target = "stopped"
                    if self._teardown_after_id is not None:
                        return StopResult.PENDING
                    return self._finish_native_teardown(
                        StopResult.PENDING, retry_stop_signal=True
                    )
                return self._continue_native_teardown()
            case unreachable:
                assert_never(unreachable)

    def set_poll_hook(self, hook: Callable[[], object] | None) -> None:
        """1ms 見張り（native）／周期刻み（after）に毎回呼ぶ処理を差し替える。

        自前の ``after(1)`` 連鎖を増やすと、この Tk では 1ms タイマーが
        3 本になった時点で数百 ms の停止が数秒おきに出る（実測）。映像の
        フレーム監視はこの口に相乗りさせる。Tk スレッドからだけ設定する。
        """
        self._assert_owner()
        self._poll_hook = hook

    def _run_poll_hook(self) -> None:
        hook = self._poll_hook
        if hook is None:
            return
        try:
            hook()
        except Exception:  # noqa: E501  # noqa: BROAD_EXCEPT_OK - Tk callback boundary.
            logger.exception("preview poll hook failed")

    def set_fps(self, fps: int) -> None:
        self._assert_owner()
        value = _validate_configured_fps(fps)
        if value == self._configured_fps:
            return
        self._configured_fps = value
        self._generation += 1
        match self._state:
            case _ClockState.HIGH_RESOLUTION:
                self._invalidate_generation(clear_only=True)
                self._send_control(
                    _ControlKind.SET_FPS,
                    self._last_schedule == "active",
                )
            case _ClockState.AFTER:
                self._fallback_deadline_s = None
                self._reschedule_fallback()
            case (
                _ClockState.STOPPED
                | _ClockState.STARTING_NATIVE
                | _ClockState.STOPPING
                | _ClockState.TEARDOWN_PENDING
                | _ClockState.FAILED
            ):
                pass
            case unreachable:
                assert_never(unreachable)

    def realign(self) -> None:
        """周期は変えずに、今ここで 1 回描いてから次の期限を据え直す。

        取込と表示の位相が重なると取りこぼしが増えるので、呼び出し側
        （core/preview_phase.py の判断）が位相の良い瞬間に呼ぶ。worker は
        control を受けた時刻 + 1 周期へ期限を取り直す（SET_FPS と同じ経路）。
        据え直し前に発行済みの tick は世代を進めて捨てる。
        """
        self._assert_owner()
        if self._dispatch_in_flight:
            return
        match self._state:
            case _ClockState.HIGH_RESOLUTION:
                self._generation += 1
                self._invalidate_generation(clear_only=True)
                self._send_control(
                    _ControlKind.REALIGN,
                    self._last_schedule == "active",
                )
                self._invoke_dispatch()
            case _ClockState.AFTER:
                self._invoke_dispatch()
                self._fallback_deadline_s = None
                self._reschedule_fallback()
            case (
                _ClockState.STOPPED
                | _ClockState.STARTING_NATIVE
                | _ClockState.STOPPING
                | _ClockState.TEARDOWN_PENDING
                | _ClockState.FAILED
            ):
                pass
            case unreachable:
                assert_never(unreachable)

    def mode(self) -> Literal["stopped", "high_resolution", "after"]:
        match self._state:
            case _ClockState.HIGH_RESOLUTION | _ClockState.STARTING_NATIVE:
                return "high_resolution"
            case _ClockState.AFTER:
                return "after"
            case (
                _ClockState.STOPPED
                | _ClockState.STOPPING
                | _ClockState.TEARDOWN_PENDING
                | _ClockState.FAILED
            ):
                return "stopped"
            case unreachable:
                assert_never(unreachable)

    def health_snapshot(self) -> Mapping[str, int | str | bool]:
        runtime_snapshot: Mapping[str, int | str | bool] = {}
        if self._runtime is not None:
            runtime_snapshot = self._runtime.snapshot()
        state_value = self._state.value
        mode_value = self.mode()
        cleanup = self._cleanup_evidence
        defaults: dict[str, int | str | bool] = {
            "state": state_value,
            "mode": mode_value,
            "epoch": self._epoch,
            "generation": self._generation,
            "configured_fps": self._configured_fps,
            "qpc_frequency_hz": 0,
            "worker_thread_native_id": 0,
            "last_sequence": 0,
            "last_delivered_sequence": 0,
            "last_wake_qpc": 0,
            "worker_tick_published_count": 0,
            "period_skipped_count": 0,
            "blit_skipped_no_new_frame": 0,
            "pending_tick_superseded_count": 0,
            "main_dispatch_count": 0,
            "stale_tick_dropped_count": 0,
            "post_failure_count": 0,
            "last_post_error": 0,
            "running": False,
            "pending_tick_present": False,
            "dispatch_in_flight": self._dispatch_in_flight,
            "reentrant_wake_count": self._reentrant_wake_count,
            "last_health_callback_result": self._last_health_callback_result,
            "window_class_destroyed": False,
            "window_class_unregistered": False,
            "cleanup_window_class_destroyed": (
                cleanup.window_class_destroyed if cleanup is not None else False
            ),
            "cleanup_window_class_unregistered": (
                cleanup.window_class_unregistered if cleanup is not None else False
            ),
            "cleanup_worker_running": (
                cleanup.worker_running if cleanup is not None else False
            ),
            "cleanup_qpc_frequency_hz": (
                cleanup.qpc_frequency_hz if cleanup is not None else 0
            ),
            "cleanup_worker_thread_native_id": (
                cleanup.worker_thread_native_id if cleanup is not None else 0
            ),
            "cleanup_last_sequence": (
                cleanup.last_sequence if cleanup is not None else 0
            ),
            "cleanup_worker_tick_published_count": (
                cleanup.worker_tick_published_count if cleanup is not None else 0
            ),
            "cleanup_main_dispatch_count": (
                cleanup.main_dispatch_count if cleanup is not None else 0
            ),
            "pending_health_after": self._health_after_id or "",
            "pending_fallback_after": self._fallback_after_id or "",
            "pending_teardown_after": self._teardown_after_id or "",
        }
        defaults.update(runtime_snapshot)
        defaults["state"] = state_value
        defaults["mode"] = mode_value
        defaults["epoch"] = self._epoch
        defaults["generation"] = self._generation
        defaults["configured_fps"] = self._configured_fps
        defaults["dispatch_in_flight"] = self._dispatch_in_flight
        defaults["blit_skipped_no_new_frame"] = self._blit_skipped_no_new_frame
        defaults["reentrant_wake_count"] = self._reentrant_wake_count
        defaults["last_health_callback_result"] = self._last_health_callback_result
        if self._runtime is None and cleanup is not None:
            defaults["window_class_destroyed"] = cleanup.window_class_destroyed
            defaults["window_class_unregistered"] = cleanup.window_class_unregistered
        return dict(defaults)

    def _send_control(self, kind: _ControlKind, active: bool) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        runtime.request_control(
            _ControlState(
                kind=kind,
                epoch=self._epoch,
                generation=self._generation,
                configured_fps=self._configured_fps,
                active=active,
            )
        )

    def _invoke_dispatch(self) -> DispatchResult:
        try:
            result = self._dispatch_tick()
        except Exception:  # noqa: E501  # noqa: BROAD_EXCEPT_OK - Tk callback boundary.
            logger.exception("preview dispatch failed")
            self._last_health_callback_result = -1
            return DispatchResult(schedule=self._last_schedule)
        if result.schedule != self._last_schedule:
            self._last_schedule = result.schedule
            match self._state:
                case _ClockState.HIGH_RESOLUTION:
                    self._generation += 1
                    self._send_control(
                        _ControlKind.ACTIVE
                        if result.schedule == "active"
                        else _ControlKind.IDLE,
                        result.schedule == "active",
                    )
                case _ClockState.AFTER:
                    self._generation += 1
                    self._fallback_deadline_s = None
                case (
                    _ClockState.STOPPED
                    | _ClockState.STARTING_NATIVE
                    | _ClockState.STOPPING
                    | _ClockState.TEARDOWN_PENDING
                    | _ClockState.FAILED
                ):
                    pass
                case unreachable:
                    assert_never(unreachable)
        return result

    def _drain_native_wakes(self) -> bool:
        self._assert_owner()
        runtime = self._runtime
        if self._state is not _ClockState.HIGH_RESOLUTION or runtime is None:
            return False
        consume_wake_signal = getattr(runtime, "consume_wake_signal", None)
        if not callable(consume_wake_signal):
            self._on_native_wake()
            return True
        drained = False
        deadline = time.perf_counter() + _NATIVE_WAKE_DRAIN_BUDGET_S
        while self._state is _ClockState.HIGH_RESOLUTION and self._runtime is runtime:
            if not consume_wake_signal():
                break
            drained = True
            if time.perf_counter() >= deadline:
                break
        return drained

    def _on_native_wake(self) -> None:
        self._assert_owner()
        if self._state is not _ClockState.HIGH_RESOLUTION or self._runtime is None:
            return
        wake: _RuntimeWake | None
        consume_wake = getattr(self._runtime, "consume_wake", None)
        if callable(consume_wake):
            wake = consume_wake()
        else:
            wake = None
        if wake is None:
            snapshot = self._runtime.snapshot()
            wake = _RuntimeWake(
                "tick",
                self._epoch,
                self._generation,
                self._configured_fps,
                qpc_frequency_hz=int(snapshot.get("qpc_frequency_hz", 0)),
            )
        if wake.epoch != self._epoch or wake.generation != self._generation:
            self._last_health_callback_result = -1
            return
        match wake.reason:
            case "clock_ready":
                self._last_valid_wake_monotonic = time.monotonic()
                self._arm_health_callback()
            case "control":
                self._last_valid_wake_monotonic = time.monotonic()
                self._arm_health_callback()
            case "tick":
                self._last_valid_wake_monotonic = time.monotonic()
                self._dispatch_one_tick()
                self._arm_health_callback()
            case "error":
                self._last_health_callback_result = 1
                self._begin_native_recovery("after")
            case unreachable:
                assert_never(unreachable)

    def _dispatch_one_tick(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        if self._dispatch_in_flight:
            self._reentrant_wake_count += 1
            record_reentrant = getattr(runtime, "record_reentrant_wake", None)
            if callable(record_reentrant):
                record_reentrant()
            return
        tick = runtime.consume_tick()
        if isinstance(tick, _StaleTickDrop):
            return
        if tick is None:
            self._blit_skipped_no_new_frame += 1
            return
        self._dispatch_in_flight = True
        set_dispatch_gate = getattr(runtime, "set_dispatch_gate", None)
        if callable(set_dispatch_gate):
            set_dispatch_gate(True)
        before_reentrant = self._reentrant_wake_count
        try:
            self._invoke_dispatch()
        finally:
            self._dispatch_in_flight = False
            if callable(set_dispatch_gate):
                set_dispatch_gate(False)
        if self._reentrant_wake_count > before_reentrant:
            self._dispatch_one_tick()

    def _schedule_native_wake_poll(self, delay_ms: int) -> None:
        runtime = self._runtime
        if self._state is not _ClockState.HIGH_RESOLUTION or runtime is None:
            return
        consume_wake_signal = getattr(runtime, "consume_wake_signal", None)
        if not callable(consume_wake_signal):
            return
        if self._native_wake_after_id is not None:
            return
        try:
            # after_idle は使わない（理由は _NATIVE_WAKE_MIN_POLL_MS の注記）。
            delay = max(_NATIVE_WAKE_MIN_POLL_MS, int(delay_ms))
            self._native_wake_after_id = str(
                self._root.after(delay, self._poll_native_wake)
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            self._begin_native_recovery("after")

    def _poll_native_wake(self) -> None:
        self._native_wake_after_id = None
        runtime = self._runtime
        if self._state is not _ClockState.HIGH_RESOLUTION or runtime is None:
            return
        self._run_poll_hook()
        consume_wake_signal = getattr(runtime, "consume_wake_signal", None)
        if not callable(consume_wake_signal):
            return
        try:
            drained = self._drain_native_wakes()
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            self._begin_native_recovery("after")
            return
        self._schedule_native_wake_poll(
            _NATIVE_WAKE_POLL_MS if drained else _NATIVE_WAKE_IDLE_POLL_MS
        )

    def _cancel_native_wake_poll(self) -> None:
        after_id = self._native_wake_after_id
        self._native_wake_after_id = None
        if after_id is None:
            return
        try:
            self._root.after_cancel(after_id)
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1

    def _arm_health_callback(self) -> None:
        if self._state is not _ClockState.HIGH_RESOLUTION:
            return
        if self._health_after_id is not None or self._health_arm_in_progress:
            return
        self._health_arm_in_progress = True
        try:
            timeout_ms = max(250, math.ceil(4000.0 / self._configured_fps))
            self._health_after_id = str(
                self._root.after(timeout_ms, self._health_callback)
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._begin_native_recovery("after")
        finally:
            self._health_arm_in_progress = False

    def _cancel_health_callback(self) -> None:
        after_id = self._health_after_id
        self._health_after_id = None
        if after_id is None:
            return
        try:
            self._root.after_cancel(after_id)
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1

    def _health_callback(self) -> None:
        self._assert_owner()
        self._health_after_id = None
        if self._state is not _ClockState.HIGH_RESOLUTION or self._runtime is None:
            return
        snapshot = self._runtime.snapshot()
        post_failure_increased = (
            int(snapshot.get("post_failure_count", 0)) > self._last_post_failure_count
        )
        worker_running = bool(snapshot.get("running", False))
        sequence_gap = (
            int(snapshot.get("last_sequence", 0))
            - int(snapshot.get("last_delivered_sequence", 0))
            > 1
        )
        last_wake_stale = self._last_valid_wake_monotonic is None or (
            time.monotonic() - self._last_valid_wake_monotonic
        ) * 1000.0 > max(250, math.ceil(4000.0 / self._configured_fps))
        self._last_post_failure_count = int(snapshot.get("post_failure_count", 0))
        health_failed = (
            post_failure_increased
            or not worker_running
            or sequence_gap
            or last_wake_stale
        )
        health_result = 1 if health_failed else 0
        self._last_health_callback_result = health_result
        record_health_callback = getattr(self._runtime, "record_health_callback", None)
        if callable(record_health_callback):
            record_health_callback(snapshot, health_result)
        if health_failed:
            self._begin_native_recovery("after")
        else:
            self._arm_health_callback()

    def _begin_native_recovery(self, target: Literal["stopped", "after"]) -> None:
        if self._state is not _ClockState.HIGH_RESOLUTION:
            return
        self._state = _ClockState.STOPPING
        self._teardown_target = target
        self._close_dispatch_gate()
        self._invalidate_generation()
        self._cancel_health_callback()
        self._cancel_native_wake_poll()
        if not self._native_stop_requested:
            self._native_stop_requested = True
            runtime = self._runtime
            if runtime is not None:
                self._request_native_stop(runtime)
        self._finish_native_teardown(
            StopResult.PENDING if target == "after" else StopResult.STOPPED
        )

    def _invalidate_generation(self, *, clear_only: bool = False) -> None:
        if not clear_only:
            self._generation += 1
        runtime = self._runtime
        if runtime is None:
            return
        invalidate = getattr(runtime, "invalidate_pending", None)
        if callable(invalidate):
            invalidate()

    def _close_dispatch_gate(self) -> None:
        self._dispatch_in_flight = False
        runtime = self._runtime
        if runtime is not None:
            set_dispatch = getattr(runtime, "set_dispatch_gate", None)
            if callable(set_dispatch):
                set_dispatch(False)

    def _request_native_stop(self, runtime: _NativeRuntime) -> bool:
        try:
            runtime.request_stop()
            snapshot = runtime.snapshot()
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._native_stop_signal_succeeded = False
            self._last_health_callback_result = -1
            return False
        self._native_stop_signal_succeeded = bool(
            snapshot.get("stop_signal_succeeded", True)
        )
        if not self._native_stop_signal_succeeded:
            self._last_health_callback_result = -1
        return self._native_stop_signal_succeeded

    def _native_worker_live(self) -> bool:
        runtime = self._runtime
        if runtime is None:
            return False
        try:
            return bool(runtime.snapshot().get("running", False))
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            return True

    def _archive_runtime_evidence(self, runtime: _NativeRuntime) -> None:
        rows = runtime.drain_evidence()
        self._archived_evidence = (*self._archived_evidence, *rows)

    def _archive_cleanup_evidence(self, runtime: _NativeRuntime) -> None:
        snapshot = runtime.snapshot()
        self._cleanup_evidence = _CleanupEvidence(
            window_class_destroyed=bool(snapshot.get("window_class_destroyed", False)),
            window_class_unregistered=bool(
                snapshot.get("window_class_unregistered", False)
            ),
            worker_running=bool(snapshot.get("running", False)),
            qpc_frequency_hz=int(snapshot.get("qpc_frequency_hz", 0)),
            worker_thread_native_id=int(snapshot.get("worker_thread_native_id", 0)),
            last_sequence=int(snapshot.get("last_sequence", 0)),
            worker_tick_published_count=int(
                snapshot.get("worker_tick_published_count", 0)
            ),
            main_dispatch_count=int(snapshot.get("main_dispatch_count", 0)),
        )

    def _native_resources_live(self) -> bool:
        runtime = self._runtime
        if runtime is None:
            return False
        checker = getattr(runtime, "has_live_resources", None)
        if callable(checker):
            try:
                return bool(checker())
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._last_health_callback_result = -1
                return True
        try:
            snapshot = runtime.snapshot()
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            return True
        return bool(
            snapshot.get("running", False)
            or not bool(snapshot.get("window_class_destroyed", False))
            or not bool(snapshot.get("window_class_unregistered", False))
        )

    def _finish_native_teardown(
        self,
        final_result: StopResult,
        *,
        retry_stop_signal: bool = False,
    ) -> StopResult:
        runtime = self._runtime
        if runtime is None:
            if self._teardown_target == "after":
                self._enter_after_fallback()
            else:
                self._state = _ClockState.STOPPED
            self._native_stop_signal_succeeded = None
            return final_result
        if retry_stop_signal and self._native_worker_live():
            if not self._request_native_stop(runtime):
                self._state = _ClockState.TEARDOWN_PENDING
                self._schedule_teardown_retry()
                return StopResult.PENDING
        try:
            joined = runtime.join(1.0)
        except (OSError, RuntimeError, ValueError, AttributeError):
            joined = False
            self._last_health_callback_result = -1
        if not joined:
            self._state = _ClockState.TEARDOWN_PENDING
            self._schedule_teardown_retry()
            return StopResult.PENDING
        try:
            runtime.destroy()
            self._archive_runtime_evidence(runtime)
            self._archive_cleanup_evidence(runtime)
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._state = _ClockState.TEARDOWN_PENDING
            self._schedule_teardown_retry()
            return StopResult.PENDING
        self._runtime = None
        self._native_stop_signal_succeeded = None
        if self._teardown_target == "after":
            self._enter_after_fallback()
            return final_result
        self._state = _ClockState.STOPPED
        self._teardown_target = None
        return final_result

    def _continue_native_teardown(self) -> StopResult:
        return self._finish_native_teardown(StopResult.STOPPED, retry_stop_signal=True)

    def _schedule_teardown_retry(self) -> None:
        if self._teardown_after_id is not None:
            return
        try:
            self._teardown_after_id = str(self._root.after(50, self._retry_teardown))
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1
            self._state = (
                _ClockState.TEARDOWN_PENDING
                if self._native_resources_live()
                else _ClockState.FAILED
            )

    def _retry_teardown(self) -> None:
        self._assert_owner()
        self._teardown_after_id = None
        self._continue_native_teardown()

    def _enter_after_fallback(self) -> None:
        self._cancel_health_callback()
        self._cancel_native_wake_poll()
        self._teardown_after_id = None
        self._runtime = None
        self._native_stop_signal_succeeded = None
        self._state = _ClockState.AFTER
        self._fallback_deadline_s = None
        self._reschedule_fallback()

    def _reschedule_fallback(self) -> None:
        if self._state is not _ClockState.AFTER:
            return
        if self._fallback_after_id is not None:
            try:
                self._root.after_cancel(self._fallback_after_id)
            except (OSError, RuntimeError, ValueError, AttributeError):
                self._last_health_callback_result = -1
            self._fallback_after_id = None
        now_s = time.perf_counter()
        delay_ms = self._after_delay_ms(now_s)
        try:
            self._fallback_after_id = str(
                self._root.after(delay_ms, self._fallback_tick)
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._state = _ClockState.FAILED

    def _fallback_tick(self) -> None:
        self._assert_owner()
        self._fallback_after_id = None
        if self._state is not _ClockState.AFTER:
            return
        self._invoke_dispatch()
        self._run_poll_hook()
        self._reschedule_fallback()

    def _cancel_fallback(self) -> None:
        after_id = self._fallback_after_id
        self._fallback_after_id = None
        self._fallback_deadline_s = None
        if after_id is None:
            return
        try:
            self._root.after_cancel(after_id)
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._last_health_callback_result = -1

    def _after_delay_ms(self, now_s: float) -> int:
        if self._fallback_deadline_s is None:
            interval_s = (
                1.0 / self._configured_fps
                if self._last_schedule == "active"
                else self._idle_interval_ms / 1000.0
            )
            self._fallback_deadline_s = now_s + interval_s
        if self._fallback_deadline_s <= now_s:
            interval_s = (
                1.0 / self._configured_fps
                if self._last_schedule == "active"
                else self._idle_interval_ms / 1000.0
            )
            skipped = math.floor((now_s - self._fallback_deadline_s) / interval_s) + 1
            self._fallback_deadline_s += skipped * interval_s
        return max(
            1,
            math.ceil((self._fallback_deadline_s - now_s) * 1000.0),
        )

    def _begin_evidence_window(self) -> None:
        self._assert_owner()
        self._accounting_window_started = True
        self._blit_skipped_at_window_start = self._blit_skipped_no_new_frame
        if self._runtime is not None:
            self._runtime.begin_accounting_window()

    def _end_evidence_window(self) -> Mapping[str, int | str | bool]:
        self._assert_owner()
        if self._runtime is None:
            return {
                "worker_tick_published_count": 0,
                "pending_tick_superseded_count": 0,
                "main_dispatch_count": 0,
                "stale_tick_dropped_count": 0,
                "pending_tick_present_at_window_end": 0,
                "latest_only_accounting_ok": False,
                "period_skipped_count": 0,
                "blit_skipped_no_new_frame": 0,
            }
        result = dict(self._runtime.end_accounting_window())
        result.setdefault("latest_only_accounting_ok", False)
        result.setdefault("period_skipped_count", 0)
        result["blit_skipped_no_new_frame"] = max(
            0, self._blit_skipped_no_new_frame - self._blit_skipped_at_window_start
        )
        self._accounting_window_started = False
        self._blit_skipped_at_window_start = 0
        return result

    def _drain_evidence(self) -> tuple[Mapping[str, object], ...]:  # noqa: E501  # noqa: OBJECT_OK
        self._assert_owner()
        active_rows = (
            self._runtime.drain_evidence() if self._runtime is not None else ()
        )
        rows = (*self._archived_evidence, *active_rows)
        self._archived_evidence = ()
        return rows
