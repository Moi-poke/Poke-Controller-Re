"""プレビュー描画の契約層。tkinter にも PIL にも触れない。

設計: docs/superpowers/specs/2026-09-26-gdi-preview-renderer-design.md 5節 と
docs/superpowers/specs/2026-10-30-gpu-preview-renderer-design.md。
Canvas の項目は廃止し、オーバーレイは「データ」として renderer へ渡す。
Tk も Win32 も SDL も知らないので core/ の境界内に置ける。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import numpy as np

#: Tk の色名 → Win32 ``COLORREF`` (``0x00BBGGRR``)。
#:
#: Tk 内部形は ``0x00RRGGBB``、Win32 ``COLORREF`` は ``0x00BBGGRR`` で、
#: **バイト位置が逆**。Tk の値をそのまま pen や brush へ渡すと赤と青が
#: 入れ替わる（``"blue"`` が赤になる）。変換は必ずこの境界で一度行う。
#:
#: ``"blue"`` が ``0x00FF0000`` に見えるのはこの反転の帰結であって誤りでは
#: ない。認識経路が渡す名前は ``"blue"`` と ``"red"`` の2つだけ
#: （``core/CommandVision.py``）だが、範囲枠とスティックの色もここを通す。
TK_COLORREF: Final[dict[str, int]] = {
    "black": 0x00000000,
    "white": 0x00FFFFFF,
    "red": 0x000000FF,
    "green": 0x0000FF00,
    "blue": 0x00FF0000,
    "yellow": 0x0000FFFF,
    "cyan": 0x00FFFF00,
    "magenta": 0x00FF00FF,
}


@dataclass(frozen=True, slots=True)
class RenderResult:
    """1 回の描画操作の結果。例外にしないので mainloop は生き残る。"""

    ok: bool
    elapsed_ns: int
    # "ok" / "no_hwnd" / "frame_not_contiguous" / "dimension_mismatch"
    # "no_frame" / "getdc_failed" / "bitblt_failed"
    # "stretchblt_failed"（表示面がキャプチャ解像度と違う.present の転送）
    # "no_room"（表示面に 1 画素も収まる場所が無い.compose）
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StickState:
    """スティックの外周円とノブ。押下中も外周円は動かない。"""

    active: bool = False
    center_x: int = 0
    center_y: int = 0
    radius: int = 0
    knob_x: int = 0  # ドラッグで動く
    knob_y: int = 0


@dataclass(frozen=True, slots=True)
class RectState:
    """範囲枠。Tk と同じ包含端の座標をそのまま持つ。"""

    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0
    visible: bool = False


@dataclass(frozen=True, slots=True)
class ImgRectState:
    """認識枠。白い外枠と認識色の内枠は別々の矩形なので両方持つ。

    ``color`` は Tk の色名ではなく Win32 ``COLORREF`` (0x00BBGGRR)。Tk 内部の
    0x00RRGGBB とバイト位置が逆なので、変換は境界で一度だけ行う。
    """

    outer: RectState = RectState()
    inner: RectState = RectState()
    visible: bool = False
    color: int = 0


@dataclass(frozen=True, slots=True)
class BadgeState:
    """プレビュー左上の状態表示。

    複数台を並べて起動したときに、ログ欄を見ずに各台が動いているか /
    止まっているかが分かるようにするための枠。**何を入れるか・いつ入れ
    替えるかは呼び出し側の責務**で、この型は「渡されたものを描く」こと
    しかしない。

    ``background`` と ``foreground`` は Tk の色名ではなく Win32
    ``COLORREF`` (0x00BBGGRR)。``ImgRectState.color`` と同じく Tk とは
    バイト順が逆なので、Tk へ渡す実装は自前で戻す責務を
    負う。

    既定は空・非表示。状態を出さない面が空の箱を描かないよう、
    ``visible`` の既定は ``False``。
    """

    text: str = ""
    background: int = 0x00303030  # Win32 COLORREF (0x00BBGGRR)
    foreground: int = 0x00FFFFFF  # Win32 COLORREF
    visible: bool = False


@dataclass(frozen=True, slots=True)
class OverlayState:
    """1 フレーム分のオーバーレイ。不変なので中途半端な更新は現れない。"""

    left_stick: StickState = StickState()
    right_stick: StickState = StickState()
    guide: RectState = RectState()
    img_rect: ImgRectState = ImgRectState()
    badge: BadgeState = BadgeState()


@runtime_checkable
class VideoRenderer(Protocol):
    """映像面の抽象。アプリ本体はこの契約だけを知り、OS の API を知らない。

    実装は 3 つ: ``core/sdl_surface.py`` の SDL 面（GPU。Windows は D3D11、
    Linux は OpenGL）、``core/gdi_surface.py`` の GDI 面（Windows の CPU 描画）、
    ``ui/photo_surface.py`` の PhotoImage 面（どの OS でも動く最終手段）。
    どれも「映像 → 合成 → 提示」の 3 段を持つ。

    ``compose`` と ``present`` を分けるのは合成時間と提示時間を別々に測るため。
    ``recompose`` は新しい映像を読まずにオーバーレイだけ描き直す。
    """

    #: True の面は Tk 以外のスレッドから compose/present を呼んでよい
    #: （GDI 子窓）。False の面は Tk のウィジェットを触るか、窓を作った
    #: スレッドに縛られる（photo / SDL）ので、描画は Tk スレッドだけで行う。
    #: ``core/preview_pump.py`` はこの値を見て描画スレッドを立てるか決める。
    cross_thread_render: bool

    def attach(self, parent_hwnd: int, size: tuple[int, int]) -> None: ...

    def resize(self, size: tuple[int, int]) -> None: ...

    def compose(self, frame: np.ndarray, overlay: OverlayState) -> RenderResult: ...

    def recompose(self, overlay: OverlayState) -> RenderResult: ...

    def present(self) -> RenderResult: ...

    def release(self) -> None: ...

    def client_size(self) -> tuple[int, int]: ...


#: 旧名。既存の import（``GuiAssets`` / 各面 / テスト）を壊さないための別名で、
#: 契約は 1 つしかない。新規コードは ``VideoRenderer`` を使う。
PreviewRenderer = VideoRenderer


@dataclass(frozen=True, slots=True)
class NativeFrameHandle:
    """GPU 側に既にある 1 枚を指す識別子。**未実装の拡張点**。

    ``kind`` は取り出し元の API 名（``"d3d11_texture"`` / ``"dmabuf"`` /
    ``"nv12_surface"`` など）、``handle`` はその API のハンドル値。
    初期実装では使わない。キャプチャ側が GPU 上にフレームを持てるように
    なったとき、CPU への読み戻しを省く経路（zero-copy）を足すための形だけを
    先に決めておく。``compose`` は ndarray しか受け取らないので、既存の
    バックエンドは何も変えずにこの型を無視できる。
    """

    kind: str
    handle: int
    width: int
    height: int
    pixel_format: str = "bgra"


@runtime_checkable
class NativeFrameTarget(Protocol):
    """GPU ハンドルを直接受け取れる面。``VideoRenderer`` の任意の拡張。

    ``compose_native`` を持たない面（GDI / PhotoImage）は、キャプチャ側が
    native ハンドルを出しても ``isinstance(面, NativeFrameTarget)`` が偽に
    なるので、呼び出し側は従来どおり ``compose`` へ ndarray を渡せばよい。
    初期実装の面はまだこれを実装しない（CPU → GPU 転送で足りている）。
    """

    def compose_native(
        self, handle: NativeFrameHandle, overlay: OverlayState
    ) -> RenderResult: ...
