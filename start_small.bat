@echo off
rem DTM 小型模式（10 台以下、沒有中央）：先把 config.small.example.toml 複製成 config.small.toml 並修改
cd /d "%~dp0"
title DTM 小型模式 - 關閉這個視窗即停止監控（正式使用建議裝成服務）
set "PY="
rem 免安裝版（內含 Python）：先用同資料夾底下的 python\python.exe
if exist "%~dp0python\python.exe" set PY="%~dp0python\python.exe"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY py --version >nul 2>nul && set "PY=py"
if not defined PY goto nopython
%PY% -c "import tomllib" >nul 2>nul || goto oldpython
if not exist "config.small.toml" goto noconfig

echo 檢查設定檔…
%PY% -m dtm -c config.small.toml check
if errorlevel 1 goto badconfig

echo.
echo ============================================================
echo   DTM 小型模式啟動中，瀏覽器將自動打開全廠儀表板 http://localhost:8080
echo   第一次使用請在這台電腦上建立管理員帳號。
echo   監控期間請不要關閉這個黑色視窗。
echo ============================================================
echo.
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8080"
%PY% -m dtm -c config.small.toml run
goto end

:noconfig
echo.
echo 找不到 config.small.toml。請把 config.small.example.toml 複製一份、改名為 config.small.toml，
echo 填好廠區名稱、各機器人的 IP 與通知方式後，再雙擊 start_small.bat。
goto end

:badconfig
echo.
echo 設定檔有錯誤，請依照上面標示【錯誤】的說明修正 config.small.toml，存檔後再雙擊 start_small.bat。
goto end

:nopython
echo.
echo 找不到 Python。請安裝 Python 3.11 以上，
echo 安裝時第一個畫面下方要勾選 Add python.exe to PATH。
goto end

:oldpython
echo.
echo 這台電腦的 Python 版本太舊，需要 3.11 以上。
goto end

:end
echo.
pause
