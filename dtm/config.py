"""設定檔載入：TOML 格式，未填的項目使用預設值。"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

from .i18n import TDict

DEFAULTS: dict = {
    "system": {
        "role": "standalone",          # standalone = 單機（一台機器人）；edge = 邊緣主機（管理多台）；central = 中央伺服器
        "site_id": "",                 # 廠區代碼，例如 "P3"（邊緣與單機上傳中央時用）
        "site_name": "",               # 廠區名稱，例如 "三廠"
        "display_name": "",            # 畫面左上角顯示的名稱；空白 = 用廠區名稱或機器人名稱（也可在「設定」修改）
        "run_dir": "run",              # 狀態檔、維護模式旗標、看門狗紀錄；相對路徑以設定檔所在資料夾為準
        "log_dir": "logs",             # 記錄檔與崩潰紀錄
        "disk_alarm_pct": 10.0,        # 資料庫所在磁碟剩不到這個百分比：注意
        "disk_critical_pct": 3.0,      # 剩不到這個百分比：警戒，並暫停寫入原始樣本（特徵、事件照常）
        "status_every_s": 5.0,         # 狀態檔更新間隔（看門狗、邊緣主機、中央用）
    },
    "robot": {"name": "ROBOT-01", "source": "sim", "poll_hz": 5.0,
              "id": "",                # 機器人代碼（多台時用，例如 "R14"）；空白 = 用 name
              "line": "",              # 產線，例如 "L3 堆疊"
              "model": "R-2000iC/165F",
              "impact": "中"},         # 停機影響度：高／中／低（風險矩陣用）
    "source": {
        "fanuc_http": {"host": "192.168.0.10", "timeout_s": 1.0, "path": "/MD/NUMREG.VA"},
        "modbus": {"host": "192.168.0.20", "port": 502, "unit": 1, "word_order": "big", "map": {}},
        "csv": {"path": "replay.csv", "speed": 1.0, "loop": False},
        "sim": {"start_day": 180, "diag_every_s": 120, "seg_const_s": 4.0, "seg_ramp_s": 0.5,
                 "spike_every_s": 0, "seed": 7,
                 "motion_codes": True,      # 模擬生產程式寫動作代碼 9001／9002
                 "cycles_per_day": 40},     # simulate-history 每天產生幾個生產循環
    },
    "registers": {"dt_start": 101, "seg": 110, "run": 111, "heartbeat": 112, "override": 113, "temp": 0},
    # R[dt_start]～R[dt_start+5] = J1～J6；有第 7 軸（行走軸 E1）時 R[dt_start+6] = E1
    "axes": {"enabled": [2, 3]},
    "axis_default": {
        "name": "",
        "dir_sign": 1,
        "k_nm_per_raw": 0.0,
        "zero_offset": 0.0,
        "rated_nm": 0.0,
        "rt_caution_pct": 80.0,
        "rt_alert_pct": 100.0,
        "rt_caution_raw": 0.0,
        "rt_alert_raw": 0.0,
        "reducer_type": "rv",          # rv = RV 減速機（J1–J3）；gearbox = 齒輪箱（J4 與手腕）；rack = 行走軸齒輪箱＋齒條（E1）
        "linear": False,               # 直線軸（行走軸）：水平軌道的重力項 G 趨近 0，不判定 G 偏移
        "grease_mass_g": 0.0,          # 腔體油脂量 g，用來把鐵粉濃度換算成生成速率 W
        "iron_caution_wt": -1.0,       # 鐵粉濃度門檻 wt%；-1 = 依型式預設（rv 0.05／0.10，gearbox 只用相對值）
        "iron_alert_wt": -1.0,
        # Pro 程式層級診斷（P5 逐行損耗）用的減速機額定值；0 = 未知，只做相對排名（rated_nm 為額定扭矩 T0）
        "life_exp": 0.0,               # 壽命指數 p；0 = 依型式（rv、rack 10/3，gearbox 3）
        "rated_rpm": 15.0,             # 額定輸出轉速 N0（rpm，RV 型錄常用 15）
        "rated_life_h": 6000.0,        # 額定壽命 L0（小時，RV 型錄常用 6000）
        "startstop_nm": 0.0,           # 起動停止容許扭矩；0 = 不檢查
        "peak_nm": 0.0,                # 瞬間最大容許扭矩；0 = 不檢查
        "peak_count": 1000,            # 瞬間最大容許扭矩的容許次數 C（每次超過消耗 1/C）
        "ratio": 0.0,                  # 減速比（換算針齒、齒位置用；0 = 不換算）
        "pins": 0,                     # 針齒數（RV）或柔輪齒數（諧波）；0 = 不換算
        "contact_deg": 0.5,            # 小幅擺動判定：擺動幅度小於這個輸出角度（度）視為微動磨損風險
    },
    "axis": {},
    "diag": {"trim_s": 1.0, "min_const_s": 2.0, "ref_pct": 10, "run_gap_s": 5.0, "max_run_s": 900.0},
    "health": {
        "baseline_runs": 10,
        "caution_ratio": 1.5,
        "alert_ratio": 2.0,
        "caution_z": 2.0,
        "alert_z": 3.0,
        "min_runs_for_z": 10,
        "z_min_ratio": 1.15,
        "min_std_frac": 0.05,
        "g_caution_frac": 0.5,
        "g_alert_frac": 1.0,
        "confirm_runs": 2,
        "trend_window_runs": 30,
        "temp_comp": True,
        "temp_min_range": 5.0,         # 基準期溫差達到這個值，就用基準期資料自己學溫度斜率
        "temp_coef_fv": 0.025,         # 預設溫度係數：溫度每低 1°C，黏性摩擦約 +2.5%（可用 temp-coef 指令從資料估計）
        "temp_coef_fc": 0.004,         # 固定摩擦受溫度影響小
        "common_mode": True,           # 跨軸共模扣除：true = 監測兩軸以上就用；"auto" = 只在沒有溫度訊號時用；false = 不用
    },
    "realtime": {"hold_s": 2.0, "stale_s": 5.0, "invalid_abs": 32767},
    "sensors": {                       # 振動＋溫度感測器（IO-Link 主站或無線閘道，以 Modbus TCP 讀特徵值）
        "enabled": False,
        "source": "modbus",            # modbus；sim = 模擬（測試用）
        "host": "192.168.0.30",
        "port": 502,
        "unit": 1,
        "word_order": "big",
        "poll_hz": 2.0,                # 照樣輪詢；感測器多久更新一次由程式自動判斷（見 slow_s）
        "slow_s": 20.0,                # 數值超過這麼久才變一次的感測器（例如電池型無線）只在變化時記一筆，主要用於每日趨勢
        "only_changes": False,         # 舊設定：true = 所有感測器都當作慢速（2.1 起改為自動判斷，一般不用設）
        "keep_s": 7200,                # 記憶體保留多久的讀值（秒），給時間窗切片用
        "vib_source": "diag",          # 振動趨勢（交叉判斷）的來源："diag" = 診斷等速段與 99xx 取樣窗；或填動作代碼，例如 9001
        "map": {},                     # "2.radial.rms" = { addr = 200, type = "float32", scale = 1.0 }；"2.temp"、"ambient" 同理
    },
    "devices": {                       # 設備設定（2.1）：每軸有哪些量測；扭矩一定有。可在儀表板「設備設定」修改（本機、管理員）
        "ambient": None,               # 環境溫度；None = 依 [sensors.map] 有沒有 "ambient" 自動判斷
        "axis": {},                    # "2" = { temp = true, radial = true, axial = false }；沒寫的軸依 [sensors.map] 自動判斷
    },
    "motion": {                        # 生產中監測：生產程式在固定動作前後寫 R[110] = 9000–9899（動作代碼）
        "enabled": True,               # 讀到動作代碼就自動開始記錄；false = 忽略動作代碼
        "metric": "rms",               # 判定用的扭矩指標：rms（均方根）、mean_abs（平均絕對值）、peak（峰值）
        "min_s": 0.4,                  # 動作長度合理範圍（秒）；太短 = 中斷，太長 = 中途暫停，不採用
        "max_s": 120.0,
        "dur_tol": 0.25,               # 跟這個代碼的典型長度差超過 25% 不採用（暫停、速度被改）
        "require_full_override": True, # 只採用倍率 100% 的循環
        "min_cycles_day": 20,          # 一天至少幾個有效循環才算一個有效日
        "baseline_days": 10,           # 重設後前 N 個有效日建立基準（每個代碼、每個軸各自）
        "caution_ratio": 1.15,         # 每日中位數相對基準：注意（生產扭矩含慣性與重力，摩擦只占一部分，門檻比診斷低）
        "alert_ratio": 1.30,           # 警戒；以上是起始值，請累積現場資料後修正
        "vib_caution_ratio": 1.5,      # 感測器振動（每日中位數）相對基準
        "vib_alert_ratio": 2.0,
        "confirm_days": 2,             # 連續幾個有效日才升降級
        "trend_days": 30,              # 用最近幾個有效日外推達警戒日期
        "codes": {},                   # 代碼名稱（選填），例如 { "9001" = "搬運（有工件）", "9002" = "空手回程" }
    },
    "vibration": {
        "baseline_n": 10,              # 重設後前 N 筆建立基準（每個方向各自）
        "caution_ratio": 2.0,          # 相對基準：注意
        "alert_ratio": 3.0,            # 相對基準：警戒
        "kurt_caution": 4.0,           # 峭度絕對值（常態約 3）
        "kurt_alert": 6.0,
        "ar_caution_ratio": 1.5,       # 軸向／徑向比相對基準的變化
    },
    "iron": {
        "rv_caution_wt": 0.05,         # RV 減速機起始預設（源自鐵粉計廠商的軸承參考值，請依現場資料修正）
        "rv_alert_wt": 0.10,
        "w_caution_ratio": 2.0,        # 鐵粉生成速率 W 相對基準
        "w_alert_ratio": 3.0,
        "min_hours": 300,              # 換脂後運轉不到這個時數，不計算 W
        "default_sample": "drain_mid", # 預設樣本：換脂排出（中段）——先丟棄排出口附近的前段，取中段送驗
        "interval_days": 0,            # 正常時的取樣間隔（天）；0 = 只在換脂時取樣
        "interval_caution_days": 60,   # 鐵粉達注意後的取樣間隔（自動排進保養排程）
        "interval_alert_days": 21,     # 鐵粉達警戒後的取樣間隔
    },
    "thermal": {                       # 溫度（2.1）：穩定高溫只縮短換脂週期；突然升溫才是異常
        "rise_c": 8.0,                 # 溫升 ΔT（外殼−環境）比自己 14 天中位數高幾度 →「溫度突升」注意
        "day_rise_c": 5.0,             # 或一天內 ΔT 升幾度（環境沒有同步升）
        "baseline_days": 14,           # 跟最近幾天的中位數比
        "min_days": 5,                 # 基準期至少幾個有效日才判斷
        "min_cycles_day": 5,           # 一天至少幾個循環才算有效日
        "post_grease_h": 72,           # 換脂後幾小時內不判突升（新脂攪拌升溫是正常的）
        "post_grease_hold_c": 5.0,     # 換脂超過上述時數，ΔT 仍比換脂前高這麼多 → 提醒內壓／油封
        "overheat_c": 0.0,             # 外殼過熱絕對門檻（°C）；0 = 停用
    },
    "life": {
        "hours_per_day": 20.0,         # 沒有時數表紀錄時用來估算運轉時數
        "grease_interval_h": 11520,    # 換脂週期（時數），依機型手冊
        "grease_interval_years": 3.0,  # 換脂週期（年），與時數先到者為準
        "grease_temp_ref_c": 40.0,     # 外殼溫度每高於此值 10°C，油脂有效老化加倍（只加速、不延長）
        "spare_lead_days": 60,         # 減速機、馬達備品前置天數
        "weibull_min_failures": 3,     # 至少幾筆失效紀錄才用自己的資料擬合
        "prior": {                     # 沒有足夠紀錄時的先驗：beta 形狀、eta_h 特徵壽命；0 = 不做年齡風險
            "reducer": {"beta": 0.0, "eta_h": 0.0},
            "gearbox": {"beta": 0.0, "eta_h": 0.0},
            "motor": {"beta": 0.0, "eta_h": 0.0},
        },
    },
    "decision": {
        "grease_age_start": 0.7,       # 油脂有效壽命用掉 70% 開始提高換脂分數
        "recovery_days": 30,           # 換脂後幾天內檢查摩擦是否回到原基準
        "recovery_ratio": 1.3,         # 換脂後仍高於原基準這個倍數 → 判定未恢復
        "act_ratio": 0.5,              # 分數 ≥ 0.5：建議／準備
        "urgent_ratio": 0.9,           # 分數 ≥ 0.9：立即／安排更換
        "p_fail_prepare": 0.2,         # 備品前置期間內的失效機率 ≥ 此值：準備備品
        "weights": {                   # 各證據在家族內的比重（0–1）
            "fc_ripple": 1.0,          # 扭矩：固定摩擦 Fc、漣波、累積比值
            "f_motion": 0.8,           # 扭矩：參考速度摩擦 F、生產循環每日中位數
            "temp_rise": 0.6,          # 溫度突升（算在摩擦家族；單獨最多「觀察」）
            "vib_reducer": 1.0,        # 振動：包絡、嚙合頻率、峭度
            "vib_axial": 0.8,          # 振動：軸向 RMS、軸向比
            "iron": 1.0,               # 鐵粉：濃度、生成速率
        },
    },
    "storage": {"db": "dtm.sqlite3", "raw_retention_days": 7, "batch_s": 2.0,
                "cycle_retention_days": 180,  # 生產循環逐筆資料保留天數；每日中位數永久保留（2.1 起由 730 改 180，控制磁碟用量）
                "backup_dir": "backups",      # 每日自動備份的資料夾；相對路徑以資料庫所在資料夾為準；空白 = 不備份
                "backup_keep": 14,            # 保留最近幾份（2.1 起由 30 改 14；要更久請加網路資料夾第二份）
                "backup_hour": 3,             # 每天幾點備份（0–23）
                "raw_keep_days": 180,         # 診斷段原始波形與警報前後的原始資料保留天數（其他原始樣本依 raw_retention_days）
                "raw_keep_alarm_s": 600,      # 每次警報保留前後各幾秒的原始資料
                "backup_mirror_dir": "",      # 第二份備份（網路資料夾，例如 \\\\NAS\\dtm\\R01）；空白 = 不複製
                "before_restore_keep": 5},    # 還原前自動備份保留幾份
    "notify": {
        "cooldown_s": 1800,
        "email_enabled": False,
        "smtp_host": "",
        "smtp_port": 25,
        "smtp_tls": False,
        "smtp_user": "",
        "smtp_password": "",
        "mail_from": "",
        "mail_to": [],
        "webhook_url": "",
        "webhook_headers": {},         # Webhook 額外標頭，例如 Authorization；值可寫 "env:變數名稱"
        "webhook_body": {},            # Webhook 內容模板；空白 = 預設 JSON。字串裡的 {text}{subject}{level}{robot}{site}{message}{axis}{kind}{time} 會被替換
        "min_level": 1,                # Email、Webhook 的最低等級
        "notify_clear": False,         # 警報解除時是否也通知
        "public_url": "",              # 儀表板網址，附在 Email／Webhook 訊息裡
        "proxy": "",                   # 對外連線需要經過的代理伺服器，例如 http://10.0.0.1:8080
        "quiet_hours": "",             # 夜間時段，例如 "22:00-07:00"：這段時間「注意」不即時發，併入每日摘要（警戒照發）
        "summary_time": "",            # 每日摘要時間，例如 "07:30"；空白 = 不發
        "summary_channels": ["email", "webhook"],   # 也可加 "sms"（只發一行摘要，當作每日心跳）
        "escalation": {                # 逐級通報（要搭配 [[notify.rules]] 的 stage = 2、3）
            "enabled": True,
            "min_level": 2,            # 哪個等級以上會逐級通報（預設只有警戒）
            "stage2_after_s": 1800,    # 30 分鐘沒人確認 → 第 2 階段（例如簡訊給值班）
            "stage3_after_s": 7200,    # 2 小時沒人確認 → 第 3 階段（例如 Email 課長）
        },
        "duty_file": "",               # 值班表 CSV：date,start,end,name,phone,email（date 空白 = 每天）
        "duty": [],                    # 或固定班別：[{ name = "日班", start = "07:00", end = "19:00", days = "1-7", phones = [...] }]
        "duty_default_phones": [],     # 值班表查不到人時的簡訊收件人
        "rules": [],                   # 收件規則 [[notify.rules]]；空白 = 每個通道依自己的最低等級與收件人發送（舊方式）
        "people": [],                  # 收件人名單 [[notify.people]]：name、phone、email、lang、groups（"default" 或規則名稱）、min_level
                                       # 每個人用自己的語言收通知；也可以在畫面「警報與通知 → 收件人」新增、修改
        "edge_fallback_s": 300,        # 邊緣主機：連不上中央超過這個秒數，才由邊緣自己發「警戒」通知（備援）
        "sms": {
            "enabled": False,
            "provider": "http",           # http（通用 HTTP 簡訊閘道）| modem（4G 簡訊數據機）
            "to": [],                     # 手機號碼，例如 "0912345678"
            "min_level": 2,               # 簡訊有成本，預設只發警戒
            "max_per_day": 30,
            "fallback": [],               # 備援：主要的送不出去時改用，例如 provider = "modem"、fallback = ["http"]
            "template": "",               # 簡訊模板；空白 = "[警戒]機器人 內容 時間"。可用 {level}{robot}{site}{message}{time}{brand}
            "brand": "",                  # 公司或品牌名稱（日本、越南的簡訊內文要帶公司名）
            "country_code": "886",        # 國碼：{phone_e164} 用
            # 通用 HTTP 簡訊閘道：三竹、Twilio、中華電信等都用這個，設定範例見 config.example.toml
            "http": {"method": "POST", "url": "", "json": False, "params": {}, "headers": {}, "success": "",
                     "basic_user": "", "basic_password": ""},
            "modem": {"port": "COM3", "baud": 115200,
                      "cnmi": "AT+CNMI=2,1,0,2,0",   # 送達回報存在數據機裡（每分鐘讀一次）；少數機型要改成 2,1,0,1,0
                      "report_mem": ""},             # 送達回報存放位置，例如 "SR"；空白 = 數據機預設
            # 送達回報（2.1）：手機收到簡訊後，電信業者回報「已送達」，畫面顯示「手機已接收」
            "report": {"enabled": False,
                       "timeout_min": 30,           # 超過這個時間還沒回報，畫面標示「逾時未回報」
                       "poll_s": 60,                # 4G 數據機：每隔多久讀一次送達回報
                       # 通用 HTTP 閘道：簡訊商把送達結果回呼到 {public_url}/api/sms/report?key=…
                       "key": "",                   # 回呼網址要帶的 key（必填才會開放回呼網址），可寫 "env:DTM_SMS_REPORT_KEY"
                       "id_regex": "",              # 從發送回應抓出訊息編號的正規式（第 1 個括號），例如 'msgid=(\w+)'
                       "id_json": "",               # 或 JSON 欄位路徑，例如 "sid"、"data.id"
                       "id_field": "msgid",         # 回呼內容裡的訊息編號欄位
                       "status_field": "status",    # 回呼內容裡的狀態欄位
                       "delivered": "^(0|DELIVRD|DELIVERED|delivered)$",   # 符合 = 已送達
                       "pending": "^(ENROUTE|ACCEPTD|ACCEPTED|accepted|queued|sent|SENT)$",  # 符合 = 還在送
                       "reply": "OK"},              # 回呼成功時回給簡訊商的文字
        },
    },
    "web": {"host": "0.0.0.0", "port": 8080, "live_window_s": 300,
            "users_db": "users.sqlite3",      # 個人帳號（python -m dtm user add …）；同一台主機的 DTM 程式共用
            "require_login": False,           # true = 看畫面也要登入（viewer 以上）
            "tls_cert": "",                   # HTTPS 憑證（PEM）；填了就改用 https://
            "tls_key": "",                    # HTTPS 私鑰（PEM）
            "write_password": "",             # 儀表板寫入密碼；空白 = 不需登入（會顯示提醒）。可寫 "env:DTM_WRITE_PASSWORD"
            "session_hours": 12,              # 登入後多久要重新登入
            # 2.1 以下各項也可以在畫面右上角「設定」修改（網頁上的值優先，存在 users_db）
            "idle_minutes": 30,               # 操作員以上沒有操作多久自動登出；0 = 不自動登出（檢視帳號不受影響）
            "max_fails": 5,                   # 10 分鐘內密碼輸錯幾次就暫停登入（同一台電腦或同一個帳號）
            "lock_minutes": 10,               # 暫停登入多久
            "allow_ips": [],                  # 允許連線的電腦（IP 或網段，例如 "192.168.10.0/24"）；空白 = 不限制，本機一律可以
            "pw_min_length": 8,               # 密碼最少長度
            "pw_require_mix": True,           # 密碼要同時有英文字母與數字，不接受常見密碼
            "pw_max_age_days": 0,             # 密碼有效天數；0 = 不限
            "pw_change_first": True,          # 管理員建立或重設的密碼，第一次登入要改
            "public_host": ""},               # 邊緣主機：這台電腦的 IP 或名稱，中央畫面用來產生機器人儀表板連結
    "ui": {"lang": "zh-TW",
           "refresh_s": 0},                  # 中央／廠區畫面自動更新間隔（秒）；0 = 自動（中央 60、廠區 20）
    "privacy": {"export_anonymize": True},    # 分析匯出預設匿名化（匯出時仍可取消勾選）
    "maint": {"currency": "TWD",              # 備品單價的幣別（ISO 4217），保養材料費估算用；也可在「設定 → 一般」修改
              "fx": "auto",                   # 畫面依語言換算當地貨幣：auto（網路匯率）、manual（下面 fx_rates）、off（不換算）
              "fx_refresh_h": 12,             # 網路匯率多久更新一次
              "fx_rates": {}},                # 手動匯率：1 單位 currency = 多少目標幣別，例如 { USD = 0.031, VND = 790 }
    "service": {                              # 服務商資訊（「保養與備品」頁底部）；也可在「設定 → 服務商」修改
        "region": "auto",                     # 原廠服務據點：auto（依畫面語言）、tw、cn、us、jp、kr、vn
        "contact_name": "", "contact_phone": "", "contact_email": "",   # 貴廠的業務或服務窗口
        "customer_no": "",                    # 客戶編號或維護合約號碼
        "note": ""},
    "fleet": {                         # 邊緣主機（[system] role = "edge"）：一台主機管理多台機器人，一台一個程式
        "base_port": 8100,             # 每台機器人自己的儀表板連接埠 = base_port + 序號（第 1 台 8101…）
        "child_host": "0.0.0.0",       # 機器人儀表板綁定的位址；只給本機看就填 "127.0.0.1"
        "data_dir": "data",            # 每台一個資料庫：data/<代碼>.sqlite3
        "stale_s": 120,                # 機器人程式的狀態檔超過幾秒沒更新 → 判定卡住，重啟那一台
        "grace_s": 90,                 # 剛啟動的寬限時間
        "max_restarts": 3,             # 每台每小時最多自動重啟幾次；超過就停止重啟並通知
        "window_s": 3600,
        "start_stagger_s": 0.5,        # 依序啟動，避免同時連線
    },
    "robots": [],                      # 邊緣主機管理的機器人清單 [[robots]]（見 config.edge.example.toml）
    "central": {                       # 上傳到中央伺服器（邊緣主機或單機都可以）；中央自己的設定也在這裡
        "url": "",                     # 中央網址，例如 "https://10.1.2.3:8443"；空白 = 不上傳
        "token": "",                   # 上傳用的金鑰，建議寫 "env:DTM_SYNC_TOKEN"
        "interval_s": 60,              # 多久上傳一次
        "clock_drift_s": 60,           # 跟中央的時間差超過幾秒就警告（0 = 不檢查）
        "ca_file": "",                 # 中央用公司內部 CA 簽的憑證時，填 CA 憑證檔（PEM）
        "batch": 3000,                 # 每次每類資料最多幾筆（第一次上傳歷史資料時分批補）
        # ---- 以下是中央伺服器（role = "central"）自己用的
        "db": "central.sqlite3",
        "tokens": {},                  # 各邊緣主機的金鑰：{ P3 = "env:DTM_EDGE_P3" }；空白 = 用 token 一組通用
        "edge_timeout_s": 300,         # 邊緣主機超過這個秒數沒有上傳 → 系統警報
        "notify_max_age_s": 3600,      # 補傳的舊警報（發生超過這個秒數）只存不發通知
        "rules_every_s": 3600,         # 多久重算一次機群壽命模型並下發
        "sites": {},                   # 廠區名稱與國別：{ P3 = { name = "三廠", country = "TW" } }
    },
    "watchdog": {                      # 程式看門狗：python -m dtm watchdog（建議用 service install 裝成第二個服務）
        "interval_s": 30,              # 每幾秒檢查一次
        "stale_s": 120,                # 狀態檔超過幾秒沒更新 → 判定卡住，重啟服務
        "loop_stale_s": 120,           # 擷取迴圈超過幾秒沒有轉 → 判定卡住，重啟服務
        "data_stale_s": 900,           # 超過幾秒沒有收到機器人資料 → 通知（機器人關機時重啟也沒用）
        "restart_on_data_stale": False,  # true = 資料中斷也重啟一次（有時能解開卡死的連線）
        "grace_s": 180,                # 剛啟動或剛重啟後的寬限時間
        "max_restarts": 3,             # window_s 內最多自動重啟幾次；超過就停止自動重啟、通知人處理
        "window_s": 3600,
        "restart_cmd": [],             # 重啟指令；空白 = 自動（Windows 用 WinSW 服務、Linux 用 systemctl）
        "service_name": "DTM",
        "notify": True,                # 重啟與異常時發通知（用 [notify] 的設定）
        "hw": {                        # 硬體看門狗：DTM 健康才「餵」；整台當機或程式救不回來時由硬體重開機
            "type": "none",            # none｜linux（/dev/watchdog）｜command（呼叫廠商工具）｜modbus（送心跳給 PLC）
            "feed_s": 20,              # 多久餵一次（要比硬體逾時短很多）
            "max_reboots_day": 3,      # 一天內因「停止餵狗」造成的重開機超過這個次數，就改成一直餵、只通知人（防止無限重開）
            "device": "/dev/watchdog",
            "feed_cmd": [], "enable_cmd": [], "disable_cmd": [],
            "host": "", "port": 502, "unit": 1, "addr": 0,  # modbus：心跳寫入的保持暫存器（PLC 檢查數值有沒有在變）
            "maint_addr": -1,          # modbus：維護模式時寫 1 的暫存器（PLC 據此暫停重啟電源）；-1 = 不用
        },
    },          # 系統語言：zh-TW、zh-CN、en、ja、ko、vi（通知、報告、工單、指令列）
    "workpack": {
        "catalog": "",                 # 備品與作業包目錄（TOML）；相對路徑以設定檔所在資料夾為準；空白 = 不使用
        "notify_level": 1,             # 維護決策達此等級時，LINE／Email 附上備品與作業包摘要
    },
    "pro": {                           # Pro 程式層級診斷：短期「診斷優化期」提高取樣，把扭矩對到程式每一行與關節每個角度
        "data_dir": "data/pro",        # 診斷期資料（每期一個資料夾）；相對路徑以設定檔所在資料夾為準
        "sample_hz": 100.0,            # 診斷期取樣頻率（範例 50–125）
        "max_days": 7.0,               # 診斷期最長天數；到期自動結束
        "line_reg": 114,               # 加註版程式在每個動作行前把行號寫進這個暫存器（掃描程式也用它標示段落）
        "lookahead": 2,                # 控制器提前執行最多幾個動作行（CNT 平滑過點時，回報的行號會比實際位置早）
        "learn_cycles": 12,            # 用前幾個循環學出每一行的路徑（行號對位用）
        "keep_raw_first": 30,          # 原始資料保留：前幾個循環全部保留
        "keep_raw_every": 20,          # 之後每幾個循環保留一個（其他循環只留逐行統計）
        "grid": 50,                    # 每一行依行內進度切成幾格做同步平均
        "torque": "torque",            # 損耗用哪個訊號：torque = 傳遞扭矩（建議）；disturbance = 干擾扭矩（替代）
        "torque_unit": "pct",          # 扭矩單位：pct = 額定百分比；nm = 牛頓米（需要各軸 rated_nm）；raw = 原始值（只做相對排名）
        "listen_host": "0.0.0.0",      # 接收控制器串流（KAREL 範本 DTM_STREAM）的位址
        "listen_port": 9750,
        "listen_allow": [],            # 只接受這些 IP；空白 = 只接受 [source.fanuc_http] host
        "slack_q": 10,                 # 等待餘裕：取實際等待時間的第幾百分位
        "slack_margin_s": 0.3,         # 安全裕度（秒）
        "lwi_caution": 1.2,            # 局部磨耗指數 LWI 相對自己基準：注意
        "lwi_alert": 1.4,              # 警戒
        "impact_k": 4.0,               # 加速規衝擊：峰值超過基準 RMS 的幾倍
        "impact_crest": 5.0,           # 而且峰值因子超過
        "stop_weight": {"estop": 0.02, "collision": 0.5, "controlled": 0.005},  # 停止事件相當於幾次「瞬間最大容許扭矩」事件（每次消耗 1/C；估計值）
        "cost": {"downtime_h": 8.0, "per_hour": 0.0, "spare": 0.0, "labor": 0.0},  # 成本換算（改善報告用；0 = 不換算）
    },
}

RESET_KINDS = {"grease_change", "reducer_replace", "gearbox_replace", "motor_replace", "baseline_reset"}
EVENT_LABELS = TDict({
    "grease_change": "換脂",
    "reducer_replace": "更換減速機",
    "gearbox_replace": "更換齒輪箱",
    "motor_replace": "更換馬達",
    "baseline_reset": "重設基準",
    "motion_reset": "重設動作基準",
    "calibration": "校正",
    "inspection": "點檢",
    "other": "其他",
})


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")      # 記事本存成「UTF-8 含 BOM」也能讀
        cfg = _merge(cfg, tomllib.loads(text))
    cfg["_path"] = str(path) if path else ""
    from .i18n import set_default
    set_default(cfg.get("ui", {}).get("lang"))
    return cfg


E1 = 7                                    # 第 7 軸（行走軸）在 DTM 內的軸號
AXES_ALL = tuple(range(1, 8))            # J1–J6 與 E1
AXIS_PRESETS = {E1: {"name": "E1", "reducer_type": "rack", "linear": True}}
REDUCER_TYPES = ("rv", "gearbox", "rack")


def axis_cfg(cfg: dict, ax: int) -> dict:
    a = dict(cfg["axis_default"])               # 軸設定都是單層數值，淺層合併即可（這個函式被呼叫得很頻繁）
    a.update(AXIS_PRESETS.get(int(ax), {}))
    a.update(cfg.get("axis", {}).get(str(ax), {}))
    if not a.get("name"):
        a["name"] = f"J{ax}"
    return a


def axis_name(cfg: dict | None, ax) -> str:
    """顯示用軸名：設定檔有取名就用設定的名字；第 7 軸預設 E1。"""
    if ax in (None, "", 0):
        return ""
    if cfg is None:
        return "E1" if int(ax) == E1 else f"J{int(ax)}"
    return axis_cfg(cfg, int(ax))["name"]


def enabled_axes(cfg: dict) -> list[int]:
    return [int(a) for a in cfg["axes"]["enabled"]]


# 事件類別 → 元件；換減速機或齒輪箱時油脂也一併換新
EVENT_COMPONENTS = {
    "grease_change": ["grease"],
    "reducer_replace": ["reducer", "grease"],
    "gearbox_replace": ["gearbox", "grease"],
    "motor_replace": ["motor"],
}
TEAR_LOCATIONS = TDict({"input_gear": "輸入齒輪", "crank_bearing": "曲軸（偏心軸）軸承", "cycloid": "擺線齒輪",
                        "pin": "針齒（針銷）", "main_bearing": "主軸承（輸出軸承）", "seal": "油封",
                        "gearbox_gear": "齒輪箱齒輪", "gearbox_bearing": "齒輪箱軸承", "motor_bearing": "馬達軸承",
                        "brake": "馬達煞車", "other": "其他"})
TEAR_MODES = TDict({"pitting": "點蝕", "spalling": "剝離", "wear": "磨耗", "broken": "斷齒", "other": "其他"})
TEAR_GREASE = TDict({"normal": "正常", "dark": "變色變黑", "metal": "含金屬粉", "emulsified": "乳化進水",
                     "hardened": "硬化乾涸", "leak": "漏油", "other": "其他"})
IRON_SAMPLES = TDict({"drain_mid": "換脂排出（中段）", "interim": "中途取樣", "change": "換脂排出（未分段）",
                      "after": "換脂後殘留（約 24 小時）"})
IRON_AT_CHANGE = ("drain_mid", "change")      # 換脂時排出的舊脂：屬於上一批油脂
WEIGHT_KEYS = ("fc_ripple", "f_motion", "temp_rise", "vib_reducer", "vib_axial", "iron")
COMP_LABELS = TDict({"grease": "潤滑脂", "reducer": "減速機", "gearbox": "齒輪箱", "motor": "伺服馬達"})
VIB_METRICS = TDict({"rms": "加速度 RMS", "env": "包絡（軸承）", "mesh": "嚙合頻率", "motor1x": "馬達轉頻", "kurt": "峭度"})


DEVICE_KINDS = ("temp", "radial", "axial")


def device_flags(cfg: dict) -> dict:
    """每軸啟用的量測：{"axes": {軸: {"torque": True, "temp": …, "radial": …, "axial": …}}, "ambient": …}。
    設定檔 [devices] 有寫的照寫的；沒寫的依 [sensors.map] 有沒有對應的鍵自動判斷（舊設定照常可用）。"""
    sc = cfg.get("sensors") or {}
    keys = set((sc.get("map") or {}).keys()) if sc.get("enabled") else set()
    if sc.get("enabled") and sc.get("source") == "sim" and not keys:
        keys = {f"{a}.{k}" for a in enabled_axes(cfg) for k in ("radial.rms", "axial.rms", "temp")} | {"ambient"}
    dv = cfg.get("devices") or {}
    out = {"axes": {}, "ambient": dv.get("ambient") if dv.get("ambient") is not None else ("ambient" in keys)}
    for ax in enabled_axes(cfg):
        set_ = (dv.get("axis") or {}).get(str(ax)) or {}
        auto = {"temp": f"{ax}.temp" in keys,
                "radial": any(k.startswith(f"{ax}.radial.") for k in keys),
                "axial": any(k.startswith(f"{ax}.axial.") for k in keys)}
        out["axes"][ax] = {"torque": True, **{k: bool(set_[k]) if k in set_ else auto[k] for k in DEVICE_KINDS}}
    out["ambient"] = bool(out["ambient"])
    return out


def device_key_on(flags: dict, key: str) -> bool:
    """感測器鍵（"2.radial.rms"、"2.temp"、"ambient"）是否屬於已啟用的量測。"""
    if key == "ambient":
        return bool(flags.get("ambient"))
    parts = key.split(".")
    try:
        ax = int(parts[0])
    except ValueError:
        return True
    a = (flags.get("axes") or {}).get(ax)
    if a is None:
        return False
    kind = "temp" if parts[1] == "temp" else parts[1]
    return bool(a.get(kind, False))


def drive_comp(cfg: dict, ax: int) -> str:
    """減速機型式 → 元件：rv = 減速機；gearbox（手腕齒輪箱）與 rack（行走軸齒輪箱＋齒條）= 齒輪箱。"""
    return "gearbox" if axis_cfg(cfg, ax)["reducer_type"] in ("gearbox", "rack") else "reducer"
