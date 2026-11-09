# -*- mode: python ; coding: utf-8 -*-

# One spec, two flavors — the only difference is whether the optional media
# stack is bundled:
#
#   default (lite)        ~50 MB: Qt window + CLI + PTY. Screen sharing is
#                         absent; media_transport degrades to available()=False.
#   RELAYDESK_BUILD=full  additionally bundles aiortc/av/numpy/PIL so the
#                         "full" download keeps screen sharing.
#
# The lite flavor is what the auto-updater picks by default, so a missing media
# dependency must never break startup.
import os

FULL = os.environ.get("RELAYDESK_BUILD", "").strip().lower() == "full"

# pywinpty ships native DLLs, so they have to be collected explicitly. A missing
# backend must not break the build: exec-only still works without PTY support.
_winpty_datas, _winpty_binaries, _winpty_hidden = [], [], []
try:
    from PyInstaller.utils.hooks import collect_all
    _winpty_datas, _winpty_binaries, _winpty_hidden = collect_all('winpty')
except Exception as _exc:  # noqa: BLE001 - build-time best effort
    print(f"[spec] winpty not collected: {_exc}")



# The frozen app has no pyproject.toml to read, so `flix --version`, `flix self`
# and MCP serverInfo would report 0.0.0+source. Bundle the installed package
# metadata (copy_metadata) so importlib.metadata resolves the real version.
try:
    from PyInstaller.utils.hooks import copy_metadata as _copy_metadata
    _version_datas = _copy_metadata('flixcli')
except Exception as _exc:  # noqa: BLE001 - build-time best effort
    print(f"[spec] flixcli metadata not bundled: {_exc}")
    _version_datas = []

# The PySide6 hook collects far more than the QWidgets UI touches: the whole
# QML/Quick stack, Qt6Pdf (only needed by the qpdf image plugin) and the
# virtual keyboard. None of them are imported anywhere in src/flixcli.
QT_EXCLUDES = [
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtPdf',
    'PySide6.QtVirtualKeyboard', 'PySide6.QtOpenGL',
    # The legacy Tk window is retired; never ship Tcl/Tk again.
    'tkinter', '_tkinter',
]

# Screen sharing is the only consumer of these. media_transport imports them in
# a try/except, so excluding them yields a working lite build.
MEDIA_EXCLUDES = [
    'av', 'aiortc', 'numpy', 'PIL', 'mss', 'pylibsrtp',
    'aioice', 'pyee', 'google_crc32c',
]

a = Analysis(
    ['scripts/launch_relaydesk.py'],
    pathex=['src'],
    binaries=_winpty_binaries,
    datas=_winpty_datas + _version_datas,
    hiddenimports=['flixcli.cli', 'flixcli.mcp_server', 'flixcli.daemon',
                   'flixcli.ptyshell', 'flixcli.autoupdate'] + _winpty_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=QT_EXCLUDES + ([] if FULL else MEDIA_EXCLUDES),
    noarchive=False,
    optimize=0,
)


# ---------------------------------------------------------------- Qt trimming
# The hook also collects every plugin family and all ~140 Qt translations. Keep
# only what this app can actually use: the windows platform plugin, the SVG
# icon engine, common raster formats, the Windows TLS backend, and zh_CN/en
# translations. The full flavor is trimmed the same way — media adds codecs,
# not Qt surface.
_DROP_PREFIXES = (
    'PySide6/Qt6Qml',
    'PySide6/Qt6Quick',
    'PySide6/Qt6Pdf',
    'PySide6/Qt6VirtualKeyboard',
    'PySide6/Qt6OpenGL',
)
_DROP_PATHS = (
    'PySide6/opengl32sw.dll',
    'PySide6/plugins/generic/',
    'PySide6/plugins/networkinformation/',
    'PySide6/plugins/platforminputcontexts/',
    'PySide6/plugins/platforms/qdirect2d.dll',
    'PySide6/plugins/platforms/qminimal.dll',
    'PySide6/plugins/platforms/qoffscreen.dll',
    'PySide6/plugins/imageformats/qicns.dll',
    'PySide6/plugins/imageformats/qpdf.dll',
    'PySide6/plugins/imageformats/qtga.dll',
    'PySide6/plugins/imageformats/qtiff.dll',
    'PySide6/plugins/imageformats/qwbmp.dll',
    'PySide6/plugins/tls/qopensslbackend.dll',
    'PySide6/plugins/tls/qcertonlybackend.dll',
)
# qt_zh_CN.qm / qtbase_zh_CN.qm / qt_en.qm / qtbase_en.qm stay; qt_help_*.qm do
# not (the app has no Qt Assistant-style help windows).
_KEEP_TRANSLATION_SUFFIXES = ('_zh_CN.qm', '_en.qm')
_DROP_TRANSLATION_NAMES = ('qt_help_en.qm', 'qt_help_zh_CN.qm')


def _keep(dest):
    path = dest.replace('\\', '/')
    if any(path.startswith(prefix) for prefix in _DROP_PREFIXES):
        return False
    if any(path == drop or path.startswith(drop) for drop in _DROP_PATHS):
        return False
    if path.startswith('PySide6/translations/'):
        name = path.rsplit('/', 1)[-1]
        if name in _DROP_TRANSLATION_NAMES:
            return False
        return path.endswith(_KEEP_TRANSLATION_SUFFIXES)
    return True


a.binaries = [entry for entry in a.binaries if _keep(entry[0])]
a.datas = [entry for entry in a.datas if _keep(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RelayDesk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/relaydesk.ico'],
)
