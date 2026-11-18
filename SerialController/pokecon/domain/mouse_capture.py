#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mouse_capture.py - Windows Raw Inputでマウスの相対移動を取る（Tkなし）。

WM_INPUT を専用スレッドの非表示ウィンドウで受け、生の移動量
（画面座標・ポインタ加速の影響を受けない count）をコールバックへ渡す。
``RIDEV_INPUTSINK`` でフォーカス外でも受け取れるが、このアプリでは
「PokeConが前面にある間だけ」を前提にし、フォーカスが外れたら
自動で解除する（カーソルの ClipCursor もフォーカス変更で外れる）。

キャプチャ中:
    ・カーソルをPokeConのクライアント領域内へ閉じ込める（ClipCursor）
    ・解除はグローバルホットキー（既定 Ctrl+Alt+M）
    ・フォーカス喪失でも自動解除

Windows以外では ``mouse_capture_available()`` が False を返し、
``start`` は何もせず False を返す（例外は投げない）。

ctypesの構造体は64bit/32bit両方でアラインメントが合うよう、
wintypesの型から組む。WM_INPUTの生データは GetRawInputData を
2回呼んで必要なサイズを先に取る。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, Final

IS_WINDOWS: Final[bool] = os.name == "nt"

WM_INPUT: Final[int] = 0x00FF
WM_HOTKEY: Final[int] = 0x0312
WM_TIMER: Final[int] = 0x0113
WM_CLOSE: Final[int] = 0x0010
WM_DESTROY: Final[int] = 0x0002
WM_QUIT: Final[int] = 0x0012

RID_INPUT: Final[int] = 0x10000003
RIM_TYPEMOUSE: Final[int] = 0
MOUSE_MOVE_ABSOLUTE: Final[int] = 0x0001
RIDEV_INPUTSINK: Final[int] = 0x00000100

GA_ROOT: Final[int] = 2
HOTKEY_ID: Final[int] = 1
RESET_HOTKEY_ID: Final[int] = 2
FOCUS_TIMER_ID: Final[int] = 1
FOCUS_TIMER_MS: Final[int] = 200
MOD_NOREPEAT: Final[int] = 0x4000
VK_RBUTTON: Final[int] = 0x02
VK_SHIFT: Final[int] = 0x10

_MOD_NAMES: Final[dict[str, int]] = {
    "ctrl": 0x0002,
    "control": 0x0002,
    "alt": 0x0001,
    "shift": 0x0004,
    "win": 0x0008,
    "windows": 0x0008,
    "meta": 0x0008,
}
_KEY_NAMES: Final[dict[str, int]] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
}


class RAWINPUTDEVICE(ctypes.Structure):
    """RAWINPUTDEVICE。登録するデバイスの種類と受け取り先。"""

    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    """RAWINPUTHEADER。dwType=0 がマウス。"""

    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RawMouseButtons(ctypes.Union):
    _fields_ = [
        ("ulButtons", wintypes.ULONG),
        ("usButtonFlags", wintypes.USHORT),
        ("usButtonData", wintypes.USHORT),
    ]


class RAWMOUSE(ctypes.Structure):
    """RAWMOUSE。lLastX/Y が相対移動量。"""

    _anonymous_ = ("u",)
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("u", _RawMouseButtons),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    """RAWINPUT（マウスだけを見るための最小形）。"""

    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_WNDPROC: Any = (
    ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    if IS_WINDOWS
    else None
)

if IS_WINDOWS:
    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _user32 = None
    _kernel32 = None

#: 生きているウィンドウのハンドラ。WndProcはHWNDから所有者を引く。
_windows: dict[int, RawMouseCapture] = {}
_windows_lock = threading.Lock()


