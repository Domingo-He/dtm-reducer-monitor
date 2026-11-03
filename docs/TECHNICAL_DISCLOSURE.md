# DTM 技術揭露文件（防禦性公開）

**Technical Disclosure: Condition Monitoring, Program-Level Wear Diagnosis and Verified Program Optimization for Industrial Robot Reducers**

| 項目 | 內容 |
| --- | --- |
| 作者 | Domingo He |
| 文件版本 | 1.5（2026-10-28 修訂：依外部審查修正描述，新增變化例，新增英文與日文全文；見第 10 節修訂紀錄） |
| 首次公開日期 | 以本專案 GitHub Release 建立時間、Zenodo DOI 與 Software Heritage 存檔日期為準（發布時填入第 10 節） |
| 對應程式 | DTM v2.1.0 起；第 6 章 P1–P13 已實作（`dtm/pro/`），6.9.2 的 AI 選用方案未實作；1.5 版新增、標示「v2.1.0 未實作」的條目只是揭露；6.15 為與已知專利的技術對照 |
| 授權 | 隨本專案以 Apache-2.0 公開 |
| 語言 | 繁體中文（本文件）、英文（`TECHNICAL_DISCLOSURE.en.md`）、日文（`TECHNICAL_DISCLOSURE.ja.md`）三版同時公開，章節編號相同（見 0.5）；第 9 章為英文摘要 |

---

## 0 公開聲明

0.1 本文件的目的是**公開**下列技術內容，使其成為先前技術（prior art）。作者不就本文件任何內容申請專利，也不主張任何專利權利；任何人都可以依 Apache-2.0 授權自由使用、修改與實施。

0.2 本文件所描述的每一個特徵、步驟、數值與變化例，都可以**單獨實施**，也可以與本文件其他任何特徵**任意組合**實施。文中的數值、門檻、取樣頻率與參數都是**範例**，不限於該數值；凡是本領域技術人員依本文件可輕易調整的數值範圍、替代感測方式、替代運算方式，均視為已揭露。

0.3 「機器人」不限於特定品牌或型號；「減速機」不限於特定型式（見第 7 章）。文中以 FANUC R-2000iC/165F 與 RV 型減速機為主要實施例，僅為說明方便。

0.4 本文件的構想、判斷規則與現場知識由作者提出；文字與程式碼由 AI（Anthropic Claude）協助撰寫，並由作者確認內容。1.5 版新增的部分變化例來自外部審查意見（包括其他 AI 模型的審查）與 AI 建議，經作者確認後一併公開。

0.5 **語言版本**：中、英、日三版同時公開；內容有差異時，各版所揭露的內容都屬公開。三版的章節與條目編號相同（`docs/TECHNICAL_DISCLOSURE.md`、`docs/TECHNICAL_DISCLOSURE.en.md`、`docs/TECHNICAL_DISCLOSURE.ja.md`），任何一版都不限縮其他版本的揭露範圍。

---

## 1 技術領域

1.1 本揭露涉及工業機器人與其他具有減速機構的設備之狀態監測、故障預測、壽命估算，以及依據實測資料找出「造成磨耗的程式行」並驗證程式修改效果的方法與系統。

---

## 2 背景與要解決的問題

### 2.1 減速機的典型失效

2.1.1 機器人關節減速機（例如 RV 型擺線減速機、諧波減速機、行星齒輪）常見的失效有：潤滑脂劣化或流失、齒面點蝕與剝離、軸承磨耗、局部損傷後快速惡化、斷齒，以及馬達側的軸承、煞車或編碼器異常。

2.1.2 減速機磨耗通常不是線性的：初期緩慢，到某個轉折點後突然惡化，最後停機。潤滑脂中的鐵粉濃度上升常是重要徵兆，但過了轉折點後，直線外推剩餘壽命並不可靠。

### 2.2 既有做法的限制

2.2.1 機器人原廠的監測服務多半只支援自家機器人，判斷方式不公開。

2.2.2 原廠離線模擬軟體可用程式模擬估算減速機磨耗（例如 FANUC JP5289608B2 揭示：模擬執行動作程式算出各軸減速機的轉速與負荷、估算壽命，負荷超過基準值時自動調降速度指令；對照見 6.15），但屬於統計模型，通常不考慮實際溫度、污染、潤滑狀態、真實急停衝擊與現場負載。

2.2.3 外加感測器方案需要硬體與安裝，且多以單一訊號（振動或聲音）判斷，誤報率高。

### 2.3 模擬判定沒問題、現場卻反覆損壞的原因

2.3.1 **只檢查單次是否超過容許值。** 典型的重負載搬運程式，可能以 0 到 100% 的加速度啟動並急停，每分鐘 5–10 次，一天 7,200–14,400 次，一個月 21.6–43.2 萬次。每一次都在容許範圍內，累積損耗仍然很大。

2.3.2 **壽命公式假設負荷平均分散在整圈。** 同一個動作反覆執行，負荷集中在減速機同一段齒面與同一段軸承滾道；小角度來回擺動時，潤滑脂不易被帶回接觸面（微動磨損、假性壓痕），平均扭矩不高也會局部磨耗。

2.3.3 **使用標稱負載與理想模型。** 高溫、油脂劣化、真實的急停衝擊不在計算內。

2.3.4 **連續運轉。** 數週到一個多月不停機，沒有冷卻時間，溫度維持高點，油脂劣化加速。

2.3.5 **節拍縮短的代價是非線性的。** 壽命與等效扭矩的 10/3 次方成反比（RV 型），等效扭矩增加 10%，壽命減少約 27%。

### 2.4 本揭露的目標

2.4.1 用控制器已有的資料（必要時加上溫度、振動、鐵粉）長期監測減速機健康，並區分潤滑、減速機、馬達三種問題。

2.4.2 在短期「診斷優化期間」提高取樣，把扭矩、振動對到程式的每一行與關節的每一個角度，找出哪一行、哪一段角度在消耗壽命。

2.4.3 提出節拍不變或代價最小的程式修改建議，並以改前改後、對照組的實測證明效果。

### 2.5 來源說明

2.5.1 第 2.1.2 節與第 2.3 節對現場狀況的描述——重負載程式的加減速方式與每分鐘次數、負荷集中在同一段齒面與滾道、模擬使用標稱負載、連續運轉數週不停機、磨耗過了轉折點後快速惡化並伴隨鐵粉上升——來自作者約十年 FANUC 機器人維修的現場經驗，屬於**經驗值**，不是引用文獻的統計；不同工廠、程式與機型會有差異。

2.5.2 壽命與等效扭矩的 10/3 次方關係取自 RV 型減速機公開的選型資料；2.3.5 的「約減少 27%」由該關係推算：1.1^(10/3) ≈ 1.374，1 ÷ 1.374 ≈ 0.73。

---

## 3 名詞與符號

| 名詞 | 定義 |
| --- | --- |
| 干擾扭矩 τ | 控制器估算的「實際馬達扭矩減去動力學模型扭矩」；FANUC 為 `$MOR_GRP[g].$CUR_DIS_TRQ[i]`。其他品牌可用外力估算值、扭矩指令與模型差、馬達電流換算值代替 |
| 傳遞扭矩 T | 減速機實際傳遞的總扭矩，可由馬達電流 × 扭矩常數 × 減速比 × 效率、或控制器扭矩指令換算 |
| 診斷程式 | 定期執行的單軸等速往返動作，用來量摩擦 |
| 段代碼 | 機器人程式寫入暫存器的整數，標示目前在哪一段動作 |
| 動作代碼 | 生產程式中固定動作的段代碼（範例 9000–9899） |
| F、Fc、Fv、G | 摩擦、固定（庫倫）摩擦、黏性摩擦係數、重力型偏差（定義見 5.2） |
| 漣波 ripple | 等速段內干擾扭矩的標準差，反映齒面與軸承的局部不平順 |
| 基準 | 每次重設（換脂、換件）後前 N 筆有效資料的平均與標準差 |
| 嚴重度 s | 0–1.5 的無因次值；0.5 = 注意、1.0 = 警戒（見 5.11） |
| 家族 | 彼此獨立的證據來源：扭矩、鐵粉、振動、溫度 |
| W | 鐵粉生成速率（mg/h） |
| ΔT | 溫升 = 減速機外殼溫度 − 環境溫度 |
| η、β | Weibull 尺度與形狀參數 |
| p | 壽命指數；RV／擺線型與滾子軸承 10/3，滾珠軸承與諧波減速機 3 |

---

## 4 系統架構

### 4.1 資料取得（不需控制器選配的實施例）

4.1.1 在控制器的背景邏輯（Background Logic）執行一支常駐程式，每個掃描週期把各軸干擾扭矩複製到數值暫存器，並寫入心跳計數與速度倍率：

```
R[101]=$MOR_GRP[1].$CUR_DIS_TRQ[1]   … R[106]（J1–J6）
R[107]=第 7 軸（行走軸 E1）干擾扭矩（選用）
R[112]=(R[112]+1) MOD 10000          心跳
R[113]=$MCR.$GENOVERRIDE              目前速度倍率
```

4.1.2 監測電腦以 HTTP 讀取控制器內建網頁伺服器的數值暫存器清單（例如 `/MD/NUMREG.VA`），範例每秒 2–5 次，不需購買通訊選配。其他實施例：Modbus TCP（經 PLC 或閘道器）、OPC UA、EtherNet/IP、控制器端程式主動以 Socket 傳送、外部資料擷取卡。

4.1.3 暫存器編號可設定，避免與現場既有用途衝突。

4.1.4 段代碼編碼（範例）：

- 診斷段：`軸號 × 1000 + 速度% × 10 + 方向`，方向 1 = A→B、2 = B→A。例：2051 = J2、5%、正向。
- 生產動作代碼：9000–9899。
- 振動取樣窗：99AA，AA = 軸號。
- 0 = 不在任何段。

4.1.5 選用感測器：外殼溫度、環境溫度、徑向與軸向振動（感測器內部計算 RMS、包絡、嚙合頻率能量、馬達轉頻能量、峭度，監測端只讀特徵值）、鐵粉濃度（人工取樣送驗或線上感測器）。

