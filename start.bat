@echo off
rem DTM 正式監控：需要先完成說明書第 8 步，建立 config.toml
cd /d "%~dp0"
title DTM 減速機扭力監控 - 關閉這個視窗即停止監控
set "PY="
rem 免安裝版（內含 Python）：先用同資料夾底下的 python\python.exe
if exist "%~dp0python\python.exe" set PY="%~dp0python\python.exe"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY py --version >nul 2>nul && set "PY=py"
if not defined PY goto nopython
%PY% -c "import tomllib" >nul 2>nul || goto oldpython

if not exist "config.toml" goto noconfig

echo 檢查設定檔…
%PY% -m dtm -c config.toml check
if errorlevel 1 goto badconfig

echo.
echo ============================================================
echo   DTM 監控啟動中，瀏覽器將自動打開 http://localhost:8080
echo   監控期間請不要關閉這個黑色視窗。
echo ============================================================
echo.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8080"
%PY% -m dtm -c config.toml run
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

:badconfig
echo.
echo 設定檔有錯誤，請依照上面標示【錯誤】的說明修正，
echo 存檔後再雙擊 start.bat。
goto end

:noconfig
echo.
echo 找不到 config.toml。請依說明書「第 8 步」把 config.example.toml
echo 複製一份、改名為 config.toml，並填好機器人的 IP。
goto end

:end
echo.
pause
