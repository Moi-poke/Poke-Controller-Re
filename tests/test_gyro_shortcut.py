"""常時見えるジャイロ開始の近道（serial_panel）の契約。実 Tk 不要。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ui.serial_panel import SerialPanelMixin


class _Var:
    def __init__(self, value: Any = "") -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.text = ""

    def config(self, **values: Any) -> None:
        self.text = str(values.get("text", self.text))


def _host(panel: Any) -> Any:
    obj: Any = SerialPanelMixin.__new__(SerialPanelMixin)
    obj._imu_panel = panel
    obj.gyro_state = _Var("")
    obj.gyro_button = _Button()
    return obj


def test_shortcut_toggles_the_panel() -> None:
    """Given: ジャイロ欄 / When: 近道のボタン / Then: パネルの開始/停止を呼ぶこと。"""
    calls: list[int] = []
    panel = SimpleNamespace(
        _on_toggle=lambda: calls.append(1), _stats_var=_Var("停止中")
    )
    host = _host(panel)
    host._toggle_gyro()
    assert calls == [1]


def test_shortcut_mirrors_panel_state() -> None:
    """Given: 送出中の統計 / When: 状態を写す / Then: 停止表示へ変わること。"""
    panel = SimpleNamespace(_on_toggle=lambda: None, _stats_var=_Var("送出中 sent=1"))
    host = _host(panel)
    host._sync_gyro_shortcut()
    assert host.gyro_state.get() == "送出中 sent=1"
    assert host.gyro_button.text == "停止"

    panel._stats_var.set("停止中")
    host._sync_gyro_shortcut()
    assert host.gyro_button.text == "開始"


def test_shortcut_without_panel_is_silent() -> None:
    """Given: パネル未生成 / When: 近道を押す / Then: 例外を出さないこと。"""
    host = _host(None)
    host._toggle_gyro()
    host._sync_gyro_shortcut()
