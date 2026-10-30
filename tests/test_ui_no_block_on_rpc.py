#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tk が運搬層 RPC で固まらないことの検証。

switch-bcon-proc の is_open は子プロセス往復（最大で約8秒級）であり、
Tk スレッド（200ms ポンプ・タイトル更新・ボタン）から同期呼びすると
1回の停滞がポンプ間隔の崩れとしてそのまま出る。Tk は IPC を待っては
いけない。8秒停滞する偽送り先で以下を確かめる（実 tk は作らない）。

(a) display_text が約200msで戻り、ポンプ間隔が崩れないこと。
(b) _update_title が約200msで戻ること。
(c) 接続ボタンは通常値で request API に依頼し、受信 callback は inbox に積むこと。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

tkinter = pytest.importorskip("tkinter")

# 子往復の上限の目安（3.0秒 + PROC_RPC_MARGIN_S 5.0秒）。
STALL_S = 8.0
# 停滞と即時復帰を見分ける上限。即時なら数ms、停滞なら8秒のため
# 余裕を持たせても判定は揺らがない。
FAST_S = 2.0


class _StalledSender:
    """isOpened だけが8秒停滞する偽送り先。"""

    def __init__(self) -> None:
        self.flushed = 0

    def isOpened(self) -> bool:
        time.sleep(STALL_S)
        return True

    def flushInputLog(self) -> None:
        self.flushed += 1


class _FakeVar:
    def __init__(self, value: Any) -> None:
        self._v = value

    def get(self) -> Any:
        return self._v


class _FakeArea:
    def __init__(self) -> None:
        self.after_calls = 0

    def after(self, _ms: int, _cb: object) -> str:
        self.after_calls += 1
        return "after-id"


def _make_inbox() -> Any:
    """GUI スレッドへの受け渡し箱（実物）。"""
    from core.ui_inbox import UiInbox

    return UiInbox()


class _FakeRoot:
    def __init__(self) -> None:
        self.titles: list[str] = []
        self.afters: list[tuple[int, Any]] = []

    def title(self, *args: Any) -> None:
        if args:
            self.titles.append(str(args[0]))

    def after(self, ms: int, cb: object) -> str:
        self.afters.append((int(ms), cb))
        return "after-0"


def _make_service(tmp_path: Any) -> Any:
    from services.serial_service import SerialService

    service = SerialService(
        notify_user=lambda _m: None,
        base_dir=str(tmp_path),
        input_log_emit=None,
    )
    service.sender = _StalledSender()  # type: ignore[assignment]
    return service


def _make_log_host(service: Any) -> Any:
    from ui import log_panel

    host = log_panel.LogPanelMixin.__new__(log_panel.LogPanelMixin)
    host._closing = False  # type: ignore[attr-defined]
    host._log_flush_ms = 0.0  # type: ignore[attr-defined]
    host._log_flush_max_ms = 0.0  # type: ignore[attr-defined]
    host._pump_gap_max_ms = 0.0  # type: ignore[attr-defined]
    host._pump_last_at = None  # type: ignore[attr-defined]
    host._stat_ticks = 0  # type: ignore[attr-defined]
    host._display_after_id = None  # type: ignore[attr-defined]
    host.log_autoscroll = _FakeVar(True)  # type: ignore[attr-defined]
    host.show_input_log = _FakeVar(True)  # type: ignore[attr-defined]
    host.logArea = _FakeArea()  # type: ignore[attr-defined]
    host.subLogArea = _FakeArea()  # type: ignore[attr-defined]
    host.inputLogArea = _FakeArea()  # type: ignore[attr-defined]
    host.camera = None  # type: ignore[attr-defined]
    host.preview = None  # type: ignore[attr-defined]
    host.runner = type("R", (), {"state": "idle"})()  # type: ignore[attr-defined]
    host._log_video_stats = lambda: None  # type: ignore[attr-defined]
    host.serial = service  # type: ignore[attr-defined]
    return host


