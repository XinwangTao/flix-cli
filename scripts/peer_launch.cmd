@echo off
rem Launcher used on the PEER machine: start the model source setup quietly.
rem
rem ASCII only on purpose. cmd.exe reads this file with the OEM code page, so
rem Chinese comments here end up as question marks (this file used to have them),
rem and a Chinese placeholder inside the command line would be passed as garbage.
rem
rem Usage: peer_launch.cmd <gateway-token-of-the-source-machine>
rem   Example: peer_launch.cmd rd-xxxxxxxxxxxxxxxxxxxx
rem The token is passed straight through to peer_apply_update.ps1 -ModelSourceToken.
if "%~1"=="" (
    echo Usage: %~nx0 ^<gateway-token-of-the-source-machine^>
    echo Get it from the source machine: RelayDesk settings - Copy phone config
    exit /b 2
)
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0peer_apply_update.ps1" -ModelSourceToken "%~1"
exit /b 0