4.1.6 感測器更新間隔由系統自動判斷：更新太慢（例如電池型無線感測器 1–5 分鐘一次）就只採用落在等速段或取樣窗內的讀值，並只用於每日趨勢。

### 4.2 執行型態

4.2.1 **單機型**：一台電腦監測一台或數台機器人，內含網頁儀表板與通知。

4.2.2 **邊緣＋中央型**：每個廠區一台邊緣主機，為每台機器人各啟動一個監測程序；中央伺服器彙整多廠資料、發通知、計算機群壽命統計並把規則下發給邊緣。

4.2.3 **小型模式**：10 台以下不設中央，由邊緣主機兼任通知與機群統計。

4.2.4 資料同步：邊緣以每個資料表的游標增量上傳（gzip 壓縮、Bearer 權杖）；中央回應時附帶待執行指令（登錄事件、撤銷、鐵粉、振動、時數、警報確認）與最新規則；邊緣執行後回報結果。邊緣離線時由邊緣自行通知（可設定僅警戒或全部），中央只補發仍在有效期限內的警報。

### 4.3 儲存

4.3.1 以本機資料庫儲存每次診斷特徵、每個生產循環特徵、每日中位數、事件（換脂、換件、程式修改）、鐵粉、振動、元件、運轉時數、警報、通知紀錄與操作紀錄。原始高頻資料只保留診斷段與警報前後各 10 分鐘（範例保留 180 天）。

---

## 5 監測方法（M 系列，DTM 監測版）

### 5.1 M1 診斷程式與切段

5.1.1 診斷程式在暖機後、相同負載下執行：每個受測軸從 A 角度到 B 角度（只有受測軸不同）以數個固定速度（範例 5%、10%、20%）各正反轉一次；每段開始前寫入段代碼，結束後寫回 0，段間停 0.5–1 秒。速度倍率必須為 100%，否則該段作廢。

5.1.2 每段去掉頭尾 `trim_s`（範例 1.0 秒）的加減速部分，剩下的等速部分至少 `min_const_s`（範例 2.0 秒）且至少 3 筆有效樣本才採用；超過無效值上限的樣本剔除。

5.1.3 每段計算平均、標準差、絕對值峰值與溫度平均。等速段的時間窗同時交給振動感測器切片，使振動特徵與扭矩特徵屬於同一動作。

5.1.4 判斷「一次診斷」的方式：專用旗標暫存器 R[111] = 1；或不使用旗標，而以段代碼出現的間隔判斷（間隔超過 `run_gap_s` 視為結束；超過 `max_run_s` 強制結束）。

### 5.2 M2 摩擦分解

5.2.1 同一速度的正反轉兩段：

$$F = s_{dir}\cdot\frac{\tau_{+}-\tau_{-}}{2}, \qquad G = \frac{\tau_{+}+\tau_{-}}{2}$$

F 為摩擦（方向相反時變號），G 為不隨方向變號的偏差（重力、平衡器、負載設定誤差）；s_dir 為依軸安裝方向設定的 ±1。

5.2.2 多個速度做線性回歸：

$$F(v) = F_c + F_v\cdot v$$

v 可為速度百分比或實際關節角速度。F_c 為固定摩擦，F_v 為黏性摩擦係數。參考速度（範例 10%）的 F 作為代表值；該速度缺測時以回歸式計算。

5.2.3 漣波 = 各速度兩方向等速段標準差的平均；峰值 = 各段絕對值峰值的最大值。

5.2.4 變化例：使用 Stribeck 等非線性摩擦模型；使用更多速度點；使用馬達電流或扭矩指令代替干擾扭矩；在不同溫度下分別建模。

5.2.5 失效模式提示（範例規則）：漣波 ≥ 1.4 倍且 F ≥ 1.2 倍 → 減速機磨耗或局部損傷；漣波 ≥ 1.8 倍 → 疑似剝離或點蝕；F_v 上升為主（≥ 1.15 倍且 > 1.1 × F_c 比值）→ 油脂劣化、過量填脂或低溫；F_c 上升為主 → 減速機磨耗、煞車拖曳或預壓異常；G 偏移 → 平衡器或負載設定，不是摩擦。

5.2.6 **訊號確認**（v2.1.0 未實作）：摩擦分解的前提是控制器讀出的干擾扭矩以摩擦為主；各廠的估算方法、單位與濾波並不公開，因此導入時先確認：(a) 讀值與示教器畫面上的值一致（同一時刻、同一軸）；(b) 單位、尺度與正負號已確認，未確認時只當相對值使用，不換算成 N·m、不估算壽命；(c) 記錄控制器軟體版本與選配，版本或選配不同的控制器分別確認；(d) **冷暖機反應檢查**：冷機時與暖機後各執行一次診斷程式，暖機後的 F_v 應明顯較低，方向相反或沒有變化時判定訊號不以摩擦為主，停用摩擦判定、只保留原始趨勢；(e) 變化例：重複執行、交換正反轉的先後順序、改變負載，檢查 F 與 G 的重複性與反應方向是否符合預期。

### 5.3 M3 基準、比值、z 分數與遲滯

5.3.1 每次重設後前 N 次（範例 10）有效診斷建立基準（平均 μ、標準差 σ）。

5.3.2 比值 r = 值 / μ 為主判定：r ≥ 1.5 注意、r ≥ 2.0 警戒（範例）。

5.3.3 z 分數只用於提早升級：基準筆數足夠時，z = (值 − μ) / max(σ, 0.05·|μ|)；z ≥ 2 且 r ≥ 1.15 可提早升「注意」；已達注意比值且 z ≥ 3 升「警戒」。

5.3.4 遲滯：連續 2 次（範例）都達到才升級、連續 2 次都低於才降級。

5.3.5 G 偏移以「|G − G_base| / F_base」判定，行走軸（水平直線）不判 G。

5.3.6 換脂、換件等重設事件把資料切成多段，每段各自建立基準；同時另存「相對新品基準」的累積比值（5.11.6）。

### 5.4 M4 溫度補償

5.4.1 有溫度訊號時：

$$x_{comp} = x\cdot e^{\,k\,(T - T_{ref})}$$

T_ref 為基準期平均溫度。預設係數 k：F_v 每 °C 約 0.025、F_c 約 0.004；F 依兩者比重加權。

5.4.2 係數可由使用者資料估計：以 ln(x) = a − k·T + c·天數 做雙變數回歸（天數項吸收緩慢劣化），回傳 k、標準誤、樣本數、溫度範圍與是否可採用。

5.4.3 只監測一個軸、且基準期溫差 ≥ 5 °C 時，改用基準期資料自己學出的 k。

### 5.5 M5 跨軸共模扣除

5.5.1 氣溫、冷機、生產節拍改變會讓所有軸的摩擦一起變；單一軸劣化只有該軸變。

5.5.2 對每個軸，以其他軸「F_v 的累積對數變化」在每個時間點取中位數，建立共同變化指數；跨越換脂、換件的那一步不計入。把共同 F_v 變化換算為等效溫差，再依各指標的溫度係數換算為 F、F_c 的倍數後扣除。如此兩軸同時的固定摩擦上升（磨耗）不會被當成共同變化扣掉。

5.5.3 設定：一律使用、僅無溫度訊號時使用、不使用。代價：所有軸同時以相同比例劣化會被扣掉，此情況由油脂壽命與鐵粉把關。

### 5.6 M6 生產循環監測（動作代碼）

5.6.1 生產程式在固定動作前後寫入動作代碼。每個循環對每軸記錄干擾扭矩的平均、RMS、峰值、標準差、動作長度、倍率、溫度，以及同一時間窗的徑向與軸向振動。

5.6.2 只採用倍率 100% 的循環；一天至少 20 個有效循環才算有效日（範例）。

5.6.3 判定看「每日中位數」：每個（動作代碼, 軸）以重設後前 10 個有效日建立基準，比值 ≥ 1.15 注意、≥ 1.30 警戒、連續 2 個有效日才升降級（範例）。生產扭矩含慣性與重力，門檻因此低於診斷程式。

5.6.4 改了該段點位或速度就重設該代碼基準；可自動偵測程式修改（比對程式檔修改時間或雜湊）並提示重設。

### 5.7 M7 趨勢外推與不確定性

5.7.1 以最近 30 次（範例）溫度補償後的 F 對天數做線性回歸，外推到注意與警戒門檻的天數；已超過者為 0。

5.7.2 外推上限 730 天，超過不顯示；顯示區間為 ±(30% × 天數 + 7 天)（經驗值）。

5.7.3 偵測到加速時改用保守估計（取較短者）。實施例：把視窗分成前後兩半各自回歸，後半斜率為正且 ≥ 前半斜率的 1.5 倍；或二次回歸的曲率為正、曲線在最新一點的斜率 ≥ 整段直線斜率的 1.5 倍，而且二次曲線的殘差平方和 ≤ 直線的 0.8 倍（避免雜訊被誤判為加速）。保守估計取「整段直線」「後半段斜率直線」「二次曲線與門檻的交點」三者中最短的天數。

5.7.4 剩餘天數取扭矩、生產循環、鐵粉、振動包絡各自外推結果的最小值；被判定為馬達造成的摩擦上升不計入。

### 5.8 M8 振動特徵

5.8.1 徑向與軸向各自以重設後前 10 筆建立基準；RMS、包絡、嚙合、馬達轉頻的比值 ≥ 2 注意、≥ 3 警戒；峭度以絕對值判定（常態約 3，≥ 4 注意、≥ 6 警戒）（範例）。

5.8.2 軸向／徑向 RMS 比相對基準的變化 ≥ 1.5 倍視為主軸承或軸向竄動徵兆。

5.8.3 包絡值以最近 30 筆對天數回歸，外推到警戒的天數。

### 5.9 M9 鐵粉濃度

