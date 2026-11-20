@echo off
chcp 65001 >nul
title 汽水音乐自动看广告（ADB 无线调试 · 扫码配对）
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3 并加入 PATH。
    pause
    exit /b 1
)

:menu
cls
echo ==========================================================
echo    汽水音乐自动看广告脚本 - 无线调试配对
echo ==========================================================
echo   [1] 扫码配对启动（推荐：电脑显示二维码，手机扫一扫）
echo   [2] 直接启动（已配对过，自动连接）
echo   [3] 配对码配对启动（手机显示 6 位配对码）
echo   [4] 诊断连接（adb 路径 / 设备 / mDNS）
echo   [5] 强制重新配对（忽略已连接设备）
echo   [6] 使用自定义 adb 路径启动
echo   [0] 退出
echo.
echo   首次使用请看 README.md 的「连接手机」章节。
echo.
choice /c 1234560 /n /m "输入序号后回车（默认 1）: "
if errorlevel 7 goto quit
if errorlevel 6 goto custom_adb
if errorlevel 5 goto repair
if errorlevel 4 goto diag
if errorlevel 3 goto pair_code
if errorlevel 2 goto run
if errorlevel 1 goto scan
goto menu

:scan
echo.
echo [扫码配对] 稍等，电脑会生成并弹出配对二维码。
echo            手机上：设置 - 开发者选项 - 无线调试 - 使用二维码配对设备 - 扫一扫
echo.
python qishui_auto.py --qr-make
echo.
echo [Exit code: %ERRORLEVEL%]
pause
goto menu

:run
echo.
python qishui_auto.py
echo.
echo [Exit code: %ERRORLEVEL%]
pause
goto menu

:pair_code
echo.
echo [配对码配对] 手机上：设置 - 开发者选项 - 无线调试 - 使用配对码配对设备
echo               页面会显示 6 位配对码，把它填到下面。
echo.
set "PCODE="
set /p PCODE=请输入 6 位配对码（直接回车返回菜单）: 
if "%PCODE%"=="" goto menu
python qishui_auto.py --pair-code "%PCODE%"
echo.
echo [Exit code: %ERRORLEVEL%]
pause
goto menu

:diag
echo.
python qishui_auto.py --diag
echo.
pause
goto menu

:repair
echo.
python qishui_auto.py --repair
echo.
echo [Exit code: %ERRORLEVEL%]
pause
goto menu

:custom_adb
echo.
set "ADBPATH="
set /p ADBPATH=请输入 adb.exe 的完整路径（例如 D:\platform-tools\adb.exe）: 
if "%ADBPATH%"=="" goto menu
python qishui_auto.py --adb "%ADBPATH%"
echo.
echo [Exit code: %ERRORLEVEL%]
pause
goto menu

:quit
exit /b 0
