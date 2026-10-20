@echo off
chcp 65001 >nul
cd /d %~dp0
echo 横向平移触发试验台：只读鼠标位置，不截屏、不注册热键、不改配置。
echo 启动后浏览器打开打印出来的地址。
echo.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe tools\swipe_lab.py %*
) else if exist .build-venv\Scripts\python.exe (
  .build-venv\Scripts\python.exe tools\swipe_lab.py %*
) else (
  python tools\swipe_lab.py %*
)
pause
