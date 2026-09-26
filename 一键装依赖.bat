@echo off
rem ===== 港口 · 一键装依赖（给没有 AI 助手的人）=====
echo ===== 港口 · 一键装依赖 =====
echo 需要 Python 3.10+ 已安装（python --version 能出版本号）。
echo.
where python >nul 2>&1
if errorlevel 1 (
  echo [!] 找不到 python —— 先去 python.org 装一个，安装时勾选 Add to PATH，再回来双击本文件。
  pause
  exit /b 1
)
if not exist ".venv" (
  echo [1/2] 创建虚拟环境 .venv ...
  python -m venv .venv
)
echo [2/2] 安装依赖（几分钟，别关窗）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [!] 装依赖失败了——看看上面的红字，或者找有 AI 助手的朋友读 安装指南-给AI.md。
  pause
  exit /b 1
)
echo.
echo 装好了！双击 港口.exe 就能见到她。第一次记得进 设置→大脑 填你自己的 API Key。
pause
