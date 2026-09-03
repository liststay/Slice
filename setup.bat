@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 setup_env.py %*
  exit /b %errorlevel%
)
where python >nul 2>&1
if %errorlevel%==0 (
  python setup_env.py %*
  exit /b %errorlevel%
)
echo 未找到 Python。请先安装 Python 3.10+ 并勾选 Add python.exe to PATH。
exit /b 1
