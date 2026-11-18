#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""利用者スクリプト向けの公開コマンド基底モジュール（再公開ファサード）.

実体は `pokecon/app/python_command_base.py` に置かれる。
既存の `from Commands.PythonCommandBase import ...` 契約（`api_surface.py`）と
音声／色／回復ヘルパーの公開名を一か所から提供する。
"""

from __future__ import annotations

from pokecon.app.python_command_base import (
    TEMPLATE_PATH as TEMPLATE_PATH,
    AudioPythonCommand as AudioPythonCommand,
    ImageProcAudioPythonCommand as ImageProcAudioPythonCommand,
    ImageProcPythonCommand as ImageProcPythonCommand,
    PokeConDialogue as PokeConDialogue,
    PythonCommand as PythonCommand,
    RecoverableError as RecoverableError,
    RecoveryAborted as RecoveryAborted,
    RecoveryAdvice as RecoveryAdvice,
    StopThread as StopThread,
    band_power as band_power,
    build_color_payload as build_color_payload,
    classifyRecoverable as classifyRecoverable,
    clear_template_cache as clear_template_cache,
    config_reject_type as config_reject_type,
    describe_bcon_errcode as describe_bcon_errcode,
    is_bcon_transport as is_bcon_transport,
    is_tone as is_tone,
    parse_color_hex as parse_color_hex,
    send_config_frame as send_config_frame,
)
