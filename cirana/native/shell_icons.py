"""Shell icon extraction for packaged (AUMID) apps, which have no icon-bearing file path."""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

SHGFI_ICON = 0x100
SHGFI_LARGEICON = 0x0
SHGFI_PIDL = 0x8
DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", wintypes.LPVOID)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ICONINFO)]
_user32.DestroyIcon.argtypes = [wintypes.HANDLE]
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.UINT]
_gdi32.DeleteObject.argtypes = [wintypes.HANDLE]


def _bitmap_bgra(hbitmap: int, width: int, height: int) -> bytes:
    header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    hdc = _user32.GetDC(None)
    try:
        _gdi32.GetDIBits(hdc, hbitmap, 0, height, buffer, ctypes.byref(header), DIB_RGB_COLORS)
    finally:
        _user32.ReleaseDC(None, hdc)
    return buffer.raw


def hicon_to_bgra(hicon: int) -> tuple[bytes, int, int] | None:
    """Return (BGRA pixels, width, height) for an HICON, or None."""
    info = _ICONINFO()
    if not _user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        bmp = _BITMAP()
        _gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp))
        width, height = bmp.bmWidth, bmp.bmHeight
        if not width or not height:
            return None
        pixels = bytearray(_bitmap_bgra(info.hbmColor, width, height))
        if not any(pixels[3::4]):  # icon without an alpha channel: build alpha from the AND mask
            mask = _bitmap_bgra(info.hbmMask, width, height)
            for i in range(0, len(pixels), 4):
                pixels[i + 3] = 0 if mask[i] else 255
        return bytes(pixels), width, height
    finally:
        _gdi32.DeleteObject(info.hbmColor)
        _gdi32.DeleteObject(info.hbmMask)


SHGFI_SYSICONINDEX = 0x4000
SHIL_EXTRALARGE = 2  # 48x48
SHIL_JUMBO = 4  # 256x256
ILD_TRANSPARENT = 0x1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


_IID_IIMAGELIST = _GUID(0x46EB5926, 0x582E, 0x4017, (ctypes.c_ubyte * 8)(0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50))
_shell32 = ctypes.WinDLL("shell32")
_shell32.SHGetImageList.argtypes = [ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetImageList.restype = ctypes.c_long
_GETICON_VTABLE_INDEX = 10  # IUnknown(3) + Add, ReplaceIcon, SetOverlayImage, Replace, AddMasked, Draw, Remove


def _imagelist_geticon(size_class: int, index: int) -> int:
    """Call IImageList::GetIcon through its vtable (pywin32 does not wrap SHGetImageList)."""
    ppv = ctypes.c_void_p()
    if _shell32.SHGetImageList(size_class, ctypes.byref(_IID_IIMAGELIST), ctypes.byref(ppv)) != 0 or not ppv.value:
        return 0
    vtable = ctypes.cast(ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get_icon = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
        vtable[_GETICON_VTABLE_INDEX]
    )
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    hicon = ctypes.c_void_p()
    try:
        if get_icon(ppv, index, ILD_TRANSPARENT, ctypes.byref(hicon)) != 0:
            return 0
        return hicon.value or 0
    finally:
        release(ppv)


def _image_list_icon(target, flags: int) -> tuple[bytes, int, int] | None:
    """Shell image-list icon (jumbo, falling back to extra large) for a path or PIDL."""
    from win32com.shell import shell

    _rc, info = shell.SHGetFileInfo(target, 0, SHGFI_SYSICONINDEX | flags, 0)
    index = info[1]
    for size in (SHIL_JUMBO, SHIL_EXTRALARGE):
        hicon = _imagelist_geticon(size, index)
        if not hicon:
            continue
        try:
            result = hicon_to_bgra(hicon)
        finally:
            _user32.DestroyIcon(hicon)
        if result:
            return result
    return None


def file_icon_bgra(path: str) -> tuple[bytes, int, int] | None:
    try:
        return _image_list_icon(path, 0)
    except Exception as exc:  # noqa: BLE001 - icons are cosmetic; never fail the UI over them
        log.debug("No shell icon for %s: %s", path, exc)
        return None


def aumid_icon_bgra(aumid: str) -> tuple[bytes, int, int] | None:
    """Icon of a packaged app, taken from the shell's AppsFolder entry for its AUMID."""
    try:
        from win32com.shell import shell

        pidl, _flags = shell.SHParseDisplayName("shell:AppsFolder\\" + aumid, 0)
        return _image_list_icon(pidl, SHGFI_PIDL)
    except Exception as exc:  # noqa: BLE001
        log.debug("No shell icon for %s: %s", aumid, exc)
        return None
