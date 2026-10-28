"""Session-scoped, deliberately limited Windows input control.

This module never executes commands and does not accept text payloads. The only
supported operations are normalized pointer motion, mouse buttons/wheel and a
small non-modifier key allow-list. Callers must enforce per-session approval.
"""
from __future__ import annotations

import ctypes
import sys
import time
from collections import deque
from ctypes import wintypes


class ControlError(ValueError):
    pass


KEYS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(i): ord(str(i)) for i in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
BUTTON_FLAGS = {
    ("left", "down"): 0x0002, ("left", "up"): 0x0004,
    ("right", "down"): 0x0008, ("right", "up"): 0x0010,
    ("middle", "down"): 0x0020, ("middle", "up"): 0x0040,
}


class RateGate:
    def __init__(self, maximum: int = 120, window: float = 1.0):
        self.maximum = maximum
        self.window = window
        self.samples: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self.samples and now - self.samples[0] > self.window:
            self.samples.popleft()
        if len(self.samples) >= self.maximum:
            return False
        self.samples.append(now)
        return True


def validate_event(event: dict, previous_seq: int = -1) -> dict:
    if not isinstance(event, dict):
        raise ControlError("event must be an object")
    if set(event) - {"v", "seq", "kind", "x", "y", "button", "action",
                     "delta", "key"}:
        raise ControlError("unknown control field")
    if event.get("v") != 1:
        raise ControlError("unsupported control version")
    seq = event.get("seq")
    if not isinstance(seq, int) or seq <= previous_seq:
        raise ControlError("sequence must increase")
    kind = event.get("kind")
    clean = {"v": 1, "seq": seq, "kind": kind}
    if kind == "pointer":
        x, y = event.get("x"), event.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ControlError("pointer coordinates must be numbers")
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ControlError("pointer coordinates must be normalized")
        clean.update(x=float(x), y=float(y))
    elif kind == "button":
        key = (str(event.get("button", "")), str(event.get("action", "")))
        if key not in BUTTON_FLAGS:
            raise ControlError("unsupported mouse button/action")
        clean.update(button=key[0], action=key[1])
    elif kind == "wheel":
        delta = event.get("delta")
        if not isinstance(delta, int) or not -1200 <= delta <= 1200:
            raise ControlError("wheel delta out of range")
        clean["delta"] = delta
    elif kind == "key":
        key = str(event.get("key", "")).lower()
        action = str(event.get("action", "tap"))
        if key not in KEYS or action not in ("down", "up", "tap"):
            raise ControlError("unsupported key/action")
        clean.update(key=key, action=action)
    else:
        raise ControlError("unsupported control kind")
    return clean


if sys.platform == "win32":
    ULONG_PTR = wintypes.WPARAM

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


def _send_input(item) -> None:
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(item))
    if sent != 1:
        raise ControlError("SendInput was denied")


def apply_event(event: dict) -> None:
    if sys.platform != "win32":
        raise ControlError("input control is Windows-only")
    kind = event["kind"]
    if kind == "pointer":
        flags = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
        item = INPUT(type=0, mi=MOUSEINPUT(round(event["x"] * 65535),
                                          round(event["y"] * 65535), 0, flags, 0, 0))
        _send_input(item)
    elif kind == "button":
        item = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0,
                                          BUTTON_FLAGS[(event["button"], event["action"])],
                                          0, 0))
        _send_input(item)
    elif kind == "wheel":
        item = INPUT(type=0, mi=MOUSEINPUT(0, 0, event["delta"] & 0xFFFFFFFF,
                                          0x0800, 0, 0))
        _send_input(item)
    else:
        vk = KEYS[event["key"]]
        actions = ("down", "up") if event["action"] == "tap" else (event["action"],)
        for action in actions:
            item = INPUT(type=1, ki=KEYBDINPUT(vk, 0, 0x0002 if action == "up" else 0,
                                              0, 0))
            _send_input(item)