5.9.1 樣本類型：換脂後初始、運轉中途、換脂排出。換脂排出樣本以「中段」為預設：先丟棄前段排出的舊脂，取中段送驗，降低沉積物與管路殘留造成的偏差。

5.9.2 生成速率（以每批油脂為一個週期）：

$$W = \frac{\Delta c}{100}\cdot\frac{M}{\Delta h}\cdot 1000 \quad[\text{mg/h}]$$

Δc 為兩次樣本的濃度差（wt%）、M 為該軸油脂量（g）、Δh 為兩次取樣間的運轉時數；換脂後運轉未達 300 小時（範例）不計算。元件更換後第一個 W 為新品基準。

5.9.3 嚴重度：濃度以絕對門檻判定（RV 型起始範例 0.05 wt% 注意、0.10 wt% 警戒，請以拆檢修正）；W 以相對新品基準的比值判定（2 倍注意、3 倍警戒）；兩者取大。

5.9.4 以目前 W 外推何時到警戒濃度、下次換脂時的預估濃度。

5.9.5 **轉折點規則**：濃度達注意後不再直線外推剩餘天數，改顯示轉折點提示（實施例的畫面文字為「已過轉折點，可能隨時惡化」），備品直接建議下單；連續兩次 W 增量加速（W₃ − W₂ > W₂ − W₁ > 0）則升一級。濃度達注意門檻是磨耗風險的篩查訊號：上升可能來自長期正常磨耗、沉積物、取樣偏差或真正的加速磨耗，不代表已確定進入加速期，應加密取樣並與扭矩、振動等其他證據交叉確認。

5.9.6 取樣間隔自動縮短：注意時 60 天、警戒時 21 天（範例），並自動排入保養排程；濃度達警戒時強制換脂等級為「立即」。

5.9.7 變化例：線上油液金屬粒子感測器、鐵譜分析（大顆粒比例）、磁性螺塞檢查。

5.9.8 **換脂後的濃度起點**：換脂多半是以新脂把舊脂推出，不是完全置換，殘留的舊脂使換脂後的濃度不為 0。換脂後取一次初始樣本作為該批油脂的濃度起點（實施例已採用）。沒有初始樣本時（v2.1.0 未實作；實施例以 0 為起點），該批第一個樣本只顯示濃度、不計算生成速率，或以設定的殘留比例 × 換脂前濃度估計起點並標示為估計值。

### 5.10 M10 溫度

5.10.1 **油脂有效老化**：外殼溫度高於參考值（範例 40 °C）時，每高 10 °C 老化速率加倍（只加速、不因低溫延長）。換脂週期以時數或年數先到者為準。

5.10.2 **穩定高溫**：只縮短換脂週期，顯示補正倍率與調整後週期，不當作磨耗。

5.10.3 **溫度突升**：同一動作代碼下的 ΔT（外殼 − 環境）與自己 14 天中位數比較，高 8 °C 以上、或一天上升 5 °C 以上，且環境溫度沒有同步上升 → 「溫度突升」注意（起始值可設定）。

5.10.4 換脂後 72 小時內不判突升；若持續不降，提醒檢查內壓與油封。

5.10.5 溫升歸在摩擦家族，比重 0.6，單獨最多只到「觀察」；另可設過熱絕對門檻（預設停用）。

5.10.6 **參考溫度的替代**：沒有環境溫度感測器時，以同一天、同一動作代碼下「其他軸外殼溫度的中位數」作為參考（至少另 2 軸），共同的氣溫、冷機與廠房空調變化會一起被扣除；連這個也沒有時，溫升只顯示、不發警報、不納入決策。突升幅度換算嚴重度：0.5×門檻以下為 0，到門檻為 0.5，到 2 倍門檻為 1.0（上限 1.5）。

5.10.7 **換脂後未回降**：換脂後 72 小時到 14 天之間，若 ΔT 仍比換脂前 14 天的中位數高 5 °C 以上（範例），提示確認排出口殘壓已釋放、填脂量是否過多、油封有無滲漏。

### 5.11 M11 證據家族與決策

5.11.1 嚴重度函數（比值型與絕對值型）：

$$s(r)=\begin{cases}0 & r\le 1\\ 0.5\,\dfrac{r-1}{c-1} & 1<r\le c\\ \min\!\left(1.5,\;0.5+0.5\,\dfrac{r-c}{a-c}\right) & r>c\end{cases}$$

c、a 為注意與警戒門檻；絕對值型以下限 floor 取代 1。

5.11.2 家族分數：

- 扭矩磨耗型 = max(Fc、漣波、0.8·F、相對新品累積 Fc、相對新品累積漣波、0.8·生產循環)。
- 扭矩黏性型 = F_v。
- 振動減速機型 = max(徑向包絡、嚙合、峭度、0.8·軸向 RMS、0.8·軸向比、生產循環振動)。
- 振動馬達型 = 馬達轉頻（徑向、軸向取大）。
- 鐵粉 = max(濃度、生成速率)。
- 溫度 = ΔT 突升（比重 0.6）。

各係數（範例 Fc／漣波 1.0、F／生產循環 0.8、溫升 0.6、包絡／嚙合／峭度 1.0、軸向 RMS／軸向比 0.8、鐵粉 1.0）可在設定檔調整。

5.11.3 **兩證據規則**：換減速機或馬達的分數，取各家族嚴重度由大到小排序；只有一個家族 ≥ 0.25 時，分數 = min(最大值, 1.5) × 0.45（單一家族最多到「觀察」，極端時到「準備」）；兩個以上時取前兩名平均。

5.11.4 **解釋掉規則**：摩擦上升、但鐵粉與減速機振動都 < 0.5，而馬達轉頻振動上升 → 扭矩磨耗分數乘以 (1 − 0.7·min(1, 馬達振動嚴重度))，研判歸給馬達（煞車拖曳、馬達軸承）。

5.11.5 三個分數：

- 換脂分數 = max(扭矩黏性型, 油脂壽命嚴重度, 0.8·min(鐵粉, 1))；油脂壽命用掉 70% 起開始計分（範例）。
- 減速機分數 = 兩證據規則（扭矩磨耗、鐵粉、振動減速機），並與年齡風險取大。
- 馬達分數 = 兩證據規則（馬達振動、無磨耗證據時的摩擦上升），並與年齡風險取大。

分數 ≥ 0.5 建議處置、≥ 0.9 立即處置（範例）。實施例的對應動作：換脂分數為「建議換脂」／「立即換脂」；減速機分數為「準備備品、縮短監測週期」／「安排更換」；馬達分數為「檢查馬達並準備備品」／「安排更換或送修」。油脂壽命 ≥ 100% 或鐵粉 ≥ 警戒時換脂強制為立即。減速機達立即時，換脂併入換件處理。

5.11.6 **換脂後復原檢查**：換脂並運轉 2–4 週後，比較 F_c 與 W 相對新品基準；回到 1.3 倍以內 → 潤滑問題已解決；仍偏高 → 減速機劣化，準備更換。累積比值以元件更換後第一批診斷為基準，不因換脂重設，避免把持續磨耗「洗掉」。

5.11.7 **年齡風險**：備品前置期間（範例 60 天）內的條件失效機率 ≥ 0.2 時準備備品（範例）；剩餘天數短於前置天數時提示立即訂購。實施例中年齡風險分數 = min(1, 0.5 × 條件失效機率 ÷ 0.2)，條件失效機率約 0.36 以上時減速機或馬達分數達 0.9（安排更換）；這是依壽命統計的預防性更換，不受兩證據規則限制。

5.11.8 **年齡風險上限的變化例**（v2.1.0 未實作）：年齡風險單獨最多到「準備備品」，安排更換仍需要兩類證據；或依後果等級（關鍵軸、備品前置期長短、停機成本）決定年齡風險可否單獨觸發安排更換。5.11.7 的實施例與本條的變化例都屬於本揭露。

### 5.12 M12 壽命模型

5.12.1 運轉時數：有時數表讀數時內插或外推，否則以每日運轉時數估算。

5.12.2 Weibull：R(t) = exp(−(t/η)^β)。失效更換為完整壽命；預防性更換與仍在使用為右設限資料，必須納入擬合。最大概似估計對 β 做格點搜尋、η 有封閉解。

5.12.3 B10 = η·(−ln 0.9)^(1/β)；已用 t 小時、接下來 Δ 小時內失效的條件機率 = 1 − R(t+Δ)/R(t)。

5.12.4 失效紀錄少於 3 筆（範例）時使用設定的先驗，並標示來源。中央以所有廠區同一元件類別的紀錄擬合「機群先驗」，以規則檔下發各邊緣，邊緣依版本自動套用並標示「來自中央」。

5.12.5 **不確定性與資訊性設限**（v2.1.0 未實作）：以輪廓對數概似求 β 的 95% 區間（ℓ_p(β̂) − ℓ_p(β) ≤ 1.92，即自由度 1 的卡方 3.84 的一半；ℓ_p 已在格點搜尋時算出），據以給出 η、B10 與條件失效機率的範圍，備品需求依區間上下限分情境列出。因警報而提前更換、拆檢卻無損傷的元件，可能造成資訊性設限（被移除的件較接近失效，η 會偏樂觀）：分別以「視為失效」與「視為設限」兩種方式擬合，列出結果差異作為敏感度。

### 5.13 M13 機群層級

5.13.1 有效等級 = max(健康、生產循環、決策)；剩餘天數 = min(各來源)，上限 730 天。

5.13.2 備品需求：依各軸剩餘天數與前置天數排序配置現有庫存，列出缺口；風險視圖預設 180 天。

5.13.3 邊緣離線、資料中斷、心跳停止產生系統警報；單台機器人可設「計畫停機」（含結束時間），期間不發資料中斷與連線警報。

5.13.4 時間偏差：同步時比對中央時間，超過 60 秒（範例）警告。

5.13.5 門檻回放：以已存的特徵與每日中位數，套用另一組門檻與比重，計算過去會報幾次、何時報。

5.13.6 導入檢查報告：運轉滿 7 天自動產生，列出取樣頻率與抖動、資料中斷、心跳、診斷段切割成功率與丟棄原因、各軸雜訊、動作代碼涵蓋率、感測器更新間隔。

