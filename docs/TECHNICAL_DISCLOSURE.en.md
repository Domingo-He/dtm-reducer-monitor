# DTM Technical Disclosure (Defensive Publication)

**Technical Disclosure: Condition Monitoring, Program-Level Wear Diagnosis and Verified Program Optimization for Industrial Robot Reducers**

| Item | Content |
| --- | --- |
| Author | Domingo He |
| Document version | 1.5 (revised 2026-10-28: descriptions corrected after external review, variants added, full English and Japanese texts added; see the revision history in section 10) |
| First publication date | 2026-11-03: GitHub Release v2.1.0 published, Zenodo DOI 10.5281/zenodo.23910956 (see section 10; the Software Heritage archive date also applies) |
| Corresponding software | DTM v2.1.0 and later; P1–P13 of chapter 6 are implemented (`dtm/pro/`), the optional AI scheme of 6.9.2 is not; items added in version 1.5 and marked "not implemented in v2.1.0" are disclosures only; 6.15 is a technical comparison with known patents |
| License | Published with this project under Apache-2.0 |
| Languages | Traditional Chinese (`TECHNICAL_DISCLOSURE.md`), English (this document) and Japanese (`TECHNICAL_DISCLOSURE.ja.md`) are published simultaneously with identical section numbers (see 0.5); chapter 9 is the English summary |

---

## 0 Statement of Disclosure

0.1 The purpose of this document is to **disclose** the following technical content so that it becomes prior art. The author will not apply for patents on any content of this document and asserts no patent rights; anyone may freely use, modify and practise it under the Apache-2.0 license.

0.2 Every feature, step, value and variant described in this document may be **practised alone** or in **any combination** with any other feature of this document. Values, thresholds, sampling rates and parameters in the text are **examples** and are not limited to those values; numerical ranges, alternative sensing methods and alternative computation methods that a person skilled in the art could readily adjust on the basis of this document are all regarded as disclosed.

0.3 "Robot" is not limited to a particular brand or model, and "reducer" is not limited to a particular type (see chapter 7). The FANUC R-2000iC/165F and RV-type reducers are used as the main embodiment for convenience of explanation only.

0.4 The concepts, decision rules and field knowledge of this document were provided by the author; the text and code were written with the assistance of AI (Anthropic Claude) and confirmed by the author. Some of the variants added in version 1.5 come from external review comments (including reviews by other AI models) and AI suggestions, and are disclosed after confirmation by the author.

0.5 **Language versions**: the Chinese, English and Japanese versions are published simultaneously; where their contents differ, whatever is disclosed in each version is disclosed. The three versions have identical chapter and item numbers (`docs/TECHNICAL_DISCLOSURE.md`, `docs/TECHNICAL_DISCLOSURE.en.md`, `docs/TECHNICAL_DISCLOSURE.ja.md`), and no version narrows the scope of disclosure of another.

---

## 1 Technical Field

1.1 This disclosure relates to methods and systems for condition monitoring, failure prediction and life estimation of industrial robots and other equipment with reduction mechanisms, and for identifying, from measured data, the "program lines that cause wear" and verifying the effect of program modifications.

---

## 2 Background and Problems to Be Solved

### 2.1 Typical reducer failures

2.1.1 Common failures of robot joint reducers (for example RV-type cycloidal reducers, harmonic drives and planetary gears) include grease degradation or loss, pitting and spalling of tooth flanks, bearing wear, rapid deterioration after local damage, broken teeth, and abnormalities of the motor-side bearing, brake or encoder.

2.1.2 Reducer wear is usually not linear: it is slow at first, deteriorates suddenly after a certain knee point and finally leads to a stop. A rise in the iron-powder concentration of the grease is often an important sign, but after the knee point, linear extrapolation of the remaining life is unreliable.

### 2.2 Limitations of existing approaches

2.2.1 Monitoring services of robot manufacturers mostly support only their own robots, and their decision methods are not disclosed.

2.2.2 Manufacturers' offline simulation software can estimate reducer wear by program simulation (for example, FANUC JP5289608B2 discloses simulating the operation program to calculate the speed and load of each axis reducer, estimating life, and automatically lowering the speed command when the load exceeds a reference value; see 6.15 for a comparison), but these are statistical models that usually do not take into account actual temperature, contamination, lubrication condition, real emergency-stop impacts or the actual load on site.

2.2.3 Add-on sensor solutions require hardware and installation, and they mostly decide on a single signal (vibration or sound), with a high false-alarm rate.

### 2.3 Why reducers fail repeatedly on site although the simulation judged them acceptable

2.3.1 **Only single events are checked against the allowable value.** A typical heavy-load handling program may start with 0 to 100% acceleration and stop abruptly, 5–10 times per minute, 7,200–14,400 times per day, and 216,000–432,000 times per month. Each event is within the allowable range, yet the cumulative damage is still large.

2.3.2 **Life formulas assume that the load is distributed evenly over the full revolution.** When the same motion is repeated, the load concentrates on the same section of tooth flanks and the same section of the bearing raceway; during small-angle oscillation the grease is not easily carried back to the contact surface (fretting, false brinelling), so local wear occurs even when the average torque is not high.

2.3.3 **Nominal loads and ideal models are used.** High temperature, grease degradation and real emergency-stop impacts are not included in the calculation.

2.3.4 **Continuous operation.** With no stop for weeks to over a month, there is no cooling time, the temperature stays high and grease degradation accelerates.

2.3.5 **The cost of a shorter cycle time is non-linear.** Life is inversely proportional to the 10/3 power of the equivalent torque (RV type); a 10% increase in equivalent torque reduces life by about 27%.

### 2.4 Objectives of this disclosure

2.4.1 To monitor reducer health over the long term with data the controller already has (adding temperature, vibration and iron powder when needed), and to distinguish among three kinds of problems: lubrication, reducer and motor.

2.4.2 To raise the sampling rate during a short "diagnostic optimization period", map torque and vibration to every program line and every joint angle, and find which line and which angle range consume life.

2.4.3 To propose program modifications that keep the cycle time unchanged or minimize its cost, and to prove the effect by before/after measurement with a control group.

### 2.5 Sources

2.5.1 The descriptions of field conditions in 2.1.2 and 2.3 — the acceleration pattern and events per minute of heavy-load programs, load concentrating on the same section of tooth flanks and raceways, simulations using nominal loads, weeks of continuous operation without stopping, and rapid deterioration with rising iron powder after the knee point — come from the author's field experience of about ten years of FANUC robot maintenance. They are **empirical values**, not statistics cited from the literature, and differ between plants, programs and models.

2.5.2 The 10/3-power relationship between life and equivalent torque is taken from published selection data for RV-type reducers; "about 27% less" in 2.3.5 is calculated from this relationship: 1.1^(10/3) ≈ 1.374, 1 ÷ 1.374 ≈ 0.73.

---

## 3 Terms and Symbols

| Term | Definition |
| --- | --- |
| Disturbance torque τ | The controller's estimate of "actual motor torque minus dynamic-model torque"; for FANUC `$MOR_GRP[g].$CUR_DIS_TRQ[i]`. For other brands, estimated external force, the difference between torque command and model, or a value converted from motor current can be used instead |
| Transmitted torque T | The total torque actually transmitted by the reducer, converted from motor current × torque constant × reduction ratio × efficiency, or from the controller's torque command |
| Diagnostic program | A periodically executed single-axis constant-speed back-and-forth motion used to measure friction |
| Segment code | An integer written to a register by the robot program to indicate which motion segment is currently running |
| Motion code | A segment code for a fixed motion in the production program (example 9000–9899) |
| F, Fc, Fv, G | Friction, constant (Coulomb) friction, viscous friction coefficient, gravity-type offset (defined in 5.2) |
| Ripple | Standard deviation of the disturbance torque within a constant-speed segment, reflecting local unevenness of tooth flanks and bearings |
| Baseline | Mean and standard deviation of the first N valid data after each reset (grease change, component replacement) |
| Severity s | A dimensionless value from 0 to 1.5; 0.5 = caution, 1.0 = alert (see 5.11) |
| Family | Mutually independent sources of evidence: torque, iron powder, vibration, temperature |
| W | Iron-powder generation rate (mg/h) |
| ΔT | Temperature rise = reducer housing temperature − ambient temperature |
| η, β | Weibull scale and shape parameters |
| p | Life exponent; 10/3 for RV/cycloidal types and roller bearings, 3 for ball bearings and harmonic drives |

---

## 4 System Architecture

### 4.1 Data acquisition (an embodiment requiring no controller options)

4.1.1 A resident program runs in the controller's Background Logic and, every scan cycle, copies each axis's disturbance torque to numeric registers and writes a heartbeat counter and the speed override:

