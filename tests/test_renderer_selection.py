"""RED contract for the preview-renderer selection seam (wave 2, task TC).

The factory that picks a preview surface (:func:`_create_preview_surface`) is the
one place that knows which backend a ``settings.ini`` ``renderer`` value means.
Today it hardcodes ``os.name == "nt"`` and there is no seam at all: the setting
that ``config.py`` already normalises against ``WindowUtils.RENDERER_VALUES``
reaches nothing. These tests pin the whole branch table before the seam exists:

===================  ==========  ==========================================
platform             ``renderer``  expected surface
===================  ==========  ==========================================
``nt``               ``auto``     ``SdlSurface`` (GPU)
``nt``               ``sdl``      ``SdlSurface``
``nt``               ``gdi``      ``GdiSurface``
``nt``               ``photo``    ``PhotoImageSurface``
``nt``               a typo       the default (``SdlSurface``) + a warning naming it
not ``nt``           ``auto``     ``SdlSurface`` (OpenGL)
not ``nt``           ``sdl``      ``SdlSurface``
not ``nt``           ``photo``    ``PhotoImageSurface``
not ``nt``           ``gdi``      ``PhotoImageSurface`` + a warning
===================  ==========  ==========================================

``auto`` now starts at the GPU surface (SDL: D3D11 on Windows, OpenGL on
Linux) because the whole point of the change is that the preview no longer
copies pixels through GDI. The lower rows are the load-bearing ones: an
explicit ``gdi`` on a platform with no GDI has to *try* and fall back, because
``CtypesGdiApi.__init__`` raises ``OSError("GDI surface は Windows 専用")``
(``core/gdi_surface.py:278``) rather than degrading on its own. An ``OSError``
escaping the factory would abort startup on mac/Linux, which the repo's own
rule forbids ("fail gracefully without crashing").

Two failure stages are pinned separately: *construction* (no SDL2 shared
library, no GDI) is handled inside the factory, and *attach* (the surface was
built but cannot be connected -- no GPU, no child window) is handled by
``CaptureArea._attach_preview_surface``, which drops exactly one step.

Validity is read from ``WindowUtils.RENDERER_VALUES`` -- the single authored
table, already duplicated-with-a-comment in ``config.py`` -- so this file must
not carry its own list of names. Every test that asserts a specific backend also
asserts the platform it pretended to be running on, so a patch that quietly
stopped reading ``os.name`` fails instead of passing by accident.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

#: The two ``os.name`` values the branch table is written against.
WINDOWS = "nt"
POSIX = "posix"

#: A value that is in no candidate list. Deliberately not a near-miss of "gdi":
#: the contract is "anything outside the table", so the test must not accidentally
#: pass because a prefix check would have accepted it.
TYPO = "gd1"


def _gui_assets() -> Any:
    """Import ``GuiAssets`` at call time so a missing seam is a per-test RED."""
    import GuiAssets as module

    return module


def _create(host: Any, renderer: str) -> Any:
    """Build a surface through the real factory with a real backend."""
    return _gui_assets()._create_preview_surface(host, renderer)


#: The two backends, named by their module and class. Spelled as dotted names
#: rather than compared with ``isinstance`` against a freshly imported class:
#: ``test_gdi_surface_contract.py`` calls ``importlib.reload`` on
#: ``core.gdi_surface``, so after it has run, ``from core.gdi_surface import
#: GdiSurface`` yields a *second* class object and ``isinstance`` would report
#: False for a surface that really is the GDI one. The backend's identity is the
#: contract; which class object this process happens to hold is a fixture leak.
SDL_BACKEND = "core.sdl_surface.SdlSurface"
GDI_BACKEND = "core.gdi_surface.GdiSurface"
PHOTO_BACKEND = "ui.photo_surface.PhotoImageSurface"


def _backend_of(surface: Any) -> str:
    """``module.QualName`` of the concrete type a factory call produced."""
    cls = type(surface)
    return f"{cls.__module__}.{cls.__qualname__}"


def _gdi_class() -> type[Any]:
    """The real GDI surface, imported lazily so a rename is a per-test RED."""
    from core.gdi_surface import GdiSurface

    return GdiSurface


class _Host:
    """A stand-in for the widget. ``attach`` is what needs it, not the ctor."""


@contextmanager
def _pretending(monkeypatch: pytest.MonkeyPatch, os_name: str) -> Iterator[None]:
    """Run the block with ``os.name`` reporting ``os_name``.

    The factory branches on ``os.name`` read at call time, so this is the only
    way to exercise the non-Windows half of the table on a Windows checkout --
    and, symmetrically, the Windows half on a mac/Linux one.
    """
    monkeypatch.setattr(os, "name", os_name)
    yield


@dataclass(frozen=True, slots=True)
class _Logged:
    """One captured log line, level included so a warning cannot pass as info."""

    level: str
    message: str


@contextmanager
def _captured(level: str = "INFO") -> Iterator[list[_Logged]]:
    """Collect loguru lines at ``level`` and above, then detach the sink.

    ``record["message"]`` rather than ``str(message)``: the string form carries a
    timestamp and a ``module:function:line`` prefix, and these assertions are
    about which value a line names.
    """
    from loguru import logger

    lines: list[_Logged] = []
    handler = logger.add(
        lambda message: lines.append(
            _Logged(message.record["level"].name, str(message.record["message"]))
        ),
        level=level,
    )
    try:
        yield lines
    finally:
        logger.remove(handler)


def _warnings(lines: list[_Logged]) -> list[str]:
    return [line.message for line in lines if line.level == "WARNING"]


# ===========================================================================
# nt: the GPU surface is the default, and GDI stays reachable by name
# ===========================================================================


@pytest.mark.parametrize("renderer", ["auto", "sdl"])
def test_windows_selects_the_gpu_surface(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    # Given: a process reporting Windows.
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: a surface is built with that name.
        surface = _create(_Host(), renderer)

    # Then: the SDL surface, which presents through D3D11 on this platform.
    assert _backend_of(surface) == SDL_BACKEND


def test_windows_gdi_is_still_reachable_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting Windows.
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: "gdi" is asked for explicitly (CPU fallback for a machine whose
        # GPU path is broken).
        surface = _create(_Host(), "gdi")

    # Then: the GDI child window, because an explicit name must not be a
    # synonym for "the platform default".
    assert _backend_of(surface) == GDI_BACKEND


def test_windows_photo_selects_the_photo_image_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting Windows and a host object.
    host = _Host()
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: "photo" is asked for on a platform that has GDI.
        surface = _create(host, "photo")

    # Then: the Canvas implementation, i.e. the override is honoured rather than
    # collapsed into the Windows default.
    assert _backend_of(surface) == PHOTO_BACKEND

    # Then: and the host is what the Canvas will be parented to, so a selection
    # that dropped it would build a surface nothing can attach.
    assert vars(surface)["_host"] is host


def test_windows_unknown_name_falls_back_to_the_platform_default_and_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting Windows.
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: a name outside the candidate table is passed.
        with _captured() as lines:
            surface = _create(_Host(), TYPO)

    # Then: the platform default, so a mistyped setting still starts up.
    assert _backend_of(surface) == SDL_BACKEND

    # Then: and the warning quotes the offending name, because "the renderer
    # setting is wrong" is only actionable if the line says which value was read.
    assert any(TYPO in message for message in _warnings(lines)), (
        f"an invalid renderer was corrected without naming it; captured: {lines!r}"
    )


# ===========================================================================
# not nt: no GDI exists, and the same protocol is met by the Canvas backend
# ===========================================================================


@pytest.mark.parametrize("renderer", ["auto", "sdl"])
def test_non_windows_selects_the_gpu_surface(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    # Given: a process reporting a platform with no GDI.
    with _pretending(monkeypatch, POSIX):
        assert os.name != WINDOWS, "the platform pre-condition is not in effect"

        # When: a surface is built.
        surface = _create(_Host(), renderer)

    # Then: the same SDL surface -- the drawing calls are shared, and SDL picks
    # OpenGL on Linux (the point of choosing SDL over a Windows-only API).
    assert _backend_of(surface) == SDL_BACKEND


def test_non_windows_photo_selects_the_photo_image_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting a platform with no GDI.
    with _pretending(monkeypatch, POSIX):
        assert os.name != WINDOWS, "the platform pre-condition is not in effect"

        # When: the Canvas surface is asked for by name.
        surface = _create(_Host(), "photo")

    # Then: the Canvas implementation, which is the last resort that always works.
    assert _backend_of(surface) == PHOTO_BACKEND


def test_non_windows_gdi_falls_back_and_warns_when_the_gdi_api_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting a platform with no GDI.
    with _pretending(monkeypatch, POSIX):
        assert os.name != WINDOWS, "the platform pre-condition is not in effect"

        # And: the fixture really has no GDI, so a passing test below can only
        # be explained by the factory catching the refusal.
        with pytest.raises(OSError):
            _gdi_class()()

        # When: "gdi" is asked for by name on that platform.
        with _captured() as lines:
            surface = _create(_Host(), "gdi")

    # Then: the Canvas implementation, and no OSError escapes -- an uncaught one
    # would abort startup on mac/Linux.
    assert _backend_of(surface) == PHOTO_BACKEND

    # Then: and the fallback is announced, so "I asked for GDI and got the
    # canvas" is visible in the log rather than only in the frame rate.
    assert _warnings(lines), (
        f"the GDI refusal was swallowed silently; captured: {lines!r}"
    )


# ===========================================================================
# The choice is reported once, and the constructor forwards the value
# ===========================================================================


def test_the_chosen_surface_is_logged_exactly_once_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting Windows, so no fallback warning is mixed in and
    # the count below can only be the construction line.
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: one surface is built.
        with _captured() as lines:
            surface = _create(_Host(), "photo")

    # Then: the chosen surface is named, once. Which backend a run got is the
    # first question asked of a "no video" report, and a line that repeats per
    # frame would be unusable.
    chosen = type(surface).__name__
    naming = [line for line in lines if chosen in line.message]
    assert naming, f"nothing logged which surface was chosen; captured: {lines!r}"
    assert len(naming) == 1, (
        f"the chosen surface is logged {len(naming)} times: {lines!r}"
    )


def _factory_call_in_init() -> ast.Call:
    """The single ``_create_preview_surface(...)`` call the widget reaches.

    ``__init__`` hands the setting to ``_attach_preview_surface``, which is where
    the factory is called (it also owns the one-step fallback when the chosen
    surface cannot be connected). Both nodes are searched so a further move does
    not silently make this contract vacuous.
    """
    from gdi_source_readers import (
        GUI_ASSETS_SOURCE,
        class_method,
        class_node,
        module_tree,
    )

    capture = class_node(module_tree(GUI_ASSETS_SOURCE), "CaptureArea")
    calls: list[ast.Call] = []
    for method in ("__init__", "_attach_preview_surface"):
        calls.extend(
            call
            for call in ast.walk(class_method(capture, method))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_create_preview_surface"
        )
    assert len(calls) == 1, f"the widget builds {len(calls)} surfaces; expected 1"
    return calls[0]


def _renderer_argument(call: ast.Call) -> ast.expr | None:
    """The value node the call passes in the factory's ``renderer`` slot."""
    if len(call.args) >= 2:
        return call.args[1]
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == "renderer"), None
    )


