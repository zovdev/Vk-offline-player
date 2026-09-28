from __future__ import annotations

import ctypes
import os
from ctypes import (POINTER, Structure, byref, c_ubyte, c_uint32, c_ulong,
                    c_ushort, c_void_p, c_wchar_p, create_unicode_buffer,
                    pointer)

# WINFUNCTYPE (stdcall) существует только в Windows-ctypes; на других
# платформах модуль должен импортироваться для юнит-тестов
if os.name == "nt":
    WINFUNCTYPE = ctypes.WINFUNCTYPE
else:
    WINFUNCTYPE = ctypes.CFUNCTYPE

__all__ = ["MediaKeys"]

_HRESULT = c_ulong
_S_OK = 0
_E_NOINTERFACE = 0x80004002
_E_FAIL = 0x80004005

_IID_IUNKNOWN = "00000000-0000-0000-c000-000000000046"
_IID_IACTIVATION_FACTORY = "00000035-0000-0000-c000-000000000046"

_IID_ISMTC_INTEROP = "ddb0472d-c911-4a1f-86d9-dc3d71a95f5a"
_IID_ISMTC = "99fa3ff4-1742-42a6-902e-087d41f965ec"
_IID_BUTTON_PRESSED_HANDLER = "0557e996-7b23-5bae-aa81-ea0d671143a4"

# enum Windows.Media.MediaPlaybackStatus
_STATUS_STOPPED, _STATUS_PLAYING, _STATUS_PAUSED = 2, 3, 4
# enum Windows.Media.MediaPlaybackType
_TYPE_MUSIC = 1
# enum Windows.Media.SystemMediaTransportControlsButton
_BTN = {0: "play", 1: "pause", 2: "stop", 3: "record",
        4: "fast_forward", 5: "rewind", 6: "next", 7: "previous",
        8: "channel_up", 9: "channel_down"}


class _GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    # «xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx»: ПЯТЬ групп, последняя пара
    # (4+12 hex) склеивается в 8-байтовое Data4
    d1, d2, d3, d4, d5 = text.split("-")
    return _GUID(int(d1, 16), int(d2, 16), int(d3, 16),
                 (c_ubyte * 8).from_buffer_copy(bytes.fromhex(d4 + d5)))


def _guid_eq(a: _GUID, b: _GUID) -> bool:
    return (a.Data1 == b.Data1 and a.Data2 == b.Data2 and a.Data3 == b.Data3
            and bytes(a.Data4) == bytes(b.Data4))


# --- ctypes-прототипы методов vtable (одинаково на x86/x64) -----------------
_QI_T = WINFUNCTYPE(_HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
_ADDREF_T = WINFUNCTYPE(c_ulong, c_void_p)
_CALL0_T = WINFUNCTYPE(_HRESULT, c_void_p)
_PUT_BOOL_T = WINFUNCTYPE(_HRESULT, c_void_p, c_ubyte)
_PUT_I32_T = WINFUNCTYPE(_HRESULT, c_void_p, c_uint32)
_PUT_HSTR_T = WINFUNCTYPE(_HRESULT, c_void_p, c_void_p)
_GET_OBJ_T = WINFUNCTYPE(_HRESULT, c_void_p, POINTER(c_void_p))
_ADD_EVENT_T = WINFUNCTYPE(_HRESULT, c_void_p, c_void_p,
                           POINTER(ctypes.c_longlong))
_REMOVE_EVENT_T = WINFUNCTYPE(_HRESULT, c_void_p, ctypes.c_longlong)
_GET_BUTTON_T = WINFUNCTYPE(_HRESULT, c_void_p, POINTER(c_uint32))
_INVOKE_T = WINFUNCTYPE(_HRESULT, c_void_p, c_void_p, c_void_p)
_GET_FOR_WINDOW_T = WINFUNCTYPE(_HRESULT, c_void_p, c_void_p, c_void_p,
                                POINTER(c_void_p))

# vtable-индексы: IUnknown 0..2, IInspectable 3..5
_VT_QI, _VT_ADDREF, _VT_RELEASE = 0, 1, 2
_SMTC_PUT_PLAYBACK_STATUS = 7
_SMTC_GET_DISPLAY_UPDATER = 8
_SMTC_PUT_IS_ENABLED = 11
_SMTC_PUT_IS_PLAY = 13
_SMTC_PUT_IS_PAUSE = 17
_SMTC_PUT_IS_PREVIOUS = 25
_SMTC_PUT_IS_NEXT = 27
_SMTC_ADD_BUTTON_PRESSED = 32
_SMTC_REMOVE_BUTTON_PRESSED = 33

_UPD_PUT_TYPE = 7
_UPD_GET_MUSIC_PROPERTIES = 12
_UPD_UPDATE = 17

_MUSIC_PUT_TITLE = 7
_MUSIC_PUT_ALBUM_ARTIST = 9
_MUSIC_PUT_ARTIST = 11

_ARGS_GET_BUTTON = 6


def _vt_call(ptr, index, func_type, *args):
    obj = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))
    vtable = obj[0]
    method = ctypes.cast(vtable[index], func_type)
    return method(ptr, *args)


