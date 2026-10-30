#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dshow_devices.py - DirectShow の映像入力デバイス列挙（ctypes のみ）.

pythonnet + 同梱の DirectShowLib-2005.dll は、.NET のローダーや DLL の
置き場所によって読み込みに失敗することがあった（同じツリーでも環境次第で
``from DirectShowLib import DsDevice`` が
``ImportError: cannot import name 'DsDevice'`` になる）。ここでは
DirectShow の COM インターフェース（ICreateDevEnum / IEnumMoniker /
IMoniker / IPropertyBag）を ctypes で直接叩き、外部ライブラリも CLR も
使わない。

手順は DirectShowLib の ``DsDevice.GetDevicesOfCat`` と同じ:

  1. ``CoCreateInstance(CLSID_SystemDeviceEnum)`` -> ICreateDevEnum
  2. ``CreateClassEnumerator(CLSID_VideoInputDeviceCategory)`` -> IEnumMoniker
  3. モニカを 1 個ずつ取り、名前は ``BindToStorage(IID_IPropertyBag)`` の
     FriendlyName、個体識別子は ``IMoniker::GetDisplayName`` から読む

COM の往復はデバイス数に比例するが、数台の列挙で 1〜2ms 程度で済む
（DirectShowLib と同じ経路）。インデックスを総当たりして
``cv2.VideoCapture`` を開く方法（1 台数秒）や WMI / PowerShell の
起動（数百ミリ秒）とは桁が違う。
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from typing import Any, NamedTuple

from loguru import logger


class VideoInputDevice(NamedTuple):
    """映像入力デバイス 1 台ぶんの情報。

    ``device_path`` は USB のポートごとに異なる不変の識別子。同じ型名の
    キャプチャボードを複数挿しても、これで個体を区別できる。
    """

    name: str
    device_path: str


_IS_WINDOWS = os.name == "nt"


class _GUID(ctypes.Structure):
    """COM の GUID。``uuid.UUID(...).bytes_le`` がそのままメモリ配置になる。"""

    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        return cls((ctypes.c_ubyte * 16)(*uuid.UUID(text).bytes_le))


# DirectShow の固定 GUID（dshow.h / uuids.h）
_CLSID_SYSTEM_DEVICE_ENUM = _GUID.parse("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")
_CLSID_VIDEO_INPUT_DEVICE = _GUID.parse("860BB310-5D01-11D0-BD3B-00A0C911CE86")
_IID_ICREATE_DEV_ENUM = _GUID.parse("29840822-5B84-11D0-BD3B-00A0C911CE86")
_IID_IPROPERTY_BAG = _GUID.parse("55272A00-42CB-11CE-8135-00AA004BB851")

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = 0x80010106

_S_OK = 0
_S_FALSE = 1
_VT_BSTR = 8
_VT_LPWSTR = 31

# vtable 上の位置（IUnknown は QueryInterface=0 / AddRef=1 / Release=2）
_VTBL_RELEASE = 2
_VTBL_CREATE_CLASS_ENUMERATOR = 3  # ICreateDevEnum : IUnknown
_VTBL_NEXT = 3  # IEnumMoniker : IUnknown
_VTBL_READ = 3  # IPropertyBag : IUnknown
# IMoniker は IUnknown + IPersistStream(5 メソッド) を継承する。
#   BindToObject=8, BindToStorage=9, ..., GetDisplayName=20
_VTBL_BIND_TO_STORAGE = 9
_VTBL_GET_DISPLAY_NAME = 20


