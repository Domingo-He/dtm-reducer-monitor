# 安全政策 / Security Policy

[中文](#中文) · [English](#english)

---

## 中文

### 支援的版本

| 版本 | 安全性修正 |
|---|---|
| 2.1.x（Beta） | ✅ |
| 2.0.x 以前 | ❌ 請升級到 2.1.x |

### 回報安全性問題

請**不要**開公開的 Issue。

1. 優先用 GitHub 倉庫的 **Security → Report a vulnerability**（私下回報，只有維護者看得到）。
2. 或寄信到 domimgo491@gmail.com，主旨寫「DTM security」。

請附上：版本（`python -m dtm version`）、執行角色（單機／邊緣主機／中央）、重現步驟、影響範圍。
不要附上工廠的設定檔、資料庫或照片；需要時用「設定 → 系統資訊 → 下載診斷包」（密碼、金鑰、網址、Email、電話已遮蔽）。

這是個人維護的開源專案，沒有漏洞獎金，回應是盡力而為：目標 7 天內回覆收到，確認後在下一個修正版修補，
並在 CHANGELOG 註明（回報者同意的話列名致謝）。

### 範圍

- 範圍內：這個倉庫的程式（`dtm/`、`tools/`）、預設設定、安裝方式。
- 範圍外：機器人控制器與其網頁伺服器、Windows／網路設備本身、第三方服務（簡訊商、郵件伺服器、匯率網站）。

### 安全設計摘要

- **對機器人只讀不寫**：資料來源只做 HTTP GET（NUMREG）或 Modbus 讀取保持暫存器，DTM 不會改機器人的程式或參數。
- **帳號**：四種角色（檢視、操作員、工程師、管理員），每個寫入 API 都檢查角色；第一個管理員只能在本機建立；
  沒有帳號前是唯讀模式。密碼用 PBKDF2-HMAC-SHA256（20 萬次、每人不同 salt）儲存；可設定長度、英數混合、
  到期天數；連續輸錯會鎖定；閒置自動登出；登入 token 在資料庫只存雜湊。
- **連線限制**：可設定允許連線的 IP 或網段；預設安全標頭（X-Frame-Options、nosniff、Referrer-Policy）。
- **HTTPS**：可選。設定 → 隱私與安全 一鍵產生自簽憑證，或使用公司內部 CA 簽發的憑證；TLS 1.2 以上。
  邊緣主機上傳到中央時用 `ca_file` 驗證中央的憑證，用各廠不同的金鑰（Bearer token）驗證身分。
- **操作紀錄**：誰、從哪台電腦、什麼時候、做了什麼（不記密碼、電話、Email），可下載 CSV。
- **對外連線**：只連到設定檔裡設定的對象（中央、簡訊、郵件、Webhook），以及抓匯率（可關閉）。
  全部列在「設定 → 隱私與安全 → 這台主機會連到哪裡」。不送出任何監測資料到網際網路。
- **供應鏈**：核心只用 Python 標準函式庫；選用套件（pymodbus、pyserial）與內附的第三方檔案
  （WinSW、html2canvas）列在 SBOM（每個 Release 附 CycloneDX 格式的 `dtm-<版本>-sbom.cdx.json`）；
  Release 檔案有 GitHub 建置來源證明（`gh attestation verify`）。

### IEC 62443-4-2 自我評估（非認證）

以下是作者依 IEC 62443-4-2 七個基本要求（FR）做的自我對照，**不是**第三方認證，也不代表符合任何安全等級（SL）。
導入前請依貴廠的資安政策評估。

| 基本要求 | DTM 已有 | 尚未具備／需由現場補足 |
|---|---|---|
| FR1 識別與鑑別 | 個人帳號、密碼原則、錯誤鎖定、閒置登出、第一個管理員限本機 | 沒有 AD／LDAP、沒有多因素驗證 |
| FR2 使用控制 | 四種角色、每個寫入 API 檢查權限、廠區範圍限制、操作紀錄 | 沒有自訂角色 |
| FR3 系統完整性 | 對機器人只讀；設定檔寫入前備份、寫入後檢查格式；資料庫完整性自我檢查；Release 建置來源證明 | 程式本身沒有數位簽章（Windows SmartScreen 會提示） |
| FR4 資料機密性 | HTTPS 可選；密碼雜湊；診斷包遮蔽密碼與聯絡資料；分析匯出可匿名化 | 資料庫檔案沒有加密（依靠作業系統權限） |
| FR5 限制資料流 | 允許連線 IP 清單；對外連線一覽；資料由邊緣主機主動上傳（中央只在有人開某台機器人的即時監測時才連到那台邊緣主機） | 網路分區、防火牆由現場設定 |
| FR6 事件的及時回應 | 操作紀錄、登入失敗紀錄、看門狗、通知中心 | 沒有直接送 syslog／SIEM（可用 Webhook） |
| FR7 資源可用性 | 連線佇列、慢速連線逾時、自動備份與網路第二份、還原精靈、磁碟空間檢查 | 沒有主備援（HA） |

---

## English

### Supported versions

| Version | Security fixes |
|---|---|
| 2.1.x (Beta) | ✅ |
| 2.0.x and older | ❌ please upgrade to 2.1.x |

### Reporting a vulnerability

Please **do not** open a public issue.

1. Preferred: **Security → Report a vulnerability** on the GitHub repository (private; only the maintainer can see it).
2. Or email domimgo491@gmail.com with the subject "DTM security".

Include the version (`python -m dtm version`), the role (standalone / edge / central), steps to reproduce and the impact.
Do not attach a plant's config file, database or photos; if needed, use Settings → System → Download diagnostic bundle
(passwords, keys, URLs, emails and phone numbers are masked).

This is an individually maintained open-source project. There is no bug bounty and responses are best-effort:
the goal is to acknowledge within 7 days, fix confirmed issues in the next patch release, and note them in the
CHANGELOG (crediting the reporter if they wish).

### Scope

- In scope: the code in this repository (`dtm/`, `tools/`), default settings and the documented installation.
- Out of scope: the robot controller and its web server, Windows and network equipment, third-party services
  (SMS gateways, mail servers, exchange-rate sites).

### Security design summary

- **Read-only toward the robot**: data sources only issue HTTP GET (NUMREG) or Modbus read-holding-register requests.
  DTM never changes robot programs or parameters.
- **Accounts**: four roles (viewer, operator, engineer, admin), checked on every write API; the first admin can only be
  created locally; read-only until an account exists. Passwords are stored with PBKDF2-HMAC-SHA256 (200,000 iterations,
  per-user salt); configurable length, letter/digit mix and expiry; lockout after repeated failures; idle logout;
  only a hash of each session token is stored.
- **Network restrictions**: optional allow-list of client IPs or subnets; security headers by default
  (X-Frame-Options, nosniff, Referrer-Policy).
- **HTTPS**: optional. One click in Settings → Privacy & security creates a self-signed certificate, or use a certificate
  from your company CA; TLS 1.2 or later. Edge hosts verify the central server with `ca_file` and authenticate with a
  per-site bearer token.
- **Audit log**: who, from which computer, when and what (never passwords, phone numbers or emails); CSV export.
- **Outbound connections**: only to the targets in the config file (central, SMS, mail, webhook) plus exchange rates
  (can be turned off), all listed in Settings → Privacy & security. No monitoring data is sent to the internet.
- **Supply chain**: the core uses only the Python standard library. Optional packages (pymodbus, pyserial) and bundled
  third-party files (WinSW, html2canvas) are listed in the SBOM (each release ships a CycloneDX
  `dtm-<version>-sbom.cdx.json`). Release files carry GitHub build provenance (`gh attestation verify`).

### IEC 62443-4-2 self-assessment (not a certification)

This is the author's own mapping against the seven foundational requirements (FR) of IEC 62443-4-2. It is **not** a
third-party certification and does not claim any security level (SL). Evaluate it against your plant's own policy.

| Foundational requirement | In DTM | Not included / site responsibility |
|---|---|---|
| FR1 Identification & authentication | Personal accounts, password policy, lockout, idle logout, first admin local-only | No AD/LDAP, no MFA |
| FR2 Use control | Four roles, permission check on every write API, plant scoping, audit log | No custom roles |
| FR3 System integrity | Read-only toward the robot; config backed up before and validated after writes; database integrity self-check; release build provenance | Executables are not code-signed (Windows SmartScreen warns) |
| FR4 Data confidentiality | Optional HTTPS; hashed passwords; diagnostic bundle masks secrets and contacts; anonymized analysis export | Database files are not encrypted (relies on OS permissions) |
| FR5 Restricted data flow | Client IP allow-list; outbound connection list; edges push data to central (central connects to an edge only when someone opens that robot's live page) | Network zoning and firewalls are set up on site |
| FR6 Timely response to events | Audit log, failed-login records, watchdog, notification center | No direct syslog/SIEM output (webhook available) |
| FR7 Resource availability | Connection queue, slow-client timeouts, daily backups with a network copy, restore wizard, disk checks | No high-availability failover |
