#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdl_surface.py - GPU プレビュー面（SDL_Renderer 経由）。tkinter には触れない。

設計: docs/superpowers/specs/2026-10-30-gpu-preview-renderer-design.md。

なぜ SDL か（選定理由は設計書 1節）:

* 描画の呼び出しが OS で共通になる。窓へ出す 1 枚のテクスチャを更新し、
  拡大縮小は GPU、提示はスワップチェーン。GDI のように CPU で Blit しない。
* Windows では D3D11、Linux では OpenGL、macOS では Metal に SDL が落とす。
  CUDA もベンダ固有 API も使わないので Intel / AMD の内蔵 GPU で動く。
* SDL2 の実体は既存の依存である pygame が同梱している。新しい外部依存も
  ビルド手順も増えない（``pygame`` は `gamepad` 用に既に import されている）。

層の分け方: アプリ本体は ``VideoRenderer``（``core/preview_renderer.py``）だけを
知る。この面は OS 固有の「窓をどう用意するか」だけを ``SdlSurfaceApi`` の
裏へ隠し、SDL の呼び出し・転送・オーバーレイは共通コードに置く。API を
注入できるのは GDI 面と同じ理由（テストが実 GPU を要らない）。

遅延の考え方: キューを持たない。``compose`` は渡された 1 枚をその場で
テクスチャへ上げ、``present`` はその場で出す。SDL 側にリングバッファは無く、
前のフレームが残って順番待ちすることはない。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

import cv2
import numpy as np
from core.Camera import CAPTURE_SIZE
from core.coordinates import fit_rect
from core.preview_renderer import (
    BadgeState,
    ImgRectState,
    OverlayState,
    RectState,
    RenderResult,
    StickState,
)
from loguru import logger

# ---------------------------------------------------------------------------
# SDL 定数（SDL2）
# ---------------------------------------------------------------------------
_SDL_INIT_VIDEO = 0x00000020
_SDL_RENDERER_ACCELERATED = 0x00000002
_SDL_TEXTUREACCESS_STREAMING = 1
#: ``SDL_DEFINE_PIXELFORMAT(PACKED32, ARGB, 8888, 32, 4)``。リトルエンディアンの
#: メモリ順は B, G, R, A で、OpenCV の BGR に A を足しただけになる。
#: D3D11 の描画面が広告する唯一の 32bit 形式でもある（実測。
#: ABGR8888 や BGR24 は広告されず、SDL 内部で CPU 変換される）。
_SDL_PIXELFORMAT_ARGB8888 = 0x16362004
#: PIL の RGBA バイト列は、リトルエンディアンでは ABGR8888 として読める。
_SDL_PIXELFORMAT_ABGR8888 = 0x16762004
_SDL_BLENDMODE_BLEND = 1
_SDL_SCALEMODE_LINEAR = 1

#: 使う順。Windows の ``direct3d`` は D3D9 で、子窓への提示が画面に出ない
#: （実測: SDL_CreateRenderer は成功するのに 1 画素も出ない）。``direct3d11``
#: は子窓でも出る（実測）。Linux/macOS は候補に無いものを飛ばす。
_DRIVER_PREFERENCE: Final[tuple[str, ...]] = (
    "direct3d11",
    "metal",
    "opengl",
    "opengles2",
    "direct3d",
)

# Win32（子窓の用意だけが OS 固有）
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000
_WS_CLIPSIBLINGS = 0x04000000
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_CHILD_CLASS = "STATIC"

# オーバーレイの色。COLORREF (0x00BBGGRR) で持つ（GDI 面と同じ値）。
_LEFT_COLOR = 0x00FFFF00  # cyan
_RIGHT_COLOR = 0x000000FF  # red
_GUIDE_COLOR = _RIGHT_COLOR
_WHITE = 0x00FFFFFF
_KNOB_RADIUS_DIVISOR = 10
_OUTER_STROKE_PX = 4
_INNER_STROKE_PX = 2
_GUIDE_STROKE_PX = 1
_RING_STROKE_PX = 1
#: 円を近似する折れ線の本数。表示面で直径 100px 程度なら継ぎ目が見えない。
_CIRCLE_SEGMENTS = 64
#: 範囲枠の破線（装置座標 px）。Tk の破線に近い見た目にする。
_DASH_PX = 4

# バッジ（左上の状態表示）は装置座標で「文字 16px・余白 4px」に固定する。
# GDI 面はキャプチャ座標へ描いてから拡大縮小されるので逆数倍していたが、
# こちらは最初から装置座標に描くのでその換算が要らない。
_BADGE_FONT_PX = 16
_BADGE_PAD_PX = 4
_BADGE_FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
)
#: バッジのテクスチャ保持数。文字が変わるたびに 1 枚作るので、数枚で足りる。
_BADGE_CACHE_SIZE = 4

#: SDL2 の実体を明示したいときの環境変数（同梱物を使わない配布向け）。
_SDL_LIB_ENV = "POKECON_SDL2_LIB"


# ---------------------------------------------------------------------------
# 小さな変換
# ---------------------------------------------------------------------------


def _rgba(colorref: int) -> tuple[int, int, int, int]:
    """COLORREF (0x00BBGGRR) を SDL の (r, g, b, a) へ。バイト位置が逆なので必須。"""
    return (colorref & 0xFF, (colorref >> 8) & 0xFF, (colorref >> 16) & 0xFF, 0xFF)


