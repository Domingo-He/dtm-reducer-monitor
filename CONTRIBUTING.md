# 貢獻方式

謝謝你願意幫忙。DTM 由作者以個人身分、非商業方式維護，回覆與合併會盡力，但不保證時間。

## 最有幫助的是現場回報

這個專案最缺的是**實機驗證**。如果你在現場試用，請用 GitHub Issues 的「現場試用回報」範本告訴我們：試了幾台、跑了多久、警報準不準、拆檢看到什麼。

**請不要上傳原始資料、資料庫、照片或任何公司的內部文件**。只要文字描述就夠了；需要數字時，用「比值大約 1.3 倍」「大約提早 3 週」這種程度即可。公司的資料屬於公司，上傳前要有權限的人同意。

其他範本：

- **安裝問題**：照 README 安裝或啟動時卡住。
- **錯誤回報**：程式出錯、畫面不對、判斷明顯不合理。

## 程式貢獻

1. 先開 Issue 說明要改什麼，避免白做。
2. 只用 Python 標準函式庫（不新增必要的第三方套件）；Python 3.11 以上。
3. 介面文字與訊息用繁體中文原文，用 `_("…")` 包起來；改完執行 `python tools/i18n_tool.py extract`。
4. 新功能要有測試：`python -m unittest discover -s tests`，並通過 `python -m pyflakes dtm tests tools`。
5. 不要放任何真實工廠的資料（設定、IP、人名、電話、照片）；範例一律用模擬器或演示資料。
6. 判斷邏輯或門檻的修改，請說明依據（現場經驗、拆檢結果、文獻），並附門檻回放（`replay`）的比較。

## DCO（開發者原創聲明）

本專案採用 [Developer Certificate of Origin 1.1](https://developercertificate.org/)。每個提交都要在訊息最後加一行簽署，表示你有權以本專案的授權（Apache-2.0）提供這段程式：

```
Signed-off-by: 你的名字 <you@example.com>
```

用 `git commit -s` 會自動加上。名字與 Email 要跟你的 Git 設定一致，不接受匿名。

DCO 1.1 全文：

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## 授權與專利

你的貢獻以 Apache-2.0 授權釋出（包含其中的專利授權條款）。本專案不申請專利，並以 `docs/TECHNICAL_DISCLOSURE.md` 公開方法細節；如果你的貢獻包含新的監測或判斷方法，歡迎一併補進技術揭露文件。

## AI 協助

使用 AI 工具寫程式沒有問題，但你要讀懂、測試並對提交的內容負責，並在 Pull Request 說明哪些部分有 AI 協助。
