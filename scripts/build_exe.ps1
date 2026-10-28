# Build flix.exe / flixd.exe / RelayDesk.exe (PyInstaller onefile) from a repo
# checkout. Usage:  powershell -File scripts\build_exe.ps1
# Artifacts: dist\flix.exe (CLI+MCP+agent), dist\flixd.exe (daemon+web UI),
#            dist\RelayDesk.exe (windowed desktop GUI, branded icon).
# The launchers carry a _load_windows_store_certs tolerance shim: machines with
# vendor security agents can hold a corrupt Windows-store cert that aborts
# frozen apps at aiohttp import; the shim skips broken stores (LAN HTTP only).
python -m venv .venv
.venv\Scripts\pip install -e ".[gui]" pyinstaller
.venv\Scripts\pyinstaller --onefile --name flix  --paths src --clean -y scripts\launch_flix.py
.venv\Scripts\pyinstaller --onefile --name flixd --paths src --clean -y scripts\launch_flixd.py
.venv\Scripts\pyinstaller --onefile --windowed --icon assets\relaydesk.ico --name RelayDesk --paths src --clean -y scripts\launch_relaydesk.py
Write-Host "artifacts: dist\flix.exe dist\flixd.exe dist\RelayDesk.exe"
