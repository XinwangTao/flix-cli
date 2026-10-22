# Build flix.exe / flixd.exe (PyInstaller onefile) from a repo checkout.
# Usage:  powershell -File scripts\build_exe.ps1
# Artifacts: dist\flix.exe (CLI+MCP+agent), dist\flixd.exe (daemon+web UI)
python -m venv .venv
.venv\Scripts\pip install -e . pyinstaller
New-Item build -ItemType Directory -Force | Out-Null
Set-Content build\launch_flix.py  'import sys`nfrom flixcli.cli import main`nsys.exit(main())'
Set-Content build\launch_flixd.py 'import sys`nfrom flixcli.daemon import main`nsys.exit(main())'
.venv\Scripts\pyinstaller --onefile --name flix  --paths src --clean -y build\launch_flix.py
.venv\Scripts\pyinstaller --onefile --name flixd --paths src --clean -y build\launch_flixd.py
Write-Host "artifacts: dist\flix.exe dist\flixd.exe"