### 5.14 M14 保養作業包與通知

5.14.1 每個決策附作業包：建議動作、零件、工具、步驟、鐵粉取樣方式；可產生工單。

5.14.2 通知分級與逐級通報（未確認時升級到下一層聯絡人）；簡訊可經 4G 數據機或通用 HTTP 閘道，並有備援、每日上限與值班表；每日摘要兼作系統心跳。

---

### 5.15 M15 資料保留、拆檢回饋與門檻回放

5.15.1 **事件導向的原始資料保留**：一般原始取樣只保留短期（範例 7 天）；每次診斷段的原始波形與每次警報發生時刻前後各一段時間（範例 ±10 分鐘）的原始資料，以時間區間標記長期保留（範例 180 天），清除原始資料時跳過被標記的區間；每日備份也只帶這些區間。

5.15.2 **拆檢回饋**：更換減速機、齒輪箱或馬達時記錄拆下零件的損傷位置（輸入齒輪、曲軸軸承、擺線齒輪、針齒、主軸承、油封、齒輪箱齒輪或軸承、馬達軸承、煞車）、損傷型態（點蝕、剝離、磨耗、斷齒）、油脂狀態與照片；若「檢查無異常」，該次更換在壽命統計中改為設限（預防更換）資料，並保留「判斷可能過早」的紀錄供修正門檻。

5.15.3 **門檻回放**：以已保存的診斷特徵與每日動作中位數，套用另一組門檻與證據比重重新計算每次（每天）的等級，套用相同遲滯規則，列出「原設定與新設定」各會發出幾次注意／警戒及第一次的時間；換脂、換件依實際時間重設基準。也可每天以當時為止的資料重算一次維護決策，比較換脂、換件建議的時間。

5.15.4 **分析資料匯出與匿名化**：依機器人與期間匯出特徵、每日中位數、事件、警報、鐵粉、振動、元件與拆檢、時數、校正、系統事件、去敏設定與保留的原始片段；遮蔽密碼、金鑰、電話、Email、通知網址、網路位址與完整檔案路徑；可選擇把廠名、機器人、產線、操作人一致地換成代號。

### 5.16 M16 運作品質

5.16.1 **導入檢查報告**：導入後第一次累積滿 7 天自動產生（也可隨時產生），檢查取樣頻率與取樣間隔百分位（抖動）、資料中斷次數與時間、心跳與連線警報、診斷段切割成功率與丟棄原因、各軸雜訊（漣波中位數、F 的變異係數）、動作代碼涵蓋率（每代碼每天有效循環數與排除原因）、感測器更新間隔，逐項標示通過或待處理。

5.16.2 **計畫停機**：對單台機器人設定停機結束時間，期間抑制資料中斷、心跳停止、連線中斷與無效值警報及看門狗的資料中斷判定，並解除既有的同類警報；其他判斷照常，到期自動失效。

5.16.3 **時間偏差**：每次與中央同步時，以「送出與收到時刻的中點 − 中央回覆時間」估計本機時鐘偏差，超過門檻（範例 60 秒）發警報；中央另以上傳內容的送出時間估計各廠偏差並顯示。

5.16.4 **小型模式**：沒有中央伺服器時，由同一廠區的邊緣主機統一發送通知、逐級通報與每日摘要（各機器人程式在邊緣主機正常時不各自發送，邊緣主機停擺時才自行發送警戒），並以本廠所有機器人的更換紀錄計算 Weibull 先驗下發；每個壽命模型標示來源（自有紀錄、本廠機群、中央機群、設定先驗）。每日摘要兼作系統心跳。

5.16.5 **拒絕輸出與原因代碼**（v2.1.0 未實作）：每項計算結果附有效旗標與原因代碼，例如訊號未確認、單位未知、時間未同步、工況改變、無足夠等速段、傳遞扭矩未校正、對位不明、模型不可辨識、型錄參數未確認、失效標記不足；條件不符時不輸出數值結論，只顯示原因與原始趨勢。資料字典區分原始訊號、代理量與已校正的物理量。

## 6 程式層級診斷與驗證方法（P 系列，進階診斷模組）

本章方法已在 DTM v2.1.0 實作（`dtm/pro/`；6.9.2 的 AI 選用方案除外），實作時採用的具體做法與參數記在 6.14。以下方法可在任何具有程式語言的機器人控制器上實施；6.14 的做法是實施例之一，不限制 6.1–6.13 的範圍。

### 6.1 P1 程式匯入與解析

6.1.1 讀取機器人程式的文字版（FANUC 為 .LS，可由控制器網頁取得；KUKA 為 .src/.dat；ABB 為 RAPID .mod；安川為 INFORM .JBI；其他品牌同理）。

6.1.2 對每一行解析：行號、指令類型（關節 J／直線 L／圓弧 C／其他）、目標點（P[n]、PR[n]、偏移、座標系 UF/UT）、速度與單位、定位方式（FINE 或 CNT 值）、加速度倍率（ACC）、等待（WAIT 時間或訊號）、I/O、呼叫、跳躍與標籤。

6.1.3 建立程式控制流程圖（含分支與子程式），並記錄每個教導點的關節值或直角座標值。程式檔的雜湊值與修改時間作為「程式版本」，任何修改都會切分資料。

### 6.2 P2 診斷優化模式（暫時提高取樣）

6.2.1 只在診斷期間（範例 3–7 天）提高取樣，期滿自動恢復常駐監測的低頻取樣。

6.2.2 扭矩與位置取樣範例每秒 50–125 次（足以看清 0.2–1 秒的加減速）；急停與衝擊需要毫秒等級，由加速規負責。

6.2.3 取得方式（任一或組合）：

- 控制器端程式（例如 KAREL）以插補週期讀取關節位置、干擾扭矩、扭矩指令或馬達電流，以 Socket 串流或寫檔。
- 控制器的外部軌跡串流介面（例如 FANUC Stream Motion）所回傳的關節與直角座標狀態。
- 外部擷取卡：加速規每秒數千次以上（範例 5–25 kHz），並以機器人數位輸出（DO／GO）在每段或每行開始時輸出代碼，作為硬體對時訊號，對時誤差在毫秒等級。
- 鉗式電流感測器量馬達電流。

6.2.4 加速規以攜帶式套件輪流安裝：耐溫規格符合環境、以螺絲鎖固（高溫下不用磁座）、線材沿手臂固定並考慮彎折壽命、避開伺服線的電磁干擾。

### 6.3 P3 行號對位（位置比對）

6.3.1 問題：以程式在每行前寫入暫存器回報行號時，控制器為了 CNT 平滑過點，可能在機器人到達前就執行後面的非動作指令，回報的行號會比實際位置早。

6.3.2 方法：以位置判斷機器人正在執行哪一行。

1. 對每個動作行，取「起點 = 前一行目標點、終點 = 本行目標點」，在關節空間（J 動作）或直角空間（L、C 動作）建立預期路徑；CNT 點附近建立轉角區（兩段路徑的混合區）。
2. 每筆樣本計算到候選路徑的距離與投影參數 u ∈ [0, 1]。
3. 以狀態機或隱藏式馬可夫模型（例如 Viterbi）強制「依程式順序前進」：只允許停留在本行、進入下一行，或進入控制流程圖允許的分支目標；距離與跳轉成本最小者為對位結果。
4. 速度接近 0 且位於某點附近的時段標記為停留（WAIT、定位、夾取）。
5. 輸出：行號、行內進度 u、信心值。PR 與偏移點以執行時讀到的暫存器值計算；無法解析時以實際軌跡的重複模式學習出路徑。

6.3.3 替代方式：診斷期間使用「加註版程式」，在每一行前後輸出 DO／GO 代碼（搭配控制器的提前或延後觸發功能修正時間），診斷結束後換回原程式。

6.3.4 **未歸因與準確率評估**（v2.1.0 未實作）：信心低於門檻或候選行無法區分的樣本列為「未歸因」，不併入任何一行的損耗帳本，並顯示未歸因損耗的比例；以 DO／GO 硬體代碼或人工標記作為真值，評估逐樣本行號正確率、每行起訖的時間誤差，以及 CNT、WAIT、CALL、END 附近的錯誤率。

### 6.4 P4 循環切割與同步平均

6.4.1 循環起點：程式起始、第一行、或位置自相關找出的重複週期。

6.4.2 每個循環中，每一行的樣本依行內進度 u 重新取樣到固定格點（範例 50 點），使等待時間不同的循環也能逐行對齊。

6.4.3 對每一行、每個格點計算平均、中位數、第 10 與第 90 百分位，畫出典型波形與差異範圍；循環間變異係數上升本身是產線不穩定的訊號；與中位數差異超過 3 倍絕對中位差的循環標記為異常循環。

### 6.5 P5 逐行損耗帳本

6.5.1 以每筆樣本的減速機輸出端角速度 ω（關節速度，或馬達速度 ÷ 減速比）與傳遞扭矩 T 計算。干擾扭矩用於異常偵測；損耗計算使用傳遞扭矩（兩者都揭露）。

6.5.2 每一行的等效扭矩：

$$T_{m,\ell}=\left(\frac{\sum_{i\in\ell}|\omega_i|\,\Delta t\,|T_i|^{p}}{\sum_{i\in\ell}|\omega_i|\,\Delta t}\right)^{1/p}$$

6.5.3 每一行的損耗（以額定扭矩下的等效轉數計）：

$$D_\ell=\sum_{i\in\ell}\frac{|\omega_i|\,\Delta t}{2\pi}\left(\frac{|T_i|}{T_0}\right)^{p}$$

額定壽命 L₀ 小時、額定輸出轉速 N₀ rpm 對應的額定總轉數 R₀ = 60·L₀·N₀；每循環壽命消耗比 = Σ D_ℓ / R₀；每日消耗 = 每循環消耗 × 每日循環數。

