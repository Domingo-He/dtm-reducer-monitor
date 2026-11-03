# 發表前檢查清單（維護者用）

每次發布 GitHub Release 前照順序檢查。打勾的方式：複製到 Issue 或筆記裡逐項勾選。

## 1. 內容

- [ ] **沒有任何公司的資料**：前公司或現職公司的報告、簡報、照片、生產資料、設定檔、IP、人名、電話都不在檔案裡，也不在提交紀錄裡。範例、截圖、測試資料全部來自模擬器或演示資料。
- [ ] 介紹簡報、改善報告、手冊等文件不放進程式倉庫。
- [ ] 搜尋可能的機密（在程式資料夾執行）：
  ```bash
  git grep -nIE "(password|token|secret)\s*=\s*\"[^\"e]" -- ':!tests' ':!docs'
  git grep -nIE "\b09[0-9]{8}\b|\b1[0-9]{2}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b" -- ':!tests'
  ```
  命中的項目逐一確認是範例（例如 `0912345678`、`192.168.10.11`）而不是真的。
- [ ] 沒有指向私人文件或對話的連結（檔案與提交紀錄都要查）：
  ```bash
  git grep -n "claude.ai/" -- ':!*.md'
  git log -p --all -S "claude.ai/code/artifact" --oneline
  ```
  提交訊息裡的 `Claude-Session:` 標記是 AI 共同作者紀錄，不是文件連結；歷史裡若有私人文件連結，推送前先清除。
- [ ] 6 種語言完整：`python tools/i18n_tool.py check`、`python tools/workpack_l10n.py check` 都是「缺 0」。
- [ ] 版本號一致：`dtm/__init__.py`、`CITATION.cff`（version、date-released）、`CHANGELOG.md`。
- [ ] README 開頭的 Beta 標示、〈第一次導入建議〉、〈授權與聲明〉都在；截圖是這一版產生的（`docs/screenshots/`）。
- [ ] 〈功能狀態表〉（`docs/FEATURE_STATUS.md`）的日期、測試項數與 README 開頭的摘要表一致；這一版新增的功能已列入；有實機或現場驗證結果時，更新「實機」「現場」兩欄。
- [ ] 提交紀錄的作者是「Domingo He」；Email 會公開在提交紀錄裡，確認可以接受（不想公開就改用 GitHub 提供的 noreply 信箱後再推送）。

## 2. 測試

- [ ] `python -m unittest discover -s tests` 全部通過；`python -m pyflakes dtm tests tools` 沒有警告。
- [ ] GitHub Actions 的 tests（Windows／Ubuntu × Python 3.11–3.13）全部綠燈。
- [ ] 在真的 Windows 電腦上：雙擊 `start_sim.bat` 能開儀表板；`start_central_demo.bat` 能開中央演示；小型模式 `start_small.bat` 能啟動；`python -m dtm pro demo` 能開 Pro 演示；右上角切換語言正常；在同一個視窗裡打開即時監測與程式診斷；齒輪（設定）各頁能開，改一個設定後「修改紀錄」能還原；管理員重設密碼後，對方第一次登入會被要求改密碼。
- [ ] 在真的 Windows 電腦上：右上角相機圖示能存下整頁 PNG；英文畫面的保養頁以美元顯示（不能連外時顯示原幣別與原因）；「設定 → 隱私與安全」一鍵產生自簽憑證，重新啟動後用 `https://` 開得起來，匯入下載的憑證後不再出現警告；單機的「第一次設定精靈」連線測試與儲存正常。
- [ ] 現場有 4G 簡訊數據機時：開啟 `[notify.sms.report]`，在「警報與通知 → 收件人」按某個人的「測試」，送達紀錄在幾分鐘內變成「手機已接收」；設定越南文的收件人收到的是越南文。

## 3. 上傳到 GitHub

- [ ] **用 git 推送，或只上傳發布用 zip 解出來的檔案。** GitHub 網頁的「Upload files」**不會套用 `.gitignore`**：直接把工作資料夾拖進去，`data/`、`logs/`、`run/`、`*.sqlite3`、`config.toml`、`users.sqlite3` 都會一起被上傳。發布用 zip 由 `git ls-files` 產生，只包含納入版本管理的檔案。
- [ ] 本機的 `backup/before-author-rewrite/*` 分支**不要推送**（只推 `release/v2.1.0` 或 `main`）。
- [ ] 倉庫設定：簡介（一句話價值）、主題標籤（README 有建議清單）、授權顯示為 Apache-2.0、開啟 Issues。
- [ ] 開啟 **Settings → Code security → Private vulnerability reporting**（SECURITY.md 請大家用這個私下回報）；確認 SECURITY.md 的回報信箱可以收信。
- [ ] 把倉庫網址填進 `CITATION.cff` 的 `repository-code`。

## 4. 存證（防禦性公開的時間證據）

- [ ] **先在 Zenodo 開啟這個倉庫的 GitHub 整合，再建立 Release。** Zenodo 只會封存「開啟之後」建立的 Release；順序反了就要再發一次版。
- [ ] Release 建立後，確認 Zenodo 產生了 DOI，把 DOI 加進 README 與 `CITATION.cff`（下一個提交）。
- [ ] 到 Software Heritage 的「Save code now」送出倉庫網址，取得封存紀錄（SWHID）。
- [ ] 確認 `docs/TECHNICAL_DISCLOSURE.md` 是這一版的內容，Release 頁面附上連結。

## 5. 建立 Release

- [ ] 標籤 `v2.1.0`，標題「DTM v2.1.0（Beta）」，說明貼 `CHANGELOG.md` 的這一版內容，並寫明「判斷門檻待現場驗證」。
- [ ] **勾選「Set as a pre-release」**（完成實機驗證、發 v3.0 時才改成正式版）。
- [ ] 附上發布用 zip；`release.yml` 會自動產生 zip、SBOM（`dtm-<版本>-sbom.cdx.json`）與建置來源證明（artifact attestation），在 Actions 頁面確認成功。
- [ ] 下載 Release 上的 zip，用 `gh attestation verify 檔名.zip --repo 帳號/倉庫` 驗證來源證明。

## 6. 發布後

- [ ] 用一台乾淨的電腦照 README 從零安裝一次。
- [ ] 試用的同事從 GitHub 自行下載；回饋請用「現場試用回報」範本，不收原始資料。
