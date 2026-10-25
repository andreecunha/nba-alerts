@echo off
title Alertas NBA
cd /d "%~dp0"
if not exist token.txt (
  echo Falta o ficheiro token.txt com o token do bot nesta pasta.
  pause
  exit /b
)
set /p TELEGRAM_TOKEN=<token.txt
:loop
py bot.py
echo O bot parou. A reiniciar em 10 segundos...
timeout /t 10 >nul
goto loop
