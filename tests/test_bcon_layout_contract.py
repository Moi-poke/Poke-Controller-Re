"""Bcon タブの並べ方の契約。実 Tk。

ボタン 17 個が見出しなしで並び、「W0無線」「X破棄」「K鍵削除」のような
符号付きの文言で、取り消せない操作（鍵削除・BOOTSEL）が普段の操作の
隣にあった。見出し付きのまとまりに分け、取り消せない操作は別のまとまりへ
離し、文言は何が起きるかで書く。
"""

from __future__ import annotations

import re
import tkinter as tk
import tkinter.ttk as ttk
from collections.abc import Iterator
from typing import Any

import pytest
from bcon_ui_support import Service
from ui.collapsible import CollapsibleSection, SectionFrame

# 取り消せない操作。押し間違えないよう普段の操作と別のまとまりに置く。
_DESTRUCTIVE = ("_btn_keys", "_btn_clear", "_btn_bootsel")


def _buttons(setup: Any) -> dict[str, ttk.Button]:
    return {
        name: value
        for name, value in vars(setup).items()
        if name.startswith("_btn_") and isinstance(value, ttk.Button)
    }


def _group(widget: tk.Misc) -> Any:
    """部品を包む一番内側の見出し付きまとまり。無ければ None。

    枠線付きの Labelframe と、枠線なしの SectionFrame / CollapsibleSection を
    同じ「見出し付きのまとまり」として扱う（見出しでグループを示す方針）。
    """
    parent: Any = widget.master
    while parent is not None:
        if isinstance(parent, (ttk.Labelframe, SectionFrame, CollapsibleSection)):
            return parent
        parent = parent.master
    return None


def _title(group: Any) -> str:
    title = getattr(group, "title", "")
    if title:
        return str(title)
    try:
        return str(group.cget("text"))
    except Exception:
        return ""


@pytest.fixture(params=[True, False], ids=["tab", "window"])
def bcon(request: Any, tk_root: tk.Tk) -> Iterator[Any]:
    from BconSetup import BconSetup

    window = tk.Toplevel(tk_root)
    if request.param:
        tab = ttk.Frame(window)
        tab.pack(fill="both", expand=True)
        setup = BconSetup(tab, Service(), embedded=True)
    else:
        setup = BconSetup(window, Service())
    try:
        window.update()
        yield setup
    finally:
        setup.close()
        window.destroy()


def test_every_bcon_action_button_sits_inside_a_titled_group(bcon: Any) -> None:
    # Given: タブ（埋め込み）と別窓の両方で組み立てた Bcon 設定。
    buttons = _buttons(bcon)
    assert len(buttons) >= 17

    # Then: どのボタンも見出し付きのまとまりの中にある（何の操作か分かる）。
    for name, button in buttons.items():
        group = _group(button)
        assert group is not None, name
        assert _title(group).strip(), name


def test_irreversible_bcon_actions_are_kept_apart_from_everyday_ones(
    bcon: Any,
) -> None:
    buttons = _buttons(bcon)

    # Then: 取り消せない操作は 1 つのまとまりに集まり、
    destructive = {str(_group(buttons[name])) for name in _DESTRUCTIVE}
    assert len(destructive) == 1

    # Then: そのまとまりには普段の操作が混ざらない。
    for name, button in buttons.items():
        if name not in _DESTRUCTIVE:
            assert str(_group(button)) not in destructive, name


def test_bcon_buttons_say_what_they_do_instead_of_short_codes(bcon: Any) -> None:
    # Then: 「W0」「E1」「X破棄」「K鍵削除」のような符号を先頭に付けない。
    for name, button in _buttons(bcon).items():
        text = str(button.cget("text"))
        assert not re.match(r"^[WEXK]\d?(?![a-z])", text), (name, text)


def test_details_start_closed_and_primary_actions_stay_outside(bcon: Any) -> None:
    # Then: 診断・保守・色・モードは閉じ、主要操作（状態更新・Switchへ再接続）は
    #       折りたたみの外に常時見える。
    for name in ("_grp_pair", "_grp_color", "_grp_diag", "_grp_danger"):
        assert getattr(bcon, name).open is False, name
    for name in ("_btn_status", "_btn_reconnect"):
        button = getattr(bcon, name)
        assert button.winfo_ismapped(), name
    assert "Switchへ再接続" == str(bcon._btn_reconnect.cget("text"))
    assert "状態を更新" == str(bcon._btn_status.cget("text"))