class _ComBase:
    def __init__(self):
        self.ok = False
        if os.name != "nt":
            return
        try:
            lib = ctypes.WinDLL("combase.dll")

            lib.RoGetActivationFactory.restype = _HRESULT
            lib.RoGetActivationFactory.argtypes = [c_void_p, c_void_p,
                                                   POINTER(c_void_p)]
            lib.WindowsCreateString.restype = _HRESULT
            lib.WindowsCreateString.argtypes = [c_wchar_p, c_uint32,
                                                POINTER(c_void_p)]
            lib.WindowsDeleteString.restype = _HRESULT
            lib.WindowsDeleteString.argtypes = [c_void_p]

            self.lib = lib
            self.ok = True
        except OSError:
            self.ok = False

    def create_string(self, text: str):
        buf = create_unicode_buffer(text)
        h = c_void_p()
        if self.lib.WindowsCreateString(buf, len(text), byref(h)) != _S_OK:
            return None
        return h

    def delete_string(self, h) -> None:
        if h:
            self.lib.WindowsDeleteString(h)

    def activation_factory(self, class_name: str) -> c_void_p | None:
        h = self.create_string(class_name)
        if h is None:
            return None
        try:
            factory = c_void_p()
            hr = self.lib.RoGetActivationFactory(
                h, byref(_guid(_IID_IACTIVATION_FACTORY)), byref(factory))
            if hr != _S_OK or not factory:
                return None
            return factory
        finally:
            self.delete_string(h)


_CB = _ComBase()


class _ButtonPressedDelegate:
    def __init__(self, on_button):
        self._on_button = on_button
        self._refs = 1
        self._iid_handler = _guid(_IID_BUTTON_PRESSED_HANDLER)
        self._iid_unknown = _guid(_IID_IUNKNOWN)

        self._qi_f = _QI_T(self._query_interface)
        self._addref_f = _ADDREF_T(self._add_ref)
        self._release_f = _ADDREF_T(self._release)
        self._invoke_f = _INVOKE_T(self._invoke)

        class _Vtbl(Structure):
            _fields_ = [("QueryInterface", _QI_T), ("AddRef", _ADDREF_T),
                        ("Release", _ADDREF_T), ("Invoke", _INVOKE_T)]

        class _Obj(Structure):
            _fields_ = [("lpVtbl", POINTER(_Vtbl))]

        self._vtbl = _Vtbl(self._qi_f, self._addref_f, self._release_f,
                           self._invoke_f)
        self._obj = _Obj(pointer(self._vtbl))
        self._ptr = ctypes.addressof(self._obj)

    @property
    def ptr(self) -> int:
        return self._ptr


    def _query_interface(self, this, riid, ppv):
        try:
            guid = ctypes.cast(riid, POINTER(_GUID)).contents
            if _guid_eq(guid, self._iid_handler) or _guid_eq(guid, self._iid_unknown):
                self._refs += 1
                ppv[0] = ctypes.c_void_p(this)
                return _S_OK
        except Exception:
            pass
        if ppv:
            ppv[0] = None
        return _E_NOINTERFACE

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        if self._refs > 0:
            self._refs -= 1
        return self._refs

    # --- ITypedEventHandler -------------------------------------------
    def _invoke(self, this, sender, args):
        try:
            button = c_uint32(0xFF)
            if args:
                _vt_call(args, _ARGS_GET_BUTTON, _GET_BUTTON_T, byref(button))
            cmd = _BTN.get(button.value)
            if cmd:
                self._on_button(cmd)
            return _S_OK
        except Exception:
            return _E_FAIL


