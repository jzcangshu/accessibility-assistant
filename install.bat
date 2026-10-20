@echo off
chcp 65001 >nul
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -U pip -q
pip install -r requirements.txt
echo.
echo 安装完成！运行 start.bat 启动。
pause