6.5.4 峰值檢查：每一行的 max|T| 相對「起動停止容許扭矩」的比值與超過次數；超過「瞬間最大容許扭矩」的事件依容許次數 C 計入，每次消耗 1/C。

6.5.5 角速度近 0 時（保持扭矩）上式為 0，另以「保持時間 × 扭矩」與「小幅振盪次數」作為微動磨損指標。

6.5.6 輸出：每一行損耗占比排行、每日總消耗、峰值超標行、預估到達額定壽命的時間（額定壽命是統計值，同型元件約 10% 在此之前失效，不代表個別元件在當時損壞）；絕對額定值未知時（客製減速機）只做相對排名，絕對壽命以換件紀錄校正。

6.5.7 變化例：以馬達電流平方積分、振動 RMS、溫升作為替代損耗指標；p 依減速機型式設定；以實測 Weibull 修正。

6.5.8 **摩擦功熱區**（v2.1.0 未實作）：只能取得干擾扭矩時，不以 p 次方計算疲勞損耗，改以

$$E_\ell=\sum_{i\in\ell}|\Delta\theta_i|\cdot|\tau_{dis,i}|$$

（指數 p = 1；也可用 P7 的 F(θ) 取代 τ_dis）累積各行、各角度區間的「摩擦功熱區」。依 Archard 磨耗律（磨耗量正比於負荷 × 滑動距離），此量反映磨耗傾向而非疲勞損耗；與疲勞損耗帳本分開命名、分開輸出，未做 N·m 校正時以代理單位表示。

6.5.9 **傳遞扭矩的換算方向**（v2.1.0 未實作）：由馬達電流或扭矩指令換算傳遞扭矩時，依功率流向區分正向驅動（T_out = η·i·T_motor）與被反向驅動（T_out = i·T_motor／η′，例如重力帶動下降、減速停止）；η、η′ 可隨速度、溫度、負載以表格設定，並記錄扭矩來源、換算係數與不確定度。平衡器（彈簧或氣壓）的力與摩擦以 G(θ) 與正反轉差異另外估計後扣除，避免平衡器摩擦被當成減速機摩擦。

### 6.6 P6 角度集中圖

6.6.1 把減速機輸出角 θ 分成小區間（範例 1°），每個區間累積：

$$d(\theta_k)=\sum_{i:\,\theta_i\in k}|\omega_i|\,\Delta t\left(\frac{|T_i|}{T_0}\right)^{p}$$

同時累積：通過次數、換向次數（速度變號）、每次擺動的幅度分佈。

6.6.2 集中度指標：包含 80% 損耗的最窄角度範圍寬度 θ₈₀、Gini 係數、前 k 個區間占比。θ₈₀ 越窄代表損耗越集中於輸出角度的同一小段範圍（與內部接觸位置的對應見 6.6.6）。

6.6.3 小幅擺動判定：擺動幅度換算到滾動體或齒面位移，若小於接觸寬度（依型號估算），標記為微動磨損風險。

6.6.4 可依減速比、擺線輪齒數、針齒數或諧波柔輪齒數，把輸出角換算為對應的齒、針或滾道位置，輸出「預估磨耗位置」，供拆檢對照。

6.6.5 建議：在不影響干涉的前提下改變工作角度（例如手腕軸以不同轉數姿態達成相同姿勢）、交替使用不同角度區段、或定期執行全範圍慢速運轉。

6.6.6 **輸出角與內部接觸位置**（v2.1.0 未實作）：6.6.4 的換算需要初始相位，可由拆檢時的損傷位置或組裝標記校正。RV 型的第二級中，輸出每轉一個針齒節距（360°／針齒數），曲軸即轉一圈、負荷區繞針齒一周；因此窄的輸出角範圍對應的是針齒與曲軸軸承的全周受力，但只涉及特定的擺線齒與針齒配對。能直接以輸出角定位的，主要是輸出側主軸承轉動環的滾道，以及小幅擺動造成的微動磨損位置。

### 6.7 P7 全範圍掃描與局部摩擦比較

6.7.1 定期（範例每週或換班時，每軸 1–3 分鐘）在無干涉的最大角度範圍內，以 1–2 個固定慢速正反轉各一次。

6.7.2 依角度區間計算摩擦與漣波：

$$F(\theta_k)=\frac{\tau_{+}(\theta_k)-\tau_{-}(\theta_k)}{2},\qquad G(\theta_k)=\frac{\tau_{+}(\theta_k)+\tau_{-}(\theta_k)}{2}$$

6.7.3 以 P6 的高使用區間為集合 U、低使用區間為參考集合 R，計算局部磨耗指數：

$$LWI=\frac{\operatorname{median}_{k\in U}F(\theta_k)}{\operatorname{median}_{k\in R}F(\theta_k)}$$

以溫度補償後的 LWI 相對自己的基準（範例 1.2 倍注意、1.4 倍警戒）與趨勢判定。常用段上升、不常用段不變，即為在用中局部磨耗的跡象（最終以拆檢確認）。

6.7.4 同時記錄角度域的漣波與振動（掃描期間加速規），找出局部衝擊的角度；G(θ) 用於平衡器與重力補償檢查。

6.7.5 慢速大範圍運轉同時讓潤滑脂重新分布，可能減緩小幅擺動造成的磨耗（效果待驗證）。

6.7.6 變化例：只掃描常用段附近的部分範圍；利用生產動作中剛好等速通過某區間的片段做「順便掃描」；以馬達電流代替干擾扭矩。

### 6.8 P8 等待餘裕分析與時間縮放

6.8.1 找出每個循環中，動作完成後等待外部訊號、計時或交握的行，量測每次實際等待時間 w。

6.8.2 等待前一個動作區塊的可用餘裕 S = q 百分位的 w − 安全裕度（範例 q = 10）。

6.8.3 時間縮放：把該區塊時間由 T_move 延長為 T_move + S，縮放係數 σ = (T_move + S)/T_move；速度縮為 1/σ、加速度縮為 1/σ²。新扭矩估計：

$$T'(t)=T_g(t)+\frac{T_{inertia}(t)}{\sigma^{2}}+T_f\!\left(\frac{v}{\sigma}\right)$$

T_g 為重力項、T_inertia 為慣性項、T_f 為摩擦項；以 P5 計算損耗下降量。節拍不變的條件：機器人仍在訊號到來前到達。以第 q 百分位扣除安全裕度，只能降低節拍受影響的機率，不是保證；改後以實測確認節拍。重力與慣性方向相反的時刻，放慢可能使該時刻的扭矩增加，因此以逐點計算的 |T′| 累積損耗。

6.8.4 實施方式：降低該區塊各行的速度或加速度倍率、插入過渡點做軟啟動，或以外部軌跡串流執行平滑軌跡。

6.8.5 無等待餘裕的關鍵路徑段，提供「節拍與壽命取捨表」：每增加 0.1 秒對應的損耗下降與年度換件、停機時數變化。

6.8.6 **估計品質與直接實測**（v2.1.0 未實作）：6.14.6 的逐行回歸須通過品質門檻才輸出預估：決定係數下限、慣性係數 c 為正、設計矩陣條件數上限、以留出的循環驗證預測誤差、係數的拔靴區間；不合格時只列「可試行放慢的候選行」，不輸出損耗下降百分比。變化例：回歸納入其他軸的角加速度與速度乘積項（耦合慣性、科氏與離心項）；或在診斷期間以速度倍率或逐行速度實際放慢到 1/σ 執行數個循環，直接量測扭矩、損耗與節拍的變化，取代或校正回歸估計。

### 6.9 P9 修改建議

6.9.1 規則庫範例：不需精準定位的點 FINE 改 CNT（減少完全停止，需確認干涉）；損耗排行高且有餘裕的行降低 ACC 或速度；長距離動作頭尾插入過渡點與遞增速度做軟啟動；避免手腕不必要的大幅翻轉；確認負載設定（有無工件分開設定）；能用關節動作時避免直線動作經過高扭矩姿態；調整動作順序使高扭矩動作落在有餘裕的位置。

6.9.2 選用 AI：把程式文字、每行數字、餘裕與限制條件（節拍上限、干涉區）結構化後交給語言模型，產出依效益排序的修改方案、修改前後程式對照與理由。數字一律由系統計算，AI 只負責撰寫與排序；可使用雲端或本機模型，送出前可匿名化點位名稱與註解。

6.9.3 安全原則：系統只輸出建議，不寫回控制器、不自動降速；修改由人執行，依序以模擬軟體確認干涉與節拍、低速手動模式試跑、再上線。

### 6.10 P10 改前改後與對照試驗

6.10.1 流程：改前診斷 3–7 天 → 修改 → 改後診斷 3–7 天 → 追蹤 4–8 週（範例）。

6.10.2 對照組：同型號、同工站或相近負載的另一台不修改。

6.10.3 指標：每行峰值扭矩、每循環損耗、溫升、振動衝擊次數、鐵粉 W、摩擦趨勢、LWI。

6.10.4 統計：以每日中位數為單位，計算差異中的差異（處理組變化 − 對照組變化），以拔靴法信賴區間或 Mann–Whitney 檢定判斷是否顯著。

6.10.5 報告：一頁結論（改了什麼、節拍變化、損耗變化、成本換算）＋附錄數據，由保養與生產雙方簽核。成本換算：

$$\text{年節省}=N_{\text{避免}}\times\left(H_{\text{停機}}\times C_{\text{每小時}}+C_{\text{備品}}+C_{\text{人工}}\right)+C_{\text{故障前產出下降}}$$

6.10.6 **統計變化例**（v2.1.0 未實作）：拔靴法以日期為單位配對重抽（同一天的處理組與對照組一起抽），或以連續數天為區塊重抽，保留跨日相關與兩台機器人的共同變化；只有一台時，在安全許可下以 A／B／A／B 交替試驗。門檻回放依時間切分，只使用當時可得的資料、當時的設定與基準，並與定期保養、單一門檻等簡單基線比較偵測率、每機器人年誤報次數與提前天數。Mann–Whitney 檢定只作輔助，不作因果證據。

### 6.11 P11 衝擊事件計數

