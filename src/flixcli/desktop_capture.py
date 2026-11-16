"""Capture what is *behind* one window without capturing the window itself.

QScreen.grabWindow grabs the whole screen, so a window that paints its own
backdrop would photograph itself (white feedback). CoreGraphics can list only
the windows below ours, which is what a live frosted backdrop needs.

Returns a QImage, or None when the system refuses the capture.
"""
from __future__ import annotations

import ctypes
import sys


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


_cg = None
_cf = None
LAST_ERROR = ""

OPTION_BELOW_WINDOW = 1 << 2
IMAGE_BOUNDS_IGNORE_FRAMING = 1 << 0
IMAGE_BEST_RESOLUTION = 1 << 3


def _libs():
    global _cg, _cf
    if _cg is None:
        _cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        _cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _cg.CGWindowListCreateImage.restype = ctypes.c_void_p
        _cg.CGWindowListCreateImage.argtypes = [
            CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        _cg.CGImageGetWidth.restype = ctypes.c_size_t
        _cg.CGImageGetWidth.argtypes = [ctypes.c_void_p]
        _cg.CGImageGetHeight.restype = ctypes.c_size_t
        _cg.CGImageGetHeight.argtypes = [ctypes.c_void_p]
        _cg.CGImageGetBytesPerRow.restype = ctypes.c_size_t
        _cg.CGImageGetBytesPerRow.argtypes = [ctypes.c_void_p]
        _cg.CGImageGetDataProvider.restype = ctypes.c_void_p
        _cg.CGImageGetDataProvider.argtypes = [ctypes.c_void_p]
        _cg.CGImageRelease.argtypes = [ctypes.c_void_p]
        _cg.CGDataProviderCopyData.restype = ctypes.c_void_p
        _cg.CGDataProviderCopyData.argtypes = [ctypes.c_void_p]
        _cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        _cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        _cf.CFDataGetLength.restype = ctypes.c_long
        _cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _cg, _cf


def window_number(ns_view: int) -> int:
    """CGWindowID for the NSWindow that owns an NSView pointer."""
    libobjc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    libobjc.objc_getClass.restype = ctypes.c_void_p
    libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
    libobjc.sel_registerName.restype = ctypes.c_void_p
    libobjc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p)(("objc_msgSend", libobjc))
    send_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                 ctypes.c_void_p)(("objc_msgSend", libobjc))
    window = send(ns_view, libobjc.sel_registerName(b"window"))
    if not window:
        return 0
    return int(send_long(window, libobjc.sel_registerName(b"windowNumber")))


def grab_below(ns_view: int, x: int, y: int, width: int, height: int):
    """QImage of the on-screen content under ``(x, y, width, height)``, in
    logical pixels, excluding our own window (None on refusal)."""
    global LAST_ERROR
    if sys.platform != "darwin":
        return None
    try:
        from PySide6.QtGui import QImage

        cg, cf = _libs()
        number = window_number(ns_view)
        if not number:
            return None
        rect = CGRect(CGPoint(float(x), float(y)),
                      CGSize(float(width), float(height)))
        image = cg.CGWindowListCreateImage(
            rect, OPTION_BELOW_WINDOW, number,
            IMAGE_BOUNDS_IGNORE_FRAMING | IMAGE_BEST_RESOLUTION)
        if not image:
            return None
        try:
            w = int(cg.CGImageGetWidth(image))
            h = int(cg.CGImageGetHeight(image))
            stride = int(cg.CGImageGetBytesPerRow(image))
            provider = cg.CGImageGetDataProvider(image)
            data = cg.CGDataProviderCopyData(provider)
            if not data:
                return None
            try:
                length = int(cf.CFDataGetLength(data))
                ptr = cf.CFDataGetBytePtr(data)
                if length < h * stride:
                    return None
                buf = ctypes.string_at(ptr, length)
            finally:
                cf.CFRelease(data)
        finally:
            cg.CGImageRelease(image)
        fmt = QImage.Format_ARGB32_Premultiplied
        out = QImage(buf, w, h, stride, fmt).copy()
        if width and height and (w != width or h != height):
            out = out.scaled(width, height)
        LAST_ERROR = ""
        return out
    except Exception as exc:  # noqa: BLE001 - no backdrop is a valid outcome
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None
