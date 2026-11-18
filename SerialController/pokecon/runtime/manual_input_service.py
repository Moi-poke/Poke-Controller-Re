#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manual_input_service.py - キーボード・ゲームパッド手動入力のランタイム制御 Mixin."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger
from pokecon.runtime.Gamepad import SwitchGamepadController
from pokecon.runtime.Keyboard import SwitchKeyboardController


class ManualInputMixin:
    """SerialService から分離したキーボード／ゲームパッド寿命・表示制御。"""

    if TYPE_CHECKING:
        sender: Any
        key_press: Any
        keyboard: Any
        gamepad: Any
        _keyboard_active: Any
        _gamepad_active: Any
        _gamepad_display: Callable[[dict[str, Any]], None] | None

        def _notify(self, message: str) -> None: ...

    # -- キーボード -------------------------------------------------------------

    def set_keyboard_enabled(self, enabled: bool, setting_path: str) -> str | None:
        """キーボード操作の有効・無効を切り替える。

        成功したら None、失敗したら理由を返す（呼び出し側は画面の
        チェックを戻す）。理由はここで利用者とファイルの両方へ出す。
        """
        if not enabled:
            self.stop_keyboard()
            return None
        if self.key_press is None:
            message = "シリアル未接続のためキーボード操作を有効にできません"
            self._notify(message)
            logger.warning(message)
            return message
        if self.keyboard is None:
            try:
                self.keyboard = SwitchKeyboardController(
                    self.key_press,
                    setting_path=setting_path,
                    is_active=self._keyboard_active,
                )
                self.keyboard.listen()
            except Exception as e:
                message = f"キーボード操作を開始できませんでした: {e}"
                self._notify(message)
                logger.warning(message)
                self.keyboard = None
                return message
        return None

    def stop_keyboard(self) -> None:
        """キーボード操作を止める。止まっていれば何もしない。"""
        if self.keyboard is not None:
            try:
                self.keyboard.stop()
            except Exception as e:
                logger.warning(f"キーボードの停止で例外: {e}")
            self.keyboard = None

    def reload_keyboard_map(self) -> None:
        """生きている実体のキー割り当てを読み直す。"""
        if self.keyboard is None:
            return
        try:
            self.keyboard.reload_key_map()
        except Exception as e:
            logger.warning(f"キー割り当ての再読込で例外: {e}")

    # -- ゲームパッド -----------------------------------------------------------

    def set_gamepad_enabled(
        self, enabled: bool, pad_index: int = 0, deadzone: int = 1638
    ) -> str | None:
        """ゲームパッド操作の有効・無効を切り替える。

        成功したら None、失敗したら理由を返す（呼び出し側は画面の
        チェックを戻す）。理由はここで利用者とファイルの両方へ出す。
        """
        if not enabled:
            self.stop_gamepad()
            return None
        if self.sender is None:
            message = "シリアル未接続のためゲームパッド操作を有効にできません"
            self._notify(message)
            logger.warning(message)
            return message
        if self.gamepad is None:
            try:
                from pokecon.domain.hidhide import hidhide_ready
                from pokecon.domain.pad_source import PadSource, sdl_debug_enabled

                self.gamepad = SwitchGamepadController(
                    self.sender,
                    pad_index=pad_index,
                    is_active=self._gamepad_active,
                    on_display=self._gamepad_display,
                    deadzone=deadzone,
                    reader=PadSource(
                        allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready
                    ),
                )
                self.gamepad.listen()
            except Exception as e:
                message = f"ゲームパッド操作を開始できませんでした: {e}"
                self._notify(message)
                logger.warning(message)
                self.gamepad = None
                return message
        return None

    def stop_gamepad(self) -> None:
        """ゲームパッド操作を止める。止まっていれば何もしない。"""
        if self.gamepad is not None:
            try:
                self.gamepad.stop()
            except Exception as e:
                logger.warning(f"ゲームパッドの停止で例外: {e}")
            self.gamepad = None

    def invalidate_gamepad(self) -> bool:
        """生きている読取器のキャッシュを捨てる（手動再接続用）。"""
        gamepad = self.gamepad
        if gamepad is None:
            return False
        invalidate = getattr(gamepad, "invalidate_reader", None)
        if not callable(invalidate):
            return False
        try:
            invalidate()
            return True
        except Exception as e:
            logger.warning(f"ゲームパッドの読み取り口を破棄できません: {e}")
            return False

    def set_gamepad_display(self, fn: Callable[[dict[str, Any]], None] | None) -> None:
        """ゲームパッド入力の表示先を差し替える（仮想パッドの鏡用）。"""
        self._gamepad_display = fn
        gamepad = self.gamepad
        if gamepad is not None:
            try:
                gamepad.on_display = fn
            except Exception:
                pass

    def list_gamepads(self) -> list[int]:
        """繋がっているパッド番号の一覧。読めなければ空リスト。"""
        try:
            from pokecon.domain.hidhide import hidhide_ready
            from pokecon.domain.pad_source import PadSource, sdl_debug_enabled

            return PadSource(
                allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready
            ).scan()
        except Exception:
            return []