6.11.1 從控制器警報紀錄計數急停、碰撞偵測、安全柵開啟等停止事件，記錄當時速度與扭矩，依停止方式（斷電煞車停、控制減速停）給不同權重計入損耗。

6.11.2 有加速規時，以「峰值 > k × 基準 RMS 且峰值因子超過門檻」偵測衝擊，對到行號與角度。

### 6.12 P12 加速曲線

6.12.1 同樣距離 D、時間 T、由靜止到靜止時，峰值加速度：梯形速度（加速、等速、減速各 1/3）為 4.5·D/T²；最小急動度（五次多項式）為 5.77·D/T²；S 曲線依急動度上限介於兩者之間或更高。曲線越平滑、衝擊越小，但同時間下峰值越高。

6.12.2 判斷邏輯：衝擊與共振（加速規看得到）以降低急動度處理；累積損耗（10/3 次方）以降低峰值處理；有餘裕的段落直接延長時間，兩者同時下降。

6.12.3 實施：逐行加速度倍率、過渡點軟啟動、外部軌跡串流執行 S 曲線或最小急動度軌跡、在扭矩與急動度上限內求最短時間的時間－急動度最佳化。

### 6.13 P13 視覺化

6.13.1 三層同步時間軸：上層各軸扭矩（典型波形與差異範圍）、中層振動、下層程式列（每格為一行指令文字），依損耗占比上色；點選一行顯示峰值占容許值比例、損耗占比、時間與等待。

6.13.2 角度集中圖以極座標或展開圖顯示每個角度區間的損耗密度、換向次數與 LWI，並可疊加拆檢記錄的損傷位置。

### 6.14 實作紀錄（DTM v2.1.0）

6.14.1 **加註版程式**：在主流程每個動作行之前插入一行把原行號寫進數值暫存器（範例 R[114]），動作、速度、點位不變。解析時判斷每個動作行的「有效定位方式」：FINE，或 CNT 但後面緊接 WAIT、CALL、END（控制器實際上會停下）視為 FINE。

6.14.2 **行號對位**：每個動作行是一個狀態，路徑樣板為該行在關節空間的 12 點路徑（有行號回報時由前幾個循環學出，沒有時以前後教導點的關節值連線為初值）；每個循環降到 20 Hz，以「只能停留或往後走」的 Viterbi 求最小成本序列（成本 = 到樣板距離² + 換行成本）。有行號回報時，候選狀態限制在「回報行往前回推、只跨過 CNT 行」到「回報行」之間（範例最多回推 2 行）；只有位置時以樣板的弦長分段加波束搜尋。換行點落在靜止時段（關節速度範數 < 1 度／秒）時移到真正開始動的時刻，靜止時段算前一行的停留。

6.14.3 **損耗與峰值**：p = 10/3（RV、齒條），3（齒輪箱）；起動停止容許扭矩與瞬間最大容許扭矩未設定時，RV 型以額定扭矩的 2.5 倍與 5 倍為預設（只是預設值；以常見 RV 型錄為例，多數型號起停約 2.5 倍，最小型號約 2 倍，應輸入所用型號的型錄值）；瞬間最大容許次數 C 範例 1000。停止事件依停止方式計入：急停每次 0.02/C、碰撞偵測 0.5/C、控制停止 0.005/C（範例權重）。

6.14.4 **角度集中**：區間寬 1°（直線軸 10 mm）；換向判定有遲滯（0.05°，直線軸 0.5 mm）；擺動幅度小於接觸寬度（範例 0.5°）記為小幅擺動。高使用區 U = 損耗密度由高到低累積到 50% 的區間；低使用區 R = 有通過、但密度最低的 30% 區間。

6.14.5 **全範圍掃描**：掃描程式以段落代碼（8000 + 軸 × 10 + 方向）標示正反轉，與診斷期資料一起串流或匯入時自動分出；LWI 以第一次掃描為基準，比值 ≥ 1.2 注意、≥ 1.4 警戒。

6.14.6 **等待餘裕與時間縮放**：等待外部訊號的行，S = 實際等待時間的第 10 百分位 − 計時等待 − 0.05 秒 − 安全裕度（範例 0.3 秒）；σ = (T + S)/T，上限 2。每一行以實測資料回歸 τ = 三次多項式(u) + c·α + v·ω（u 為行內進度、α 角加速度、ω 角速度；位置只跟 u 有關，重力項不受縮放影響），縮放後 T' = T − c·α·(1 − 1/σ²) − v·ω·(1 − 1/σ)，再以 6.5 的公式計算損耗。沒有餘裕的行，取捨表以每多 0.1 秒計算。

6.14.7 **改前改後與對照**：統計單位依資料量選每日、每小時或每 10 分鐘中位數（各組至少 3 個時段才用較長的單位）；效果 = 處理組（改後 ÷ 改前）÷ 對照組（改後 ÷ 改前）− 1，以對數差異中的差異計算；95% 信賴區間以拔靴法（各組內重抽時段，2000 次）；處理組改前改後另做 Mann–Whitney 檢定（n₁·n₂ ≤ 400 且無同值時用精確分布，否則常態近似含同值修正）。

6.14.8 **衝擊與加速曲線**：加速規以峰值 > 4 倍基準 RMS 且峰值因子 > 5 判定衝擊（範例），只看時間域；靜止到靜止的動作以 κ = a_peak·T²/D 判斷曲線形狀（梯形約 4.5、最小急動度約 5.77）。

6.14.9 **扭矩來源**：損耗計算以傳遞扭矩（扭矩指令或馬達電流換算）為準；只能取得干擾扭矩時，損耗排名標示為相對參考。

6.14.10 **建議與安全**：建議由規則產生、只輸出文字（修改前後對照與理由），不把修改寫回控制器（機器人端的背景邏輯、診斷程式、動作代碼與加註版程式由人員安裝，只寫入專用暫存器）；分析只使用實測資料，內建模擬器只用於產生內建演示程式的演示資料。

### 6.15 先前技術對照

6.15.1 **目的與限制**：本節把與 P 系列最接近的已知專利逐項對照，寫清楚 DTM 的做法與該專利請求項的技術差異，以及本專案刻意保留的設計邊界（實作面見 `docs/DESIGN_CONSTRAINTS.md`）。

- 這是技術比對，**不是法律意見**；作者與撰寫協助者都不是專利律師。是否落入某件專利的範圍，應由專利律師依各國公告的請求項全文判斷。
- 專利只在取得專利的國家或地區有效。
- 檢索日 2026-10-19，資料來源 Google Patents；有效與否、屆滿日以各國專利局紀錄為準。
- 一般原則（全要件原則）：要落入一項請求項的文義範圍，須具備該請求項的**全部**構成要件；均等範圍另由專利律師判斷。

6.15.2 **對照一：FANUC JP5289608B2（P 系列第一件重點對照）**

| 項目 | 內容 |
| --- | --- |
| 名稱 | ロボット減速機寿命推定シミュレーション装置（機器人減速機壽命估算模擬裝置） |
| 權利人 | ファナック株式会社；發明人 梁 衍学 |
| 日期 | 優先權 2011-12-13；申請 2012-07-06；公開 JP2013-144349A（2013-07-25）；特許公報 2013-09-11 |
| 狀態 | Google Patents 標示有效；若維持年費，預估 2032-07-06 屆滿 |
| 同族 | US8731886B2（預估 2032-11-16 屆滿）、CN103176410B、DE102012112019B4；檢索時未見台灣同族 |
| 與 DTM 的關係 | 同樣以「各軸轉速與負荷 → 減速機壽命 → 負荷過大的程式行 → 降低速度」為主軸，是 P5、P8、P9 最接近的已知技術 |

6.15.3 **請求項 1 的構成要件**（中文摘要，非逐字翻譯）：

1. 模擬執行動作程式，按取樣週期算出各軸減速機的轉速與負荷。
2. 依時間順序記憶轉速與負荷。
3. 由記憶的轉速與負荷算出各軸減速機壽命。
4. 設定機器人的稼動時間比率（或間接表示它的資料）。
5. 由算出的壽命與稼動時間比率推估減速機壽命。
6. 指定各軸減速機負荷的基準值（上限）。
7. 以評價函數判斷負荷是否超過基準值。
8. 固定工具前端點的軌跡，不受速度指令影響。
9. 在軌跡固定下，把動作程式的速度指令設為依規則決定的最高速度。
10. 負荷超過基準值時，在軌跡固定下把速度指令從最高速度降低預定量，變更動作程式，使負荷不超過基準值。

附屬項 2–5：設定目標壽命或目標節拍，反覆執行程式變更直到達成；未達成時重設基準值再反覆。

6.15.4 **逐項對照**：

| 要件 | DTM Pro 的做法 | 對照 |
| --- | --- | --- |
| 1 程式模擬算轉速與負荷 | 只分析診斷優化期間實際量到的關節角度與扭矩（6.14.10）。6.8 的時間縮放扭矩是由實測樣本回歸後換算（6.14.6），不是由程式模擬機器人動作。內建模擬器只產生內建演示程式的演示資料，載入使用者程式會拒絕執行 | 實機使用時不具備；演示模式有程式模擬，但只限內建演示程式 |
| 2 時間序列記憶 | 實測樣本依時間儲存 | 相似 |
| 3 由轉速與負荷算壽命 | 逐行損耗帳本以實測 ω、T 計算 p 次方損耗（6.5） | 相似，輸入為實測值 |
| 4 稼動時間比率 | 以實際量到的每日循環數換算每日消耗（6.5.3） | 功能相似，數值來自實測 |
| 5 推估壽命 | 預估壽命（6.5.6）；絕對額定值未知時只做相對排名 | 相似 |
| 6 負荷基準值 | 起動停止容許扭矩、瞬間最大容許扭矩（6.5.4、6.14.3） | 相似 |
| 7 判斷是否超過 | 每行峰值 ÷ 容許值與超過次數 | 相似 |
| 8 固定軌跡 | 不控制軌跡；6.8 的估計只是以「點位與路徑不變」為前提 | 部分相似（估計前提，不是固定軌跡的機能） |
| 9 速度設為最高 | 不把速度設到最高；只在量到的等待餘裕內建議放慢，以節拍不變為目標並以實測確認（6.8） | 不具備，方向相反 |
| 10 自動變更程式 | 只輸出文字建議（修改前後對照與理由），不寫回控制器、不自動降速、不反覆修改程式（6.9.3、6.14.10）；修改由人執行，再以 P10 實測驗證 | 不具備 |
| 附屬項 2–5 | 「節拍與壽命取捨表」（6.8.5）只列數字，不自動搜尋目標壽命或目標節拍 | 不具備 |