def test_toggling_details_keeps_values_and_log(bcon: Any) -> None:
    bcon._append("状態を更新しました。")
    bcon._seconds.set(22)
    bcon._colors[0].set("112233")

    # When: 詳細を開いて閉じ、タブを隠して戻す。
    bcon._grp_diag.set_open(True)
    bcon._grp_color.set_open(True)
    bcon._grp_pair.set_open(True)
    bcon._grp_diag.set_open(False)
    bcon._grp_color.set_open(False)
    bcon._grp_pair.set_open(False)
    bcon.set_visible(False)
    bcon.set_visible(True)

    # Then: 入力値・下書き・ログは消えない（作り直さない）。
    assert bcon._seconds.get() == 22
    assert bcon._colors[0].get() == "112233"
    assert "状態を更新しました。" in bcon._log.get("1.0", "end-1c")


def test_results_are_shown_next_to_the_controls(bcon: Any) -> None:
    # When: 作業スレッドの結果が1行届く。
    bcon._append("Switchとの再接続を受け付けました。")

    # Then: ログのほか、主要操作の直下の 1 行にも出る（毎回ログを追わせない）。
    assert "再接続" in str(bcon._result.cget("text"))


def test_a_mode_change_warns_about_the_restart_beforehand(bcon: Any) -> None:
    texts = []

    def walk(widget: Any) -> None:
        try:
            texts.append(str(widget.cget("text")))
        except Exception:
            pass
        for child in widget.winfo_children():
            walk(child)

    walk(bcon._grp_pair)
    joined = "\n".join(texts)
    assert "再起動" in joined
    assert "COM ポートが外れ" in joined
    # 再起動後に入口へ戻れる（新しい自動再接続は作らない）。
    assert "シリアルタブで接続し直す" in joined


def test_controller_state_has_its_own_bcon_group(bcon: Any) -> None:
    # Then: LED・ジャイロ・振動は「コントローラ状態」に見出し付きで揃う。
    group = _group(bcon._imu_label)
    assert _title(group) == "コントローラ状態"
    assert _group(bcon._vib_label) is group
    assert _group(bcon._lamp_canvas) is group


def test_a_failed_connection_shows_recovery_shortcuts(bcon: Any) -> None:
    from dataclasses import replace

    bcon._snapshot = replace(
        bcon._service.connection_snapshot(),
        phase="failed",
        opened=True,
        message="応答がありません",
    )
    bcon._render_summary()
    bcon._body.update()

    # Then: 詳細を開かなくても、やり直しと速度探索へ届く。
    assert bcon._recovery_row.winfo_ismapped()
    assert "HELLO" in str(bcon._btn_retry_quick.cget("text"))
    assert "速度を探す" == str(bcon._btn_hunt_quick.cget("text"))

    # When: 接続できた。
    bcon._snapshot = replace(bcon._snapshot, phase="ready", message="接続済み")
    bcon._render_summary()
    bcon._body.update()

    # Then: 復旧の行は消える（日常の操作を増やさない）。
    assert not bcon._recovery_row.winfo_ismapped()


def test_the_reconnect_button_is_disabled_while_another_action_runs(
    bcon: Any,
) -> None:
    from dataclasses import replace

    bcon._service.snapshot = replace(
        bcon._service.snapshot, opened=True, phase="ready", operation="状態確認中"
    )
    bcon._set_busy()
    for name, button in _buttons(bcon).items():
        assert str(button.cget("state")) == tk.DISABLED, name
    bcon._service.snapshot = replace(
        bcon._service.snapshot, operation=None, player_info=b"\x01\x04"
    )
    bcon._set_busy()
    for name, button in _buttons(bcon).items():
        assert str(button.cget("state")) == tk.NORMAL, name


def test_the_bcon_log_has_a_scrollbar(bcon: Any) -> None:
    # Then: ログ欄はスクロールバーで遡れる（1000 行まで溜まる）。
    command = str(bcon._log.cget("yscrollcommand"))
    assert command
    scrollbars = [
        child
        for child in bcon._log.master.winfo_children()
        if isinstance(child, ttk.Scrollbar)
    ]
    assert scrollbars
