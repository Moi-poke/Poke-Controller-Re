"""UI の余白を論理pxで持つ。

Tk の padding は画面のピクセルで解釈されるため、同じ数値でも DPI が
変わると見た目の比率が変わる。設定値は 96dpi 基準の論理pxで持ち、
実際の padding は px() でそのウィンドウの倍率へ直す。
"""

from __future__ import annotations

from typing import Any

#: 設定タブ本文の左右・上下（コントローラタブの control_lf と同じ）。
TAB_PAD_X = 6
TAB_PAD_Y = 4
#: 同じグループ内の行間。
GAP_ITEM = 4
#: グループ間。
GAP_GROUP = 16
#: 見出しと本文の間。
GAP_HEADER = 4
#: ボタン間。
PAD_BUTTON = 4


def dpi_factor(widget: Any) -> float:
    """96dpi を 1 とした倍率。端数で境界が揺れないよう整数DPIから求める。"""
    try:
        return max(1.0, round(float(widget.winfo_fpixels("1i"))) / 96.0)
    except Exception:
        return 1.0


def px(widget: Any, logical: int | float) -> int:
    """論理pxを、そのウィンドウの物理pxへ直す。"""
    return max(1, round(float(logical) * dpi_factor(widget)))
