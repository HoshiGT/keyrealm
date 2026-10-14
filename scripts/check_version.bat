@echo off
rem 检查更新（薄引导，**无交互**）。【Windows 分支未在开发机实测】
setlocal EnableExtensions
call "%~dp0_bootstrap.bat"
if errorlevel 1 (echo. & pause & exit /b 2)
"%PY%" "%~dp0check_version.py" %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (echo. & echo 执行失败，退出码 %RC% & pause)
exit /b %RC%
