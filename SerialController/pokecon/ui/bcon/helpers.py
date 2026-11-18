"""BCON 設定 UI の定数と色欄プレビュー補助。"""

from __future__ import annotations

from typing import Any

from pokecon.domain.bcon_status import (
    COLOR_READ_FALLBACK_NOTE,
    request_color,
)
from pokecon.domain.procon_color import parse_color_hex

BCON_WINDOW_TITLE = "BCON 設定"

#: 操作列と情報列を左右に並べる最小の可視幅（px）。Microsoft の
#: ブレークポイント（Medium 641 以上）に近い値。狭いときは 1 列。
_TWO_COLUMN_MIN_WIDTH = 560
#: タブの最低の高さ（px）。数行しか見えない高さまで縮むと操作できない。
_TAB_MIN_HEIGHT = 280

# 計測の間引き幅。debug への書き出しはこの秒数に 1 件までにする。
# per-frame の I/O にしないための枠で、数え自体は毎回行う。
COUNTERS_LOG_INTERVAL_S = 5.0

# RX→UI の洪水対策（LogPane の flushQueue と同一規律）。
# 未処理の溜めに上限を設け、超えた分は古い方から捨てる。捨てた件数は
# 「... N行省略 ...」の 1 行で可視化する（黙って捨てない）。
BCON_QUEUE_MAX = 1024
# 1 回の _poll で取り出す上限。洪水でも 1 tick で抱え込まない。
# LogPane の FLUSH_MAX_LINES と同値。rumble/player_info の連打程度
# （～200 件）は 1 tick で畳み切れ、溢れは次 tick へ残る。
BCON_FLUSH_MAX = 512
# ログ欄に残す行数。Text は行数に比例して重くなる。
BCON_MAX_LINES = 1000

# プレイヤー LED の色（PLAYER_INFO のランプ表示）。点灯=黄緑・消灯=灰。
PLAYER_LAMP_ON_COLOR = "green yellow"
PLAYER_LAMP_OFF_COLOR = "gray"


def prefill_color_entries(transport: Any, entries: Any, notify: Any = None) -> None:
    """開いた直後の 4 欄へ問合せ結果を前埋めする。失敗は 000000＋注意。

    entries は `.get()`/`.set()` を持つ 4 つ組（StringVar 相当）。
    notify は注意文を受ける口（`notes.append` 相当）。例外は投げない。
    """
    try:
        items = list(entries)
    except TypeError:
        return
    if len(items) != 4:
        return
    try:
        payload = request_color(transport)
    except Exception:
        payload = None
    if payload is not None and len(payload) == 12:
        for index, item in enumerate(items):
            try:
                red = int(payload[index * 3]) & 0xFF
                green = int(payload[index * 3 + 1]) & 0xFF
                blue = int(payload[index * 3 + 2]) & 0xFF
                item.set(f"{red:02x}{green:02x}{blue:02x}")
            except Exception:
                try:
                    item.set("000000")
                except Exception:
                    pass
        return
    for item in items:
        try:
            item.set("000000")
        except Exception:
            pass
    if callable(notify):
        try:
            notify(COLOR_READ_FALLBACK_NOTE)
        except Exception:
            pass


def update_color_preview(entries: Any, canvas: Any, items: Any) -> None:
    """Entry 文字列→Canvas の fill へ映す（打鍵のたびの見た目だけ）。

    不正な欄は前を残す（送らない・落とさない）。例外は投げない。
    """
    try:
        entry_list = list(entries)
        item_list = list(items)
    except TypeError:
        return
    for entry, item in zip(entry_list, item_list):
        try:
            raw = entry.get()
        except Exception:
            continue
        slot = parse_color_hex(raw)
        if slot is None:
            continue
        try:
            red, green, blue = slot
            canvas.itemconfigure(item, fill=f"#{red:02x}{green:02x}{blue:02x}")
        except Exception:
            continue