```
R[101]=$MOR_GRP[1].$CUR_DIS_TRQ[1]   … R[106] (J1–J6)
R[107]=disturbance torque of axis 7 (travel axis E1) (optional)
R[112]=(R[112]+1) MOD 10000          heartbeat
R[113]=$MCR.$GENOVERRIDE              current speed override
```

4.1.2 The monitoring computer reads the numeric register list from the controller's built-in web server over HTTP (for example `/MD/NUMREG.VA`), for example 2–5 times per second, without purchasing communication options. Other embodiments: Modbus TCP (via a PLC or gateway), OPC UA, EtherNet/IP, a controller-side program sending actively over a socket, an external data acquisition card.

4.1.3 Register numbers are configurable to avoid conflicts with existing uses on site.

4.1.4 Segment code encoding (example):

- Diagnostic segment: `axis number × 1000 + speed% × 10 + direction`, direction 1 = A→B, 2 = B→A. Example: 2051 = J2, 5%, forward.
- Production motion codes: 9000–9899.
- Vibration sampling window: 99AA, AA = axis number.
- 0 = not in any segment.

4.1.5 Optional sensors: housing temperature, ambient temperature, radial and axial vibration (the sensor computes RMS, envelope, mesh-frequency energy, motor-rotation-frequency energy and kurtosis internally, and the monitoring side reads only the feature values), and iron-powder concentration (manual sampling sent to a laboratory, or an online sensor).

4.1.6 The system determines the sensor update interval automatically: if updates are too slow (for example battery-powered wireless sensors updating every 1–5 minutes), only readings falling within constant-speed segments or sampling windows are used, and only for daily trends.

### 4.2 Deployment types

4.2.1 **Single-machine type**: one computer monitors one or several robots and includes a web dashboard and notifications.

4.2.2 **Edge + central type**: one edge host per plant starts one monitoring process for each robot; the central server aggregates data from several plants, sends notifications, computes fleet life statistics and distributes rules to the edges.

4.2.3 **Small mode**: with 10 robots or fewer, no central server is set up, and the edge host also handles notifications and fleet statistics.

4.2.4 Data synchronization: the edge uploads increments per table using cursors (gzip compression, Bearer token); the central server's response carries pending commands (event registration, revocation, iron powder, vibration, operating hours, alarm acknowledgement) and the latest rules; the edge executes them and reports the results. While the edge is offline it sends notifications itself (configurable: alerts only or all), and the central server re-sends only alarms that are still within their validity period.

### 4.3 Storage

4.3.1 A local database stores the features of every diagnostic run, the features of every production cycle, daily medians, events (grease change, component replacement, program modification), iron powder, vibration, components, operating hours, alarms, notification records and operation logs. Raw high-frequency data are kept only for diagnostic segments and 10 minutes before and after alarms (example retention 180 days).

---

## 5 Monitoring Methods (M series, DTM monitoring edition)

### 5.1 M1 Diagnostic program and segmentation

5.1.1 The diagnostic program runs after warm-up under the same load: each axis under test moves from angle A to angle B (only the axis under test differs) once forward and once backward at several fixed speeds (example 5%, 10%, 20%); a segment code is written before each segment starts and reset to 0 after it ends, with a 0.5–1 s pause between segments. The speed override must be 100%; otherwise the segment is discarded.

5.1.2 The acceleration and deceleration parts at the head and tail of each segment (`trim_s`, example 1.0 s) are removed; the remaining constant-speed part is used only if it lasts at least `min_const_s` (example 2.0 s) and contains at least 3 valid samples; samples beyond the invalid-value limit are rejected.

5.1.3 For each segment, the mean, standard deviation, absolute peak and mean temperature are computed. The time window of the constant-speed segment is also passed to the vibration sensor for slicing, so that vibration features and torque features belong to the same motion.

5.1.4 How "one diagnostic run" is identified: a dedicated flag register R[111] = 1; or, without a flag, by the interval between occurrences of segment codes (an interval longer than `run_gap_s` ends the run; a run longer than `max_run_s` is ended forcibly).

### 5.2 M2 Friction decomposition

5.2.1 For the forward and backward segments at the same speed:

$$F = s_{dir}\cdot\frac{\tau_{+}-\tau_{-}}{2}, \qquad G = \frac{\tau_{+}+\tau_{-}}{2}$$

F is friction (changes sign with direction), and G is the offset that does not change sign with direction (gravity, balancer, payload-setting error); s_dir is ±1 set according to the mounting direction of the axis.

5.2.2 Linear regression over several speeds:

$$F(v) = F_c + F_v\cdot v$$

v may be the speed percentage or the actual joint angular velocity. F_c is the constant friction and F_v is the viscous friction coefficient. F at the reference speed (example 10%) is the representative value; if that speed is missing, it is computed from the regression.

5.2.3 Ripple = mean of the standard deviations of the constant-speed segments in both directions at each speed; peak = maximum of the absolute peaks of all segments.

5.2.4 Variants: using non-linear friction models such as the Stribeck model; using more speed points; using motor current or the torque command instead of the disturbance torque; modelling separately at different temperatures.

5.2.5 Failure-mode hints (example rules): ripple ≥ 1.4× and F ≥ 1.2× → reducer wear or local damage; ripple ≥ 1.8× → suspected spalling or pitting; mainly F_v rising (≥ 1.15× and > 1.1 × the F_c ratio) → grease degradation, overfilling or low temperature; mainly F_c rising → reducer wear, brake drag or abnormal preload; G offset → balancer or payload setting, not friction.

5.2.6 **Signal check** (not implemented in v2.1.0): friction decomposition assumes that the disturbance torque read from the controller is dominated by friction; since each manufacturer's estimation method, units and filtering are not disclosed, the following is confirmed at introduction: (a) the reading matches the value on the teach-pendant screen (same moment, same axis); (b) the units, scale and sign are confirmed, and until they are, the value is used only as a relative value, not converted to N·m and not used to estimate life; (c) the controller software version and options are recorded, and controllers with different versions or options are confirmed separately; (d) **cold/warm response check**: the diagnostic program is run once when cold and once after warm-up; F_v after warm-up should be clearly lower, and if the change is in the opposite direction or absent, the signal is judged not to be dominated by friction, friction judgments are disabled and only the raw trend is kept; (e) variants: repeat runs, swap the order of forward and backward motion, and change the load, checking whether the repeatability and the direction of response of F and G match expectations.

### 5.3 M3 Baseline, ratio, z-score and hysteresis

5.3.1 The first N (example 10) valid diagnostic runs after each reset establish the baseline (mean μ, standard deviation σ).

5.3.2 The ratio r = value / μ is the primary criterion: r ≥ 1.5 caution, r ≥ 2.0 alert (example).

5.3.3 The z-score is used only for early escalation: when the baseline has enough samples, z = (value − μ) / max(σ, 0.05·|μ|); z ≥ 2 and r ≥ 1.15 may escalate early to "caution"; when the caution ratio has been reached and z ≥ 3, the level escalates to "alert".

5.3.4 Hysteresis: the level is raised only after 2 consecutive runs (example) reach it, and lowered only after 2 consecutive runs fall below it.

5.3.5 The G offset is judged by "|G − G_base| / F_base"; G is not judged for a travel axis (horizontal linear).

5.3.6 Reset events such as grease changes and component replacements divide the data into periods, each with its own baseline; in addition, a cumulative ratio against the "relative-to-new baseline" is kept (5.11.6).

### 5.4 M4 Temperature compensation

5.4.1 When a temperature signal is available:

$$x_{comp} = x\cdot e^{\,k\,(T - T_{ref})}$$

T_ref is the mean temperature of the baseline period. Default coefficient k: about 0.025 per °C for F_v and about 0.004 for F_c; F is weighted by the proportions of the two.

5.4.2 The coefficient can be estimated from the user's data: a two-variable regression ln(x) = a − k·T + c·days (the days term absorbs slow degradation), returning k, its standard error, the sample size, the temperature range and whether it can be adopted.

5.4.3 When only one axis is monitored and the temperature difference within the baseline period is ≥ 5 °C, k learned from the baseline-period data itself is used instead.

### 5.5 M5 Cross-axis common-mode removal

5.5.1 Changes in air temperature, warm-up and production takt make the friction of all axes change together; degradation of a single axis changes only that axis.

5.5.2 For each axis, a common-change index is built by taking, at each time point, the median of the "cumulative log change of F_v" of the other axes; steps across grease changes and component replacements are not counted. The common F_v change is converted into an equivalent temperature difference, which is then converted into multipliers for F and F_c according to each indicator's temperature coefficient and removed. In this way a simultaneous rise in constant friction (wear) on two axes is not removed as a common change.

5.5.3 Settings: always used, used only without a temperature signal, not used. Cost: if all axes degrade simultaneously in the same proportion, this is removed; grease life and iron powder guard against this case.

### 5.6 M6 Production-cycle monitoring (motion codes)

