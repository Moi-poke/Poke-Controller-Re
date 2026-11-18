#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sender_posture.py - Sender の姿勢合成・所有者管理・行エンコード補助 Mixin."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pokecon.domain.serial import encoding


class SenderPostureMixin:
    """Sender から切り出したコントローラ姿勢・所有者調停・行組立て。"""

    if TYPE_CHECKING:
        _lock: threading.RLock
        _logger: Any
        _arbiter: Any

        def _liveCapable(self, transport: Any = None) -> bool: ...
        def _bumpRevision(self) -> int: ...
        def getRevision(self) -> int: ...
        def snapshot(self) -> dict[str, Any]: ...
        def putLive(
            self, snap: dict[str, Any] | None = None, *, _closing: bool = False
        ) -> None: ...
        def _ensureArbitration(self) -> None: ...
        def _accept(self, source: str | None, releasing: bool = False) -> bool: ...
        def markEvent(self, source: str | None = None) -> None: ...

    #: Keys を import せず循環を避けるため中立値を定数で持つ。
    POSTURE_CENTER = 128
    POSTURE_HAT_CENTER = 8
    DEFAULT_OWNER = "default"

    def _initPosture(self) -> None:
        """姿勢を中立へ戻す。releaseAll と遅延初期化から呼ぶ。"""
        c = self.POSTURE_CENTER
        self._posture = {
            "btn": 0,
            "hat": self.POSTURE_HAT_CENTER,
            "lx": c,
            "ly": c,
            "rx": c,
            "ry": c,
        }
        self._L_stick_changed = False
        self._R_stick_changed = False
        self._hat_pos = self.POSTURE_HAT_CENTER

    def _ensurePosture(self) -> None:
        """姿勢がまだ無ければ作る。"""
        if not hasattr(self, "_posture"):
            self._initPosture()

    def applyButtons(
        self, press: Any = None, release: Any = None, source: Any = None
    ) -> None:
        """ボタンの押下と解放を差分で適用し、変化があれば live に渡す。"""
        snap = None
        owner = self._ownerKey(source)
        with self._lock:
            self._ensurePosture()
            self._ensureOwners()
            bits = int(self._owner_btn.get(owner, 0))
            for btn in press or ():
                bits |= int(btn)
            for btn in release or ():
                bits &= ~int(btn)
            if bits:
                self._owner_btn[owner] = bits
            else:
                self._owner_btn.pop(owner, None)
            if self._applyComposed():
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)

    def applyHat(self, hat: Any = None) -> None:
        """Hat の向きを差し替え、変化があれば live に渡す。"""
        snap = None
        with self._lock:
            self._ensurePosture()
            self._ensureHatHold()
            if hat is None:
                held = self._heldHatValue()
                self._hat_pos = held if held is not None else self.POSTURE_HAT_CENTER
            else:
                self._hat_pos = int(hat)
            if self._posture["hat"] != self._hat_pos:
                self._posture["hat"] = self._hat_pos
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)

    def _ensureOwners(self) -> None:
        """所有者ごとの申告を保持する入れ物を作る（遅延初期化）。"""
        if not hasattr(self, "_owner_btn"):
            self._owner_btn: dict[str, int] = {}
        if not hasattr(self, "_owner_hat"):
            self._owner_hat: dict[str, Any] = {}
        if not hasattr(self, "_owner_stick"):
            self._owner_stick: dict[str, Any] = {}

    def _ownerKey(self, source: Any = None) -> str:
        """所有者の名札を返す。指定が無ければ既定の所有者とする。"""
        return self.DEFAULT_OWNER if source is None else str(source)

    def _composeButtons(self) -> int:
        """全所有者のビット列を OR して、実際に送る値を返す。"""
        self._ensureOwners()
        merged = 0
        for bits in self._owner_btn.values():
            merged |= int(bits)
        return merged

    def _composeLatest(self, table: dict[str, Any]) -> Any | None:
        """revision が最大の申告の値を返す。申告が無ければ None。"""
        latest = None
        latest_rev = -1
        for value, rev in table.values():
            if int(rev) >= latest_rev:
                latest_rev = int(rev)
                latest = value
        return latest

    def suspendOwner(self, source: Any = None) -> bool:
        """所有者 1 人分の申告を退避し、姿勢から外す。"""
        owner = self._ownerKey(source)
        snap = None
        with self._lock:
            self._ensurePosture()
            self._ensureOwners()
            self._ensureHatHold()
            self._ensureSuspended()
            if owner in self._owner_saved:
                return False
            saved = {
                "btn": self._owner_btn.pop(owner, None),
                "hat": self._owner_hat.pop(owner, None),
                "stick": self._owner_stick.pop(owner, None),
            }
            if all(v is None for v in saved.values()):
                return False
            self._owner_saved[owner] = saved
            self._hat_held.pop(str(source), None)
            if self._applyComposed():
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)
        return True

    def restoreOwner(self, source: Any = None) -> bool:
        """退避した申告を戻す。"""
        owner = self._ownerKey(source)
        snap = None
        with self._lock:
            self._ensurePosture()
            self._ensureOwners()
            self._ensureHatHold()
            self._ensureSuspended()
            saved = self._owner_saved.pop(owner, None)
            if saved is None:
                return False
            if saved["btn"] is not None:
                self._owner_btn[owner] = saved["btn"]
            if saved["hat"] is not None:
                value = saved["hat"][0]
                self._owner_hat[owner] = (value, self.getRevision() + 1)
                self._hat_held[str(source)] = int(value)
            if saved["stick"] is not None:
                claim = saved["stick"][0]
                revisions = {side: self.getRevision() + 1 for side in claim}
                self._owner_stick[owner] = (claim, revisions)
            if self._applyComposed():
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)
        return True

    def isOwnerSuspended(self, source: Any = None) -> bool:
        """その所有者の申告が退避中かを返す（読むだけ）。"""
        with self._lock:
            self._ensureSuspended()
            return self._ownerKey(source) in self._owner_saved

    def getSuspended(self) -> dict[str, Any]:
        """退避中の申告の写しを返す（検算用・副作用なし）。"""
        with self._lock:
            self._ensureSuspended()
            return {k: dict(v) for k, v in self._owner_saved.items()}

    def _ensureSuspended(self) -> None:
        """退避の入れ物を作る（遅延初期化）。"""
        if not hasattr(self, "_owner_saved"):
            self._owner_saved: dict[str, Any] = {}

    def dropOwner(self, source: Any = None) -> bool:
        """所有者 1 人分の申告だけを取り下げる。"""
        owner = self._ownerKey(source)
        snap = None
        with self._lock:
            self._ensurePosture()
            self._ensureOwners()
            had = self._owner_btn.pop(owner, None) is not None
            had = (self._owner_hat.pop(owner, None) is not None) or had
            had = (self._owner_stick.pop(owner, None) is not None) or had
            if had and self._applyComposed():
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)
        return had

    def getOwners(self) -> dict[str, Any]:
        """所有者ごとの申告の写しを返す（検算用・副作用なし）。"""
        with self._lock:
            self._ensureOwners()
            return {
                "btn": dict(self._owner_btn),
                "hat": dict(self._owner_hat),
                "stick": dict(self._owner_stick),
            }

    def _composeStickSide(self, side: str) -> Any | None:
        """その側のスティック申告のうち revision が最大の座標を返す。"""
        self._ensureOwners()
        latest = None
        latest_rev = -1
        for claim, revisions in self._owner_stick.values():
            if side not in claim:
                continue
            rev = int(revisions.get(side, -1))
            if rev >= latest_rev:
                latest_rev = rev
                latest = claim[side]
        return latest

    def _applyComposed(self) -> bool:
        """合成した結果を姿勢へ書き、変化があれば True を返す。"""
        self._ensurePosture()
        self._ensureOwners()
        changed = False
        merged = self._composeButtons()
        if int(self._posture["btn"]) != merged:
            self._posture["btn"] = merged
            changed = True
        hat = self._composeLatest(self._owner_hat)
        if hat is None:
            hat = self.POSTURE_HAT_CENTER
        if int(self._posture["hat"]) != int(hat):
            self._posture["hat"] = int(hat)
            self._hat_pos = int(hat)
            changed = True
        for side, kx, ky in (("L", "lx", "ly"), ("R", "rx", "ry")):
            target = self._composeStickSide(side)
            tx, ty = (
                target
                if target is not None
                else (self.POSTURE_CENTER, self.POSTURE_CENTER)
            )
            if int(self._posture[kx]) != int(tx) or int(self._posture[ky]) != int(ty):
                self._posture[kx] = int(tx)
                self._posture[ky] = int(ty)
                if side == "L":
                    self._L_stick_changed = True
                else:
                    self._R_stick_changed = True
                changed = True
        return changed

    def _ensureHatHold(self) -> None:
        """Hat の押しっぱなし記録がまだ無ければ作る（遅延初期化）。"""
        if not hasattr(self, "_hat_held"):
            self._hat_held: dict[str, int] = {}

    def _heldHatValue(self) -> int | None:
        """押しっぱなしにされている Hat の値。無ければ None。"""
        self._ensureHatHold()
        self._ensureOwners()
        return self._composeLatest(self._owner_hat)

    def holdHat(self, hat: Any, source: str | None = None) -> bool:
        """十字キーを押しっぱなしにすることを申告する。"""
        if not self._accept(source):
            return False
        self.markEvent(source)
        with self._lock:
            self._ensureHatHold()
            self._ensureOwners()
            owner = self._ownerKey(source)
            self._hat_held[str(source)] = int(hat)
            self._owner_hat[owner] = (int(hat), self.getRevision() + 1)
            self.applyHat(int(hat))
        return True

    def releaseHat(self, source: str | None = None) -> bool:
        """十字キーの押しっぱなしをやめる。"""
        if not self._accept(source, releasing=True):
            return False
        self.markEvent(source)
        with self._lock:
            self._ensureHatHold()
            self._ensureOwners()
            self._hat_held.pop(str(source), None)
            self._owner_hat.pop(self._ownerKey(source), None)
            self.applyHat(None)
        return True

    def getHatHold(self) -> dict[str, int]:
        """押しっぱなしの一覧（確認・デバッグ用）。"""
        with self._lock:
            self._ensureHatHold()
            return dict(self._hat_held)

    def applyStick(
        self,
        stick: str,
        x: int | None = None,
        y: int | None = None,
        source: Any = None,
    ) -> None:
        """スティック座標を差分で適用し、変化があれば live に渡す。"""
        snap = None
        owner = self._ownerKey(source)
        with self._lock:
            self._ensurePosture()
            self._ensureOwners()
            side = (
                "R"
                if str(stick).upper().endswith("R")
                or str(stick).upper().endswith("RIGHT")
                else "L"
            )
            prev_claim, prev_revisions = self._owner_stick.get(owner, ({}, {}))
            claim = dict(prev_claim)
            revisions = dict(prev_revisions)
            if side not in claim:
                ax, ay = ("lx", "ly") if side == "L" else ("rx", "ry")
                claim[side] = (int(self._posture[ax]), int(self._posture[ay]))
            cx, cy = claim[side]
            if x is not None:
                cx = int(x)
            if y is not None:
                cy = int(y)
            claim[side] = (cx, cy)
            revisions[side] = self.getRevision() + 1
            self._owner_stick[owner] = (claim, revisions)
            if self._applyComposed():
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)

    def releaseAll(self) -> None:
        """全項目を中立に戻し、変化があれば live に渡す。"""
        snap = None
        with self._lock:
            before = dict(self._posture) if hasattr(self, "_posture") else None
            self._initPosture()
            self._ensureHatHold()
            self._ensureOwners()
            self._hat_held.clear()
            self._owner_btn.clear()
            self._owner_hat.clear()
            self._owner_stick.clear()
            self._ensureSuspended()
            self._owner_saved.clear()
            self._ensureArbitration()
            self._arbiter.release_handover()
            self._L_stick_changed = True
            self._R_stick_changed = True
            if before != self._posture:
                self._bumpRevision()
                if self._liveCapable():
                    snap = self.snapshot()
            if snap is not None:
                self.putLive(snap)

    def getPosture(self) -> dict[str, int]:
        """現在の姿勢の写しを返す（確認・デバッグ用）。"""
        with self._lock:
            self._ensurePosture()
            return dict(self._posture)

    def _buildRow(self) -> str:
        """現在の姿勢から送信行を組む。"""
        with self._lock:
            self._ensurePosture()
            p = self._posture
            row = encoding.format_legacy_row(
                p["btn"],
                p["hat"],
                p["lx"],
                p["ly"],
                p["rx"],
                p["ry"],
                self._L_stick_changed,
                self._R_stick_changed,
            )
            self._L_stick_changed = False
            self._R_stick_changed = False
            return row

    def verifyAgainstSendFormat(self, sf: Any) -> bool:
        """SendFormat と姿勢を突き合わせ、行が一致するか見る。"""
        mine = self._buildRow()
        theirs = sf.convert2str()
        if mine != theirs:
            self._logger.error(
                f"_buildRow mismatch: mine={mine!r} sendformat={theirs!r}"
            )
        return mine == theirs

    @staticmethod
    def encodePicoState(snap: dict[str, Any]) -> str:
        """snapshot から Pico 用の 1 行を組む。実体は encoding が持つ。"""
        return encoding.encode_pico_state(snap)

    @staticmethod
    def _picoField(snap: dict[str, Any], key: str) -> int:
        """1 項目を Pico が受け取れる範囲へ収める。実体は encoding が持つ。"""
        return encoding.pico_field(snap, key)

    def buildPicoRow(self) -> str:
        """いまの姿勢から Pico 用の 1 行を組む（読み取りだけ・副作用なし）。"""
        return self.encodePicoState(self.snapshot())

    @staticmethod
    def verifyPicoEncoder() -> bool:
        """encoder が仕様どおりかを機械で確かめる。実体は encoding が持つ。"""
        return encoding.verify_pico_encoder()
