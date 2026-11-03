@echo off
rem DTM 中央伺服器：先把 config.central.example.toml 複製成 config.central.toml 並修改
cd /d "%~dp0"
title DTM 中央伺服器 - 關閉這個視窗即停止（正式使用請裝成服務）
set "PY="
rem 免安裝版（內含 Python）：先用同資料夾底下的 python\python.exe
if exist "%~dp0python\python.exe" set PY="%~dp0python\python.exe"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY py --version >nul 2>nul && set "PY=py"
if not defined PY goto nopython
%PY% -c "import tomllib" >nul 2>nul || goto oldpython
if not exist "config.central.toml" goto noconfig

%PY% -m dtm -c config.central.toml check
if errorlevel 1 goto badconfig
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:8090"
%PY% -m dtm -c config.central.toml run
goto end

:noconfig
echo.
echo 找不到 config.central.toml。請把 config.central.example.toml 複製一份改名為 config.central.toml，
echo 修改上傳金鑰、廠區名稱與通知設定後再執行。只想看畫面請雙擊 start_central_demo.bat。
goto end

:badconfig
echo.
echo 設定檔有錯誤，請依上面的訊息修正 config.central.toml。
goto end

:nopython
echo.
echo 找不到 Python。請依說明書「第 2 步」安裝 Python，
echo 安裝時第一個畫面下方要勾選 Add python.exe to PATH。
goto end

:oldpython
echo.
echo 這台電腦的 Python 版本太舊，需要 3.11 以上。
goto end

:end
echo.
pause