5.6.1 The production program writes motion codes before and after fixed motions. For each cycle and each axis, the mean, RMS, peak and standard deviation of the disturbance torque, the motion duration, the override and the temperature are recorded, together with radial and axial vibration in the same time window.

5.6.2 Only cycles with 100% override are used; a day counts as a valid day only with at least 20 valid cycles (example).

5.6.3 The judgement uses "daily medians": for each (motion code, axis), the first 10 valid days after a reset form the baseline; ratio ≥ 1.15 caution, ≥ 1.30 alert, and the level changes only after 2 consecutive valid days (example). Production torque includes inertia and gravity, so the thresholds are lower than for the diagnostic program.

5.6.4 When the points or speed of that segment are changed, the baseline of that code is reset; program modifications can be detected automatically (by comparing the modification time or hash of the program file) with a reset prompt.

### 5.7 M7 Trend extrapolation and uncertainty

5.7.1 A linear regression of the temperature-compensated F of the last 30 runs (example) against days is extrapolated to the number of days until the caution and alert thresholds; already exceeded = 0.

5.7.2 The extrapolation limit is 730 days, beyond which nothing is shown; the displayed interval is ±(30% × days + 7 days) (empirical).

5.7.3 When acceleration is detected, a conservative estimate (the shorter) is used. Embodiment: the window is split into first and second halves regressed separately, and the second-half slope is positive and ≥ 1.5 times the first-half slope; or the curvature of a quadratic regression is positive, the slope of the curve at the latest point is ≥ 1.5 times the slope of the whole-window line, and the residual sum of squares of the quadratic is ≤ 0.8 times that of the line (to avoid mistaking noise for acceleration). The conservative estimate takes the shortest of "whole-window line", "line with the second-half slope" and "intersection of the quadratic with the threshold".

5.7.4 The remaining days are the minimum of the extrapolations of torque, production cycles, iron powder and vibration envelope; friction rises judged to be caused by the motor are not counted.

### 5.8 M8 Vibration features

5.8.1 Radial and axial directions each establish a baseline from the first 10 records after a reset; ratios of RMS, envelope, mesh and motor rotation frequency ≥ 2 caution, ≥ 3 alert; kurtosis is judged by absolute value (normal about 3, ≥ 4 caution, ≥ 6 alert) (example).

5.8.2 A change of the axial/radial RMS ratio of ≥ 1.5 times relative to the baseline is regarded as a sign of main-bearing problems or axial play.

5.8.3 The envelope value of the last 30 records is regressed against days and extrapolated to the number of days until the alert.

### 5.9 M9 Iron-powder concentration

5.9.1 Sample types: initial after regreasing, mid-operation, grease discharged at a change. The default for discharged samples is the "middle portion": the old grease discharged first is discarded and the middle portion is sent for analysis, to reduce bias from sediment and residue in the lines.

5.9.2 Generation rate (each grease batch is one period):

$$W = \frac{\Delta c}{100}\cdot\frac{M}{\Delta h}\cdot 1000 \quad[\text{mg/h}]$$

Δc is the concentration difference between two samples (wt%), M is the grease quantity of that axis (g), and Δh is the operating hours between the two samples; it is not computed until 300 operating hours (example) after regreasing. The first W after a component replacement is the new-component baseline.

5.9.3 Severity: concentration is judged by absolute thresholds (starting example for RV types 0.05 wt% caution, 0.10 wt% alert; to be corrected by teardown findings); W is judged by its ratio to the new-component baseline (2× caution, 3× alert); the larger of the two is taken.

5.9.4 The current W is extrapolated to when the alert concentration will be reached and to the expected concentration at the next grease change.

5.9.5 **Knee-point rule**: after the concentration reaches caution, the remaining days are no longer extrapolated linearly; a knee-point notice is shown instead (the screen text of the embodiment is "past the knee point, may deteriorate at any time") and ordering spares directly is recommended; two consecutive accelerating increases of W (W₃ − W₂ > W₂ − W₁ > 0) raise the level by one. Reaching the caution concentration is a screening signal for wear risk: the rise may come from long-term normal wear, sediment, sampling bias or genuinely accelerated wear, and does not establish that the accelerated phase has begun; sampling should be made more frequent and cross-checked with other evidence such as torque and vibration.

5.9.6 Sampling intervals shorten automatically: 60 days at caution, 21 days at alert (example), and are scheduled into maintenance automatically; when the concentration reaches alert, the grease-change level is forced to "immediate".

5.9.7 Variants: online oil metal-particle sensors, ferrography (proportion of large particles), magnetic plug inspection.

5.9.8 **Concentration starting point after regreasing**: regreasing mostly pushes the old grease out with new grease and is not a complete replacement, so the residual old grease makes the concentration after regreasing non-zero. An initial sample taken after regreasing serves as the concentration starting point of that grease batch (adopted in the embodiment). Without an initial sample (not implemented in v2.1.0; the embodiment uses 0 as the starting point), the first sample of the batch only shows the concentration and no generation rate is computed, or the starting point is estimated as a set residual fraction × the concentration before regreasing and marked as an estimate.

### 5.10 M10 Temperature

5.10.1 **Effective grease aging**: when the housing temperature is above the reference (example 40 °C), the aging rate doubles for every 10 °C higher (acceleration only; no extension at low temperature). The grease-change interval is whichever comes first, hours or years.

5.10.2 **Steady high temperature**: only shortens the grease-change interval, showing the correction factor and the adjusted interval; not treated as wear.

5.10.3 **Sudden temperature rise**: ΔT (housing − ambient) for the same motion code is compared with its own 14-day median; 8 °C or more above it, or a rise of 5 °C or more in one day, with the ambient temperature not rising at the same time → "sudden temperature rise" caution (starting values configurable).

5.10.4 No sudden rise is judged within 72 hours after regreasing; if the rise persists, a reminder to check internal pressure and oil seals is given.

5.10.5 Temperature rise belongs to the friction family with weight 0.6 and alone reaches at most "observe"; an absolute overheating threshold can also be set (disabled by default).

5.10.6 **Substitute reference temperature**: without an ambient temperature sensor, the "median housing temperature of the other axes" on the same day under the same motion code is used as the reference (at least 2 other axes), so common changes in air temperature, warm-up and plant air conditioning are removed together; if even this is not available, the temperature rise is only displayed, raises no alarm and is not used in decisions. Converting the rise to severity: 0 at or below 0.5 × threshold, 0.5 at the threshold, 1.0 at 2 × threshold (upper limit 1.5).

5.10.7 **No return to normal after regreasing**: between 72 hours and 14 days after regreasing, if ΔT is still 5 °C or more above the median of the 14 days before regreasing (example), a prompt asks to confirm that residual pressure at the outlet has been released, whether too much grease was filled and whether the oil seals leak.

### 5.11 M11 Evidence families and decisions

5.11.1 Severity function (ratio type and absolute type):

$$s(r)=\begin{cases}0 & r\le 1\\ 0.5\,\dfrac{r-1}{c-1} & 1<r\le c\\ \min\!\left(1.5,\;0.5+0.5\,\dfrac{r-c}{a-c}\right) & r>c\end{cases}$$

c and a are the caution and alert thresholds; for the absolute type, the lower bound floor replaces 1.

5.11.2 Family scores:

- Torque wear type = max(Fc, ripple, 0.8·F, cumulative Fc relative to new, cumulative ripple relative to new, 0.8·production cycles).
- Torque viscous type = F_v.
- Vibration reducer type = max(radial envelope, mesh, kurtosis, 0.8·axial RMS, 0.8·axial ratio, production-cycle vibration).
- Vibration motor type = motor rotation frequency (larger of radial and axial).
- Iron powder = max(concentration, generation rate).
- Temperature = sudden ΔT rise (weight 0.6).

Each coefficient (example Fc/ripple 1.0, F/production cycles 0.8, temperature rise 0.6, envelope/mesh/kurtosis 1.0, axial RMS/axial ratio 0.8, iron powder 1.0) is adjustable in the configuration file.

5.11.3 **Two-evidence rule**: for the score for replacing a reducer or motor, the family severities are sorted in descending order; when only one family is ≥ 0.25, score = min(maximum, 1.5) × 0.45 (a single family reaches at most "observe", or "prepare" in extreme cases); with two or more, the mean of the top two is taken.

5.11.4 **Explain-away rule**: friction rises, but iron powder and reducer vibration are both < 0.5 while motor-rotation-frequency vibration rises → the torque wear score is multiplied by (1 − 0.7·min(1, motor vibration severity)) and the diagnosis is attributed to the motor (brake drag, motor bearing).

5.11.5 Three scores:

