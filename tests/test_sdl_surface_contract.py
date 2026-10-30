"""Contract for the GPU preview surface (``core/sdl_surface.py``).

The SDL surface is the new default preview renderer. It is exercised here with
an injected ``SdlSurfaceApi`` double, so no GPU, display or SDL2 library is
needed. What is pinned is everything the widget's draw loop depends on:

* ``attach`` builds exactly one window, one renderer and one texture, and turns
  vsync off (the Tk tick, not the compositor, owns the cadence);
* a failure at any step is reported through ``attached`` and never raises,
  because ``CaptureArea`` uses that flag to fall back to GDI / PhotoImage;
* ``compose`` uploads the frame as BGRA with a tight pitch into one reusable
  buffer -- no per-frame allocation, no PIL, no ``ImageTk``;
* a frame whose size differs from the previous one gets a new texture, so a
  1080p camera works even though the app's default capture is 1280x720;
* ``present`` clears, draws the video, then the overlay inside the video rect,
  then presents; ``recompose`` never re-uploads the video;
* an empty ``OverlayState`` issues no draw calls at all, and every colour is
  converted from Win32 ``COLORREF`` (0x00BBGGRR) to (r, g, b, a) exactly once.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from core.coordinates import fit_rect
from core.preview_renderer import (
    BadgeState,
    ImgRectState,
    OverlayState,
    RectState,
    RenderResult,
    StickState,
)
from core.sdl_surface import SdlSurface

CAPTURE = (1280, 720)
VIEWPORT = (640, 360)


class FakeSdlApi:
    """Records every SDL call the surface makes.

    ``textures`` counts creations so a test can say whether a frame of a new
    size caused one more allocation or reused the existing texture.
    """

    def __init__(
        self,
        *,
        window: int = 11,
        renderer: int = 21,
        video_ok: bool = True,
        output: tuple[int, int] = VIEWPORT,
    ) -> None:
        self.calls: list[Any] = []
        self.window = window
        self.renderer = renderer
        self.video_ok = video_ok
        self.output = output
        self.next_texture = 100
        self.textures: dict[int, tuple[int, int]] = {}
        self.texture_data: dict[int, Any] = {}
        self.upload_pitches: list[int] = []
        self.upload_pointers: list[int] = []
        self.draw_colors: list[tuple[int, int, int, int]] = []
        self.color = (0, 0, 0, 0)
        # (kind, payload, rect) — payload is a colour, a texture id or a count.
        self.shapes: list[tuple[str, Any, Any]] = []
        self.released: list[str] = []

    # -- lifecycle ---------------------------------------------------
    def init_video(self) -> bool:
        self.calls.append("init_video")
        return self.video_ok

    def create_embedded_window(self, host: int, size: tuple[int, int]) -> int:
        self.calls.append(("window", host, size))
        return self.window if self.video_ok else 0

    def resize_window(self, window: int, size: tuple[int, int]) -> None:
        self.output = size

    def create_renderer(self, window: int) -> int:
        self.calls.append(("renderer", window))
        return self.renderer

    def renderer_name(self, renderer: int) -> str:
        return "direct3d11"

    def output_size(self, renderer: int) -> tuple[int, int]:
        return self.output

    def set_vsync(self, renderer: int, on: bool) -> None:
        self.calls.append(("vsync", renderer, on))

    def destroy_renderer(self, renderer: int) -> None:
        self.released.append("renderer")

    def destroy_window(self, window: int) -> None:
        self.released.append("window")

    # -- textures ----------------------------------------------------
    def create_texture(self, renderer: int, size: tuple[int, int]) -> int:
        handle = self.next_texture
        self.next_texture += 1
        self.textures[handle] = size
        self.calls.append(("texture", handle, size))
        return handle

    def create_text_texture(
        self, renderer: int, size: tuple[int, int], pixels: int, pitch: int
    ) -> int:
        handle = self.create_texture(renderer, size)
        self.calls.append(("text_texture", handle, pixels, pitch))
        return handle

    def destroy_texture(self, texture: int) -> None:
        self.textures.pop(texture, None)
        self.released.append(f"texture:{texture}")

    def update_texture(self, texture: int, pixels: int, pitch: int) -> bool:
        self.upload_pointers.append(pixels)
        self.upload_pitches.append(pitch)
        self.texture_data[texture] = pixels
        return True

    # -- drawing -----------------------------------------------------
    def clear(self, renderer: int, rgba: tuple[int, int, int, int]) -> bool:
        self.shapes.append(("clear", rgba, (0, 0, 0, 0)))
        return True

    def draw_texture(
        self, renderer: int, texture: int, dest: tuple[int, int, int, int]
    ) -> bool:
        self.shapes.append(("texture", texture, dest))
        return True

    def set_draw_color(self, renderer: int, rgba: tuple[int, int, int, int]) -> None:
        self.color = rgba
        self.draw_colors.append(rgba)

    def fill_rects(self, renderer: int, rects: list[tuple[int, int, int, int]]) -> None:
        for rect in rects:
            self.shapes.append(("rect", self.color, rect))

    def draw_segments(
        self, renderer: int, segments: list[tuple[float, float, float, float]]
    ) -> None:
        for segment in segments:
            self.shapes.append(("segment", self.color, segment))

    def fill_triangles(
        self,
        renderer: int,
        vertices: list[tuple[float, float]],
        rgba: tuple[int, int, int, int],
    ) -> None:
        self.shapes.append(("triangles", rgba, vertices))

    def set_clip_rect(
        self, renderer: int, rect: tuple[int, int, int, int] | None
    ) -> None:
        self.shapes.append(("clip", rect, (0, 0, 0, 0)))

    def present(self, renderer: int) -> bool:
        self.shapes.append(("present", renderer, (0, 0, 0, 0)))
        return True


def _attached(**kwargs: Any) -> tuple[SdlSurface, FakeSdlApi]:
    api = FakeSdlApi(**kwargs)
    surface = SdlSurface(api)
    surface.attach(7, VIEWPORT)
    return surface, api


def _frame(width: int = 1280, height: int = 720) -> np.ndarray:
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:, :, 0] = 10
    frame[:, :, 1] = 20
    frame[:, :, 2] = 30
    return frame


def _shape_kinds(api: FakeSdlApi) -> list[str]:
    return [shape[0] for shape in api.shapes]


# ===========================================================================
# attach
# ===========================================================================


def test_attach_builds_one_window_renderer_and_texture_and_turns_vsync_off() -> None:
    surface, api = _attached()

    assert surface.attached is True
    assert surface.self_test().outcome == "renderer_ready"
    assert surface.renderer_name == "direct3d11"
    assert ("window", 7, VIEWPORT) in api.calls
    assert ("renderer", api.window) in api.calls
    assert ("vsync", api.renderer, False) in api.calls
    assert len(api.textures) == 1, api.textures
    assert list(api.textures.values()) == [CAPTURE]
    assert surface.client_size() == VIEWPORT


def test_attach_without_video_reports_not_attached_and_raises_nothing() -> None:
    api = FakeSdlApi(video_ok=False)
    surface = SdlSurface(api)

    surface.attach(7, VIEWPORT)

    assert surface.attached is False
    assert surface.self_test().outcome == "no_renderer"
    assert not any(
        call[0] == "texture" for call in api.calls if isinstance(call, tuple)
    )
    # 窓すら作れていないので、後片付けする物も無い。
    assert api.released == []


def test_attach_destroys_the_window_when_no_renderer_can_be_built() -> None:
    api = FakeSdlApi()
    api.create_renderer = lambda window: 0  # type: ignore[method-assign]
    surface = SdlSurface(api)

    surface.attach(7, VIEWPORT)

    assert surface.attached is False
    assert api.released == ["window"]


# ===========================================================================
# compose
# ===========================================================================


@pytest.mark.parametrize(
    "frame",
    [
        np.zeros((720, 1280), np.uint8),  # 2-D
        np.zeros((720, 1280, 4), np.uint8),  # BGRA
        np.zeros((720, 1280, 3), np.int16),  # wrong depth
    ],
)
def test_compose_rejects_frames_that_are_not_bgr8(frame: np.ndarray) -> None:
    surface, api = _attached()

    result = surface.compose(frame, OverlayState())

    assert result.ok is False
    assert result.detail == "frame_format"
    assert api.upload_pitches == []


def test_compose_rejects_a_frame_that_is_not_contiguous() -> None:
    surface, api = _attached()
    frame = np.zeros((720, 1280, 3), np.uint8)[:, ::-1]

    result = surface.compose(frame, OverlayState())

    assert result.ok is False
    assert result.detail == "frame_not_contiguous"
    assert api.upload_pitches == []


def test_compose_uploads_bgra_with_a_tight_pitch_into_one_reused_buffer() -> None:
    surface, api = _attached()

    first = surface.compose(_frame(), OverlayState())
    second = surface.compose(_frame(), OverlayState())

    assert first.ok and second.ok
    assert api.upload_pitches == [1280 * 4, 1280 * 4]
    # 同じ 1 本の作業バッファへ書き続ける（毎フレーム 3.7MB を確保しない）。
    assert api.upload_pointers[0] == api.upload_pointers[1]

    # 色順は BGR のまま A を足すだけ。ここが入れ替わると赤と青が反転する。
    buffer = surface._bgra
    assert buffer is not None
    assert buffer[0, 0].tolist() == [10, 20, 30, 255]


def test_compose_recreates_the_texture_when_the_frame_size_changes() -> None:
    surface, api = _attached()
    surface.compose(_frame(), OverlayState())
    created = [call[2] for call in api.calls if call[0] == "texture"]

    # 1080p のカメラへ切り替わった（固定 CAPTURE_SIZE だと 1 枚も出なくなる）。
    result = surface.compose(_frame(1920, 1080), OverlayState())

    assert result.ok, result.detail
    assert created == [CAPTURE], created
    created = [call[2] for call in api.calls if call[0] == "texture"]
    assert created == [CAPTURE, (1920, 1080)], created
    assert api.upload_pitches[-1] == 1920 * 4
    # 余白の計算も新しい映像の大きさでやり直している。
    assert surface._dest == fit_rect((1920, 1080), VIEWPORT)


# ===========================================================================
# present / recompose
# ===========================================================================


def test_present_without_a_composed_frame_reports_no_frame() -> None:
    surface, api = _attached()

    result = surface.present()

    assert result.ok is False
    assert result.detail == "no_frame"
    assert "present" not in _shape_kinds(api)


def test_present_clears_then_draws_video_then_overlay_then_presents() -> None:
    surface, api = _attached()
    surface.compose(_frame(), OverlayState())

    result = surface.present()

    assert result.ok
    kinds = _shape_kinds(api)
    assert kinds[0] == "clear"
    assert kinds[1] == "texture"
    assert kinds[-1] == "present"
    assert api.shapes[1][2] == fit_rect(CAPTURE, VIEWPORT)
    # 提示した後の再提示は「新しいフレーム無し」で止まる。
    assert surface.present().detail == "no_frame"


def test_recompose_repaints_the_overlay_without_uploading_again() -> None:
    surface, api = _attached()
    surface.compose(_frame(), OverlayState())
    surface.present()
    uploads = len(api.upload_pitches)

    overlay = OverlayState(left_stick=StickState(active=True, radius=40))
    assert surface.recompose(overlay).ok
    assert surface.present().ok

    assert len(api.upload_pitches) == uploads, "recompose re-uploaded the video"
    assert "segment" in _shape_kinds(api)


def test_recompose_before_any_frame_reports_no_frame() -> None:
    surface, _api = _attached()

    result = surface.recompose(OverlayState())

    assert result.ok is False
    assert result.detail == "no_frame"


# ===========================================================================
# overlay
# ===========================================================================


def test_an_empty_overlay_draws_no_shape_at_all() -> None:
    surface, api = _attached()
    surface.compose(_frame(), OverlayState())

    surface.present()

    drawn = [kind for kind in _shape_kinds(api) if kind not in ("clear", "clip")]
    assert drawn == ["texture", "present"], drawn


def test_sticks_are_drawn_in_their_own_colours() -> None:
    surface, api = _attached()
    overlay = OverlayState(
        left_stick=StickState(active=True, center_x=300, center_y=300, radius=60),
        right_stick=StickState(active=True, center_x=900, center_y=300, radius=60),
    )
    surface.compose(_frame(), overlay)

    surface.present()

    colors = [
        color for kind, color, _ in api.shapes if kind in ("segment", "triangles")
    ]
    # cyan (0x00FFFF00) と red (0x000000FF)。COLORREF のバイト位置を戻している。
    assert (0, 255, 255, 255) in colors
    assert (255, 0, 0, 255) in colors
    assert _shape_kinds(api).count("triangles") == 2, "each stick has one knob"


def test_the_knob_follows_the_stick_state_instead_of_staying_at_the_centre() -> None:
    surface, api = _attached()
    stick = StickState(
        active=True, center_x=300, center_y=300, radius=100, knob_x=420, knob_y=250
    )
    surface.compose(_frame(), OverlayState(left_stick=stick))

    surface.present()

    vertices = next(
        payload for kind, _color, payload in api.shapes if kind == "triangles"
    )
    # 扇の最初の頂点が円の中心。ここが押下位置のままだと、ドラッグしても
    # 小円が動かない（実測でそうなっていた）。
    center = vertices[0]
    expected = surface._point(stick.knob_x, stick.knob_y)
    assert abs(center[0] - expected[0]) <= 1.0, (center, expected)
    assert abs(center[1] - expected[1]) <= 1.0, (center, expected)
    ring_center = surface._point(stick.center_x, stick.center_y)
    assert center != ring_center, "the knob is still pinned to the ring centre"


def test_the_image_rect_draws_a_white_outer_border_then_the_colour_inner() -> None:
    surface, api = _attached()
    overlay = OverlayState(
        img_rect=ImgRectState(
            outer=RectState(x0=10, y0=10, x1=110, y1=60, visible=True),
            inner=RectState(x0=12, y0=12, x1=108, y1=58, visible=True),
            visible=True,
            color=0x0000FF00,  # green
        )
    )
    surface.compose(_frame(), overlay)

    surface.present()

    rects = [(color, rect) for kind, color, rect in api.shapes if kind == "rect"]
    assert rects, "nothing was drawn for a visible ImgRect"
    # 外枠（白）が先、内枠（認識色）が後。内枠は外枠の内側に収まる。
    assert rects[0][0] == (255, 255, 255, 255)
    assert any(color == (0, 255, 0, 255) for color, _rect in rects)
    outer = rects[0][1]
    inner = next(rect for color, rect in rects if color == (0, 255, 0, 255))
    assert inner[0] >= outer[0] and inner[1] >= outer[1]


def test_the_badge_is_rasterised_once_per_text_and_reused() -> None:
    surface, api = _attached()
    badge = BadgeState(text="slot 1", visible=True)
    overlay = OverlayState(badge=badge)
    surface.compose(_frame(), overlay)

    surface.present()
    surface.recompose(overlay)
    surface.present()

    text_textures = [call for call in api.calls if call[0] == "text_texture"]
    assert len(text_textures) == 1, text_textures
    # 2 回目も同じテクスチャを置くだけ（毎フレーム焼き直さない）。
    video = fit_rect(CAPTURE, VIEWPORT)
    draws = [rect for kind, _tex, rect in api.shapes if kind == "texture"]
    assert draws.count(video) == 2, draws
    assert len(draws) == 4, draws


def test_the_badge_is_drawn_inside_the_video_rect_not_at_the_window_corner() -> None:
    surface, api = _attached(output=(1000, 1000))
    surface.attach(7, (1000, 1000))
    surface.compose(_frame(), OverlayState(badge=BadgeState(text="x", visible=True)))

    surface.present()

    dest = fit_rect(CAPTURE, (1000, 1000))
    badge_rect = [
        rect for kind, tex, rect in api.shapes if kind == "texture" and tex != 100
    ]
    assert badge_rect, "the badge texture was never placed"
    assert badge_rect[0][0] >= dest[0], badge_rect[0]
    assert badge_rect[0][1] >= dest[1], badge_rect[0]


# ===========================================================================
# resize / release
# ===========================================================================


def test_resize_updates_the_window_and_the_letterbox() -> None:
    surface, api = _attached()

    surface.resize((1000, 1000))

    assert api.output == (1000, 1000)
    assert surface.client_size() == (1000, 1000)
    assert surface._dest == fit_rect(CAPTURE, (1000, 1000))


def test_resize_ignores_the_one_pixel_configure_tk_sends_before_layout() -> None:
    surface, api = _attached()
    api.calls.clear()

    surface.resize((1, 1))

    assert api.output == VIEWPORT, "a 1x1 surface was pushed to the window"
    assert surface.client_size() == VIEWPORT


def test_release_destroys_textures_renderer_and_window() -> None:
    surface, api = _attached()
    surface.compose(_frame(), OverlayState(badge=BadgeState(text="x", visible=True)))
    surface.present()
    texture_ids = set(api.textures)

    surface.release()

    assert surface.attached is False
    destroyed = {name for name in api.released if name.startswith("texture:")}
    assert destroyed == {f"texture:{texture}" for texture in texture_ids}
    assert api.released[-2:] == ["renderer", "window"]


def test_render_results_are_never_raised() -> None:
    surface = SdlSurface(FakeSdlApi(video_ok=False))
    surface.attach(7, VIEWPORT)

    for call in (
        lambda: surface.compose(_frame(), OverlayState()),
        lambda: surface.recompose(OverlayState()),
        surface.present,
    ):
        result = call()
        assert isinstance(result, RenderResult)
        assert result.ok is False
        assert result.detail == "no_hwnd"
