"""BCON 設定（公開再公開ファサード）。

実体は `pokecon.domain.bcon_status`（純粋デコード）と `pokecon.ui.bcon`（UI）へ
分離されている。既存呼び出し元・テストの参照互換を保つため主要記号を再公開する。
"""

from __future__ import annotations

import tkinter.messagebox as tkmsg

from pokecon.domain.bcon_status import (
    ACCEL_RANGE_LABELS as ACCEL_RANGE_LABELS,
    CAPTURE_DEADLINE_MARGIN_S as CAPTURE_DEADLINE_MARGIN_S,
    CAPTURE_MAX_S as CAPTURE_MAX_S,
    CAPTURE_MIN_S as CAPTURE_MIN_S,
    COLOR_DEFAULTS as COLOR_DEFAULTS,
    COLOR_READ_FALLBACK_NOTE as COLOR_READ_FALLBACK_NOTE,
    COLOR_SLOT_NAMES as COLOR_SLOT_NAMES,
    GYRO_RANGE_LABELS as GYRO_RANGE_LABELS,
    PLAYER_FLAG_CAP_SAVED as PLAYER_FLAG_CAP_SAVED,
    SAVE_SUCCESS_TEXT as SAVE_SUCCESS_TEXT,
    SAVE_TIMEOUT_TEXT as SAVE_TIMEOUT_TEXT,
    STATUS_FLAG_NAMES as STATUS_FLAG_NAMES,
    STATUS_FLAG_WIRED as STATUS_FLAG_WIRED,
    decode_player_flags as decode_player_flags,
    decode_player_imu_range as decode_player_imu_range,
    decode_player_lamp as decode_player_lamp,
    decode_player_save as decode_player_save,
    format_status_block as format_status_block,
    format_status_flags as format_status_flags,
    parse_capture_seconds as parse_capture_seconds,
    request_color as request_color,
)
from pokecon.domain.procon_color import (
    _CONFIG_TYPE_NAMES as _CONFIG_TYPE_NAMES,
    build_color_payload as build_color_payload,
    config_reject_type as config_reject_type,
    describe_bcon_errcode as describe_bcon_errcode,
    is_bcon_transport as is_bcon_transport,
    parse_color_hex as parse_color_hex,
    send_config_frame as send_config_frame,
)
from pokecon.ui.bcon.dialog import BconSetup as BconSetup
from pokecon.ui.bcon.helpers import (
    BCON_FLUSH_MAX as BCON_FLUSH_MAX,
    BCON_MAX_LINES as BCON_MAX_LINES,
    BCON_QUEUE_MAX as BCON_QUEUE_MAX,
    BCON_WINDOW_TITLE as BCON_WINDOW_TITLE,
    COUNTERS_LOG_INTERVAL_S as COUNTERS_LOG_INTERVAL_S,
    PLAYER_LAMP_OFF_COLOR as PLAYER_LAMP_OFF_COLOR,
    PLAYER_LAMP_ON_COLOR as PLAYER_LAMP_ON_COLOR,
    _TAB_MIN_HEIGHT as _TAB_MIN_HEIGHT,
    _TWO_COLUMN_MIN_WIDTH as _TWO_COLUMN_MIN_WIDTH,
    prefill_color_entries as prefill_color_entries,
    update_color_preview as update_color_preview,
)

# `pokecon.ui.BconSetup.tkmsg.askquestion` へのテスト差し替え窓口を維持する。
_ = tkmsg
