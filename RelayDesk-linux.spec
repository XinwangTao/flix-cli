# -*- mode: python ; coding: utf-8 -*-
"""Linux bundle for RelayDesk (onefile binary).

Build on Linux:  pyinstaller RelayDesk-linux.spec --noconfirm --clean
Artifact:        dist/RelayDesk

Same single binary as Windows (`RelayDesk serve` / `RelayDesk mcp` / no argument
opens the window). The Windows-only winpty collection lives in RelayDesk.spec and
is deliberately absent here: the Linux PTY backend uses the stdlib `pty` module.
"""


# The frozen app has no pyproject.toml to read, so `flix --version`, `flix self`
# and MCP serverInfo would report 0.0.0+source. Bundle the installed package
# metadata (copy_metadata) so importlib.metadata resolves the real version.
try:
    from PyInstaller.utils.hooks import copy_metadata as _copy_metadata
    _version_datas = _copy_metadata('flixcli')
except Exception as _exc:  # noqa: BLE001 - build-time best effort
    print(f"[spec] flixcli metadata not bundled: {_exc}")
    _version_datas = []

a = Analysis(
    ['scripts/launch_relaydesk.py'],
    pathex=['src'],
    binaries=[],
    datas=_version_datas,
    hiddenimports=['flixcli.cli', 'flixcli.mcp_server', 'flixcli.daemon',
                   'flixcli.ptyshell'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
