@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   1. 本地预览（不联网，用现有数据）
echo   2. 用假数据重跑一遍，看全链路
echo   3. 真实采集（本机连不上 Steam，只有第三方会通）
echo   4. 构建可发布的 _site
echo.
set /p c=选择 [1-4]:
if "%c%"=="1" python serve.py
if "%c%"=="2" python collector\run.py --fake && python serve.py
if "%c%"=="3" python collector\run.py --batch 60 && python serve.py
if "%c%"=="4" python build_site.py
pause