def test_capture_area_forwards_its_renderer_argument_to_the_factory() -> None:
    # Given: the real CaptureArea __init__ source. A tk.Frame cannot be built
    # without a display and __init__ is the only caller of the factory, so the
    # wiring is read from the parsed file.
    call = _factory_call_in_init()

    # When: the value handed to the factory's renderer slot is located.
    node = _renderer_argument(call)

    # Then: it is the constructor's own value, not a literal. A hardcoded "auto"
    # here would leave the kwarg dead -- the signature would accept the setting
    # and quietly ignore it, which is the bug this seam exists to prevent.
    assert isinstance(node, ast.Name), (
        "__init__ does not forward its renderer argument to the factory; it passes "
        f"{ast.unparse(call)!r}"
    )


def test_non_windows_gdi_maps_to_photo_without_constructing_gdi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a process reporting a platform with no GDI, and a GDI surface
    # class that explodes if touched: mapping must happen by platform check,
    # not by attempting construction and catching the refusal.
    module = _gui_assets()

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("GdiSurface must not be constructed off-Windows")

    monkeypatch.setattr(module, "GdiSurface", _forbidden)
    with _pretending(monkeypatch, POSIX):
        assert os.name != WINDOWS, "the platform pre-condition is not in effect"

        # When: "gdi" is asked for by name on that platform.
        surface = _create(_Host(), "gdi")

    # Then: the Canvas implementation, reached without constructing GDI.
    assert _backend_of(surface) == PHOTO_BACKEND


