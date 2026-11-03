# 設計邊界：本系統刻意不做的事

DTM 有些功能是**刻意不做**的。理由有三種：現場安全、判斷的可信度，以及發布前的專利篩查（這些方向有他人已公開的專利或申請，本專案選擇不實作）。

修改程式或新增功能前，請先看這份清單。如果你要在自己的版本加入這些功能，請自行評估安全與專利風險；本專案的 Pull Request 不接受這些方向的實作。

> 本文件說明的是**實作範圍**，不是技術揭露的範圍。〈技術揭露〉（`docs/TECHNICAL_DISCLOSURE.md`）公開的方法與變化例，不因本文件而縮小。

## 1. 安全

| 不做 | 改用 |
|---|---|
| 對機器人控制器寫入資料、送動作指令 | 監控程式只**讀取**控制器資料（HTTP 讀暫存器、PLC Modbus）。機器人端的背景邏輯、診斷程式、動作代碼都由人照變更流程上傳。 |
| 自動修改機器人程式、自動降速 | Pro 的修改建議**只輸出文字**（修改前後對照與理由），由人確認干涉與節拍、低速試跑後再上線。 |
| 以單一證據建議更換減速機或馬達 | 換件要兩類獨立證據（扭矩、鐵粉、振動）同時異常；單一證據最多到「觀察／準備」。 |

## 2. 常駐監測

| 不做 | 改用 |
|---|---|
| 從馬達電流推估潤滑脂溫度 | 溫度只用實際量到的外殼溫度、環境溫度或其他軸外殼溫度（溫升 ΔT）。沒有溫度訊號時，溫度相關判斷只顯示、不發警報。 |
| 把鐵粉與油脂老化合併成一個壽命模型 | 鐵粉濃度（含生成速率 W）與油脂壽命（時數、年限、溫度修正）分別判斷，在交叉判斷裡是各自獨立的證據。 |
| 把診斷動作併進生產程式 | 診斷程式 `DTM_DIAG`、全範圍掃描 `DTM_SWEEP` 都是**獨立程式**，在停機或換班時單獨執行；生產中只在固定動作前後寫動作代碼（`R[110]`）。 |
| 內建無線（電池型）鐵粉／油液感測器的整合 | 鐵粉以換脂排出或中途取樣送驗後人工登錄；其他感測器經 Modbus／IO-Link 閘道當一般數值讀入。 |
| 以「馬達扭矩與外力同時超過門檻」判斷碰撞或異常 | 即時警報只看扭矩相對額定值（或原始值門檻）並需持續一段時間；健康判斷用摩擦分解後的特徵相對自己的基準。 |
| 以加權的扭矩分布（直方圖）比對做異常判斷 | 用摩擦分解特徵（F、Fc、Fv、漣波）相對基準的比值、z 分數與連續確認；生產中用每日中位數。 |

## 3. Pro 程式層級診斷

| 不做 | 改用 |
|---|---|
| 用模擬或離線計算的結果做判斷 | 只分析**實際量到**的資料。內建模擬器只產生內建演示程式的演示資料，給使用者的程式會拒絕執行。 |
| 「修改 → 量測 → 自動再修改」的反覆最佳化 | 修改建議一次給出（規則版），由人決定改不改；效果用改前、改後與對照組的實測驗證。 |
| 「先把速度設到最高、再逐步調降」，或以目標壽命、目標節拍自動搜尋速度與程式參數 | 只在量到的等待餘裕內建議放慢（節拍不變）；「節拍與壽命取捨表」只列數字，由人決定。 |
| 以能量或耗電模型最佳化速度 | 速度與加速度的建議只依實測扭矩、等待餘裕（節拍不變）與損耗（等效轉數）。 |
| 馬達電流的共振頻帶（頻譜）分析 | 加速規衝擊只看時間域：峰值 > k × 基準 RMS 且峰值因子超過門檻。 |

本節前三列與 FANUC JP5289608B2（以程式模擬估算減速機壽命、自動調降速度指令並變更程式）的逐項技術對照，見〈技術揭露〉6.15。

## 4. 目前沒做、以後可能做

這些不是禁止事項，只是 2.1 還沒做；要做時照一般流程討論。每項功能目前的完成與驗證程度見〈功能狀態表〉（`docs/FEATURE_STATUS.md`）。

- Pro 的 AI 修改建議（技術揭露 6.9.2 的選用方案）；2.1 只有規則版。
- 其他機型、其他品牌的程式解析（目前只有 FANUC .LS、R-2000iC/165F）。
- KAREL 串流範本 `robot/DTM_STREAM.KL` 的實機驗證。

---

## English summary

**Design boundaries — what DTM deliberately does not do.** Reasons are shop-floor safety, credibility of the judgement, and a pre-release patent screening (others hold published patents or applications in some of these directions, so this project does not implement them). The list limits what this repository implements; it does not narrow the methods and variations published in `docs/TECHNICAL_DISCLOSURE.md`. Pull requests that implement these directions are not accepted here.

- **Safety**: never writes to the robot controller or sends motion commands (read-only); never modifies robot programs or lowers speed automatically (Pro suggestions are text only); never recommends replacing a reducer or motor on a single line of evidence.
- **Monitoring**: no grease temperature estimated from motor current (measured housing/ambient temperature only); no combined iron-debris × grease life model (separate pieces of evidence); diagnostic and sweep programs run standalone, not inside production programs; no built-in wireless iron/oil sensor integration; no "motor torque AND external force both above threshold" rule; no weighted torque-distribution comparison.
- **Pro**: analysis of measured data only (the simulator runs only the built-in demo programs); no iterative modify-measure-modify optimisation loop; no "set speed to maximum, then lower it step by step" and no automatic search for speeds or program parameters toward a target life or cycle time; no energy-model speed optimisation; no motor-current resonance-band analysis (accelerometer impacts are judged in the time domain). The element-by-element comparison with FANUC JP5289608B2 is in section 6.15 of the technical disclosure.
- **Not yet done (may come later)**: AI-written suggestions, other robot brands and models, field validation of the KAREL stream template.