def test_display_text_returns_fast_with_stalled_transport(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """表示ポンプが8秒停滞に引きずられないこと。"""
    import LogPane
    from ui import log_panel

    service = _make_service(tmp_path)
    host = _make_log_host(service)

    # 空だと高速路で serial を触らずに終わるため、わざと1行積んで
    # is_open を通る経路（flush 側）へ回す。描画自体は stub する。
    monkeypatch.setattr(LogPane, "text_queue", LogPane.DropOldestQueue(maxsize=100))
    monkeypatch.setattr(LogPane, "sub_log_queue", LogPane.DropOldestQueue(maxsize=100))
    monkeypatch.setattr(
        LogPane, "input_log_queue", LogPane.DropOldestQueue(maxsize=100)
    )
    LogPane.text_queue.put("probe")
    monkeypatch.setattr(LogPane, "flushQueue", lambda _q, _w, _f: None)

    began = time.perf_counter()
    log_panel.LogPanelMixin.display_text(host)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - began
    assert elapsed < FAST_S, f"display_text が停滞しました: {elapsed:.1f}秒"

    began = time.perf_counter()
    log_panel.LogPanelMixin.display_text(host)  # type: ignore[arg-type]
    elapsed2 = time.perf_counter() - began
    assert elapsed2 < FAST_S, f"display_text が停滞しました: {elapsed2:.1f}秒"
    gap_ms = float(host._pump_gap_max_ms)  # type: ignore[attr-defined]
    assert gap_ms < FAST_S * 1000.0, f"ポンプ間隔が崩れました: {gap_ms:.0f}ms"


def test_update_title_returns_fast_with_stalled_transport(tmp_path: Any) -> None:
    """タイトル更新が8秒停滞に引きずられないこと。"""
    import Window

    service = _make_service(tmp_path)
    host = Window.PokeControllerApp.__new__(Window.PokeControllerApp)
    host.profile = ""  # type: ignore[attr-defined]
    host.com_port_name = _FakeVar("COM9")  # type: ignore[attr-defined]
    host.serial = service  # type: ignore[attr-defined]
    host._running_command = ""  # type: ignore[attr-defined]
    host._paused = False  # type: ignore[attr-defined]
    host.runner = type("R", (), {"stop_waited": 0})()  # type: ignore[attr-defined]
    host.root = _FakeRoot()  # type: ignore[attr-defined]
    host.inbox = _make_inbox()  # type: ignore[attr-defined]

    began = time.perf_counter()
    Window.PokeControllerApp._update_title(host)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - began
    assert elapsed < FAST_S, f"_update_title が停滞しました: {elapsed:.1f}秒"
    assert host.root.titles  # type: ignore[attr-defined]


def test_connect_button_only_submits_plain_values() -> None:
    """接続ハンドラが transport や同期 connect を呼ばず即座に戻る。"""
    from core.connection_state import ConnectionSnapshot
    from ui.serial_panel import SerialPanelMixin

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def connection_snapshot(self) -> ConnectionSnapshot:
            return ConnectionSnapshot()

        def uses_bcon(self, name: str) -> bool:
            return True

        def request_connect(self, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            return True

        def connect(self, **kwargs: Any) -> None:
            raise AssertionError("GUI が同期接続を呼びました")

    service = Service()
    host = SerialPanelMixin.__new__(SerialPanelMixin)
    host.serial = service
    host._connection_change_allowed = lambda: True
    host._refresh_connection_controls = lambda: None
    host._com_port_map = {"device": "COM9"}
    host.com_port_name = _FakeVar("COM9")
    host.com_port = _FakeVar(9)
    host.transport_name = _FakeVar("switch-bcon-proc")
    host.baud_rate = _FakeVar("4800")
    host.is_use_keyboard = _FakeVar(True)
    host.settings = type("S", (), {"setting_path": "settings.profile.ini"})()
    host._currentBaudRate = lambda: 2000000
    began = time.perf_counter()
    host.activateSerial()
    assert time.perf_counter() - began < FAST_S
    assert service.calls == [
        {
            "port_num": 9,
            "port_name": "COM9",
            "baud": 2000000,
            "keyboard_enabled": True,
            "setting_path": "settings.profile.ini",
        }
    ]


def test_worker_snapshot_callback_only_enqueues() -> None:
    from core.connection_state import ConnectionSnapshot
    from ui.serial_panel import SerialPanelMixin

    host = SerialPanelMixin.__new__(SerialPanelMixin)
    host.inbox = _make_inbox()
    host.root = _FakeRoot()
    snapshot = ConnectionSnapshot(generation=3, phase="hello", opened=True)
    snapshot_reads: list[int] = []

    class Service:
        def connection_snapshot(self) -> ConnectionSnapshot:
            snapshot_reads.append(threading.get_ident())
            return snapshot

    host.serial = Service()
    seen: list[tuple[int, Any]] = []
    host._apply_connection_snapshot = lambda snapshot: seen.append(
        (threading.get_ident(), snapshot)
    )
    worker = threading.Thread(target=host._queue_connection_snapshot, args=(snapshot,))
    worker.start()
    worker.join(FAST_S)
    assert not worker.is_alive()
    assert host.root.afters == []
    assert not seen
    assert not snapshot_reads
    host.inbox.drain()
    assert snapshot_reads == [threading.get_ident()]
    assert seen == [(threading.get_ident(), snapshot)]


def test_late_status_notification_renders_failed_service_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同世代の古い受信通知が失敗後に届いても両画面は最新を表示する。"""
    from core.connection_state import ConnectionSnapshot
    from core.transport.bcon_protocol import T_STATUS
    from test_bcon_connection_lifecycle import (
        connect_in_thread,
        make_service,
        wait_snapshot,
    )
    from test_bcon_connection_views import SnapshotService, detail_view, serial_view

    service, _, wires = make_service(monkeypatch)
    host = serial_view(service)
    detail = detail_view(SnapshotService(service.connection_snapshot()))
    detail._service = service
    held = threading.Event()
    release = threading.Event()
    late_delivered = threading.Event()
    final_delivered = threading.Event()
    old: list[ConnectionSnapshot] = []

    def hold(snapshot: ConnectionSnapshot) -> None:
        if snapshot.phase == "hello" and snapshot.status is not None:
            old.append(snapshot)
            held.set()
            release.wait(15)

    def observe(snapshot: ConnectionSnapshot) -> None:
        if snapshot.phase == "hello" and snapshot.status is not None:
            late_delivered.set()
        if snapshot.phase == "failed" and snapshot.operation is None:
            final_delivered.set()

    unsubscribers = [
        service.subscribe_connection(hold),
        service.subscribe_connection(host._queue_connection_snapshot),
        service.subscribe_connection(detail._receive_snapshot),
        service.subscribe_connection(observe),
    ]
    thread = connect_in_thread(service)
    try:
        wait_snapshot(service, lambda snapshot: snapshot.phase == "hello")
        wire = wires[0]
        assert wire.hello_seen.wait(5)
        wire.inject(T_STATUS, bytes([32, 1, 0, 0, 0, 0, 0]))
        assert held.wait(5)
        assert final_delivered.wait(10)
        thread.join(5)
        assert not thread.is_alive()
        current = service.connection_snapshot()
        assert current.phase == "failed" and current.operation is None
        assert current.status is None and current.generation == old[0].generation
        release.set()
        assert late_delivered.wait(5)
        host.inbox.drain()
        detail.inbox.drain()
        assert host._connection_view == current == detail._snapshot
        assert host.bcon_wired.get() == "不明"
        assert set(host.player_lamp_canvas.colors.values()) == {"gray"}
        assert detail._btn_connect["state"] == "normal"
        assert detail._btn_hunt["state"] == "normal"
        assert "接続失敗" in detail._summary["text"]

        # 旧世代の通知が現在の表示を上書きしても、現在の写しを描く。
        service.disconnect()
        wait_snapshot(
            service,
            lambda snapshot: snapshot.phase == "disconnected"
            and snapshot.operation is None,
        )
        current = service.connection_snapshot()
        assert current.generation != old[0].generation
        host._queue_connection_snapshot(old[0])
        detail._receive_snapshot(old[0])
        host.inbox.drain()
        detail.inbox.drain()
        assert host._connection_view == current == detail._snapshot
    finally:
        release.set()
        thread.join(10)
        for unsubscribe in unsubscribers:
            unsubscribe()
        host.inbox.close()
        detail.inbox.close()
        service.shutdown()


def test_connection_logs_discard_stale_delivery_and_preserve_history(monkeypatch):
    from services.serial_service import SerialService
    from ui.serial_panel import SerialPanelMixin

    raw = []
    rendered = []
    detail = []
    service = SerialService(notify_user=raw.append, base_dir=".", input_log_emit=None)
    host = SerialPanelMixin.__new__(SerialPanelMixin)
    host.serial = service
    host._closing = False
    host.inbox = _make_inbox()
    host._apply_connection_snapshot = lambda snapshot: None
    host._subscribe_connection_state()
    service.subscribe_connection_log(lambda generation, message: detail.append(message))
    monkeypatch.setattr("builtins.print", lambda message: rendered.append(message))

    def logging_boundary(message):
        if message == "in-flight":
            service.disconnect()

    monkeypatch.setattr("services.serial_service.logger.info", logging_boundary)
    try:
        service._connection_log(0, "history")
        host.inbox.drain()
        service._connection_log(0, "queued-old")
        service._connection_log(0, "in-flight")
        generation = service.connection_snapshot().generation
        service._connection_log(generation, "current-one")
        service._connection_log(generation, "current-two")
        host.inbox.drain()
        assert rendered == ["history", "current-one", "current-two"]
        assert detail == ["history", "queued-old", "current-one", "current-two"]
        assert raw == []
        host._closing = True
        host._dispose_connection_subscription()
        service._connection_log(generation, "after-disposal")
        host.inbox.drain()
        assert rendered == ["history", "current-one", "current-two"]
    finally:
        host._dispose_connection_subscription()
        service.shutdown()