class _Variant(ctypes.Structure):
    """VARIANT。BSTR を受け取るだけなので共用体はポインタ 2 本で足りる。

    x64 で 24 バイト、x86 で 16 バイトになり、ネイティブのサイズと一致する。
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("value", ctypes.c_void_p),
        ("extra", ctypes.c_void_p),
    ]


_ole32: Any = None
_oleaut32: Any = None

if _IS_WINDOWS:
    _ole32 = ctypes.WinDLL("ole32")
    _oleaut32 = ctypes.WinDLL("oleaut32")
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole32.CoTaskMemFree.restype = None
    _oleaut32.VariantClear.argtypes = [ctypes.POINTER(_Variant)]
    _oleaut32.VariantClear.restype = ctypes.c_long


def _hresult(hr: int) -> str:
    """負の HRESULT を `0x80004005` の形で読めるようにする。"""
    return f"0x{hr & 0xFFFFFFFF:08X}"


#: 関数ポインタ -> 呼び出し可能な COM メソッド。同じ vtable を何度も
#: 包み直すと 1 回あたり数十マイクロ秒かかり、デバイス数ぶん積み上がる。
_CALLS: dict[int, Any] = {}


def _method(ptr: Any, index: int, restype: Any, *argtypes: Any) -> Any:
    """COM オブジェクトの vtable[index] を呼べる関数にする。

    ラッパーは関数ポインタでキャッシュする。異なるデバイス（ハードウェアと
    OBS などのソフトウェア）は vtable が違うが、同じ実装なら使い回せる。
    """
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = vtable[index]
    call = _CALLS.get(fn)
    if call is None:
        call = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)
        _CALLS[fn] = call
    return call


def _release(ptr: Any) -> None:
    """IUnknown::Release。NULL は何もしない。"""
    if ptr:
        _method(ptr, _VTBL_RELEASE, ctypes.c_ulong)(ptr)


def _read_property(bag: Any, prop: str) -> str:
    """IPropertyBag から文字列プロパティを 1 つ読む。無ければ空文字。"""
    variant = _Variant()
    read = _method(
        bag,
        _VTBL_READ,
        ctypes.c_long,
        ctypes.c_wchar_p,
        ctypes.POINTER(_Variant),
        ctypes.c_void_p,
    )
    if read(bag, prop, ctypes.byref(variant), None) < 0:
        return ""
    try:
        if variant.vt in (_VT_BSTR, _VT_LPWSTR) and variant.value:
            return ctypes.wstring_at(variant.value)
        return ""
    finally:
        # BSTR は Read が確保する。呼び出し側で解放する。
        _oleaut32.VariantClear(ctypes.byref(variant))


def _display_name(moniker: Any) -> str:
    """IMoniker::GetDisplayName で個体識別子を読む。

    DirectShowLib の ``DsDevice.DevicePath`` と同じ経路。ハードウェアは
    ``@device:pnp:\\\\?\\usb#...``、ソフトウェア デバイス（OBS など）は
    ``@device:sw:{カテゴリ}\\{CLSID}`` になる。保存済みの識別子と
    一致させるため、プロパティバッグの DevicePath ではなくこちらを使う。
    戻り値は CoTaskMemAlloc された文字列なので、読んだら解放する。
    """
    out = ctypes.c_wchar_p()
    get_display_name = _method(
        moniker,
        _VTBL_GET_DISPLAY_NAME,
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    if get_display_name(moniker, None, None, ctypes.byref(out)) < 0 or not out.value:
        return ""
    try:
        return out.value
    finally:
        _ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


#: スレッドごとの COM 初期化状態。``_ensure_com`` が一度だけ True にする。
_COM_READY = threading.local()


def _ensure_com() -> None:
    """このスレッドで COM を使える状態にする（初回だけ初期化する）。

    列挙のたびに ``CoUninitialize`` すると devenum.dll が解放され、次の
    列挙で再読み込みに数 ms かかる。GUI スレッドは終了まで生きるため、
    初期化したままにする（Python の COM ラッパーも同じ扱い）。
    """
    if getattr(_COM_READY, "ok", False):
        return
    hr = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    # 既に別のモデルで初期化済み（RPC_E_CHANGED_MODE）でも COM は使える。
    if hr < 0 and (hr & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitializeEx failed: {_hresult(hr)}")
    _COM_READY.ok = True


def _enumerate_windows() -> list[VideoInputDevice]:
    """ICreateDevEnum で映像入力カテゴリを列挙する。"""
    _ensure_com()

    dev_enum = ctypes.c_void_p()
    moniker_enum = ctypes.c_void_p()
    try:
        hr = _ole32.CoCreateInstance(
            ctypes.byref(_CLSID_SYSTEM_DEVICE_ENUM),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_ICREATE_DEV_ENUM),
            ctypes.byref(dev_enum),
        )
        if hr < 0 or not dev_enum:
            raise OSError(
                f"CoCreateInstance(CLSID_SystemDeviceEnum) failed: {_hresult(hr)}"
            )

        create_enum = _method(
            dev_enum,
            _VTBL_CREATE_CLASS_ENUMERATOR,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
        )
        hr = create_enum(
            dev_enum,
            ctypes.byref(_CLSID_VIDEO_INPUT_DEVICE),
            ctypes.byref(moniker_enum),
            0,
        )
        if hr < 0:
            raise OSError(f"CreateClassEnumerator failed: {_hresult(hr)}")
        if not moniker_enum:
            # S_FALSE: このカテゴリのデバイスが無い（失敗ではない）。
            return []

        next_moniker = _method(
            moniker_enum,
            _VTBL_NEXT,
            ctypes.c_long,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_ulong),
        )
        devices: list[VideoInputDevice] = []
        moniker = ctypes.c_void_p()
        fetched = ctypes.c_ulong()
        while True:
            hr = next_moniker(
                moniker_enum, 1, ctypes.byref(moniker), ctypes.byref(fetched)
            )
            if hr != _S_OK or fetched.value != 1 or not moniker:
                break
            try:
                bag = ctypes.c_void_p()
                bind = _method(
                    moniker,
                    _VTBL_BIND_TO_STORAGE,
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(_GUID),
                    ctypes.POINTER(ctypes.c_void_p),
                )
                hr = bind(
                    moniker,
                    None,
                    None,
                    ctypes.byref(_IID_IPROPERTY_BAG),
                    ctypes.byref(bag),
                )
                name = ""
                if hr >= 0 and bag:
                    try:
                        name = _read_property(bag, "FriendlyName")
                    finally:
                        _release(bag)
                devices.append(
                    VideoInputDevice(name=name, device_path=_display_name(moniker))
                )
            finally:
                _release(moniker)
                moniker = ctypes.c_void_p()
        return devices
    finally:
        _release(moniker_enum)
        _release(dev_enum)


def list_video_input_devices() -> list[VideoInputDevice]:
    """接続されている DirectShow 映像入力デバイスを返す。

    認識順は OS 任せで、起動のたびに入れ替わりうる。個体の照合には
    ``device_path`` を使うこと。Windows 以外や列挙失敗時は空リストを
    返し、GUI の起動を止めない（呼び出し側は「カメラ無し」として扱う）。
    """
    if not _IS_WINDOWS:
        return []
    try:
        return _enumerate_windows()
    except Exception as exc:  # COM の失敗で起動を止めない
        logger.warning(f"DirectShow のデバイス列挙に失敗: {exc}")
        return []
