# -*- mode: python ; coding: utf-8 -*-
"""macOS bundle for RelayDesk (onedir + .app).

Build on macOS:  pyinstaller RelayDesk-macos.spec --noconfirm --clean
Artifact:        dist/RelayDesk.app

onedir is intentional: a onefile .app unpacks ~110 MB to /tmp on every launch
and macOS Gatekeeper scans it each time, which made startup visibly slow.
"""

a = Analysis(
    ['scripts/launch_relaydesk.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
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
    [],
    exclude_binaries=True,
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
    icon=['assets/relaydesk.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='RelayDesk',
)
app = BUNDLE(
    coll,
    name='RelayDesk.app',
    icon='assets/relaydesk.icns',
    bundle_identifier='com.xinwangtao.relaydesk',
    info_plist={
        'CFBundleName': 'RelayDesk',
        'CFBundleDisplayName': 'RelayDesk',
        'CFBundleShortVersionString': '2.2.1',
        'CFBundleVersion': '2.2.1',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        # macOS 14+ asks before an app touches the local network; the daemon
        # needs UDP multicast + LAN TCP for discovery and pairing.
        'NSLocalNetworkUsageDescription': 'RelayDesk 需要访问局域网，以发现并连接已配对的设备。',
    },
)
