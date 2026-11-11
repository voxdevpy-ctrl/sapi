@echo off
rem Abre o SAPI (interface + gerente da linha de producao) e o navegador.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo O SAPI ainda nao foi instalado nesta pasta. Rode primeiro o instalar.bat.
  pause
  exit /b 1
)
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PYTHONIOENCODING=utf-8
rem abre o navegador 3 s depois, quando o servidor ja esta respondendo
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:8770'"
".venv\Scripts\python.exe" -m cortes iniciar