# ===========================================================================
# A missing GPU is a warning, never a failed startup
# ===========================================================================


def _forbid_sdl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``SdlSurface()`` raise ``OSError`` exactly like a missing library."""
    from core import sdl_surface

    class _Missing:
        def __init__(self) -> None:
            raise OSError("SDL2 の共有ライブラリが見つかりません: test")

    monkeypatch.setattr(sdl_surface, "SdlSurface", _Missing)


def test_windows_without_sdl_falls_back_to_gdi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Windows and no loadable SDL2.
    _forbid_sdl(monkeypatch)
    with _pretending(monkeypatch, WINDOWS):
        assert os.name == WINDOWS, "the platform pre-condition is not in effect"

        # When: the default surface is built.
        with _captured() as lines:
            surface = _create(_Host(), "auto")

    # Then: the GDI child window keeps the app usable.
    assert _backend_of(surface) == GDI_BACKEND

    # Then: and the reason is in the log rather than only in the frame rate.
    assert _warnings(lines), f"the SDL refusal was silent; captured: {lines!r}"


def test_non_windows_without_sdl_falls_back_to_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a platform with neither SDL nor GDI available.
    _forbid_sdl(monkeypatch)
    with _pretending(monkeypatch, POSIX):
        assert os.name != WINDOWS, "the platform pre-condition is not in effect"

        # When: the default surface is built.
        surface = _create(_Host(), "auto")

    # Then: the Canvas surface, which always works, is the last resort.
    assert _backend_of(surface) == PHOTO_BACKEND


class _FakeSurface:
    """Records the two calls the fallback path makes: ``attach`` and ``release``."""

    def __init__(self, attached: bool) -> None:
        self.attached = attached
        self.attach_calls: list[tuple[int, tuple[int, int]]] = []
        self.released = False

    def attach(self, parent_hwnd: int, size: tuple[int, int]) -> None:
        self.attach_calls.append((parent_hwnd, size))

    def release(self) -> None:
        self.released = True


class _AreaStub:
    """Only what ``_attach_preview_surface`` reads from ``self``."""

    show_size = (640, 360)

    def winfo_id(self) -> int:
        return 0


def _attach_with(
    monkeypatch: pytest.MonkeyPatch, broken: _FakeSurface
) -> tuple[Any, list[str]]:
    """Run the real ``_attach_preview_surface`` with a broken primary surface."""
    module = _gui_assets()
    built: list[str] = []
    good = _FakeSurface(attached=True)

    monkeypatch.setattr(
        module, "_create_preview_surface", lambda host, renderer: broken
    )

    def _build(host: Any, name: str) -> Any:
        built.append(name)
        return good

    monkeypatch.setattr(module, "_build_named_surface", _build)
    surface = module.CaptureArea._attach_preview_surface(_AreaStub(), "auto")
    return surface, built


def test_an_unattachable_surface_falls_back_one_step_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a surface that was constructed but cannot be connected (no GPU).
    broken = _FakeSurface(attached=False)
    with _pretending(monkeypatch, WINDOWS):
        # When: the real attach path runs.
        surface, built = _attach_with(monkeypatch, broken)

    # Then: the broken surface is released and the GDI surface is attached.
    assert broken.released, "the unattachable surface leaked its resources"
    assert built == ["gdi"], f"attached {built!r}; expected one step down to gdi"
    assert surface.attach_calls == [(0, (640, 360))], surface.attach_calls


def test_an_unattachable_surface_falls_back_to_photo_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the same broken surface on a platform with no GDI.
    broken = _FakeSurface(attached=False)
    with _pretending(monkeypatch, POSIX):
        # When: the real attach path runs.
        surface, built = _attach_with(monkeypatch, broken)

    # Then: the Canvas surface is the only remaining step.
    assert broken.released, "the unattachable surface leaked its resources"
    assert built == ["photo"], f"attached {built!r}; expected photo"
    assert surface.attach_calls == [(0, (640, 360))], surface.attach_calls


def test_an_attached_surface_is_kept_without_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a surface that attaches fine.
    working = _FakeSurface(attached=True)
    module = _gui_assets()
    monkeypatch.setattr(
        module, "_create_preview_surface", lambda host, renderer: working
    )

    def _unexpected(host: Any, name: str) -> Any:
        raise AssertionError("a working surface must not be replaced")

    monkeypatch.setattr(module, "_build_named_surface", _unexpected)

    # When: the real attach path runs.
    surface = module.CaptureArea._attach_preview_surface(_AreaStub(), "auto")

    # Then: the same object comes back, attached once.
    assert surface is working
    assert working.attach_calls == [(0, (640, 360))]
    assert not working.released
