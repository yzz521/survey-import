@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PORT=8765
set URL=http://127.0.0.1:%PORT%

where python >nul 2>nul
if errorlevel 1 (
  echo 找不到 Python。请先安装 https://www.python.org/downloads/ 并勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 第一次使用：正在创建本地环境并安装依赖（大约半分钟）…
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

echo 正在启动…
start "" cmd /c "timeout /t 2 /nobreak >nul & start %URL%"
".venv\Scripts\python.exe" app.py
pause