def mouse_capture_available() -> bool:
    """この環境でRaw Inputのキャプチャを使えるか。"""
    return IS_WINDOWS


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """``ctrl+alt+m`` 形式を (modifiers, virtual_key) へ直す。

    修飾キーが無い指定は、通常の打鍵を奪うため受け付けない。
    解釈できない・キーが無い場合は None。
    """
    parts = [part.strip().lower() for part in str(text).split("+")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    mods = 0
    key: int | None = None
    for part in parts:
        modifier = _MOD_NAMES.get(part)
        if modifier is not None:
            mods |= modifier
            continue
        name = _KEY_NAMES.get(part)
        if name is not None:
            if key is not None:
                return None
            key = name
            continue
        if len(part) == 1 and part.isalnum():
            if key is not None:
                return None
            key = ord(part.upper())
            continue
        if part.startswith("f") and part[1:].isdigit():
            number = int(part[1:])
            if 1 <= number <= 24 and key is None:
                key = 0x70 + number - 1
                continue
        return None
    if key is None or mods == 0:
        return None
    return (mods, key)


def _call_log(callback: Callable[[str], None] | None, text: str) -> None:
    """ログcallbackの例外で入力スレッドを止めない。"""
    if callback is None:
        return
    try:
        callback(text)
    except Exception:
        pass


if IS_WINDOWS:
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE),
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.RegisterRawInputDevices.restype = wintypes.BOOL
    _user32.GetRawInputData.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    ]
    _user32.GetRawInputData.restype = wintypes.UINT
    _user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.SetTimer.argtypes = [
        wintypes.HWND,
        ctypes.c_size_t,
        wintypes.UINT,
        wintypes.LPVOID,
    ]
    _user32.SetTimer.restype = ctypes.c_size_t
    _user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    _user32.KillTimer.restype = wintypes.BOOL
    _user32.ClipCursor.argtypes = [wintypes.LPVOID]
    _user32.ClipCursor.restype = wintypes.BOOL
    _user32.GetClientRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.POINT),
    ]
    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _wnd_proc(hwnd: Any, msg: Any, wparam: Any, lparam: Any) -> int:
    """全ウィンドウ共通の窓関数。HWNDから所有者を引いて振る。"""
    handle = int(hwnd or 0)
    with _windows_lock:
        owner = _windows.get(handle)
    if owner is not None:
        try:
            result = owner._handle_message(handle, int(msg), int(wparam), int(lparam))
            if result is not None:
                return result
        except Exception as exc:  # 窓関数から例外を漏らさない
            owner._emit_log(f"マウス入力の処理でエラー: {exc!r}")
    try:
        return int(_user32.DefWindowProcW(hwnd, msg, wparam, lparam))
    except Exception:
        return 0


_wndproc_ref: Any = _WNDPROC(_wnd_proc) if IS_WINDOWS else None

if IS_WINDOWS:
    _windows_class: dict[str, Any] = {
        "atom": 0,
        "name": "",
        "hinstance": None,
        "lock": threading.Lock(),
    }
else:
    _windows_class = {}


