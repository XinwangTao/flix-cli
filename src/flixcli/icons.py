"""Vector icon set for the RelayDesk desktop client.

Hand-drawn 24x24 icons with square caps and 1.8px strokes: the squared
terminals read like instrument markings instead of the rounded default icon
library look. The brand mark is a flat ink tile with a single relay arc.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_M = ('fill="none" stroke="COLOR" stroke-width="1.8" '
      'stroke-linecap="square" stroke-linejoin="miter"')

ICONS: dict[str, str] = {
    # devices / session picker
    "devices": f'<rect x="2" y="4" width="14" height="11" rx="2" {_M}/>'
               f'<path d="M6 19h6M22 8v7a2 2 0 0 1-2 2h-3" {_M}/>'
               f'<circle cx="19" cy="5" r="2" {_M}/>',
    "search": f'<circle cx="11" cy="11" r="7" {_M}/><path d="m20 20-3.5-3.5" {_M}/>',
    "groups": f'<circle cx="9" cy="8" r="3.2" {_M}/>'
              f'<path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5S14 16 14.5 19" {_M}/>'
              f'<circle cx="17" cy="9.5" r="2.6" {_M}/>'
              f'<path d="M16 14.7c2.6.1 4.2 1.5 4.7 4.3" {_M}/>',
    "transfers": f'<path d="M7 4v12M7 4 4 7M7 4l3 3" {_M}/>'
                 f'<path d="M17 20V8m0 12 3-3m-3 3-3-3" {_M}/>',
    "android": f'<rect x="7" y="3" width="10" height="18" rx="2.4" {_M}/>'
               f'<path d="M11 17.6h2" {_M}/>',
    "settings": f'<circle cx="12" cy="12" r="3.1" {_M}/>'
                f'<path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4'
                f'M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M18.5 5.5l-1.7 1.7'
                f'M7.2 16.8l-1.7 1.7" {_M}/>',
    "file": f'<path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" {_M}/>'
            f'<path d="M13 3v6h6" {_M}/>',
    "clipboard": f'<rect x="5" y="4" width="14" height="17" rx="2" {_M}/>'
                 f'<path d="M9 4a2 2 0 0 1 6 0" {_M}/>'
                 f'<path d="M9 11h6M9 15h4" {_M}/>',
    "remote": f'<rect x="3" y="4" width="18" height="13" rx="2" {_M}/>'
              f'<path d="M8 21h8M12 17v4" {_M}/>'
              f'<path d="m10 8 3.5 2.5L10 13" {_M}/>',
    "more": f'<circle cx="5.5" cy="12" r="1.4" fill="COLOR" stroke="none"/>'
            f'<circle cx="12" cy="12" r="1.4" fill="COLOR" stroke="none"/>'
            f'<circle cx="18.5" cy="12" r="1.4" fill="COLOR" stroke="none"/>',
    "send": f'<path d="M21 3 10.5 13.5M21 3l-6.8 18-3.7-7.5L3 9.8z" {_M}/>',
    "image": f'<rect x="3" y="4" width="18" height="16" rx="2" {_M}/>'
             f'<circle cx="9" cy="10" r="1.7" {_M}/>'
             f'<path d="m5 18 4.5-4.5L13 17l3-3 3 3" {_M}/>',
    "min": f'<path d="M6 12h12" {_M}/>',
    "max": f'<rect x="6" y="6" width="12" height="12" rx="1.5" {_M}/>',
    "restore": f'<rect x="5" y="8" width="11" height="11" rx="1.5" {_M}/>'
               f'<path d="M9 5h9a1.5 1.5 0 0 1 1.5 1.5V15" {_M}/>',
    "close": f'<path d="M6.5 6.5l11 11M17.5 6.5l-11 11" {_M}/>',
    "refresh": f'<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" {_M}/>'
               f'<path d="M21 3v5h-5" {_M}/>',
    "plus": f'<path d="M12 5v14M5 12h14" {_M}/>',
}

LOGO_MARK = (
    '<path d="M16 48C26 48 38 16 48 16" fill="none" stroke="#4fc38f" '
    'stroke-width="5.5" stroke-linecap="square"/>'
    '<circle cx="16" cy="48" r="7.5" fill="#e9eeeb"/>'
    '<circle cx="48" cy="16" r="7.5" fill="#4fc38f"/>'
    '<circle cx="32" cy="32" r="3.4" fill="#e9eeeb"/>'
)


def _svg(body: str, color: str, size: int) -> QByteArray:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
           f'height="{size}" viewBox="0 0 24 24">{body}</svg>')
    return QByteArray(svg.replace("COLOR", color).encode("utf-8"))


def pixmap(name: str, color: str = "#8291a8", size: int = 20) -> QPixmap:
    renderer = QSvgRenderer(_svg(ICONS[name], color, size))
    pm = QPixmap(size * 2, size * 2)  # 2x for crisp display scaling
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, pm.width(), pm.height()))
    painter.end()
    pm.setDevicePixelRatio(2.0)
    return pm


def icon(name: str, color: str = "#8291a8", size: int = 20) -> QIcon:
    return QIcon(pixmap(name, color, size))


def logo_pixmap(size: int = 256, with_background: bool = True) -> QPixmap:
    """Brand mark: two relay nodes joined by one arc on a flat ink tile."""
    zoom = 2  # render at 2x so the mark stays crisp on hidpi displays
    pm = QPixmap(size * zoom, size * zoom)
    pm.setDevicePixelRatio(zoom)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    # QPainter works in logical coordinates once the pixmap carries a DPR.
    if with_background:
        radius = size * 0.24
        painter.setPen(QColor("#34433c"))
        painter.setBrush(QColor("#1a2420"))
        painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1),
                                radius, radius)
    k = size / 64.0
    painter.scale(k, k)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
           f'viewBox="0 0 64 64">{LOGO_MARK}</svg>')
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, 64, 64))
    painter.end()
    return pm


def logo_icon() -> QIcon:
    return QIcon(logo_pixmap(256))