- Grease score = max(torque viscous type, grease-life severity, 0.8·min(iron powder, 1)); scoring starts when 70% of grease life is used (example).
- Reducer score = two-evidence rule (torque wear, iron powder, vibration reducer), taking the larger with age risk.
- Motor score = two-evidence rule (motor vibration, friction rise without wear evidence), taking the larger with age risk.

Score ≥ 0.5 recommends action, ≥ 0.9 immediate action (example). Corresponding actions in the embodiment: for the grease score, "regreasing recommended" / "regrease immediately"; for the reducer score, "prepare spares, shorten the monitoring interval" / "schedule replacement"; for the motor score, "inspect the motor and prepare spares" / "schedule replacement or repair". When grease life ≥ 100% or iron powder ≥ alert, the grease change is forced to immediate. When the reducer reaches immediate, the grease change is handled together with the replacement.

5.11.6 **Recovery check after regreasing**: after regreasing and 2–4 weeks of operation, F_c and W are compared with the new-component baseline; back within 1.3× → the lubrication problem is solved; still high → the reducer is degrading, prepare a replacement. The cumulative ratio uses the first diagnostic runs after component replacement as its baseline and is not reset by regreasing, so that continuing wear is not "washed out".

5.11.7 **Age risk**: when the conditional failure probability within the spare-part lead time (example 60 days) is ≥ 0.2, spares are prepared (example); when the remaining days are shorter than the lead time, an immediate order is prompted. In the embodiment, the age-risk score = min(1, 0.5 × conditional failure probability ÷ 0.2); at a conditional failure probability of about 0.36 or more, the reducer or motor score reaches 0.9 (schedule replacement); this is a preventive replacement based on life statistics and is not limited by the two-evidence rule.

5.11.8 **Variant of the age-risk cap** (not implemented in v2.1.0): age risk alone reaches at most "prepare spares", and scheduling a replacement still requires two kinds of evidence; or whether age risk alone may trigger a scheduled replacement is decided by consequence class (critical axis, length of the spare-part lead time, downtime cost). Both the embodiment of 5.11.7 and the variant of this item belong to this disclosure.

### 5.12 M12 Life model

5.12.1 Operating hours: interpolated or extrapolated from hour-meter readings when available, otherwise estimated from daily operating hours.

5.12.2 Weibull: R(t) = exp(−(t/η)^β). Failure replacements are complete lives; preventive replacements and units still in service are right-censored data and must be included in the fit. Maximum likelihood estimation uses a grid search over β, and η has a closed form.

5.12.3 B10 = η·(−ln 0.9)^(1/β); the conditional probability of failure within the next Δ hours after t hours of use = 1 − R(t+Δ)/R(t).

5.12.4 With fewer than 3 failure records (example), a configured prior is used and its source is shown. The central server fits a "fleet prior" from the records of the same component class at all plants and distributes it to the edges in a rule file; the edges apply it automatically according to its version and mark it "from central".

5.12.5 **Uncertainty and informative censoring** (not implemented in v2.1.0): a 95% interval for β is obtained from the profile log-likelihood (ℓ_p(β̂) − ℓ_p(β) ≤ 1.92, i.e. half of the chi-square value 3.84 with 1 degree of freedom; ℓ_p is already computed during the grid search), from which ranges for η, B10 and the conditional failure probability are given, and spare-part needs are listed as scenarios for the lower and upper bounds of the interval. Components replaced early because of an alarm but found undamaged at teardown may cause informative censoring (removed units are closer to failure, so η is biased optimistically): the model is fitted both "treated as failures" and "treated as censored", and the difference between the results is listed as a sensitivity.

### 5.13 M13 Fleet level

5.13.1 Effective level = max(health, production cycles, decision); remaining days = min(all sources), upper limit 730 days.

5.13.2 Spare-part demand: existing stock is allocated in order of each axis's remaining days and lead time, and shortfalls are listed; the risk view defaults to 180 days.

5.13.3 Edge offline, data gaps and heartbeat stops generate system alarms; a single robot can be set to "planned downtime" (with an end time), during which no data-gap or connection alarms are sent.

5.13.4 Clock deviation: compared with central time at synchronization; a warning above 60 seconds (example).

5.13.5 Threshold replay: stored features and daily medians are recomputed with another set of thresholds and weights to show how many times and when alarms would have been raised in the past.

5.13.6 Commissioning report: generated automatically after 7 days of operation, listing sampling rate and jitter, data gaps, heartbeat, segmentation success rate and rejection reasons of diagnostic segments, per-axis noise, motion-code coverage and sensor update intervals.

### 5.14 M14 Maintenance work packs and notifications

5.14.1 Each decision comes with a work pack: recommended action, parts, tools, steps and iron-powder sampling method; a work order can be generated.

5.14.2 Graded notifications and escalation (escalating to the next level of contacts when not acknowledged); SMS via a 4G modem or a generic HTTP gateway, with fallback, daily limits and an on-duty roster; the daily summary also serves as a system heartbeat.

---

### 5.15 M15 Data retention, teardown feedback and threshold replay

5.15.1 **Event-oriented retention of raw data**: ordinary raw samples are kept only short-term (example 7 days); the raw waveforms of each diagnostic segment and the raw data for a period before and after each alarm (example ±10 minutes) are marked by time ranges for long-term retention (example 180 days), and marked ranges are skipped when raw data are purged; daily backups also carry only these ranges.

5.15.2 **Teardown feedback**: when a reducer, gearbox or motor is replaced, the damage location of the removed parts (input gear, crankshaft bearing, cycloidal gear, pins, main bearing, oil seal, gearbox gear or bearing, motor bearing, brake), the damage mode (pitting, spalling, wear, broken tooth), the grease condition and photographs are recorded; if "no fault found", that replacement becomes censored (preventive replacement) data in the life statistics, and a record that "the judgement may have been premature" is kept for threshold correction.

5.15.3 **Threshold replay**: using stored diagnostic features and daily motion medians, the level of each run (each day) is recomputed with another set of thresholds and evidence weights under the same hysteresis rule, listing how many cautions/alerts the original and new settings would each have raised and when the first would have occurred; baselines are reset at the actual times of grease changes and replacements. The maintenance decision can also be recomputed each day with the data available up to that day, to compare when grease changes and replacements would have been recommended.

5.15.4 **Analysis data export and anonymization**: features, daily medians, events, alarms, iron powder, vibration, components and teardowns, hours, calibrations, system events, sanitized settings and retained raw segments are exported by robot and period; passwords, keys, phone numbers, e-mail, notification URLs, network addresses and full file paths are masked; plant names, robots, lines and operators can optionally be replaced consistently with codes.

### 5.16 M16 Operational quality

5.16.1 **Commissioning report**: generated automatically when 7 days have first accumulated after introduction (and on demand at any time), checking the sampling rate and sampling-interval percentiles (jitter), the number and duration of data gaps, heartbeat and connection alarms, the segmentation success rate and rejection reasons of diagnostic segments, per-axis noise (median ripple, coefficient of variation of F), motion-code coverage (valid cycles per code per day and exclusion reasons) and sensor update intervals, each marked as passed or pending.

5.16.2 **Planned downtime**: a downtime end time is set for a single robot; during it, data-gap, heartbeat-stop, connection-loss and invalid-value alarms and the watchdog's data-gap judgement are suppressed, and existing alarms of the same kinds are cleared; other judgements continue, and the setting expires automatically.

5.16.3 **Clock deviation**: at each synchronization with the central server, the local clock deviation is estimated as "midpoint of send and receive times − central reply time", raising an alarm above a threshold (example 60 seconds); the central server also estimates and displays each plant's deviation from the send times of uploads.

5.16.4 **Small mode**: without a central server, the edge host of the plant sends all notifications, escalations and daily summaries (the robot programs do not send on their own while the edge host is healthy, and send alerts themselves only when the edge host is down), and computes and distributes Weibull priors from the replacement records of all robots in the plant; each life model shows its source (own records, plant fleet, central fleet, configured prior). The daily summary also serves as a system heartbeat.

5.16.5 **Withholding output with reason codes** (not implemented in v2.1.0): each computed result carries a validity flag and a reason code, for example signal unconfirmed, unit unknown, time not synchronized, operating conditions changed, insufficient constant-speed segments, transmitted torque uncalibrated, alignment ambiguous, model unidentifiable, catalogue parameters unconfirmed, insufficient failure labels; when conditions are not met, no numerical conclusion is output, only the reason and the raw trend. The data dictionary distinguishes raw signals, proxy quantities and calibrated physical quantities.

## 6 Program-Level Diagnosis and Verification Methods (P series, advanced diagnostic module)

The methods of this chapter are implemented in DTM v2.1.0 (`dtm/pro/`; except the optional AI scheme of 6.9.2), and the specific approaches and parameters used in the implementation are recorded in 6.14. The following methods can be practised on any robot controller that has a programming language; the approach of 6.14 is one embodiment and does not limit the scope of 6.1–6.13.

