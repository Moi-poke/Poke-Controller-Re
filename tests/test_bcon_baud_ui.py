"""bcon baud切替UIの試験（実機不要・headless）。

- 表示・可否・候補の組み立ては core.transport.bcon_baud（純粋関数）が持つ。
- メイン画面の baud 欄は bcon 系のときだけ readonly で選べる。
- レガシーは disabled（9600 前提のマイコンを誤らないため）。
- 永続化は settings.ini の bcon_baud_index（0-4）。
"""

from __future__ import annotations

import pytest


def test_baud_index_to_bps_uses_protocol_table():
    """各indexはBAUD_TABLEの値に一致する（期待値は表から読む）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    # 表の5件すべてが引けること。ハードコードせず表に当てる。
    for index in sorted(BAUD_TABLE.keys()):
        assert baud_ui.baud_bps_for_index(index) == BAUD_TABLE[index]


def test_baud_default_index_is_table_default():
    """既定indexはDEFAULT_BAUD_INDEX（=3）で1Mbpsを指す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE, DEFAULT_BAUD_INDEX

    assert DEFAULT_BAUD_INDEX == 3
    assert BAUD_TABLE[DEFAULT_BAUD_INDEX] == 1000000
    assert (
        baud_ui.baud_bps_for_index(baud_ui.BAUD_DEFAULT_INDEX)
        == BAUD_TABLE[DEFAULT_BAUD_INDEX]
    )


def test_baud_invalid_index_never_sent():
    """範囲外indexは送出せず拒否する（BAUD_SETを投げない）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    for bad in (-1, 5, 99):
        assert bad not in BAUD_TABLE
        with pytest.raises(ValueError):
            baud_ui.baud_bps_for_index(bad)
    # 送出ガード：不正indexは送信可否がFalse。
    assert baud_ui.baud_index_sendable(-1) is False
    assert baud_ui.baud_index_sendable(5) is False


def test_baud_adopt_confirm_message():
    """adopt成功時は確定文言（切替完了＋bps入り）を出す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE, DEFAULT_BAUD_INDEX

    bps = BAUD_TABLE[DEFAULT_BAUD_INDEX]
    msg = baud_ui.baud_adopt_message(bps)
    assert str(bps) in msg
    assert "切替" in msg


def test_baud_revert_display_message():
    """adopt失敗時は復帰文言（新旧bps入り・確定と文面が違う）を出す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    new_bps = BAUD_TABLE[1]
    old_bps = BAUD_TABLE[3]
    adopt = baud_ui.baud_adopt_message(new_bps)
    revert = baud_ui.baud_revert_message(new_bps, old_bps)
    assert str(new_bps) in revert
    assert str(old_bps) in revert
    assert revert != adopt
    assert "戻" in revert


def test_combo_state_is_editable_only_for_bcon():
    """レガシーは固定、bcon だけ選べる。"""
    from core.transport import bcon_baud as baud_ui

    assert baud_ui.baud_combo_state(True) == "readonly"
    assert baud_ui.baud_combo_state(False) == "disabled"


def test_combo_values_switch_between_table_and_legacy():
    """bcon は BAUD_TABLE の表示名、レガシーは渡した候補をそのまま使う。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    labels = baud_ui.baud_combo_values(True, [9600, 4800])
    assert labels == [baud_ui.BAUD_DISPLAY_LABELS[i] for i in sorted(BAUD_TABLE)]
    assert baud_ui.baud_combo_values(False, [9600, 4800]) == ["9600", "4800"]


def test_label_and_index_roundtrip_covers_table():
    """コンボの表示名と index が全段で往復する（選んだ段を取り違えない）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    for index in sorted(BAUD_TABLE):
        label = baud_ui.baud_label_for_index(index)
        assert baud_ui.baud_index_for_label(label) == index
