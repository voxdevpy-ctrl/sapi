@echo off
rem Instala o SAPI nesta pasta (sem administrador). Depois, use o iniciar.bat.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
pause
