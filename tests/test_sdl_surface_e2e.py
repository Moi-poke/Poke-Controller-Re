"""Opt-in real-screen check for the SDL surface's window geometry.

The contract tests inject a fake ``SdlSurfaceApi``, so they cannot see the one
thing that broke the live preview: ``SDL_SetWindowSize`` returns success but does
**not** resize a window wrapped by ``SDL_CreateWindowFrom``. The surface therefore
has to resize the child HWND itself. Only a real window can prove that, so this
file is gated like ``test_renderer_e2e.py`` and skipped by default:

    set POKECON_RUN_SDL_E2E=1

What it pins:

* ``resize`` changes the actual client size of the SDL window (the video and the
  overlay are drawn in that space; if it stays 640x360, the picture stops growing
  with the pane and the stick circle lands somewhere else);
* a frame can be composed and presented at the new size (the swapchain follows
  the window rather than the size it was created with).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

GATE_ENV = "POKECON_RUN_SDL_E2E"
WINDOWS = os.name == "nt"


def _gated() -> None:
    if os.environ.get(GATE_ENV) != "1":
        pytest.skip(
            f"real-screen SDL check; set {GATE_ENV}=1 on a Windows desktop to run it"
        )
    if not WINDOWS:
        pytest.skip("the child-window path under test is Windows-only")


def test_resize_grows_the_real_sdl_client_area() -> None:
    _gated()
    import tkinter as tk

    from core.sdl_surface import SdlSurface

    root = tk.Tk()
    root.geometry("1000x700+80+80")
    frame = tk.Frame(root, bg="gray20")
    frame.pack(fill="both", expand=True)
    root.update()
    surface = SdlSurface()
    try:
        surface.attach(int(frame.winfo_id()), (640, 360))
        assert surface.attached, "the GPU surface could not be connected"
        assert surface.client_size() == (640, 360)

        surface.resize((900, 500))
        root.update()

        # The bug: SDL reported success but the HWND kept the old size, so this
        # stayed at 640x360 while the widget grew.
        assert surface.client_size() == (900, 500)
    finally:
        surface.release()
        root.destroy()


def test_a_frame_and_its_overlay_can_be_presented_at_the_new_size() -> None:
    _gated()
    import tkinter as tk

    from core.preview_renderer import OverlayState, StickState
    from core.sdl_surface import SdlSurface

    root = tk.Tk()
    root.geometry("1000x700+80+80")
    frame = tk.Frame(root, bg="gray20")
    frame.pack(fill="both", expand=True)
    root.update()
    surface = SdlSurface()
    try:
        surface.attach(int(frame.winfo_id()), (640, 360))
        assert surface.attached
        surface.resize((900, 500))
        root.update()

        picture = np.zeros((720, 1280, 3), np.uint8)
        picture[:, :, 1] = 180
        overlay = OverlayState(
            left_stick=StickState(active=True, center_x=640, center_y=360, radius=120)
        )
        composed = surface.compose(picture, overlay)
        presented = surface.present()

        assert composed.ok, composed.detail
        assert presented.ok, presented.detail
    finally:
        surface.release()
        root.destroy()