class MediaKeys:
    def __init__(self):
        self._attached = False
        self._smtc = None            # c_void_p — ISystemMediaTransportControls
        self._interop = None
        self._factory = None
        self._delegate = None        # _ButtonPressedDelegate
        self._token = None           # EventRegistrationToken (int64)
        self._callbacks = {}         # cmd -> callable

    @property
    def attached(self) -> bool:
        return self._attached



    def attach(self, hwnd: int) -> bool:

        if self._attached:
            return True
        if not _CB.ok or not hwnd:
            return False
        try:
            self._factory = _CB.activation_factory(
                "Windows.Media.SystemMediaTransportControls")
            if not self._factory:
                return False

            # фабрика -> ISystemMediaTransportControlsInterop
            interop = c_void_p()
            hr = _vt_call(self._factory, _VT_QI, _QI_T,
                          byref(_guid(_IID_ISMTC_INTEROP)), byref(interop))
            if hr != _S_OK or not interop:
                self._release_all()
                return False
            self._interop = interop

            # interop.GetForWindow(hwnd, IID_ISMTC, &smtc) — слот 6
            smtc = c_void_p()
            hr = _vt_call(self._interop, 6, _GET_FOR_WINDOW_T,
                          c_void_p(hwnd), byref(_guid(_IID_ISMTC)),
                          byref(smtc))
            if hr != _S_OK or not smtc:
                self._release_all()
                return False
            self._smtc = smtc

            # включаем и разрешаем только те кнопки, которые умеем
            for slot in (_SMTC_PUT_IS_ENABLED, _SMTC_PUT_IS_PLAY,
                         _SMTC_PUT_IS_PAUSE, _SMTC_PUT_IS_PREVIOUS,
                         _SMTC_PUT_IS_NEXT):
                if _vt_call(self._smtc, slot, _PUT_BOOL_T, c_ubyte(1)) != _S_OK:
                    self._release_all()
                    return False

            # подписка на нажатия (делегат живёт, пока жив self._delegate)
            self._delegate = _ButtonPressedDelegate(self._dispatch)
            token = ctypes.c_longlong(0)
            hr = _vt_call(self._smtc, _SMTC_ADD_BUTTON_PRESSED, _ADD_EVENT_T,
                          c_void_p(self._delegate.ptr), byref(token))
            if hr != _S_OK:
                self._delegate = None
                self._release_all()
                return False
            self._token = token.value

            self._attached = True
            return True
        except Exception as ex:
            print("[MediaKeys] attach failed:", ex)
            self._release_all()
            return False

    def on_command(self, cmd: str, callback) -> None:

        self._callbacks[cmd] = callback

    def set_track(self, title: str, artist: str = "") -> None:

        if not self._attached or self._smtc is None:
            return
        try:
            updater = c_void_p()
            if _vt_call(self._smtc, _SMTC_GET_DISPLAY_UPDATER, _GET_OBJ_T,
                        byref(updater)) != _S_OK or not updater:
                return
            try:
                _vt_call(updater, _UPD_PUT_TYPE, _PUT_I32_T,
                         c_uint32(_TYPE_MUSIC))
                music = c_void_p()
                if _vt_call(updater, _UPD_GET_MUSIC_PROPERTIES, _GET_OBJ_T,
                            byref(music)) == _S_OK and music:
                    try:
                        for slot, value in ((_MUSIC_PUT_TITLE, title),
                                            (_MUSIC_PUT_ARTIST, artist),
                                            (_MUSIC_PUT_ALBUM_ARTIST, artist)):
                            if not value:
                                continue
                            h = _CB.create_string(value)
                            if h:
                                try:
                                    _vt_call(music, slot, _PUT_HSTR_T, h)
                                finally:
                                    _CB.delete_string(h)
                    finally:
                        _vt_call(music, _VT_RELEASE, _ADDREF_T)
                _vt_call(updater, _UPD_UPDATE, _CALL0_T)
            finally:
                _vt_call(updater, _VT_RELEASE, _ADDREF_T)
        except Exception as ex:
            print("[MediaKeys] set_track failed:", ex)

    def set_status(self, status) -> None:

        if not self._attached or self._smtc is None:
            return
        value = (_STATUS_PLAYING if status is True
                 else _STATUS_PAUSED if status is False
                 else _STATUS_STOPPED)
        try:
            _vt_call(self._smtc, _SMTC_PUT_PLAYBACK_STATUS, _PUT_I32_T,
                     c_uint32(value))
        except Exception as ex:
            print("[MediaKeys] set_status failed:", ex)

    def close(self) -> None:
        if not self._attached:
            return
        self._attached = False
        try:
            if self._smtc is not None and self._token is not None:
                _vt_call(self._smtc, _SMTC_REMOVE_BUTTON_PRESSED,
                         _REMOVE_EVENT_T, ctypes.c_longlong(self._token))
            if self._smtc is not None:
                _vt_call(self._smtc, _SMTC_PUT_IS_ENABLED, _PUT_BOOL_T,
                         c_ubyte(0))
        except Exception as ex:
            print("[MediaKeys] close failed:", ex)
        finally:
            self._token = None
            self._delegate = None
            self._release_all()



    def _dispatch(self, cmd: str) -> None:

        cb = self._callbacks.get(cmd)
        if cb is not None:
            try:
                cb()
            except Exception as ex:
                print("[MediaKeys] command handler failed:", ex)

    def _release_all(self) -> None:
        for name in ("_smtc", "_interop", "_factory"):
            ptr = getattr(self, name, None)
            if ptr:
                try:
                    _vt_call(ptr, _VT_RELEASE, _ADDREF_T)
                except Exception:
                    pass
                setattr(self, name, None)


if __name__ == "__main__":  # pragma: no cover — ручная проверка на Windows
    print("combase available:", _CB.ok)
    if _CB.ok:
        f = _CB.activation_factory("Windows.Media.SystemMediaTransportControls")
        print("activation factory:", "OK" if f else "FAIL")
        if f:
            _vt_call(f, _VT_RELEASE, _ADDREF_T)
