@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .build-venv (
  echo 创建打包环境 .build-venv ...
  python -m venv .build-venv
)
call .build-venv\Scripts\activate.bat
python -m pip install -q -U pip
python -m pip install -q aiohttp mss keyboard Pillow qrcode pyinstaller zeroconf psutil pywebview pythonnet rapidocr-onnxruntime
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name 无障碍助手PC-OCR ^
  --icon assets\app.ico ^
  --add-data "web;web" ^
  --collect-all rapidocr_onnxruntime ^
  run_pc.py
echo.
echo 打包完成：dist\无障碍助手PC-OCR.exe（约 100MB，含本地 OCR）
pause
