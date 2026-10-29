@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo [1/3] 创建虚拟环境...
  python -m venv .venv || (echo 未找到 Python 3.11+，请先安装 & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
echo [2/3] 检查依赖...
pip install -q -r requirements.txt || (echo 依赖安装失败 & pause & exit /b 1)
echo [3/3] 启动（真实浏览器模式，需本机已安装 Google Chrome）...
python -m shopmgr %*
pause
