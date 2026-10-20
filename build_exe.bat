@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .build-venv (
  echo 创建打包环境 .build-venv ...
  python -m venv .build-venv
)
call .build-venv\Scripts\activate.bat
python -m pip install -q -U pip
REM pywebview / pythonnet：主窗口内嵌 WebView2（Chromium）需要（pywebview 自带
REM PyInstaller hook，会自动把 Microsoft.Web.WebView2.*.dll 打进去）
python -m pip install -q aiohttp mss keyboard Pillow qrcode pyinstaller zeroconf psutil pywebview pythonnet
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name 无障碍助手PC ^
  --icon assets\app.ico ^
  --add-data "web;web" ^
  --exclude-module numpy --exclude-module rapidocr_onnxruntime ^
  run_pc.py
echo.
echo 打包完成：dist\无障碍助手PC.exe（数据文件生成在 exe 旁边）
pause
