@echo off
rem DTM 模擬器：雙擊這個檔案即可試跑，不需要接機器人
cd /d "%~dp0"
title DTM 模擬器 - 關閉這個視窗即停止
set "PY="
rem 免安裝版（內含 Python）：先用同資料夾底下的 python\python.exe
if exist "%~dp0python\python.exe" set PY="%~dp0python\python.exe"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY py --version >nul 2>nul && set "PY=py"
if not defined PY goto nopython
%PY% -c "import tomllib" >nul 2>nul || goto oldpython

if not exist "dtm_sim.sqlite3" (
  echo.
  echo 第一次執行：正在產生 180 天的模擬資料，大約需要 1 分鐘，請稍候...
  echo.
  %PY% -m dtm -c config.sim.toml simulate-history --days 180
  if errorlevel 1 goto failed
)

echo.
echo ============================================================
echo   DTM 儀表板啟動中，瀏覽器將自動打開 http://localhost:8080
echo   使用期間請不要關閉這個黑色視窗；要停止就直接關閉它。
echo ============================================================
echo.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8080"
%PY% -m dtm -c config.sim.toml run
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

:failed
echo.
echo 產生模擬資料時發生錯誤，請把上面的訊息拍照或複製下來。
goto end

:end
echo.
pause
