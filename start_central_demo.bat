@echo off
rem DTM 中央演示：雙擊即可看中央 8 個畫面（6 廠 160 台演示資料，不接機器人、不發通知）
cd /d "%~dp0"
title DTM 中央演示 - 關閉這個視窗即停止
set "PY="
rem 免安裝版（內含 Python）：先用同資料夾底下的 python\python.exe
if exist "%~dp0python\python.exe" set PY="%~dp0python\python.exe"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY py --version >nul 2>nul && set "PY=py"
if not defined PY goto nopython
%PY% -c "import tomllib" >nul 2>nul || goto oldpython

echo.
echo ============================================================
echo   DTM 中央演示啟動中，瀏覽器將自動打開 http://localhost:8090
echo   畫面上的數字都是程式產生的演示資料。
echo   使用期間請不要關閉這個黑色視窗；要停止就直接關閉它。
echo ============================================================
echo.
start "" cmd /c "timeout /t 8 /nobreak >nul & start http://localhost:8090"
%PY% -m dtm -c config.central.example.toml demo
goto end

:nopython
echo.
echo 找不到 Python。請依說明書「第 2 步」安裝 Python，
echo 安裝時第一個畫面下方要勾選 Add python.exe to PATH。
goto end

:oldpython
echo.
echo 這台電腦的 Python 版本太舊，需要 3.11 以上。
echo 請依說明書「第 2 步」安裝新版 Python。
goto end

:end
echo.
pause