def _sdl_library_candidates() -> list[str]:
    """SDL2 の共有ライブラリ候補。pygame 同梱物を最優先する。

    別途 SDL2 を入れる手順を利用者へ押し付けないため、既に依存にある
    pygame が持っている実体を使う。見つからなければ空を返し、呼び出し側が
    「GPU 面は使えない」と判断して次の面へ落ちる。
    """
    override = os.environ.get(_SDL_LIB_ENV)
    if override:
        return [override]
    found: list[str] = []
    try:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
    except Exception:
        package_dir = ""
    else:
        package_dir = os.path.dirname(os.path.abspath(pygame.__file__))
    if package_dir:
        for base in (
            package_dir,
            os.path.join(os.path.dirname(package_dir), "pygame.libs"),
        ):
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                name = entry.lower()
                if not name.startswith(("sdl2", "libsdl2")):
                    continue
                if name.endswith((".dll", ".dylib")) or ".so" in name:
                    path = os.path.join(base, entry)
                    if path not in found:
                        found.append(path)
    for name in ("SDL2", "SDL2-2.0"):
        resolved = ctypes.util.find_library(name)
        if resolved and resolved not in found:
            found.append(resolved)
    return found


# ---------------------------------------------------------------------------
# SDL の呼び出し口
# ---------------------------------------------------------------------------


class SdlSurfaceApi(Protocol):
    """``SdlSurface`` が OS / SDL に触る唯一の口。テストはここを差し替える。"""

    def init_video(self) -> bool: ...

    def create_embedded_window(self, host: int, size: tuple[int, int]) -> int: ...

    def resize_window(self, window: int, size: tuple[int, int]) -> None: ...

    def create_renderer(self, window: int) -> int: ...

    def renderer_name(self, renderer: int) -> str: ...

    def output_size(self, renderer: int) -> tuple[int, int]: ...

    def set_vsync(self, renderer: int, on: bool) -> None: ...

    def destroy_renderer(self, renderer: int) -> None: ...

    def destroy_window(self, window: int) -> None: ...

    def create_texture(self, renderer: int, size: tuple[int, int]) -> int: ...

    def create_text_texture(
        self, renderer: int, size: tuple[int, int], pixels: Any, pitch: int
    ) -> int: ...

    def destroy_texture(self, texture: int) -> None: ...

    def update_texture(self, texture: int, pixels: Any, pitch: int) -> bool: ...

    def clear(self, renderer: int, rgba: tuple[int, int, int, int]) -> bool: ...

    def draw_texture(
        self, renderer: int, texture: int, dest: tuple[int, int, int, int]
    ) -> bool: ...

    def set_draw_color(
        self, renderer: int, rgba: tuple[int, int, int, int]
    ) -> None: ...

    def fill_rects(
        self, renderer: int, rects: list[tuple[int, int, int, int]]
    ) -> None: ...

    def draw_segments(
        self, renderer: int, segments: list[tuple[float, float, float, float]]
    ) -> None: ...

    def fill_triangles(
        self,
        renderer: int,
        vertices: list[tuple[float, float]],
        rgba: tuple[int, int, int, int],
    ) -> None: ...

    def set_clip_rect(
        self, renderer: int, rect: tuple[int, int, int, int] | None
    ) -> None: ...

    def present(self, renderer: int) -> bool: ...


class _SDLVertex(ctypes.Structure):
    """``SDL_Vertex``。位置 2 + 色 4 + テクスチャ座標 2 で 20 バイト。"""

    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("r", ctypes.c_uint8),
        ("g", ctypes.c_uint8),
        ("b", ctypes.c_uint8),
        ("a", ctypes.c_uint8),
        ("u", ctypes.c_float),
        ("v", ctypes.c_float),
    ]


class _SDLRect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
    ]


class _SDLRendererInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("flags", ctypes.c_uint32),
        ("num_texture_formats", ctypes.c_uint32),
        ("texture_formats", ctypes.c_uint32 * 16),
        ("max_texture_width", ctypes.c_int),
        ("max_texture_height", ctypes.c_int),
    ]


