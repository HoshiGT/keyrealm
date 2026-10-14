@echo off
rem 一键更新：备份 → 取源码 → 编译 → 数据库迁移 → 重启（薄引导，**无交互**）。
rem 逻辑全在 scripts/build_update.py（运行日志自动落 data\logs\build_update-*.log）。
rem 【Windows 分支未在开发机实测】
setlocal EnableExtensions
call "%~dp0_bootstrap.bat"
if errorlevel 1 (echo. & pause & exit /b 2)
"%PY%" "%~dp0build_update.py" %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (echo. & echo 更新失败，退出码 %RC% & pause)
exit /b %RC%
