@echo off
rem Makes CampusFix reachable from other devices on the same trusted Wi-Fi/LAN.
rem Visitors can view and report; only people with the moderator token can edit or delete.
setlocal
cd /d "%~dp0"
set HOST=0.0.0.0
call Start_CampusFix.bat