### 6.1 P1 Program import and parsing

6.1.1 The text version of the robot program is read (for FANUC, .LS, obtainable from the controller's web pages; for KUKA, .src/.dat; for ABB, RAPID .mod; for Yaskawa, INFORM .JBI; likewise for other brands).

6.1.2 Each line is parsed for: line number, instruction type (joint J / linear L / circular C / other), target point (P[n], PR[n], offset, coordinate frames UF/UT), speed and unit, positioning type (FINE or CNT value), acceleration override (ACC), wait (WAIT time or signal), I/O, calls, jumps and labels.

6.1.3 A program control-flow graph (including branches and subprograms) is built, and the joint or Cartesian values of each taught point are recorded. The hash and modification time of the program file serve as the "program version", and any modification splits the data.

### 6.2 P2 Diagnostic optimization mode (temporarily higher sampling)

6.2.1 Sampling is raised only during the diagnostic period (example 3–7 days) and automatically returns to the low-rate sampling of resident monitoring when the period ends.

6.2.2 Torque and position are sampled, for example, 50–125 times per second (enough to resolve accelerations of 0.2–1 s); emergency stops and impacts require millisecond resolution and are covered by accelerometers.

6.2.3 Acquisition methods (any one or a combination):

- A controller-side program (for example KAREL) reads joint position, disturbance torque, torque command or motor current at the interpolation period and streams it over a socket or writes it to a file.
- The joint and Cartesian states returned by the controller's external trajectory-streaming interface (for example FANUC Stream Motion).
- An external acquisition card: accelerometers at thousands of samples per second or more (example 5–25 kHz), with the robot's digital outputs (DO/GO) emitting a code at the start of each segment or line as a hardware synchronization signal, giving synchronization errors on the order of milliseconds.
- Clamp-on current sensors measuring motor current.

6.2.4 Accelerometers are installed in rotation as a portable kit: temperature rating suited to the environment, fastened with screws (no magnetic bases at high temperature), cables fixed along the arm with consideration of bending life, and routed away from electromagnetic interference of servo cables.

### 6.3 P3 Line alignment (position matching)

6.3.1 Problem: when the program reports line numbers by writing a register before each line, the controller may, to blend through CNT points smoothly, execute later non-motion instructions before the robot arrives, so the reported line number runs ahead of the actual position.

6.3.2 Method: determine from position which line the robot is executing.

1. For each motion line, take "start = target point of the previous line, end = target point of this line" and build the expected path in joint space (J motion) or Cartesian space (L, C motion); near CNT points, build a corner zone (the blending zone of two path segments).
2. For each sample, compute the distance to the candidate paths and the projection parameter u ∈ [0, 1].
3. A state machine or hidden Markov model (for example Viterbi) enforces "progress in program order": only staying on the current line, entering the next line, or entering a branch target permitted by the control-flow graph is allowed; the result with the smallest distance and jump cost is the alignment.
4. Periods with speed near 0 near a point are marked as dwell (WAIT, positioning, gripping).
5. Output: line number, in-line progress u, confidence. PR and offset points are computed from the register values read at execution time; when they cannot be resolved, the path is learned from the repeating pattern of the actual trajectory.

6.3.3 Alternative: during the diagnostic period, an "instrumented program" outputs DO/GO codes before and after each line (with the controller's early or delayed trigger function correcting the timing), and the original program is restored after the diagnosis.

6.3.4 **Unassigned samples and accuracy evaluation** (not implemented in v2.1.0): samples whose confidence is below a threshold or whose candidate lines cannot be distinguished are classed as "unassigned", are not merged into any line's damage ledger, and the proportion of unassigned damage is shown; with DO/GO hardware codes or manual labels as ground truth, the per-sample line accuracy, the start and end time errors of each line, and the error rates near CNT, WAIT, CALL and END are evaluated.

### 6.4 P4 Cycle segmentation and synchronous averaging

6.4.1 Cycle start: program start, the first line, or the repetition period found by position autocorrelation.

6.4.2 In each cycle, the samples of each line are resampled by in-line progress u onto a fixed grid (example 50 points), so that cycles with different waiting times can also be aligned line by line.

6.4.3 For each line and each grid point, the mean, median and 10th and 90th percentiles are computed to draw the typical waveform and the range of variation; a rising coefficient of variation between cycles is itself a sign of line instability; cycles deviating from the median by more than 3 times the median absolute deviation are marked as abnormal cycles.

### 6.5 P5 Per-line damage ledger

6.5.1 Computed from the reducer output angular velocity ω (joint speed, or motor speed ÷ reduction ratio) and the transmitted torque T of each sample. Disturbance torque is used for anomaly detection; damage calculation uses transmitted torque (both are disclosed).

6.5.2 Equivalent torque of each line:

$$T_{m,\ell}=\left(\frac{\sum_{i\in\ell}|\omega_i|\,\Delta t\,|T_i|^{p}}{\sum_{i\in\ell}|\omega_i|\,\Delta t}\right)^{1/p}$$

6.5.3 Damage of each line (in equivalent revolutions at rated torque):

$$D_\ell=\sum_{i\in\ell}\frac{|\omega_i|\,\Delta t}{2\pi}\left(\frac{|T_i|}{T_0}\right)^{p}$$

The rated total revolutions for a rated life of L₀ hours at a rated output speed of N₀ rpm are R₀ = 60·L₀·N₀; life consumption per cycle = Σ D_ℓ / R₀; daily consumption = consumption per cycle × cycles per day.

6.5.4 Peak check: the ratio of each line's max|T| to the "start/stop allowable torque" and the number of exceedances; events exceeding the "momentary maximum allowable torque" are counted against the allowable number C, each consuming 1/C.

6.5.5 When the angular velocity is near 0 (holding torque), the above is 0; "holding time × torque" and the "number of small oscillations" are used separately as fretting indicators.

6.5.6 Output: ranking of each line's share of damage, total daily consumption, lines with peak exceedances, and the estimated time to reach the rated life (the rated life is a statistical value: about 10% of components of the same type fail before it, and it does not mean that an individual component fails at that time); when absolute ratings are unknown (custom reducers), only relative ranking is done, and absolute life is calibrated from replacement records.

6.5.7 Variants: the integral of squared motor current, vibration RMS or temperature rise as alternative damage indicators; p set according to the reducer type; correction by measured Weibull statistics.

6.5.8 **Friction-work hotspots** (not implemented in v2.1.0): when only the disturbance torque is available, fatigue damage is not computed with the p-th power; instead,

$$E_\ell=\sum_{i\in\ell}|\Delta\theta_i|\cdot|\tau_{dis,i}|$$

(exponent p = 1; F(θ) from P7 may replace τ_dis) is accumulated per line and per angle bin as "friction-work hotspots". According to Archard's wear law (wear volume proportional to load × sliding distance), this quantity reflects a tendency to wear rather than fatigue damage; it is named and output separately from the fatigue damage ledger and, without N·m calibration, is expressed in proxy units.

6.5.9 **Direction of transmitted-torque conversion** (not implemented in v2.1.0): when transmitted torque is converted from motor current or the torque command, forward driving (T_out = η·i·T_motor) and back-driving (T_out = i·T_motor / η′, for example gravity-driven lowering or decelerating stops) are distinguished by the direction of power flow; η and η′ can be set in tables as functions of speed, temperature and load, and the torque source, conversion factors and uncertainty are recorded. The force and friction of balancers (spring or gas) are estimated separately from G(θ) and the forward/backward difference and removed, so that balancer friction is not taken as reducer friction.

### 6.6 P6 Angle-concentration map

6.6.1 The reducer output angle θ is divided into small bins (example 1°), and each bin accumulates:

$$d(\theta_k)=\sum_{i:\,\theta_i\in k}|\omega_i|\,\Delta t\left(\frac{|T_i|}{T_0}\right)^{p}$$

Also accumulated: number of passes, number of reversals (sign changes of speed), and the distribution of the amplitude of each oscillation.

6.6.2 Concentration indicators: the width θ₈₀ of the narrowest angular range containing 80% of the damage, the Gini coefficient, and the share of the top k bins. A narrower θ₈₀ means the damage is more concentrated in a single small range of output angle (for the correspondence with internal contact positions see 6.6.6).

6.6.3 Small-oscillation judgement: the oscillation amplitude is converted into rolling-element or tooth-flank displacement, and if it is smaller than the contact width (estimated per model), fretting risk is flagged.

6.6.4 Using the reduction ratio and the number of cycloidal teeth, pins or harmonic flexspline teeth, the output angle can be converted into the corresponding tooth, pin or raceway position, giving an "estimated wear location" for comparison at teardown.

6.6.5 Recommendations: without causing interference, change the working angle (for example achieving the same pose with a wrist axis at a different turn number), alternate between different angle ranges, or periodically run slowly over the full range.

6.6.6 **Output angle and internal contact positions** (not implemented in v2.1.0): the conversion of 6.6.4 requires an initial phase, which can be calibrated from the damage location at teardown or from assembly marks. In the second stage of an RV type, each pin pitch of output rotation (360° / number of pins) corresponds to one revolution of the crankshaft, during which the load zone travels once around the pins; a narrow range of output angle therefore corresponds to load all around the pins and crankshaft bearings, but involves only particular pairings of cycloidal teeth and pins. What can be located directly by output angle is mainly the raceway of the rotating ring of the output-side main bearing and the fretting spots caused by small oscillations.

### 6.7 P7 Full-range sweep and local friction comparison

6.7.1 Periodically (example weekly or at shift changes, 1–3 minutes per axis), each axis moves once forward and once backward over its largest interference-free angular range at 1–2 fixed slow speeds.

6.7.2 Friction and ripple are computed per angle bin:

$$F(\theta_k)=\frac{\tau_{+}(\theta_k)-\tau_{-}(\theta_k)}{2},\qquad G(\theta_k)=\frac{\tau_{+}(\theta_k)+\tau_{-}(\theta_k)}{2}$$

6.7.3 With the high-use bins of P6 as set U and the low-use bins as reference set R, the local wear index is computed:

$$LWI=\frac{\operatorname{median}_{k\in U}F(\theta_k)}{\operatorname{median}_{k\in R}F(\theta_k)}$$

The temperature-compensated LWI is judged against its own baseline (example 1.2× caution, 1.4× alert) and its trend. A rise in the frequently used range with no change in the rarely used range is a sign of local wear in service (to be confirmed finally by teardown).

6.7.4 Ripple and vibration in the angle domain (accelerometers during the sweep) are recorded at the same time to find the angles of local impacts; G(θ) is used to check the balancer and gravity compensation.

6.7.5 Slow full-range motion also redistributes the grease and may slow the wear caused by small oscillations (effect to be verified).

6.7.6 Variants: sweeping only part of the range around the frequently used range; "incidental sweeps" using segments of production motion that happen to pass a range at constant speed; motor current instead of disturbance torque.

### 6.8 P8 Waiting-slack analysis and time scaling

6.8.1 In each cycle, the lines that wait for an external signal, a timer or a handshake after a motion completes are found, and each actual waiting time w is measured.

6.8.2 The usable slack of the motion block before the wait is S = the q-th percentile of w − a safety margin (example q = 10).

6.8.3 Time scaling: the block time is extended from T_move to T_move + S, with scaling factor σ = (T_move + S)/T_move; speed is scaled by 1/σ and acceleration by 1/σ². New torque estimate:

$$T'(t)=T_g(t)+\frac{T_{inertia}(t)}{\sigma^{2}}+T_f\!\left(\frac{v}{\sigma}\right)$$

T_g is the gravity term, T_inertia the inertia term and T_f the friction term; the reduction in damage is computed with P5. Condition for an unchanged cycle time: the robot still arrives before the signal comes. Subtracting a safety margin from the q-th percentile only lowers the probability that the cycle time is affected and is not a guarantee; the cycle time is confirmed by measurement after the change. At moments when gravity and inertia act in opposite directions, slowing down may increase the torque at that moment, so damage is accumulated from |T′| computed point by point.

6.8.4 Implementation: lowering the speed or acceleration override of the lines in the block, inserting intermediate points for a soft start, or executing a smooth trajectory by external trajectory streaming.

6.8.5 For critical-path segments without waiting slack, a "cycle-time versus life trade-off table" is provided: the damage reduction and the change in annual replacements and downtime hours for each additional 0.1 s.

6.8.6 **Estimation quality and direct measurement** (not implemented in v2.1.0): the per-line regression of 6.14.6 must pass quality gates before an estimate is output: a lower limit on the coefficient of determination, a positive inertia coefficient c, an upper limit on the condition number of the design matrix, validation of the prediction error on held-out cycles, and bootstrap intervals of the coefficients; if it fails, only "candidate lines that may be slowed on trial" are listed, without a percentage of damage reduction. Variants: including in the regression products of the angular accelerations and velocities of other axes (coupled inertia, Coriolis and centrifugal terms); or, during the diagnostic period, actually slowing to 1/σ by the speed override or per-line speed for several cycles and measuring the changes in torque, damage and cycle time directly, replacing or calibrating the regression estimate.

### 6.9 P9 Modification suggestions

6.9.1 Example rule base: change FINE to CNT at points that do not need precise positioning (fewer full stops; interference must be checked); lower ACC or speed on lines with high damage ranking and slack; insert intermediate points and increasing speeds at the start and end of long moves for a soft start; avoid unnecessary large wrist flips; check payload settings (separate settings with and without workpiece); avoid linear moves through high-torque postures where a joint move can be used; reorder motions so that high-torque motions fall where there is slack.

6.9.2 Optional AI: the program text, per-line numbers, slack and constraints (cycle-time limit, interference zones) are structured and given to a language model, which produces modification proposals ranked by benefit, before/after program comparisons and reasons. All numbers are computed by the system, and the AI only writes and ranks; cloud or local models can be used, and point names and comments can be anonymized before sending.

6.9.3 Safety principle: the system only outputs suggestions, does not write back to the controller and does not lower speed automatically; modifications are made by people, checked in order by simulation software for interference and cycle time, trial-run in low-speed manual mode, and then put into production.

### 6.10 P10 Before/after and control trials

6.10.1 Procedure: diagnosis before the change for 3–7 days → modification → diagnosis after the change for 3–7 days → follow-up for 4–8 weeks (example).

6.10.2 Control group: another robot of the same model, at the same station or with similar load, that is not modified.

6.10.3 Indicators: peak torque per line, damage per cycle, temperature rise, number of vibration impacts, iron-powder W, friction trend, LWI.

6.10.4 Statistics: with daily medians as the unit, the difference-in-differences (change of the treatment group − change of the control group) is computed, and significance is judged by bootstrap confidence intervals or the Mann–Whitney test.

6.10.5 Report: a one-page conclusion (what was changed, cycle-time change, damage change, cost conversion) plus appendix data, signed off by both maintenance and production. Cost conversion:

$$\text{annual saving}=N_{\text{avoided}}\times\left(H_{\text{downtime}}\times C_{\text{per hour}}+C_{\text{spares}}+C_{\text{labour}}\right)+C_{\text{output loss before failure}}$$

6.10.6 **Statistical variants** (not implemented in v2.1.0): the bootstrap resamples paired by date (the treatment and control groups of the same day are drawn together), or resamples blocks of consecutive days, preserving correlation across days and the common changes of the two robots; with only one robot, A/B/A/B alternating trials are used where safety permits. Threshold replay is split by time, using only the data, settings and baselines available at the time, and compares detection rate, false alarms per robot-year and lead time with simple baselines such as scheduled maintenance and a single threshold. The Mann–Whitney test serves only as an auxiliary and not as causal evidence.

### 6.11 P11 Impact event counting

6.11.1 Stop events such as emergency stops, collision detection and safety-fence opening are counted from the controller alarm history, with the speed and torque at the time recorded, and added to damage with different weights according to the stop method (power-off brake stop, controlled deceleration stop).

6.11.2 With accelerometers, impacts are detected by "peak > k × baseline RMS and crest factor above a threshold" and mapped to line and angle.

### 6.12 P12 Acceleration profiles

6.12.1 For the same distance D and time T from rest to rest, the peak acceleration is 4.5·D/T² for a trapezoidal velocity profile (acceleration, constant speed and deceleration each 1/3) and 5.77·D/T² for minimum jerk (quintic polynomial); S-curves lie between the two or higher depending on the jerk limit. The smoother the profile, the smaller the impact, but the higher the peak for the same time.

6.12.2 Decision logic: impacts and resonance (visible to accelerometers) are handled by lowering jerk; cumulative damage (10/3 power) is handled by lowering the peak; for segments with slack, the time is simply extended, lowering both.

6.12.3 Implementation: per-line acceleration override, soft start with intermediate points, executing S-curve or minimum-jerk trajectories by external trajectory streaming, and time–jerk optimization seeking the shortest time within torque and jerk limits.

### 6.13 P13 Visualization

6.13.1 Three synchronized timelines: axis torques at the top (typical waveform and range of variation), vibration in the middle and the program at the bottom (each cell the text of one instruction line), coloured by share of damage; clicking a line shows its peak as a proportion of the allowable value, its share of damage, its time and its wait.

6.13.2 The angle-concentration map is shown in polar or unrolled form with the damage density, number of reversals and LWI of each angle bin, and can overlay the damage locations recorded at teardown.

### 6.14 Implementation record (DTM v2.1.0)

6.14.1 **Instrumented program**: a line writing the original line number to a numeric register (example R[114]) is inserted before every motion line of the main flow, with motions, speeds and points unchanged. During parsing, the "effective positioning type" of each motion line is determined: FINE, or CNT immediately followed by WAIT, CALL or END (where the controller actually stops), is treated as FINE.

6.14.2 **Line alignment**: each motion line is a state, and the path template is the 12-point joint-space path of that line (learned from the first few cycles when line numbers are reported, otherwise initialized from the line between the joint values of the preceding and following taught points); each cycle is reduced to 20 Hz, and a "stay or move forward only" Viterbi finds the minimum-cost sequence (cost = distance to template² + line-change cost). When line numbers are reported, the candidate states are restricted to between "the reported line traced back, crossing only CNT lines" and "the reported line" (example at most 2 lines back); with position only, chord-length segmentation of the templates and beam search are used. A line change falling within a stationary period (joint speed norm < 1 degree/s) is moved to the moment motion actually starts, and the stationary period counts as dwell of the previous line.

6.14.3 **Damage and peaks**: p = 10/3 (RV, rack), 3 (gearbox); when the start/stop allowable torque and the momentary maximum allowable torque are not set, RV types default to 2.5 times and 5 times the rated torque (defaults only; taking common RV catalogues as an example, the start/stop value is about 2.5 times for most models and about 2 times for the smallest model, and the catalogue values of the model used should be entered); example allowable number of momentary maximum events C = 1000. Stop events are counted by stop method: emergency stop 0.02/C, collision detection 0.5/C, controlled stop 0.005/C per event (example weights).

6.14.4 **Angle concentration**: bin width 1° (10 mm for linear axes); reversal detection with hysteresis (0.05°, 0.5 mm for linear axes); oscillations smaller than the contact width (example 0.5°) are counted as small oscillations. High-use set U = the bins accumulating 50% of the damage in descending order of damage density; low-use set R = the 30% of visited bins with the lowest density.

6.14.5 **Full-range sweep**: the sweep program marks forward and backward motion with segment codes (8000 + axis × 10 + direction), which are separated automatically when streamed or imported together with the diagnostic-period data; LWI uses the first sweep as its baseline, ratio ≥ 1.2 caution, ≥ 1.4 alert.

6.14.6 **Waiting slack and time scaling**: for lines waiting for an external signal, S = 10th percentile of actual waiting time − timer wait − 0.05 s − safety margin (example 0.3 s); σ = (T + S)/T, upper limit 2. Each line is regressed on measured data as τ = cubic polynomial(u) + c·α + v·ω (u is in-line progress, α angular acceleration, ω angular velocity; position depends only on u, and the gravity term is unaffected by scaling); after scaling, T' = T − c·α·(1 − 1/σ²) − v·ω·(1 − 1/σ), and damage is computed with the formulas of 6.5. For lines without slack, the trade-off table is computed per additional 0.1 s.

6.14.7 **Before/after and control**: the statistical unit is chosen by data volume as daily, hourly or 10-minute medians (the longer unit is used only when each group has at least 3 periods); effect = treatment (after ÷ before) ÷ control (after ÷ before) − 1, computed as a log difference-in-differences; the 95% confidence interval by bootstrap (resampling periods within each group, 2000 times); the treatment group's before/after is also tested with Mann–Whitney (exact distribution when n₁·n₂ ≤ 400 and there are no ties, otherwise the normal approximation with tie correction).

6.14.8 **Impacts and acceleration profiles**: accelerometer impacts are judged by peak > 4 times the baseline RMS and crest factor > 5 (example), in the time domain only; the profile shape of rest-to-rest motions is judged by κ = a_peak·T²/D (trapezoid about 4.5, minimum jerk about 5.77).

6.14.9 **Torque source**: damage calculation is based on transmitted torque (converted from the torque command or motor current); when only disturbance torque is available, the damage ranking is marked as a relative reference.

6.14.10 **Suggestions and safety**: suggestions are generated by rules and output only as text (before/after comparison and reasons), and modifications are not written back to the controller (the robot-side background logic, diagnostic program, motion codes and instrumented program are installed by personnel and write only to dedicated registers); analysis uses only measured data, and the built-in simulator is used only to generate demonstration data for the built-in demonstration programs.

### 6.15 Comparison with prior art

6.15.1 **Purpose and limits**: this section compares item by item the known patents closest to the P series, stating clearly the technical differences between DTM's approach and the claims of each patent, as well as the design boundaries this project deliberately keeps (for the implementation side see `docs/DESIGN_CONSTRAINTS.md`).

- This is a technical comparison, **not a legal opinion**; neither the author nor those assisting in writing are patent attorneys. Whether something falls within the scope of a patent should be judged by a patent attorney on the full text of the claims as published in each country.
- Patents are valid only in the countries or regions where they are granted.
- Search date 2026-10-19, source Google Patents; validity and expiry dates are as recorded by each patent office.
- General principle (all-elements rule): to fall within the literal scope of a claim, **all** of its elements must be present; the range of equivalents is to be judged separately by a patent attorney.

6.15.2 **Comparison 1: FANUC JP5289608B2 (first key comparison for the P series)**

| Item | Content |
| --- | --- |
| Title | ロボット減速機寿命推定シミュレーション装置 (robot reducer life estimation simulation device) |
| Right holder | ファナック株式会社 (FANUC CORPORATION); inventor 梁 衍学 |
| Dates | Priority 2011-12-13; filed 2012-07-06; laid open as JP2013-144349A (2013-07-25); patent gazette 2013-09-11 |
| Status | Shown as active by Google Patents; if annuities are maintained, expected to expire 2032-07-06 |
| Family | US8731886B2 (expected expiry 2032-11-16), CN103176410B, DE102012112019B4; no Taiwanese family member found at the time of search |
| Relationship to DTM | Shares the main line "speed and load of each axis → reducer life → program lines with excessive load → lower speed", and is the closest known art to P5, P8 and P9 |

6.15.3 **Elements of claim 1** (English summary, not a literal translation):

1. Simulating execution of the operation program and calculating the speed and load of each axis reducer at each sampling period.
2. Storing the speed and load in time sequence.
3. Calculating the life of each axis reducer from the stored speed and load.
4. Setting the operating-time ratio of the robot (or data indirectly representing it).
5. Estimating the reducer life from the calculated life and the operating-time ratio.
6. Specifying a reference value (upper limit) for the load of each axis reducer.
7. Judging with an evaluation function whether the load exceeds the reference value.
8. Fixing the trajectory of the tool centre point so that it is not affected by the speed command.
9. With the trajectory fixed, setting the speed commands of the operation program to the maximum speed determined by a rule.
10. When the load exceeds the reference value, lowering the speed command from the maximum speed by a predetermined amount with the trajectory fixed, changing the operation program so that the load does not exceed the reference value.

Dependent claims 2–5: setting a target life or target cycle time and repeating the program change until it is achieved; when not achieved, resetting the reference value and repeating.

6.15.4 **Item-by-item comparison**:

| Element | DTM Pro approach | Comparison |
| --- | --- | --- |
| 1 Program simulation of speed and load | Analyses only joint angles and torques actually measured during the diagnostic optimization period (6.14.10). The time-scaled torque of 6.8 is converted from a regression on measured samples (6.14.6), not from simulating robot motion with the program. The built-in simulator only produces demonstration data for the built-in demonstration programs and refuses to run when a user program is loaded | Absent in real use; the demonstration mode has program simulation, but only for the built-in demonstration programs |
| 2 Time-series storage | Measured samples stored by time | Similar |
| 3 Life from speed and load | The per-line damage ledger computes p-th-power damage from measured ω and T (6.5) | Similar; inputs are measured values |
| 4 Operating-time ratio | Daily consumption converted from the actually measured number of cycles per day (6.5.3) | Functionally similar; values come from measurement |
| 5 Life estimation | Estimated life (6.5.6); only relative ranking when absolute ratings are unknown | Similar |
| 6 Load reference value | Start/stop allowable torque, momentary maximum allowable torque (6.5.4, 6.14.3) | Similar |
| 7 Judging exceedance | Peak per line ÷ allowable value, and the number of exceedances | Similar |
| 8 Fixed trajectory | Does not control the trajectory; the estimate of 6.8 merely assumes "points and path unchanged" | Partly similar (an assumption of the estimate, not a function that fixes the trajectory) |
| 9 Speed set to maximum | Never sets speed to the maximum; suggests slowing down only within measured waiting slack, aiming at an unchanged cycle time confirmed by measurement (6.8) | Absent; opposite direction |
| 10 Automatic program change | Outputs only text suggestions (before/after comparison and reasons); does not write back to the controller, does not lower speed automatically and does not modify the program repeatedly (6.9.3, 6.14.10); modifications are made by people and then verified by measurement with P10 | Absent |
| Dependent claims 2–5 | The "cycle-time versus life trade-off table" (6.8.5) only lists numbers and does not automatically search for a target life or target cycle time | Absent |

6.15.5 **Conclusion of the technical comparison**: DTM Pro lacks elements 9 and 10, lacks element 1 in real use, and has no automatic iteration of dependent claims 2–5. Under the all-elements rule, it generally does not fall within the literal scope of claim 1; the final judgement rests with the opinion of a patent attorney.

6.15.6 **Parts already disclosed by that patent and not claimed as new in this disclosure** (JP2013-144349A was laid open on 2013-07-25):

- Calculating reducer life from time series of the speed and load of each axis, and converting with an operating-time ratio.
- Identifying and highlighting the corresponding program lines when the load exceeds the reference value (second embodiment of the description; not in the claims).
- Lowering reducer load by lowering the speed of specific program lines.
- The 10/3-power relationship between life and equivalent torque itself is published reducer selection data (2.5.2).

6.15.7 **Technical differences between this disclosure and that patent** (the focus of what this document discloses):

- **Data source**: measured torque and angle, including real temperature, grease condition, emergency-stop impacts and actual load on site (2.3), not program simulation.
- **Line alignment**: measured positions are matched to the taught path, handling early execution under CNT (6.3, 6.14.2).
- **Local wear**: the angle-concentration map (6.6) finds the output-angle range where damage concentrates, and the local friction comparison LWI (6.7) measures the friction change in that range; the claims of that patent do not cover this.
- **Direction of modification**: slowing down only within measured waiting slack, aiming at an unchanged cycle time confirmed by measurement (6.8); not setting the maximum speed first and then lowering it.
- **Verification**: measured comparison of before, after and a control group (6.10), not judgement by simulation results.
- **Execution**: modifications are made by people and put into production only after interference checks and low-speed trial runs (6.9.3).

6.15.8 **Design boundaries kept by this project** (please keep them in modified or derivative versions):

- No automatic rewriting or repeated modification of operation programs, no writing of suggestions back to the controller, no automatic speed reduction.
- No "set speed to the maximum first, then lower it step by step" procedure.
- Loads computed by operation-program simulation (dynamic models) do not replace measurement; the built-in simulator continues to run only the built-in demonstration programs; time-scaling estimates remain based on measured samples.
- No function that automatically and repeatedly searches speeds or program parameters conditioned on a target life or target cycle time.
- Before using or distributing in Japan, the United States, China or Germany a derivative version that adds any of the above functions, please have a patent attorney assess it first.

6.15.9 **Comparison 2: FANUC JP4112594B2 (brief)**

- Title: 減速機異常診断方法及び減速機異常診断装置 (reducer abnormality diagnosis method and reducer abnormality diagnosis device); right holder ファナック株式会社 (FANUC CORPORATION); filed 2006-07-27. Shown as expired by Google Patents (expected expiry date 2026-07-27).
- Summary of claim 1: an observer estimates the disturbance value of the reducer from the torque command and speed feedback; frequency analysis is performed, the specific spectrum at integer multiples of the intermediate-shaft rotation frequency is taken, and an abnormality is judged when its amplitude exceeds a threshold.
- DTM: the ripple indicator is the time-domain standard deviation of the disturbance torque within constant-speed segments (5.2.3), and no frequency analysis of the disturbance torque is performed; vibration features such as envelope and mesh are computed by external sensors or gateways and read in as values (5.8).

6.15.10 Comparisons added later are numbered comparison 3, comparison 4 and so on, in the same format as 6.15.2–6.15.8.

---

## 7 Variants and Scope of Application

7.1 **Robots**: any brand and type, including FANUC, KUKA, ABB, Yaskawa, Kawasaki, Nachi-Fujikoshi, Denso, Epson, Stäubli, Universal Robots, Estun, Efort, Inovance and others; vertical articulated, horizontal articulated (SCARA), parallel-link, collaborative and gantry robots, seventh-axis travel tracks and positioners.

7.2 **Transmission mechanisms**: RV/cycloidal reducers, harmonic drives, planetary gears, worm gears, belts, rack and pinion, ball screws, linear guides and bearings.

7.3 **Other equipment**: machine-tool rotary tables and turrets, wheel reducers of automated guided vehicles and mobile robots, wind-turbine pitch and yaw bearings, roller tables and oscillating mechanisms of steel and paper production lines, cranes, injection-moulding take-out robots.

7.4 **Signal sources**: disturbance torque, motor current, torque command, position error, external torque sensors, accelerometers, microphones (sound), acoustic emission sensors, thermocouples and infrared temperature, online oil metal-particle sensors, laboratory oil analysis.

7.5 **Computing location**: inside the controller, in a PLC, on an edge computer, a server or the cloud; any programming language.

7.6 **Decision methods**: rules, statistics, machine learning (anomaly detection, classification, regression), language models, or combinations of these.

7.7 All methods of this document may be practised alone or in any combination (see 0.2).

---

## 8 Example Parameters

| Parameter | Example value |
| --- | --- |
| Resident sampling | 2–5 times per second |
| Diagnostic segment trimming | 1.0 s at head and tail; constant speed at least 2.0 s |
| Diagnostic speeds | 5%, 10%, 20%; reference speed 10% |
| Baseline size | Diagnosis 10 runs; production cycles 10 valid days; vibration 10 records |
| Torque thresholds | Diagnosis 1.5/2.0×; production cycles 1.15/1.30× |
| z-score | 2 (with ratio ≥ 1.15) early caution; 3 escalates to alert; σ floor = 5% of baseline |
| Hysteresis | 2 consecutive runs (production cycles: 2 consecutive valid days) |
| Temperature coefficients | F_v 0.025/°C; F_c 0.004/°C |
| Vibration thresholds | Ratio 2/3×; kurtosis 4/6; axial ratio 1.5× |
| Iron powder | RV type 0.05/0.10 wt%; W ratio 2/3×; at least 300 hours |
| Sampling intervals | Caution 60 days; alert 21 days |
| Temperature | Reference 40 °C, aging doubles per +10 °C; sudden rise +8 °C or +5 °C/day; 72 hours excluded after regreasing |
| Decision | Action 0.5, immediate 0.9; single family × 0.45; grease score from 70% of grease life |
| Recovery check | 2–4 weeks after regreasing, within 1.3× |
| Life | Weibull at least 3 failures; spare-part lead time 60 days; conditional failure probability 0.2 |
| Extrapolation | Limit 730 days; interval ±(30% + 7 days) |
| Local wear index | 1.2/1.4× |
| Diagnostic optimization period | 3–7 days; torque 50–125 samples/s; accelerometers 5–25 kHz |
| Control trial | 4–8 weeks |

---

## 9 Summary

This chapter is the English summary that accompanies the Chinese version; chapters 0–8 above are the complete English text.

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

## 10 Publication Record

| Item | Content |
| --- | --- |
| GitHub Release | v2.1.0 (Beta, pre-release), published 2026-11-03 07:43:54 UTC (15:43:54 UTC+8); tag v2.1.0 points to commit f9027ed. https://github.com/Domingo-He/dtm-reducer-monitor/releases/tag/v2.1.0 |
| Zenodo DOI | 10.5281/zenodo.23910956 (v2.1.0, 2026-11-03), https://doi.org/10.5281/zenodo.23910956 |
| Software Heritage | (filled in after archiving) |
| Release artifact attestation | Generated by the automated workflow; see the Release page |
| Revision history | 1.0 (2026-10-16) first written; 1.1 (2026-10-17) added 5.7.3, 5.10.6–5.10.7, 5.15, 5.16 and 9.10; 1.2 (2026-10-17) added the scope statement 0.5; 1.3 (2026-10-18) chapter 6 changed to implemented, added the implementation record 6.14, the sources 2.5 and 9.11 (the former 9.11 on scope became 9.12); deleted the scope statement 0.5, the decision rules for lubrication failure modes likewise fall under 0.2 and chapter 7; 1.4 (2026-10-19) added the prior-art comparison 6.15 and 9.13; 1.5 (2026-10-28) corrected the descriptions in 5.9.5, 5.11.5, 5.11.7, 6.5.6, 6.6.2, 6.7.3, 6.8.3, 6.14.3, 6.14.10, 6.15, 9.4, 9.7, 9.8, 9.11 and 9.13 after external review, added the variants 5.2.6, 5.9.8, 5.11.8, 5.12.5, 5.16.5, 6.3.4, 6.5.8, 6.5.9, 6.6.6, 6.8.6, 6.10.6 and 9.14, supplemented 0.4 with sources; added the language statement 0.5 (the former 0.5 was deleted in 1.3) and the full English and Japanese texts, with the Chinese, English and Japanese versions published simultaneously; on 2026-11-03 the publication record of this section and the first publication date were filled in (disclosed content unchanged) |
