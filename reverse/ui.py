"""Minimal Windows UI driver: screenshot / click / paste-type for Flix RE.

Usage: python ui.py shot <out.png>
       python ui.py click <x> <y>
       python ui.py paste <text>          (clipboard + Ctrl-V)
       python ui.py key <vk_name>         (enter/esc/tab)
       python ui.py rect <title_substr>
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)


def find_hwnd(title_substr: str) -> int:
    res = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, buf, 256)
        if title_substr.lower() in buf.value.lower() and u32.IsWindowVisible(hwnd):
            res.append(hwnd)
        return True
    u32.EnumWindows(cb, 0)
    return res[0] if res else 0


def rect(hwnd):
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def shot(out: str) -> None:
    from PIL import ImageGrab
    img = ImageGrab.grab()
    img.save(out)
    print("saved", out, img.size)


def click(x: int, y: int) -> None:
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    u32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    u32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    print(f"clicked {x},{y}")


def rclick(x: int, y: int) -> None:
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    u32.mouse_event(0x0008, 0, 0, 0, 0)  # RIGHTDOWN
    u32.mouse_event(0x0010, 0, 0, 0, 0)  # RIGHTUP
    print(f"rclicked {x},{y}")


def paste(text: str) -> None:
    import subprocess
    subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
    u32.keybd_event(0x11, 0, 0, 0)
    u32.keybd_event(ord("V"), 0, 0, 0)
    u32.keybd_event(ord("V"), 0, 2, 0)
    u32.keybd_event(0x11, 0, 2, 0)
    print("pasted", text[:30])


def type_text(text: str) -> None:
    import time as _t
    for ch in text:
        vk = ord(ch.upper()) if ch.isalnum() else 0
        if vk:
            u32.keybd_event(vk, 0, 0, 0)
            u32.keybd_event(vk, 0, 2, 0)
        _t.sleep(0.06)
    print("typed", text)


def key(name: str) -> None:
    vk = {"enter": 0x0D, "esc": 0x1B, "tab": 0x09, "back": 0x08}[name]
    u32.keybd_event(vk, 0, 0, 0)
    u32.keybd_event(vk, 0, 2, 0)
    print("key", name)


def focus(hwnd: int) -> None:
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    print("focused", hwnd)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "shot":
        shot(sys.argv[2])
    elif cmd == "click":
        click(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "rclick":
        rclick(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "type":
        type_text(sys.argv[2])
    elif cmd == "paste":
        paste(sys.argv[2])
    elif cmd == "key":
        key(sys.argv[2])
    elif cmd == "rect":
        h = find_hwnd(sys.argv[2])
        print(hex(h), rect(h) if h else "not found")
    elif cmd == "focus":
        h = find_hwnd(sys.argv[2])
        if h:
            focus(h)
            time.sleep(0.5)
            print("rect", rect(h))
        else:
            print("not found")
