@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  echo 请先运行 install.bat 安装依赖
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pc.main
pause
