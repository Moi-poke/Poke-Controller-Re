#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""camera_backend.py - カメラ取得側の抽象。tkinter にも描画 API にも触れない。

プレビューは「取得 → 最新1枚 → 描画」の3段で、この層は**取得と最新1枚まで**。
描画（``VideoRenderer``）は ``core/preview_renderer.py`` が受け持つ。両者を
分けているのは、取得の実装（スレッド / プロセス + 共有メモリ / 将来の
GStreamer・DirectShow・V4L2・GPU 直取り）を増やしても描画面のコードが
増えないようにするため。

既存の実装は 2 つで、どちらもこの契約を構造型で満たす（継承は要求しない）:

* ``core.Camera``        … cv2.VideoCapture を別スレッドで回し、最新1枚を箱へ置く
* ``core.CameraQueue``   … 子プロセスで回し、共有メモリの 3 面をローテーション

**キューを持たない**のがこの契約の要。取り込みは最新1枚を上書きし、描画は
その瞬間の最新を読む。読む側が遅れても古いフレームが溜まらない（遅延が
積み上がらない）。``readFrameWithSeq`` の ``seq`` は上書きのたびに進むので、
描画側は「同じ seq なら描き直さない」で重複変換を省ける。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np

#: 1 枚の映像。BGR 8bit・C 連続（``cv2.VideoCapture.read`` の出力形）。
Frame = np.ndarray


@runtime_checkable
class CameraBackend(Protocol):
    """プレビューが必要とする取得側の最小面。

    実装は持っている機能を全部出せばよい。描画側はここにあるものしか
    呼ばないので、余分なメソッド（``isRunning`` / ``getStats`` など）は
    実装しても契約に現れない。
    """

    #: 要求したキャプチャ解像度 ``(w, h)``。実フレームの形とは限らない
    #: （ドライバが別の大きさを返すことがある）。座標変換は実フレームから
    #: 取るので、これは「まだ 1 枚も来ていないとき」の名目値として使う。
    capture_size: tuple[int, int]

    def readFrame(self, copy: bool = False) -> Frame | None:
        """最新フレームを返す。まだ 1 枚も無ければ None。

        ``copy=False`` の戻りは次のフレームで書き換わりうる。長く持つなら
        ``copy=True``。プロセス版は「まだ 1 枚も無い」を黒画像で表すので、
        None を返すのは共有メモリが無いときだけ（既存の契約）。
        """

    def readFrameWithSeq(self, copy: bool = False) -> tuple[Frame | None, int]:
        """最新フレームと世代番号を同時に返す。描画側の重複排除の鍵。"""

    def frame_seq(self) -> int:
        """現在の世代番号。フレームを読まずに進みだけ見たいとき用。"""

    def isOpened(self) -> bool:
        """デバイスが開いているか。"""

    def setFps(self, fps: int) -> None:
        """取得 FPS を変える。"""

    def saveCapture(
        self,
        filename: str | None = None,
        crop: Any = None,
        crop_ax: list | None = None,
        img: Frame | None = None,
    ) -> bool:
        """現在のフレームを保存する。成否を返す。"""

    def destroy(self) -> None:
        """取得を止めて資源を手放す。"""


@runtime_checkable
class TimedFrameSource(Protocol):
    """公開時刻まで返せる取得側。位相合わせ（``core/preview_phase.py``）用。

    ``core.Camera`` は満たし、``core.CameraQueue`` は満たさない（共有メモリ
    越しに ``t_ready_ns`` を運んでいない）。描画側は
    ``callable(getattr(camera, "readFrameWithTiming", None))`` で任意に使い、
    無いカメラでも動く。
    """

    def readFrameWithTiming(self) -> tuple[Frame | None, int, int, int]:
        """(frame, seq, t_capture_ns, t_ready_ns) を原子的に返す。"""
