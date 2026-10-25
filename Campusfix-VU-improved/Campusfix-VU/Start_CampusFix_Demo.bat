@echo off
rem Starts CampusFix with sample reports so the dashboard and map are not empty.
setlocal
cd /d "%~dp0"
set CAMPUSFIX_DEMO_DATA=true
call Start_CampusFix.bat