def _ensure_class() -> tuple[str, Any, int] | None:
    """ウィンドウクラスを1回だけ登録する。(name, hinstance, atom) か None。"""
    if not IS_WINDOWS:
        return None
    with _windows_class["lock"]:
        if _windows_class["atom"]:
            return (
                str(_windows_class["name"]),
                _windows_class["hinstance"],
                int(_windows_class["atom"]),
            )
        hinstance = _kernel32.GetModuleHandleW(None)
        if not hinstance:
            return None
        name = f"PokeConRawMouse{os.getpid()}"
        wc = _WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(_wndproc_ref, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinstance
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = name
        atom = int(_user32.RegisterClassW(ctypes.byref(wc)) or 0)
        if not atom:
            return None
        _windows_class["atom"] = atom
        _windows_class["name"] = name
        _windows_class["hinstance"] = hinstance
        return (name, hinstance, atom)


class RawMouseCapture:
    """Raw Inputの受信スレッド。1インスタンスが1つの非表示窓を持つ。

    ``on_motion(dx, dy, roll)`` はキャプチャ有効中だけ呼ばれる。
    ``roll`` は設定した修飾（右ボタン/Shift）が押されているか。
    ``on_state(enabled, reason)`` は有効/無効が変わるたびに呼ばれる
    （reason は "start" / "hotkey" / "focus" / "stop"）。
    ``on_reset`` は姿勢リセットのホットキーが押されたときに呼ばれる。
    """

    def __init__(
        self,
        *,
        on_motion: Callable[[int, int, bool], None],
        on_state: Callable[[bool, str], None],
        on_log: Callable[[str], None] | None = None,
        hotkey: str = "ctrl+alt+m",
        reset_hotkey: str = "",
        on_reset: Callable[[], None] | None = None,
        roll_modifier: str = "none",
    ) -> None:
        self._on_motion = on_motion
        self._on_state = on_state
        self._on_log = on_log
        self._on_reset = on_reset
        self._hotkey = str(hotkey or "").strip()
        self._reset_hotkey = str(reset_hotkey or "").strip()
        self._roll_modifier = (
            str(roll_modifier or "none").strip().lower()
            if str(roll_modifier or "none").strip().lower()
            in ("none", "right", "shift")
            else "none"
        )
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hwnd = 0
        self._owner_hwnd = 0
        self._clip_rect: tuple[int, int, int, int] | None = None
        self._enabled = False
        self._clipped = False
        self._started = False
        self._start_error = ""
        self._absolute_logged = False
        self._last_focus_check = 0.0

    def _emit_log(self, text: str) -> None:
        _call_log(self._on_log, text)

    def _notify(self, enabled: bool, reason: str) -> None:
        try:
            self._on_state(bool(enabled), str(reason))
        except Exception:
            pass

    def start(
        self,
        *,
        owner_hwnd: int = 0,
        clip_rect: tuple[int, int, int, int] | None = None,
    ) -> bool:
        """受信スレッドと非表示窓を作る。成功したらTrue。"""
        if not IS_WINDOWS:
            self._start_error = "このOSではマウス入力を扱えません。"
            self._emit_log(self._start_error)
            return False
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._owner_hwnd = int(owner_hwnd or 0)
            self._clip_rect = clip_rect
            self._started = False
            self._start_error = ""
            self._absolute_logged = False
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name="mouse-raw-input", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(2.0):
            self._start_error = "マウス入力スレッドが応答しません。"
            self.stop()
            self._emit_log(self._start_error)
            return False
        if not self._started:
            message = self._start_error or "マウス入力を開始できませんでした。"
            self.stop()
            self._emit_log(message)
            return False
        return True

    def enabled(self) -> bool:
        """キャプチャ中か。"""
        with self._state_lock:
            return self._enabled

    def set_enabled(self, enabled: bool, reason: str = "") -> None:
        """キャプチャを切り替える。呼び出しはどのスレッドからでもよい。"""
        enabled = bool(enabled)
        if not IS_WINDOWS:
            if enabled:
                self._notify(False, reason or "stop")
            return
        if enabled:
            self._clip_rect = self._compute_clip_rect()
        with self._state_lock:
            if enabled == self._enabled:
                return
            if enabled and not self._foreground_ok():
                self._emit_log("PokeConが前面にないため、マウス入力を開始できません。")
                self._notify(False, "focus")
                return
            self._enabled = enabled
            should_clip = enabled and self._clip_rect is not None
        if should_clip:
            self._set_clip(True)
        else:
            self._set_clip(False)
        self._notify(enabled, reason or ("start" if enabled else "stop"))

    def stop(self) -> None:
        """解除して受信スレッドを止める。例外は投げない。"""
        self.set_enabled(False, "stop")
        if not IS_WINDOWS:
            return
        thread = self._thread
        if thread is None:
            self._set_clip(False)
            return
        if thread.is_alive() and thread is not threading.current_thread():
            thread_id = self._thread_id
            if thread_id:
                try:
                    _user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
                except Exception:
                    pass
            thread.join(1.0)
        self._set_clip(False)
        if thread.is_alive():
            self._emit_log("マウス入力スレッドの停止がタイムアウトしました。")
        self._thread = None
        self._thread_id = 0

    def _foreground_ok(self) -> bool:
        """PokeConのトップレベルが前面にあるか。0指定なら検査しない。"""
        owner = self._owner_hwnd
        if not owner or not IS_WINDOWS:
            return True
        try:
            root = _user32.GetAncestor(wintypes.HWND(owner), GA_ROOT)
            foreground = _user32.GetForegroundWindow()
            return bool(root) and int(root) == int(foreground or 0)
        except Exception:
            return False

    def _compute_clip_rect(self) -> tuple[int, int, int, int] | None:
        """所有者ウィンドウのクライアント領域を画面座標で返す。

        Tkのwinfo座標はDPI設定で物理ピクセルとずれることがあるため、
        Win32のClientToScreenで物理座標を取る。取れなければ明示指定を
        そのまま使う。
        """
        if not IS_WINDOWS or not self._owner_hwnd:
            return self._clip_rect
        try:
            hwnd = wintypes.HWND(self._owner_hwnd)
            rect = wintypes.RECT()
            if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return self._clip_rect
            point = wintypes.POINT(0, 0)
            if not _user32.ClientToScreen(hwnd, ctypes.byref(point)):
                return self._clip_rect
            left = int(point.x)
            top = int(point.y)
            return (left, top, left + int(rect.right), top + int(rect.bottom))
        except Exception:
            return self._clip_rect

    def _set_clip(self, clip: bool) -> None:
        """カーソルの閉じ込めを切り替える。失敗しても続行する。"""
        if not IS_WINDOWS:
            return
        with self._state_lock:
            if clip:
                rect = self._clip_rect
                if rect is None or self._clipped:
                    return
            elif not self._clipped:
                return
        if clip:
            rect = self._clip_rect
            if rect is None:
                return
            left, top, right, bottom = (int(v) for v in rect)
            if right <= left or bottom <= top:
                return
            clip_rect = wintypes.RECT(left, top, right, bottom)
            ok = bool(_user32.ClipCursor(ctypes.byref(clip_rect)))
            with self._state_lock:
                self._clipped = ok
            if not ok:
                self._emit_log("カーソルを固定できませんでした。")
            return
        try:
            _user32.ClipCursor(None)
        except Exception:
            pass
        with self._state_lock:
            self._clipped = False

    def _check_focus(self, force: bool = False) -> None:
        """前面から外れたら解除する。WM_INPUTとタイマーの両方から呼ぶ。

        WM_INPUTは毎秒1000件ほど来るため、前面の確認は100msに1回へ
        間引く。タイマーからは force=True で必ず確認する。
        """
        now = time.monotonic()
        if not force and now - self._last_focus_check < 0.1:
            return
        self._last_focus_check = now
        with self._state_lock:
            enabled = self._enabled
        if enabled and not self._foreground_ok():
            self.set_enabled(False, "focus")

    def _run(self) -> None:
        """非表示窓を作り、WM_INPUT/WM_HOTKEY/WM_TIMERを回す。"""
        hwnd = 0
        class_name = ""
        hinstance = None
        try:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            prepared = _ensure_class()
            if prepared is None:
                self._start_error = "マウス入力用のウィンドウクラスを作れません。"
                return
            class_name, hinstance, _atom = prepared
            hwnd = int(
                _user32.CreateWindowExW(
                    0,
                    class_name,
                    "PokeConRawMouse",
                    0,
                    0,
                    0,
                    0,
                    0,
                    None,
                    None,
                    hinstance,
                    None,
                )
                or 0
            )
            if not hwnd:
                self._start_error = "マウス入力用の非表示ウィンドウを作れません。"
                return
            self._hwnd = hwnd
            with _windows_lock:
                _windows[hwnd] = self
            device = RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, wintypes.HWND(hwnd))
            if not _user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
            ):
                self._start_error = (
                    "Raw Inputを登録できませんでした"
                    f"（error={ctypes.get_last_error()}）。"
                )
                return
            self._register_hotkeys(hwnd)
            _user32.SetTimer(hwnd, FOCUS_TIMER_ID, FOCUS_TIMER_MS, None)
            self._started = True
            self._ready.set()
            msg = wintypes.MSG()
            while True:
                result = int(_user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if result > 0:
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                    continue
                if result < 0:
                    self._emit_log(
                        "マウス入力のメッセージループが失敗しました"
                        f"（error={ctypes.get_last_error()}）。"
                    )
                break
        except Exception as exc:
            if not self._ready.is_set():
                self._start_error = f"マウス入力の初期化に失敗: {exc!r}"
        finally:
            self._ready.set()
            try:
                if hwnd:
                    _user32.KillTimer(hwnd, FOCUS_TIMER_ID)
                    _user32.UnregisterHotKey(hwnd, HOTKEY_ID)
                    _user32.UnregisterHotKey(hwnd, RESET_HOTKEY_ID)
            except Exception:
                pass
            self._set_clip(False)
            if hwnd:
                with _windows_lock:
                    _windows.pop(hwnd, None)
                try:
                    _user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                self._hwnd = 0
            # ウィンドウクラスはプロセス内で共有する。ここで解除すると、
            # 直後に別インスタンスが同じクラスを使えなくなる。終了まで残す。

    def _register_hotkeys(self, hwnd: int) -> None:
        """解除と姿勢リセットのホットキーを登録する。失敗しても入力を続ける。"""
        self._register_one_hotkey(
            hwnd,
            HOTKEY_ID,
            self._hotkey,
            "解除",
            "ctrl+alt+m",
        )
        if self._on_reset is not None and self._reset_hotkey:
            self._register_one_hotkey(
                hwnd,
                RESET_HOTKEY_ID,
                self._reset_hotkey,
                "姿勢リセット",
                "ctrl+alt+r",
            )

    def _register_one_hotkey(
        self,
        hwnd: int,
        hotkey_id: int,
        text: str,
        label: str,
        example: str,
    ) -> None:
        """1つのホットキーを登録する。失敗はログだけ。"""
        if not text:
            return
        parsed = parse_hotkey(text)
        if parsed is None:
            self._emit_log(
                f"{label}ホットキーを解釈できません: {text}"
                f"（修飾キー付きで指定してください。例: {example}）"
            )
            return
        mods, key = parsed
        if _user32.RegisterHotKey(hwnd, hotkey_id, mods | MOD_NOREPEAT, key):
            return
        # MOD_NOREPEAT非対応の環境では、修飾キーだけで再試行する。
        if _user32.RegisterHotKey(hwnd, hotkey_id, mods, key):
            return
        self._emit_log(
            f"{label}ホットキーを登録できませんでした: {text}"
            f"（error={ctypes.get_last_error()}）。"
            "他のアプリが同じキーを使っている可能性があります。"
        )

    def set_roll_modifier(self, value: str) -> None:
        """ロール修飾を実行中に差し替える（次のWM_INPUTから）。"""
        text = str(value or "none").strip().lower()
        if text not in ("none", "right", "shift"):
            text = "none"
        with self._state_lock:
            self._roll_modifier = text

    def _handle_message(
        self, hwnd: int, msg: int, wparam: int, lparam: int
    ) -> int | None:
        """窓関数の実体。処理したら戻り値、未処理はNone。"""
        if msg == WM_INPUT:
            self._check_focus()
            self._read_raw_input(lparam)
            # RIM_INPUT/RIM_INPUTSINKのどちらでも既定処理を通す
            # （システム側の後片付け）。
            try:
                _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except Exception:
                pass
            return 0
        if msg == WM_HOTKEY:
            hotkey_id = int(wparam)
            if hotkey_id == HOTKEY_ID:
                self.set_enabled(not self.enabled(), "hotkey")
                return 0
            if hotkey_id == RESET_HOTKEY_ID and self._on_reset is not None:
                try:
                    self._on_reset()
                except Exception as exc:
                    self._emit_log(f"姿勢リセットでエラー: {exc!r}")
                return 0
            return None
        if msg == WM_TIMER:
            if int(wparam) == FOCUS_TIMER_ID:
                self._check_focus(force=True)
                return 0
            return None
        if msg == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return None

    def _read_raw_input(self, lparam: int) -> None:
        """WM_INPUTの生データから相対移動量を取り出す。"""
        handle = wintypes.HANDLE(lparam)
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        result = _user32.GetRawInputData(
            handle, RID_INPUT, None, ctypes.byref(size), header_size
        )
        if int(result) == 0xFFFFFFFF or int(size.value) < header_size:
            return
        buffer = ctypes.create_string_buffer(int(size.value))
        copied = _user32.GetRawInputData(
            handle,
            RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        )
        if int(copied) == 0xFFFFFFFF or int(copied) < header_size:
            return
        raw = RAWINPUT.from_buffer_copy(buffer.raw[: int(copied)])
        if int(raw.header.dwType) != RIM_TYPEMOUSE:
            return
        flags = int(raw.mouse.usFlags)
        if flags & MOUSE_MOVE_ABSOLUTE:
            # リモートデスクトップやタブレットの絶対座標は相対移動へ
            # 変換できない。初回だけ知らせる（毎回出すとログが埋まる）。
            if not self._absolute_logged:
                self._absolute_logged = True
                self._emit_log(
                    "絶対座標のマウス入力を無視します"
                    "（リモートデスクトップ等では使えません）。"
                )
            return
        dx = int(raw.mouse.lLastX)
        dy = int(raw.mouse.lLastY)
        if not dx and not dy:
            return
        with self._state_lock:
            enabled = self._enabled
            modifier = self._roll_modifier
        if not enabled:
            return
        roll = False
        try:
            if modifier == "right":
                roll = bool(_user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
            elif modifier == "shift":
                roll = bool(_user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        except Exception:
            roll = False
        try:
            self._on_motion(dx, dy, roll)
        except Exception as exc:
            self._emit_log(f"マウス移動の処理でエラー: {exc!r}")