6.15.5 **技術比對結論**：DTM Pro 欠缺要件 9、10，實機使用時也欠缺要件 1，並且沒有附屬項 2–5 的自動反覆機能。依全要件原則，一般不落入請求項 1 的文義範圍；最終判斷仍以專利律師的意見為準。

6.15.6 **已由該專利公開、本揭露不主張為新的部分**（JP2013-144349A 已於 2013-07-25 公開）：

- 由各軸轉速與負荷的時間序列計算減速機壽命，並以稼動時間比率換算。
- 負荷超過基準值時找出對應的程式行並強調顯示（說明書第二實施形態；不在請求項內）。
- 以降低特定程式行的速度來降低減速機負荷。
- 壽命與等效扭矩的 10/3 次方關係本身是減速機公開選型資料（2.5.2）。

6.15.7 **本揭露與該專利的技術差異**（本文件公開的重點）：

- **資料來源**：用實測扭矩與角度，包含真實溫度、油脂狀態、急停衝擊與現場負載（2.3），不是程式模擬。
- **行號對位**：以實測位置對到教導路徑，處理 CNT 提前執行（6.3、6.14.2）。
- **局部磨耗**：角度集中圖（6.6）找出損耗集中的輸出角度範圍，局部摩擦比較 LWI（6.7）量測該範圍的摩擦變化；該專利請求項未涉及。
- **修改方向**：只在量到的等待餘裕內放慢，以節拍不變為目標並以實測確認（6.8）；不是先設最高速度再往下調。
- **驗證**：改前、改後與對照組的實測比較（6.10），不是以模擬結果判定。
- **執行**：修改由人執行，經干涉確認與低速試跑後才上線（6.9.3）。

6.15.8 **本專案保留的設計邊界**（修改或衍生版本請保持）：

- 不自動改寫或反覆修改動作程式，不把建議寫回控制器，不自動降速。
- 不加入「先把速度設到最高、再逐步調降」的流程。
- 不以動作程式模擬（動力學模型）算出的負荷取代實測；內建模擬器維持只跑內建演示程式；時間縮放估計維持以實測樣本為基礎。
- 不加入以目標壽命或目標節拍為條件、自動反覆搜尋速度或程式參數的機能。
- 要在日本、美國、中國或德國使用或散布加入上述機能的衍生版本，請先請專利律師評估。

6.15.9 **對照二：FANUC JP4112594B2（簡述）**

- 名稱：減速機異常診断方法及び減速機異常診断装置；權利人 ファナック株式会社；申請 2006-07-27。Google Patents 標示已屆滿（預估屆滿日 2026-07-27）。
- 請求項 1 摘要：以觀測器由扭矩指令與速度回授估計減速機的干擾值，做頻率分析，取中間軸轉動頻率整數倍的特定頻譜，振幅超過門檻判定異常。
- DTM：漣波指標是等速段內干擾扭矩的時間域標準差（5.2.3），沒有對干擾扭矩做頻率分析；振動的包絡、嚙合等特徵由外部感測器或閘道計算後當數值讀入（5.8）。

6.15.10 之後新增的對照依序編為對照三、對照四……，格式同 6.15.2–6.15.8。

---

## 7 變化例與適用範圍

7.1 **機器人**：任何品牌與型式，包括 FANUC、KUKA、ABB、安川、川崎、不二越、Denso、Epson、Stäubli、Universal Robots、埃斯頓、埃夫特、匯川等；垂直多關節、水平多關節、平行連桿、協作型、龍門型、第七軸行走軌道與變位機。

7.2 **傳動機構**：RV／擺線減速機、諧波減速機、行星齒輪、蝸輪蝸桿、皮帶、齒條齒輪、滾珠螺桿、線性滑軌與軸承。

7.3 **其他設備**：工具機旋轉工作台與刀塔、無人搬運車與移動機器人輪組減速機、風力機變槳與偏航軸承、鋼鐵與造紙產線的輥道與擺動機構、起重機、射出成型取出機。

7.4 **訊號來源**：干擾扭矩、馬達電流、扭矩指令、位置誤差、外部扭力感測器、加速規、麥克風（聲音）、聲射感測器、熱電偶與紅外線溫度、線上油液金屬粒子感測器、實驗室油液分析。

7.5 **運算位置**：控制器內、PLC、邊緣電腦、伺服器或雲端；任何程式語言。

7.6 **判斷方式**：規則、統計、機器學習（異常偵測、分類、迴歸）、語言模型，或其組合。

7.7 本文件所有方法可單獨或任意組合實施（見 0.2）。

---

## 8 範例參數

| 參數 | 範例值 |
| --- | --- |
| 常駐取樣 | 每秒 2–5 次 |
| 診斷段去頭尾 | 1.0 秒；等速至少 2.0 秒 |
| 診斷速度 | 5%、10%、20%；參考速度 10% |
| 基準筆數 | 診斷 10 次；生產循環 10 個有效日；振動 10 筆 |
| 扭矩門檻 | 診斷 1.5／2.0 倍；生產循環 1.15／1.30 倍 |
| z 分數 | 2（且比值 ≥ 1.15）提早注意；3 升警戒；σ 下限 = 5% 基準 |
| 遲滯 | 連續 2 次（生產循環連續 2 個有效日） |
| 溫度係數 | F_v 0.025／°C；F_c 0.004／°C |
| 振動門檻 | 比值 2／3 倍；峭度 4／6；軸向比 1.5 倍 |
| 鐵粉 | RV 型 0.05／0.10 wt%；W 比值 2／3 倍；最少 300 小時 |
| 取樣間隔 | 注意 60 天；警戒 21 天 |
| 溫度 | 參考 40 °C、每 +10 °C 老化加倍；突升 +8 °C 或 +5 °C／天；換脂後 72 小時排除 |
| 決策 | 處置 0.5、立即 0.9；單一家族 × 0.45；換脂分數自油脂壽命 70% 起 |
| 復原檢查 | 換脂後 2–4 週，1.3 倍以內 |
| 壽命 | Weibull 至少 3 筆失效；備品前置 60 天；條件失效機率 0.2 |
| 外推 | 上限 730 天；區間 ±(30% + 7 天) |
| 局部磨耗指數 | 1.2／1.4 倍 |
| 診斷優化期 | 3–7 天；扭矩 50–125 次／秒；加速規 5–25 kHz |
| 對照試驗 | 4–8 週 |

---

## 9 English Summary

This document is a defensive publication. The author does not seek patents and dedicates the following methods to the public as prior art under the Apache-2.0 license. Every feature may be used alone or in any combination; all values are examples.

**9.1 Friction-based reducer monitoring without added sensors.** A background program on the robot controller copies each joint's disturbance torque (FANUC `$MOR_GRP[1].$CUR_DIS_TRQ[i]`) to numeric registers together with a heartbeat and the speed override; a PC polls the controller's built-in web server at 2–5 Hz. A diagnostic program moves one joint at a time at several constant speeds in both directions, tagging each segment with a code (axis×1000 + speed%×10 + direction). After trimming acceleration phases, friction F = (τ₊ − τ₋)/2 and a direction-independent offset G = (τ₊ + τ₋)/2 are computed; regression over speeds gives Coulomb friction Fc and viscous coefficient Fv; the standard deviation within constant-speed segments is a "ripple" index of local damage.

**9.2 Robust baselines.** Ratios to a post-reset baseline (caution 1.5×, alert 2×) are primary; z-scores only escalate early; hysteresis requires two consecutive runs. Temperature compensation multiplies values by exp(k(T − Tref)) with k learned from ln(value) = a − kT + c·days. A cross-axis common-mode index, built from the median cumulative log-change of other axes' Fv and converted to an equivalent temperature, removes shared effects (ambient, warm-up, takt changes) without removing simultaneous Coulomb-friction (wear) increases.

**9.3 In-production monitoring.** Production programs write "motion codes" around fixed moves; per-cycle torque statistics are reduced to daily medians per (code, axis) and compared with a baseline of 10 valid days (1.15× / 1.30×).

**9.4 Evidence families and the two-evidence rule.** Severities (0–1.5, piecewise linear in the ratio) are grouped into independent families: torque-wear, torque-viscous, iron debris (concentration and generation rate W = Δwt%·M/Δh), vibration (envelope, mesh, kurtosis, axial/radial ratio), motor-frequency vibration, and temperature rise. Replacing a reducer or motor requires two families; a single family is capped at 0.45× ("observe" or, at the extreme, "prepare spares"). The only exception is age risk: a high Weibull conditional failure probability alone can recommend a preventive replacement. An explain-away rule attributes friction rise to the motor when debris and reducer vibration are normal but motor-frequency vibration rises. A cumulative ratio against the new-component baseline is not reset by grease changes. After the iron concentration passes the caution threshold, linear remaining-life extrapolation is suppressed and a knee-point notice is shown (a screening signal, not proof of accelerated wear), two consecutive accelerating increases of W escalate one level, and sampling intervals shorten automatically (60 / 21 days). Steady high temperature only shortens the grease interval (aging doubles per +10 °C above 40 °C); a sudden rise of housing-minus-ambient temperature (+8 °C vs. the 14-day median or +5 °C/day, ambient not rising, excluding 72 h after regreasing) raises a caution.

**9.5 Fleet life statistics.** Weibull fits with right-censored data (preventive replacements and running units) are computed centrally per component class and distributed to edge sites as priors; spare parts are allocated by remaining days versus lead time.