class CtypesSdlApi:
    """SDL2 を ctypes で呼ぶ実装。窓の用意だけ OS ごとに分かれる。"""

    def __init__(self, library: str | None = None) -> None:
        self._dll = self._load(library)
        self._children: dict[int, int] = {}
        self._driver_names: list[str] = []
        self._bind()

    @staticmethod
    def _load(library: str | None) -> Any:
        candidates = [library] if library else _sdl_library_candidates()
        errors: list[str] = []
        for path in candidates:
            if not path:
                continue
            try:
                loader = ctypes.WinDLL if os.name == "nt" else ctypes.CDLL
                return loader(path)
            except OSError as error:
                errors.append(f"{path}: {error}")
        raise OSError("SDL2 の共有ライブラリが見つかりません: " + "; ".join(errors))

    def _bind(self) -> None:
        sdl = self._dll
        u32, i32, ptr, u8 = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint8,
        )
        rgba = [u8, u8, u8, u8]
        signatures: dict[str, tuple[list[Any], Any]] = {
            "SDL_Init": ([u32], i32),
            "SDL_GetError": ([], ctypes.c_char_p),
            "SDL_GetNumRenderDrivers": ([], i32),
            "SDL_GetRenderDriverInfo": ([i32, ctypes.POINTER(_SDLRendererInfo)], i32),
            "SDL_CreateWindowFrom": ([ptr], ptr),
            "SDL_DestroyWindow": ([ptr], None),
            "SDL_SetWindowSize": ([ptr, i32, i32], i32),
            "SDL_CreateRenderer": ([ptr, i32, u32], ptr),
            "SDL_DestroyRenderer": ([ptr], None),
            "SDL_GetRendererInfo": ([ptr, ctypes.POINTER(_SDLRendererInfo)], i32),
            "SDL_GetRendererOutputSize": (
                [ptr, ctypes.POINTER(i32), ctypes.POINTER(i32)],
                i32,
            ),
            "SDL_RenderSetVSync": ([ptr, i32], i32),
            "SDL_CreateTexture": ([ptr, u32, i32, i32, i32], ptr),
            "SDL_DestroyTexture": ([ptr], None),
            "SDL_UpdateTexture": ([ptr, ctypes.POINTER(_SDLRect), ptr, i32], i32),
            "SDL_SetTextureScaleMode": ([ptr, i32], i32),
            "SDL_SetTextureBlendMode": ([ptr, i32], i32),
            "SDL_SetRenderDrawColor": ([ptr, *rgba], i32),
            "SDL_RenderClear": ([ptr], i32),
            "SDL_RenderCopy": (
                [ptr, ptr, ctypes.POINTER(_SDLRect), ctypes.POINTER(_SDLRect)],
                i32,
            ),
            "SDL_RenderFillRect": ([ptr, ctypes.POINTER(_SDLRect)], i32),
            "SDL_RenderDrawLine": ([ptr, i32, i32, i32, i32], i32),
            "SDL_RenderGeometry": (
                [ptr, ptr, ctypes.POINTER(_SDLVertex), i32, ptr, i32],
                i32,
            ),
            "SDL_RenderSetClipRect": ([ptr, ctypes.POINTER(_SDLRect)], i32),
            "SDL_RenderPresent": ([ptr], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(sdl, name)
            function.argtypes = args
            function.restype = result

    # -- video / window ---------------------------------------------------
    def init_video(self) -> bool:
        if self._dll.SDL_Init(_SDL_INIT_VIDEO) != 0:
            logger.warning("SDL の映像初期化に失敗: {}", self._last_error())
            return False
        count = int(self._dll.SDL_GetNumRenderDrivers())
        names: list[str] = []
        for index in range(count):
            info = _SDLRendererInfo()
            if self._dll.SDL_GetRenderDriverInfo(index, ctypes.byref(info)) == 0:
                name = info.name.decode("utf-8", "replace") if info.name else ""
                names.append(name)
        self._driver_names = names
        return True

    def create_embedded_window(self, host: int, size: tuple[int, int]) -> int:
        """SDL の窓を作る。Windows は Tk の子窓を用意してから包む。

        Tk の Frame の HWND をそのまま渡すと SDL がその窓の手続きを差し替え、
        Tk の事象処理と取り合いになる。GDI 面と同じく「Tk が知らない子窓」を
        1 枚作り、そこへ描く。子窓は入力を受け取らない（``EnableWindow`` を
        落とす）ので、マウスは従来どおり親の Tk へ届く。
        """
        width, height = int(size[0]), int(size[1])
        if os.name == "nt":
            child = self._create_child_window(host, width, height)
            if not child:
                logger.warning("プレビュー子窓を作れない error={}", self._last_error())
                return 0
            window = int(self._dll.SDL_CreateWindowFrom(ctypes.c_void_p(child)) or 0)
            if not window:
                logger.warning("SDL の窓を作れない: {}", self._last_error())
                self._destroy_child(child)
                return 0
            self._children[window] = child
            return window
        # X11 / Wayland: Tk の widget の窓 ID をそのまま渡す。
        return int(self._dll.SDL_CreateWindowFrom(ctypes.c_void_p(host)) or 0)

    def resize_window(self, window: int, size: tuple[int, int]) -> None:
        """包んだ窓を新しい大きさにする。

        ``SDL_SetWindowSize`` は ``SDL_CreateWindowFrom`` で包んだ窓
        （SDL_WINDOW_FOREIGN）には効かない。戻り値は成功なのに HWND は
        1 画素も変わらない（実測）。広げるのは自分で作った子窓なので、
        ``SetWindowPos`` で直接広げる。Windows の子窓なら SDL の窓手続きが
        WM_SIZE を受け取り、描画器の出力サイズもその場で追従する。
        Tk の窓をそのまま包む環境（X11 等）は Tk が既に新しい大きさを
        持っているので、ここでは触らない。
        """
        child = self._children.get(window)
        if not child:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowPos.restype = ctypes.c_int
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos(
            ctypes.c_void_p(child),
            None,
            0,
            0,
            int(size[0]),
            int(size[1]),
            _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE,
        )

    def create_renderer(self, window: int) -> int:
        for name in _DRIVER_PREFERENCE:
            if name not in self._driver_names:
                continue
            index = self._driver_names.index(name)
            renderer = int(
                self._dll.SDL_CreateRenderer(
                    ctypes.c_void_p(window), index, _SDL_RENDERER_ACCELERATED
                )
                or 0
            )
            if renderer:
                return renderer
            logger.warning("SDL の描画器 {} を作れない: {}", name, self._last_error())
        # 候補表に無い環境（将来の SDL 等）は SDL に選ばせる。
        renderer = int(
            self._dll.SDL_CreateRenderer(
                ctypes.c_void_p(window), -1, _SDL_RENDERER_ACCELERATED
            )
            or 0
        )
        if not renderer:
            logger.warning("SDL の描画器を作れない: {}", self._last_error())
        return renderer

    def renderer_name(self, renderer: int) -> str:
        info = _SDLRendererInfo()
        if (
            self._dll.SDL_GetRendererInfo(ctypes.c_void_p(renderer), ctypes.byref(info))
            != 0
        ):
            return ""
        return info.name.decode("utf-8", "replace") if info.name else ""

    def output_size(self, renderer: int) -> tuple[int, int]:
        width, height = ctypes.c_int(0), ctypes.c_int(0)
        if (
            self._dll.SDL_GetRendererOutputSize(
                ctypes.c_void_p(renderer), ctypes.byref(width), ctypes.byref(height)
            )
            != 0
        ):
            return (0, 0)
        return (int(width.value), int(height.value))

    def set_vsync(self, renderer: int, on: bool) -> None:
        # 失敗しても構わない（作った描画器が vsync 切替を持たないだけ）。
        self._dll.SDL_RenderSetVSync(ctypes.c_void_p(renderer), 1 if on else 0)

    def destroy_renderer(self, renderer: int) -> None:
        self._dll.SDL_DestroyRenderer(ctypes.c_void_p(renderer))

    def destroy_window(self, window: int) -> None:
        child = self._children.pop(window, 0)
        self._dll.SDL_DestroyWindow(ctypes.c_void_p(window))
        if child:
            self._destroy_child(child)

    # -- textures ---------------------------------------------------------
    def create_texture(self, renderer: int, size: tuple[int, int]) -> int:
        texture = int(
            self._dll.SDL_CreateTexture(
                ctypes.c_void_p(renderer),
                _SDL_PIXELFORMAT_ARGB8888,
                _SDL_TEXTUREACCESS_STREAMING,
                int(size[0]),
                int(size[1]),
            )
            or 0
        )
        if not texture:
            logger.warning("プレビュー用テクスチャを作れない: {}", self._last_error())
            return 0
        self._dll.SDL_SetTextureScaleMode(
            ctypes.c_void_p(texture), _SDL_SCALEMODE_LINEAR
        )
        return texture

    def create_text_texture(
        self, renderer: int, size: tuple[int, int], pixels: Any, pitch: int
    ) -> int:
        texture = int(
            self._dll.SDL_CreateTexture(
                ctypes.c_void_p(renderer),
                _SDL_PIXELFORMAT_ABGR8888,
                _SDL_TEXTUREACCESS_STREAMING,
                int(size[0]),
                int(size[1]),
            )
            or 0
        )
        if not texture:
            return 0
        self._dll.SDL_SetTextureBlendMode(
            ctypes.c_void_p(texture), _SDL_BLENDMODE_BLEND
        )
        if (
            self._dll.SDL_UpdateTexture(
                ctypes.c_void_p(texture), None, ctypes.c_void_p(pixels), int(pitch)
            )
            != 0
        ):
            self._dll.SDL_DestroyTexture(ctypes.c_void_p(texture))
            return 0
        return texture

    def destroy_texture(self, texture: int) -> None:
        self._dll.SDL_DestroyTexture(ctypes.c_void_p(texture))

    def update_texture(self, texture: int, pixels: Any, pitch: int) -> bool:
        return (
            self._dll.SDL_UpdateTexture(
                ctypes.c_void_p(texture), None, ctypes.c_void_p(pixels), int(pitch)
            )
            == 0
        )

    # -- drawing ----------------------------------------------------------
    def clear(self, renderer: int, rgba: tuple[int, int, int, int]) -> bool:
        if self._dll.SDL_SetRenderDrawColor(ctypes.c_void_p(renderer), *rgba) != 0:
            return False
        return self._dll.SDL_RenderClear(ctypes.c_void_p(renderer)) == 0

    def draw_texture(
        self, renderer: int, texture: int, dest: tuple[int, int, int, int]
    ) -> bool:
        rect = _SDLRect(*dest)
        return (
            self._dll.SDL_RenderCopy(
                ctypes.c_void_p(renderer),
                ctypes.c_void_p(texture),
                None,
                ctypes.byref(rect),
            )
            == 0
        )

    def set_draw_color(self, renderer: int, rgba: tuple[int, int, int, int]) -> None:
        self._dll.SDL_SetRenderDrawColor(ctypes.c_void_p(renderer), *rgba)

    def fill_rects(self, renderer: int, rects: list[tuple[int, int, int, int]]) -> None:
        for rect in rects:
            if rect[2] <= 0 or rect[3] <= 0:
                continue
            box = _SDLRect(*rect)
            self._dll.SDL_RenderFillRect(ctypes.c_void_p(renderer), ctypes.byref(box))

    def draw_segments(
        self, renderer: int, segments: list[tuple[float, float, float, float]]
    ) -> None:
        for x0, y0, x1, y1 in segments:
            self._dll.SDL_RenderDrawLine(
                ctypes.c_void_p(renderer),
                int(round(x0)),
                int(round(y0)),
                int(round(x1)),
                int(round(y1)),
            )

    def fill_triangles(
        self,
        renderer: int,
        vertices: list[tuple[float, float]],
        rgba: tuple[int, int, int, int],
    ) -> None:
        """三角形の並びを単色で塗る。

        ``SDL_RenderGeometry`` は描画色ではなく**頂点色**を使う。ここで
        頂点色を入れないと 0（黒）になり、ノブが映像に穴を開けたように
        見える（実測）。色は呼び出し元から受け取り、頂点へそのまま入れる。
        """
        if not vertices:
            return
        red, green, blue, alpha = rgba
        array = (_SDLVertex * len(vertices))()
        for index, (x, y) in enumerate(vertices):
            vertex = array[index]
            vertex.x = x
            vertex.y = y
            vertex.r = red
            vertex.g = green
            vertex.b = blue
            vertex.a = alpha
        self._dll.SDL_RenderGeometry(
            ctypes.c_void_p(renderer), None, array, len(vertices), None, 0
        )

    def set_clip_rect(
        self, renderer: int, rect: tuple[int, int, int, int] | None
    ) -> None:
        if rect is None:
            self._dll.SDL_RenderSetClipRect(ctypes.c_void_p(renderer), None)
            return
        box = _SDLRect(*rect)
        self._dll.SDL_RenderSetClipRect(ctypes.c_void_p(renderer), ctypes.byref(box))

    def present(self, renderer: int) -> bool:
        self._dll.SDL_RenderPresent(ctypes.c_void_p(renderer))
        return True

    # -- Win32 child window ----------------------------------------------
    @staticmethod
    def _create_child_window(parent: int, width: int, height: int) -> int:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 64bit では EXE の HMODULE が 32bit に収まらない（例 0x140000000）。
        # restype を書かないと ctypes は int で受けて上位を落とし、壊れた
        # hInstance を CreateWindowExW へ渡してアクセス違反になる。
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        instance = kernel32.GetModuleHandleW(None)
        child = int(
            user32.CreateWindowExW(
                0,
                _CHILD_CLASS,
                "",
                _WS_CHILD | _WS_VISIBLE | _WS_CLIPSIBLINGS,
                0,
                0,
                max(1, width),
                max(1, height),
                ctypes.c_void_p(parent),
                None,
                ctypes.c_void_p(instance),
                None,
            )
            or 0
        )
        if not child:
            return 0
        # 子窓が入力を持つと Tk のマウス束縛（スティック操作）が止まる。
        # GDI 面と同じ扱いにする。
        user32.EnableWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        user32.EnableWindow.restype = ctypes.c_bool
        user32.EnableWindow(ctypes.c_void_p(child), False)
        return child

    @staticmethod
    def _destroy_child(child: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.IsWindow.restype = ctypes.c_int
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        if user32.IsWindow(ctypes.c_void_p(child)):
            user32.DestroyWindow(ctypes.c_void_p(child))

    def _last_error(self) -> str:
        message = self._dll.SDL_GetError()
        return message.decode("utf-8", "replace") if message else ""


# ---------------------------------------------------------------------------
# 面の本体
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelfTestResult:
    """起動時の自己検査。GDI 面と同じ形にしておく（呼び出し側が共通で読む）。"""

    outcome: str  # "not_run" | "renderer_ready" | "no_renderer"
    fully_occluded: bool | None = False
    clip_box: tuple[int, int, int, int] = (0, 0, 0, 0)


def _font(px: int) -> Any:
    """バッジ用フォント。見つからなければ PIL の既定に落ちる（落ちない）。"""
    from PIL import ImageFont

    for path in _BADGE_FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # 古い Pillow
        return ImageFont.load_default()


class SdlSurface:
    """SDL の描画器を持つ GPU 面。オーバーレイも同じ面へ描く。"""

    #: SDL の窓は作ったスレッド（Tk のメインスレッド）からしか描けない。
    #: ``core/preview_pump.py`` に描画を任せない（Tk の clock が描く）。
    cross_thread_render = False

    def __init__(self, api: SdlSurfaceApi | None = None) -> None:
        self._api: SdlSurfaceApi = api if api is not None else CtypesSdlApi()
        self._window = 0
        self._renderer = 0
        self._texture = 0
        #: テクスチャの大きさ = 今の映像の解像度。カメラが別の大きさを出したら
        #: 作り直す（CAPTURE_SIZE 固定にしない。1080p のカメラでも通る）。
        self._video_size: tuple[int, int] = (0, 0)
        #: BGR → BGRA を書き込む作業バッファ。毎フレームの確保を避ける。
        self._bgra: np.ndarray[tuple[int, ...], np.dtype[np.uint8]] | None = None
        #: 表示面（子窓）の画素での大きさ。
        self._viewport: tuple[int, int] = (0, 0)
        #: ビューポートの中で映像が置かれる矩形 (x, y, w, h)。
        self._dest: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._overlay: OverlayState = OverlayState()
        #: テクスチャに 1 枚入っているか（compose 済みか）。
        self._has_frame = False
        #: 次の present で描き直す必要があるか。
        self._dirty = False
        self._self_test = SelfTestResult("not_run")
        self._renderer_name = ""
        self._logged_failures: set[str] = set()
        self._badge_textures: dict[tuple[Any, ...], tuple[int, int, int]] = {}
        self._badge_order: list[tuple[Any, ...]] = []

    # -- 状態 -------------------------------------------------------------
    @property
    def attached(self) -> bool:
        """面が本当に出来ているか。GUI は偽なら次の面へ落ちる。"""
        return self._renderer != 0 and self._texture != 0

    @property
    def self_test_result(self) -> SelfTestResult:
        return self._self_test

    @property
    def renderer_name(self) -> str:
        """SDL が選んだ描画器の名前（direct3d11 / opengl / metal など）。"""
        return self._renderer_name

    def self_test(self) -> SelfTestResult:
        return self._self_test

    def client_size(self) -> tuple[int, int]:
        if self._renderer == 0:
            return (0, 0)
        return self._api.output_size(self._renderer)

    # -- 接続 -------------------------------------------------------------
    def attach(self, parent_hwnd: int, size: tuple[int, int]) -> None:
        """子窓を作り、描画器とテクスチャを用意する。

        失敗しても例外にしない。呼び出し側は ``attached`` が偽なら次の面へ
        落ちる（GPU の無い環境・SDL を読めない環境で起動を止めない）。
        """
        if not self._api.init_video():
            self._self_test = SelfTestResult("no_renderer")
            return
        window = self._api.create_embedded_window(parent_hwnd, size)
        if not window:
            self._self_test = SelfTestResult("no_renderer")
            return
        self._window = window
        renderer = self._api.create_renderer(window)
        if not renderer:
            self._api.destroy_window(window)
            self._window = 0
            self._self_test = SelfTestResult("no_renderer")
            return
        self._renderer = renderer
        self._renderer_name = self._api.renderer_name(renderer)
        # vsync は切る。Tk の tick が提示の間隔を決めており、vsync を入れると
        # 取得から表示までの待ちが 1 フレーム増える。
        self._api.set_vsync(renderer, False)
        width, height = self._api.output_size(renderer)
        if width <= 0 or height <= 0:
            width, height = int(size[0]), int(size[1])
        self._viewport = (max(1, width), max(1, height))
        self._ensure_texture(CAPTURE_SIZE)
        self._dest = fit_rect(self._video_size, self._viewport)
        self._self_test = SelfTestResult(
            "renderer_ready" if self.attached else "no_renderer"
        )
        logger.info(
            "GPU プレビュー面を作成 renderer={} size={}x{}",
            self._renderer_name or "unknown",
            self._viewport[0],
            self._viewport[1],
        )

    def resize(self, size: tuple[int, int]) -> None:
        """表示面を新しい大きさにする。窓と余白の計算だけをやり直す。"""
        if self._window == 0:
            return
        width, height = int(size[0]), int(size[1])
        # Tk は geom が決まる前に 1x1 の <Configure> を送る。1 画素の面を作ると
        # 絵が出ないので、決まるまで待つ（GDI 面と同じ規則）。
        if width <= 1 or height <= 1 or (width, height) == self._viewport:
            return
        self._api.resize_window(self._window, (width, height))
        actual = self._api.output_size(self._renderer)
        if actual != (width, height):
            logger.warning(
                "SDL の出力 {} が要求 {} と一致しない（DPI のずれ）",
                actual,
                (width, height),
            )
        if actual[0] <= 0 or actual[1] <= 0:
            actual = (width, height)
        self._viewport = actual
        self._dest = fit_rect(self._video_size, self._viewport)
        # 大きさが変わるとスワップチェーンの中身は失われる。撮った絵は
        # テクスチャに残っているので、次の present で新しい配置へ描き直す。
        self._dirty = self._has_frame

    # -- 合成 -------------------------------------------------------------
    def compose(self, frame: np.ndarray, overlay: OverlayState) -> RenderResult:
        started = time.perf_counter_ns()
        if not self.attached:
            return self._fail("no_hwnd", "プレビュー面が未接続で合成できない", started)
        if not frame.flags.c_contiguous:
            return self._fail(
                "frame_not_contiguous",
                "プレビュー合成は連続した BGR を要求する",
                started,
            )
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            return self._fail(
                "frame_format",
                f"BGR 8bit でないフレームを捨てた shape={frame.shape} dtype={frame.dtype}",
                started,
            )
        size = (int(frame.shape[1]), int(frame.shape[0]))
        if not self._ensure_texture(size):
            return self._fail("texture_failed", "テクスチャを用意できない", started)
        dest_w, dest_h = self._dest[2], self._dest[3]
        if dest_w <= 0 or dest_h <= 0:
            return self._fail(
                "no_room",
                f"表示面 {self._viewport} に映像 {size} を収める場所が無い",
                started,
            )
        # BGR → BGRA は 1 パス。D3D11 が受けるのは ARGB8888（メモリ順 BGRA）
        # だけで、BGR24 を渡すと SDL 内部で CPU 変換される（実測で広告外）。
        bgra = self._bgra
        if bgra is None:
            return self._fail("texture_failed", "作業バッファが無い", started)
        cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA, dst=bgra)
        row_bytes = size[0] * 4
        if not self._api.update_texture(
            self._texture, int(bgra.ctypes.data), row_bytes
        ):
            return self._fail("upload_failed", "テクスチャへの転送に失敗した", started)
        self._overlay = overlay
        self._has_frame = True
        self._dirty = True
        return RenderResult(True, time.perf_counter_ns() - started, "ok")

    def recompose(self, overlay: OverlayState) -> RenderResult:
        """映像はそのまま、オーバーレイだけ差し替える。次の present で描く。"""
        started = time.perf_counter_ns()
        if not self.attached:
            return self._fail(
                "no_hwnd", "プレビュー面が未接続で再合成できない", started
            )
        if not self._has_frame:
            # 一度も合成していない。映像が無いのにオーバーレイだけ出さない。
            return RenderResult(False, time.perf_counter_ns() - started, "no_frame")
        self._overlay = overlay
        self._dirty = True
        return RenderResult(True, time.perf_counter_ns() - started, "ok")

    def present(self) -> RenderResult:
        started = time.perf_counter_ns()
        if not self.attached:
            return self._fail("no_hwnd", "プレビュー面が未接続で提示できない", started)
        if not self._dirty:
            return RenderResult(False, time.perf_counter_ns() - started, "no_frame")
        api = self._api
        # 余白は毎回黒で塗る。前の配置の絵が残ると窓を動かした直後に混ざる。
        if not api.clear(self._renderer, (0, 0, 0, 255)):
            return self._fail("clear_failed", "余白の消去に失敗した", started)
        if not api.draw_texture(self._renderer, self._texture, self._dest):
            return self._fail("draw_failed", "映像の描画に失敗した", started)
        api.set_clip_rect(self._renderer, self._dest)
        self._draw_overlay(self._overlay)
        api.set_clip_rect(self._renderer, None)
        api.present(self._renderer)
        self._dirty = False
        return RenderResult(True, time.perf_counter_ns() - started, "ok")

    def release(self) -> None:
        for texture, _w, _h in self._badge_textures.values():
            self._api.destroy_texture(texture)
        self._badge_textures = {}
        self._badge_order = []
        if self._texture:
            self._api.destroy_texture(self._texture)
            self._texture = 0
        if self._renderer:
            self._api.destroy_renderer(self._renderer)
            self._renderer = 0
        if self._window:
            self._api.destroy_window(self._window)
            self._window = 0
        self._bgra = None
        self._video_size = (0, 0)
        self._viewport = (0, 0)
        self._dest = (0, 0, 0, 0)
        self._has_frame = False
        self._dirty = False

    # -- 内部 -------------------------------------------------------------
    def _fail(self, detail: str, message: str, started: int) -> RenderResult:
        if detail not in self._logged_failures:
            self._logged_failures.add(detail)
            logger.warning("{}（detail={}）", message, detail)
        return RenderResult(False, time.perf_counter_ns() - started, detail)

    def _ensure_texture(self, size: tuple[int, int]) -> bool:
        """映像サイズのテクスチャと作業バッファを用意する（変わったら作り直す）。

        カメラの解像度は起動後に変わりうる（ドライバが要求を拒否する、
        設定で切り替える）。固定サイズで作ると、その瞬間から 1 枚も
        合成できなくなるので、初回と変更時にここで追従する。
        """
        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            return False
        if self._texture and self._video_size == (width, height):
            return True
        texture = self._api.create_texture(self._renderer, (width, height))
        if not texture:
            return False
        if self._texture:
            self._api.destroy_texture(self._texture)
        self._texture = texture
        self._video_size = (width, height)
        self._bgra = np.empty((height, width, 4), np.uint8)
        self._dest = fit_rect(self._video_size, self._viewport)
        # 大きさが変わったら古い絵は使えない。
        self._has_frame = False
        self._dirty = False
        return True

    def _scales(self) -> tuple[float, float]:
        """キャプチャ座標 → 装置座標の倍率。"""
        dest_w, dest_h = self._dest[2], self._dest[3]
        if self._video_size[0] <= 0 or self._video_size[1] <= 0:
            return (0.0, 0.0)
        return (dest_w / self._video_size[0], dest_h / self._video_size[1])

    def _point(self, x: float, y: float) -> tuple[float, float]:
        scale_x, scale_y = self._scales()
        return (
            self._dest[0] + x * scale_x,
            self._dest[1] + y * scale_y,
        )

    def _draw_overlay(self, overlay: OverlayState) -> None:
        """オーバーレイを装置座標で描く。空の既定値なら 1 つも呼ばない。"""
        sticks = (
            (overlay.left_stick, _LEFT_COLOR),
            (overlay.right_stick, _RIGHT_COLOR),
        )
        for stick, color in sticks:
            if stick.active:
                self._draw_stick(stick, color)
        if overlay.guide.visible:
            self._draw_rect_dashed(overlay.guide, _GUIDE_COLOR)
        if overlay.img_rect.visible:
            self._draw_img_rect(overlay.img_rect)
        badge = overlay.badge
        if badge.visible and badge.text:
            self._draw_badge(badge)

    def _draw_stick(self, stick: StickState, color: int) -> None:
        scale_x, _scale_y = self._scales()
        api = self._api
        center = self._point(stick.center_x, stick.center_y)
        radius_x = stick.radius * scale_x
        if radius_x <= 0:
            return
        api.set_draw_color(self._renderer, _rgba(color))
        # 外周は折れ線。装置座標での太さは 1px（GDI 面の 1px を拡大縮小した
        # 見た目に相当し、縮小時も消えない）。
        ring: list[tuple[float, float, float, float]] = []
        previous = self._circle_point(center, radius_x, 0)
        for index in range(1, _CIRCLE_SEGMENTS + 1):
            current = self._circle_point(center, radius_x, index)
            ring.append((previous[0], previous[1], current[0], current[1]))
            previous = current
        api.draw_segments(self._renderer, ring)
        # ノブは塗り。三角形の扇で近似する。色は頂点へ入れる（描画色は
        # SDL_RenderGeometry では使われない）。
        # 中心は外周円の中心ではなく **ノブ自身の位置**。押下位置に固定して
        # 描くと、ドラッグしても小円が動かない（GDI 面の `_draw_stick` と
        # 同じく knob_x / knob_y を使う）。
        knob = max(1.0, (stick.radius // _KNOB_RADIUS_DIVISOR) * scale_x)
        knob_center = self._point(stick.knob_x, stick.knob_y)
        api.fill_triangles(
            self._renderer, self._disc_triangles(knob_center, knob), _rgba(color)
        )

    def _circle_point(
        self, center: tuple[float, float], radius: float, index: int
    ) -> tuple[float, float]:
        angle = 2.0 * np.pi * index / _CIRCLE_SEGMENTS
        return (
            center[0] + radius * float(np.cos(angle)),
            center[1] + radius * float(np.sin(angle)),
        )

    def _disc_triangles(
        self, center: tuple[float, float], radius: float
    ) -> list[tuple[float, float]]:
        vertices: list[tuple[float, float]] = []
        previous = self._circle_point(center, radius, 0)
        for index in range(1, _CIRCLE_SEGMENTS + 1):
            current = self._circle_point(center, radius, index)
            vertices.extend((center, previous, current))
            previous = current
        return vertices

    def _outline(
        self,
        rect: RectState,
        color: int,
        thickness: int,
    ) -> None:
        """矩形の輪郭を 4 本の塗り矩形で描く。"""
        x0, y0 = self._point(rect.x0, rect.y0)
        x1, y1 = self._point(rect.x1 + 1, rect.y1 + 1)
        left, top = int(round(min(x0, x1))), int(round(min(y0, y1)))
        right, bottom = int(round(max(x0, x1))), int(round(max(y0, y1)))
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return
        # 太さはキャプチャ座標の値。GDI 面が縮小されるのと同じ見た目になるよう
        # 倍率を掛ける（縮小時に消えないよう下限 1px）。
        t = max(1, int(round(thickness * self._scales()[0])))
        self._api.set_draw_color(self._renderer, _rgba(color))
        self._api.fill_rects(
            self._renderer,
            [
                (left, top, width, min(t, height)),
                (left, bottom - min(t, height), width, min(t, height)),
                (left, top, min(t, width), height),
                (right - min(t, width), top, min(t, width), height),
            ],
        )

    def _draw_rect_dashed(self, rect: RectState, color: int) -> None:
        """破線の矩形。SDL に破線は無いので装置座標で刻む。"""
        x0, y0 = self._point(rect.x0, rect.y0)
        x1, y1 = self._point(rect.x1 + 1, rect.y1 + 1)
        left, top = int(round(min(x0, x1))), int(round(min(y0, y1)))
        right, bottom = int(round(max(x0, x1))), int(round(max(y0, y1)))
        if right <= left or bottom <= top:
            return
        segments: list[tuple[float, float, float, float]] = []
        for a, b in ((left, right), (top, bottom)):
            for start in range(a, b, _DASH_PX * 2):
                end = min(start + _DASH_PX, b)
                segments.append((start, top, end, top))
                segments.append((start, bottom, end, bottom))
        for a, b in ((top, bottom),):
            for start in range(a, b, _DASH_PX * 2):
                end = min(start + _DASH_PX, b)
                segments.append((left, start, left, end))
                segments.append((right, start, right, end))
        self._api.set_draw_color(self._renderer, _rgba(color))
        self._api.draw_segments(self._renderer, segments)

    def _draw_img_rect(self, img_rect: ImgRectState) -> None:
        self._outline(img_rect.outer, _WHITE, _OUTER_STROKE_PX)
        self._outline(img_rect.inner, img_rect.color, _INNER_STROKE_PX)

    def _draw_badge(self, badge: BadgeState) -> None:
        """左上の状態表示。文字は変化時だけ焼く（毎フレーム描かない）。"""
        texture = self._badge_texture(badge)
        if texture is None:
            return
        handle, width, height = texture
        pad = _BADGE_PAD_PX
        left = self._dest[0] + pad
        top = self._dest[1] + pad
        self._api.draw_texture(self._renderer, handle, (left, top, width, height))

    def _badge_texture(self, badge: BadgeState) -> tuple[int, int, int] | None:
        key = (badge.text, badge.background, badge.foreground, _BADGE_FONT_PX)
        cached = self._badge_textures.get(key)
        if cached is not None:
            return cached
        image = _render_badge(badge)
        if image is None:
            return None
        height, width = image.shape[0], image.shape[1]
        handle = self._api.create_text_texture(
            self._renderer, (width, height), int(image.ctypes.data), width * 4
        )
        if not handle:
            return None
        self._badge_textures[key] = (handle, width, height)
        self._badge_order.append(key)
        while len(self._badge_order) > _BADGE_CACHE_SIZE:
            old = self._badge_order.pop(0)
            entry = self._badge_textures.pop(old, None)
            if entry is not None:
                self._api.destroy_texture(entry[0])
        return self._badge_textures[key]


def _render_badge(badge: BadgeState) -> Any:
    """バッジを 1 枚の RGBA に焼く。PIL を使うのはここだけ。

    毎フレームではない。文字が変わった瞬間に 1 枚作り、以後はテクスチャを
    使い回す。映像の経路に PIL は入らない。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    font = _font(_BADGE_FONT_PX)
    pad = _BADGE_PAD_PX
    probe = Image.new("RGBA", (1, 1))
    measure = ImageDraw.Draw(probe)
    try:
        box = measure.textbbox((0, 0), badge.text, font=font)
    except Exception:
        return None
    text_w = int(round(box[2] - box[0]))
    text_h = int(round(box[3] - box[1]))
    if text_w <= 0 or text_h <= 0:
        return None
    image = Image.new(
        "RGBA", (text_w + pad * 4, text_h + pad * 4), _rgba(badge.background)
    )
    draw = ImageDraw.Draw(image)
    draw.text(
        (pad * 2 - box[0], pad * 2 - box[1]),
        badge.text,
        font=font,
        fill=_rgba(badge.foreground),
    )
    return np.asarray(image, dtype=np.uint8)
