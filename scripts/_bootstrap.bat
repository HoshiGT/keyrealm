@echo off
rem 共享引导：确保项目下有一个可用的 Python（含虚拟环境与依赖）。**无交互**。
rem 用法（在其它 .bat 里）：
rem   call "%~dp0_bootstrap.bat"
rem   if errorlevel 1 exit /b %errorlevel%
rem   "%PY%" "%~dp0<脚本>.py" %*
rem 注意：本文件**不 setlocal**——它要把 ROOT / PY 留在调用者的环境里（靠 call 同环境生效）。
rem 【Windows 分支未在开发机（Linux/WSL）实测】

set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  set "BOOT="
  for %%P in (py.exe) do if not defined BOOT if exist "%%~$PATH:P" set "BOOT=py -3"
  if not defined BOOT for %%P in (python.exe) do if not defined BOOT if exist "%%~$PATH:P" set "BOOT=python"
  if not defined BOOT (
    echo 未找到 Python，请先安装 Python 3.11+ 后重试 1>&2
    exit /b 2
  )
  echo 创建虚拟环境 .venv ...
  %BOOT% -m venv "%ROOT%\.venv" || (echo 创建 venv 失败 1>&2 & exit /b 2)
)

echo 安装 / 校验依赖 ...（失败不中止：稍后由更新器按代理重试）
"%PY%" -m pip install -q -r "%ROOT%\requirements.txt"
if errorlevel 1 echo 提示：requirements 未装成功，稍后由更新器按代理重试 1>&2
"%PY%" -m pip install -q pyinstaller
if errorlevel 1 echo 提示：pyinstaller 未装成功，稍后由更新器按代理重试 1>&2

rem 让中文与符号在控制台正常显示
chcp 65001 >nul 2>&1
exit /b 0