**9.6 Program-line wear accounting.** During a temporary diagnostic period (3–7 days) torque and joint position are sampled at 50–125 Hz and accelerometers at kHz rates, synchronized by digital-output markers. Each sample is mapped to the executing program line by matching measured position to the taught path under a monotonic program-order constraint (HMM/Viterbi), because register-based line reporting may run ahead under continuous (CNT) motion. Cycles are aligned per line on a normalized progress grid and averaged. Per line, an equivalent torque Tm = (Σ|ω|Δt|T|ᵖ / Σ|ω|Δt)^(1/p) and damage D = Σ(|ω|Δt/2π)(|T|/T₀)ᵖ (p = 10/3 for RV/cycloidal, 3 for harmonic drives) rank lines by share of life consumption; peak and emergency-stop events add per-event consumption.

**9.7 Angle-concentration map and local wear index.** Damage density, reversal counts and oscillation amplitudes are accumulated per output-angle bin; θ₈₀ (narrowest range holding 80% of damage) quantifies concentration, and oscillations smaller than the contact width flag fretting risk. A periodic slow full-range sweep computes F(θ); the local wear index LWI = median F(frequently used bins) / median F(rarely used bins) indicates localized wear in service (to be confirmed by teardown) (caution 1.2×, alert 1.4×). Output angles can be mapped to pin, tooth or raceway positions for comparison with teardown findings; the output angle locates the output main-bearing raceway and fretting spots directly, whereas pins and crank bearings see load all around within a narrow output range (6.6.6).

**9.8 Slack-based optimization aiming at unchanged cycle time.** Actual wait times after each motion block are measured; the q-th percentile minus a margin is the usable slack S. Time-scaling the block by σ = (T + S)/T divides acceleration by σ², reducing inertial torque (T' = Tg + Tinertia/σ² + Tf(v/σ)) while the robot still arrives before the awaited signal; a percentile-based slack lowers, but does not eliminate, the chance of a cycle-time increase, so the cycle time is confirmed by measurement. Rule-based and optionally LLM-written proposals (FINE→CNT where precision is not needed, lower ACC on high-damage lines with slack, soft start via intermediate points, wrist-flip avoidance, payload correction) are advisory only; humans verify by simulation and low-speed test.

**9.9 Verification.** Before/after diagnostic periods with an unmodified control robot, difference-in-differences on daily medians, and a one-page signed report with cost conversion provide evidence within weeks using leading indicators (peak torque, temperature rise, debris generation rate, friction trend, LWI) instead of waiting for failures.

**9.10 Data quality and feedback (v2.1).** Raw samples around each diagnostic run and ±10 min around each alarm are retained long-term while other raw data expire; a commissioning report after the first 7 days checks sampling rate and jitter, gaps, segmentation success and rejection reasons, per-axis noise (ripple, coefficient of variation of F) and motion-code coverage. Teardown findings (location, damage mode, grease condition, photos) are recorded; a "no fault found" teardown turns the replacement into a censored observation. A threshold replay recomputes past levels from stored features and daily medians under alternative thresholds and evidence weights and reports how many cautions and alerts would have been raised and when. Accelerating trends switch remaining-life estimates to the shortest of linear, recent-half-slope and quadratic projections. Per-robot planned downtime suppresses communication alarms; clock drift against the central server is estimated from the midpoint of request and response times. Without a central server, one edge host sends all notifications and computes site-level Weibull priors.

**9.11 Implementation notes (v2.1, section 6.14).** An instrumented copy of the program writes the original line number to a register before every motion line; a CNT move followed by WAIT, CALL or END is treated as a stop. Line alignment uses 12-point joint-space path templates per motion line and a forward-only Viterbi at 20 Hz, restricted to the reported line and the CNT lines just before it; position-only data uses chord templates from taught joint points with a beam search. Damage exponent p = 10/3 (RV, rack) or 3 (gearbox); start/stop and momentary peak limits default to 2.5× and 5× rated torque (defaults only; small RV models may allow about 2× for start/stop, so catalogue values should be entered); emergency stops, collisions and controlled stops add 0.02, 0.5 and 0.005 peak-event equivalents. Angle bins of 1° (10 mm for a travel axis) with 0.05° reversal hysteresis; the high-use set U holds 50% of damage, the reference set R is the lowest-density 30% of visited bins. Slack S = 10th percentile of measured waits − timer waits − 0.05 s − margin, σ capped at 2; per-line regression τ = cubic(u) + c·α + v·ω gives T' = T − cα(1 − 1/σ²) − vω(1 − 1/σ). Before/after effects use log difference-in-differences on daily, hourly or 10-minute medians, a 2000-sample bootstrap and a Mann–Whitney test. Accelerometer impacts use peak > 4 × baseline RMS and crest factor > 5 (time domain only). Suggestions are rule-based text; no program change is written back to the controller (robot-side templates installed by personnel write only dedicated registers); only measured data are analysed.

**9.12 Scope.** Any robot brand, any reducer or transmission type, other machines with gearboxes or oscillating bearings, any signal source (disturbance torque, motor current, accelerometers, acoustics, oil debris) and any computing location.

**9.13 Prior-art comparison (section 6.15; technical comparison, not a legal opinion).** FANUC JP5289608B2 (family US8731886B2, CN103176410B, DE102012112019B4; priority 2011-12-13; laid open as JP2013-144349A) claims a simulator that computes reducer speed and load by simulating the operation program, estimates life with an operating ratio, checks loads against a reference value, fixes the tool path, sets speed commands to a maximum and lowers them by a set amount, changing the program, until the load is within the reference; dependent claims iterate toward a target life or target cycle time. DTM Pro analyses measured data only (its built-in simulator runs only the built-in demo programs, and the time-scaling estimate is a regression on measured samples), never sets speeds to a maximum, suggests slowing down only within measured waiting slack, aiming at an unchanged cycle time confirmed by measurement, outputs text suggestions only, and never rewrites programs, writes program changes back to the controller or lowers speed automatically (robot-side templates installed by personnel write only dedicated registers for segment codes and line numbers), and verifies changes by before/after measurement with a control robot. Life calculation from speed and load, operating-ratio scaling, highlighting program lines whose load exceeds a reference, and lowering the speed of such lines were already published by that application and are not presented here as new. FANUC JP4112594B2 (frequency analysis of observer-estimated disturbance at multiples of the intermediate-shaft frequency; shown as expired by Google Patents) differs from DTM's time-domain ripple index.

**9.14 Variants added in version 1.5 (disclosed, not implemented in v2.1.0).** (a) Signal validity check before friction analysis: controller readings must match the teach-pendant screen, units/scale/sign must be confirmed (otherwise values are used only as relative indices), controller software and options are recorded, and a cold-versus-warm diagnostic run must show a clearly lower viscous coefficient Fv after warm-up; otherwise friction judgments are disabled (5.2.6). (b) Iron debris: a post-regrease sample sets the concentration starting point because regreasing pushes old grease out only partly (this part is implemented); without such a sample, no generation rate is computed or the start is estimated from a residual fraction (5.9.8). (c) Age risk capped at "prepare spares", or allowed to trigger replacement depending on consequence class (5.11.8). (d) Profile-likelihood intervals for the Weibull shape (drop of 1.92) propagated to η, B10 and conditional failure probability; informative censoring is bounded by refitting alarm-triggered no-fault removals as failures and as censored (5.12.5). (e) Validity flags and reason codes; numerical conclusions are withheld when conditions fail (5.16.5). (f) Low-confidence line alignment goes to an "unassigned" bucket; alignment accuracy is evaluated against DO/GO markers or manual labels (6.3.4). (g) With disturbance torque only, a friction-work hotspot E = Σ|Δθ|·|τ_dis| (exponent 1, Archard-type wear tendency) is reported separately from fatigue damage (6.5.8). (h) Transmitted torque converted with direction-dependent efficiency (driving versus back-driven) and balancer forces/friction removed using G(θ) (6.5.9). (i) Output angle mapped to internal phase with a calibrated initial phase; in an RV second stage one pin pitch of output rotation equals one crank revolution, so only the output main-bearing raceway and fretting spots are located directly by output angle (6.6.6). (j) Quality gates for the time-scaling regression (R², positive inertia coefficient, condition number, held-out cycles, bootstrap intervals), coupling terms of other axes, or direct trial runs at 1/σ speed replacing the regression (6.8.6). (k) Date-paired or block bootstrap, A/B/A/B trials for a single robot, time-split threshold replay and comparison with simple baselines (6.10.6).

---

## 10 公開紀錄

| 項目 | 內容 |
| --- | --- |
| GitHub Release | （發布後填入版本與日期） |
| Zenodo DOI | （發布後填入） |
| Software Heritage | （存檔後填入） |
| Release 產出物證明 | 由自動流程產生，見 Release 頁 |
| 修訂紀錄 | 1.0（2026-10-16）首次撰寫；1.1（2026-10-17）補充 5.7.3、5.10.6–5.10.7、5.15、5.16、9.10；1.2（2026-10-17）新增 0.5 範圍說明；1.3（2026-10-18）第 6 章改為已實作，新增 6.14 實作紀錄、2.5 來源說明、9.11（原 9.11 範圍改為 9.12）；刪除 0.5 範圍說明，潤滑失效型態的判斷規則同樣適用 0.2 條與第 7 章；1.4（2026-10-19）新增 6.15 先前技術對照與 9.13；1.5（2026-10-28）依外部審查修正 5.9.5、5.11.5、5.11.7、6.5.6、6.6.2、6.7.3、6.8.3、6.14.3、6.14.10、6.15、9.4、9.7、9.8、9.11、9.13 的描述，新增變化例 5.2.6、5.9.8、5.11.8、5.12.5、5.16.5、6.3.4、6.5.8、6.5.9、6.6.6、6.8.6、6.10.6 與 9.14，0.4 補充來源說明；新增 0.5 語言版本聲明（原 0.5 已於 1.3 刪除）與英文、日文全文，中、英、日三版同時公開 |
