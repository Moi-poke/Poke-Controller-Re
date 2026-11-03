"""操作盤の種類（自動 / プロコン / Joy-Con L/R）を選ぶ画面側の契約。

Tk を使わない。選択欄・設定・パッド名の写しと、実際に操作盤へ渡す
種類の関係だけを調べる。
"""

from __future__ import annotations

from typing import Any

from pokecon.domain.pad_layout import (
    AUTO,
    JOYCON_L,
    JOYCON_R,
    LAYOUTS,
    LAYOUT_LABELS,
    PROCON,
)
from pokecon.ui.BconSetup import BconSetup
from pokecon.ui.bcon_panel import BconPanelMixin
from pokecon.ui.serial_panel import SerialPanelMixin


class _Var:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class _Settings:
    def __init__(self, layout: str = AUTO) -> None:
        self.gamepad_index = _Var(0)
        self.controller_layout = _Var(layout)


class _Dock:
    def __init__(self) -> None:
        self.layouts: list[str] = []

    def set_layout(self, layout: str) -> None:
        self.layouts.append(layout)


class _Panel(SerialPanelMixin):
    def __init__(self, *, layout: str = AUTO, pad_name: str = "") -> None:
        self.settings = _Settings(layout)
        self.controller_layout_choice = _Var(
            LAYOUT_LABELS.get(layout, LAYOUT_LABELS[AUTO])
        )
        self._controller_layout_labels = {LAYOUT_LABELS[key]: key for key in LAYOUTS}
        self._gamepad_names = {0: pad_name}
        self.controller_dock = _Dock()
        self.saved = 0

    def _on_setting_changed(self) -> None:
        self.saved += 1


def test_auto_uses_the_connected_pad_name() -> None:
    panel = _Panel(pad_name="Joy-Con (L)")
    panel._apply_controller_layout()
    assert panel.controller_dock.layouts == [JOYCON_L]

    panel._gamepad_names = {0: "Joy-Con (R)"}
    panel._apply_controller_layout()
    assert panel.controller_dock.layouts == [JOYCON_L, JOYCON_R]

    panel._gamepad_names = {0: "Pro Controller"}
    panel._apply_controller_layout()
    assert panel.controller_dock.layouts[-1] == PROCON


def test_a_manual_choice_wins_over_the_pad_name() -> None:
    panel = _Panel(layout=JOYCON_R, pad_name="Joy-Con (L)")
    panel._apply_controller_layout()
    assert panel.controller_dock.layouts == [JOYCON_R]

    panel.controller_layout_choice.set(LAYOUT_LABELS[PROCON])
    panel._apply_controller_layout()
    assert panel.controller_dock.layouts == [JOYCON_R, PROCON]


def test_selecting_a_layout_saves_it_and_applies_immediately() -> None:
    panel = _Panel(pad_name="Joy-Con (L)")
    panel.controller_layout_choice.set(LAYOUT_LABELS[JOYCON_R])

    panel._on_controller_layout_selected()

    assert panel.settings.controller_layout.get() == JOYCON_R
    assert panel.controller_dock.layouts == [JOYCON_R]
    assert panel.saved == 1


def test_loading_a_saved_layout_restores_the_choice_and_the_pad() -> None:
    panel = _Panel(layout=JOYCON_L, pad_name="Pro Controller")
    panel._load_controller_layout_setting()

    assert panel.controller_layout_choice.get() == LAYOUT_LABELS[JOYCON_L]
    assert panel.controller_dock.layouts == [JOYCON_L]


def test_an_unknown_saved_layout_falls_back_to_auto() -> None:
    panel = _Panel(layout="joycon_x", pad_name="Joy-Con (R)")
    panel._load_controller_layout_setting()

    assert panel.controller_layout_choice.get() == LAYOUT_LABELS[AUTO]
    assert panel.controller_dock.layouts == [JOYCON_R]


def test_bcon_role_change_selects_the_matching_layout() -> None:
    class _BconPanel(BconPanelMixin):
        def __init__(self) -> None:
            self.chosen: list[str] = []

        def _set_controller_layout_choice(self, mode: str) -> None:
            self.chosen.append(mode)

    panel = _BconPanel()
    panel._on_bcon_emulate_role(0)
    panel._on_bcon_emulate_role(1)
    panel._on_bcon_emulate_role(2)
    panel._on_bcon_emulate_role(9)

    assert panel.chosen == [PROCON, JOYCON_L, JOYCON_R]


def test_bcon_notifies_the_role_only_after_a_successful_switch() -> None:
    calls: list[int] = []
    setup = object.__new__(BconSetup)
    setup._closed = False
    setup._on_emulate_role = calls.append

    setup._notify_emulate_role(1)

    assert calls == [1]

    # 画面を閉じたあとは呼ばない（破棄済みの Tk を触らない）。
    setup._closed = True
    setup._notify_emulate_role(2)
    assert calls == [1]


def test_bcon_success_can_be_dispatched_without_the_bcon_tab_poll() -> None:
    dispatched: list[Any] = []
    setup = object.__new__(BconSetup)
    setup._success_dispatch = dispatched.append

    def callback() -> None:
        pass

    setup._dispatch_success(callback)

    assert dispatched == [callback]


def test_bcon_success_goes_to_the_app_inbox_when_the_tab_is_hidden() -> None:
    class _Inbox:
        def __init__(self) -> None:
            self.callbacks: list[Any] = []

        def post(self, callback: Any) -> None:
            self.callbacks.append(callback)

    class _Panel(BconPanelMixin):
        def __init__(self) -> None:
            self.inbox = _Inbox()

    panel = _Panel()

    def callback() -> None:
        pass

    panel._queue_bcon_success(callback)

    assert panel.inbox.callbacks == [callback]
