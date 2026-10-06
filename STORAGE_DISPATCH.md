# STORAGE_DISPATCH.md —— 储能 SOC 模型与调度策略

> **当前状态**：**V2 开发中**。截至本文档修订时，**配置模型已落地**：
> `domain/timeseries.py:StorageDispatchConfig`（15 字段）已存在，本文档第 4、8 节涉及的
> 阈值与开关字段均**逐字段对照源码核实**。算法模块 `src/cenep/calculation/storage_soc.py`、
> `src/cenep/calculation/dispatch_engine.py` **尚未创建**，其算法部分为**设计**。
> 全文一律使用「设计」「本期实现」表述，**不得**据此认为功能已可用。
>
> **优先级**：受 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) 约束；
> 储能 SOC 与调度的字段定义以 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) 第 4、5 节为准，
> 本文档只规定**算法与判定流程**。
>
> **记号**：`V2 §N` = V2 总规范第 N 条；`V1 §N` = V1 产品规范第 N 条；
> `核心 §N` / `核心 LN` = `CORE_PARAMETERS_AND_FORMULAS.md` 第 N 节 / 第 N 条限制；
> 【设计】= 本期尚未实现；【V1 沿用】= V1 已有实现，V2 直接复用。
>
> **适用范围**：工商业储能与光储一体化项目的**逐时**储能建模（V2 §10）。
> V1 的**年度等效循环模型**（核心 §4.2、L11）继续保留，作为时序模型不可用时的回退路径。

---

## 1. 范围与设计定位（V2 §10）

V1 的储能模型是**年度等效**的：先算年放电量、再算年充电量（核心 §4.2 式 9、式 10），
**不出现 SOC、不出现小时**。V2 要在逐时维度回答三件事：

| # | 问题 | V1 | V2（【设计】） |
|---|---|---|---|
| 1 | 第 t 小时储能在充还是放？ | 不建模 | **逐时决策 + 中文原因**（V2 §16） |
| 2 | SOC 会不会越界？ | 不建模 | **SOC 上下限硬约束**（V2 §11） |
| 3 | 套利赚了多少？ | 年度 `放电×替代价 − 充电×充电价` | **逐时同一公式求和**（V2 §29） |

### 1.1 模块职责

| 模块 | 职责 | 禁止事项 |
|---|---|---|
| `calculation/storage_soc.py` | SOC 递推、边界与功率约束、可用充放电能力计算 | **禁止**决策"该不该充/放" |
| `calculation/dispatch_engine.py` | 三种策略的决策逻辑、生成 `DispatchDecision` | **禁止**直接改 SOC 数组（只能下指令） |
| `calculation/energy_balance.py` | B3 储能守恒残差校验 | **禁止**修正决策结果 |
| `optimization/*.py` | 参数寻优（见 [OPTIMIZATION.md](OPTIMIZATION.md)） | **禁止**绕过上述两个模块 |

---

## 2. SOC 模型（V2 §10、§11）

### 2.1 定义与可用容量

```text
SOC_t          = 第 t 小时结束时的荷电状态（小数，0~1）
E_capacity     = 储能额定容量 E_rated（kWh）── 来自 StorageConfig.storage_energy_kwh
E_usable       = E_capacity × (soc_max − soc_min)         可用容量（kWh）
E_available(t) = (soc_t − soc_min) × E_capacity           当前可放电能量（kWh）
E_headroom(t)  = (soc_max − soc_t) × E_capacity           当前可充电空间（kWh）
```

**初始值**：

```text
soc_0 = soc_min      （默认；可通过 StorageDispatchConfig 显式指定初值）
```

> **设计取舍**：SOC 初值默认取 `soc_min`，即"开局空仓"。理由：避免凭空给项目送一份免费电量，
> 且使得**首日必然先充后放**，符合实际投运行为。该取舍必须在报告口径页披露（TIMESERIES_MODEL.md §10.4 V2-L3）。

### 2.2 充放电效率方向（V2 §11）—— 关键易错点

**规范 §11 明确规定了效率的作用方向，不得用反**：

```text
充电：E_inc = P_charge    × η_charge    × Δt        （乘效率：电表 1 kWh → 电池得 η kWh）
放电：E_dec = P_discharge / η_discharge × Δt        （除效率：电池取 1 kWh → 电表得 1/η kWh）
```

| 方向 | 公式 | 物理含义 | 若用反了的后果 |
|---|---|---|---|
| 充电 | `E_inc = P_charge × η_charge × Δt` | 交流侧计量 → 电池内部储能 | 充电"变便宜"，套利收益虚高 |
| 放电 | `E_dec = P_discharge / η_discharge × Δt` | 电池内部储能 → 交流侧计量 | 放电"变多"，套利收益虚高 |

**SOC 递推式**（与 TIMESERIES_MODEL.md §6.1 第 13 行一致）：

```text
soc_end = soc_start + (storage_charge × η_charge × Δt − storage_discharge / η_discharge × Δt) / E_capacity
```

**往返效率闭合校验**（与 V1 一致，核心 §4.2 式 8）：

```text
η_round_trip = η_charge × η_discharge
```

| 取值 | η_charge = η_discharge | η_round_trip | 说明 |
|---|---|---|---|
| V1 默认口径 | `√0.88 = 0.938083` | **精确 0.88** | 由往返效率均分反推 |
| **V2 实现默认** | **`0.938`** | **`0.879844`** | 三位小数，与 0.88 偏差 0.018% |

> ⚠️ **口径提示**：V2 实现把默认效率取为 `0.938`，其往返效率为 `0.879844`，
> **不等于** V1 默认的 `0.88`。若要求严格等于 0.88，须显式传入 `0.938083`。
> 该差异已在 [CHANGELOG.md](CHANGELOG.md) 的 V2 条目登记，最终取值以评审结论为准。

### 2.3 SOC 边界约束（V2 §11）

| # | 约束 | 处理方式 |
|---|---|---|
| S1 | `soc_min ≤ soc_t ≤ soc_max`，**任一时刻** | 越界即判定计算失败（不得裁剪后继续） |
| S2 | 充放电**不得**使 SOC 越过上下限 | 决策阶段先算"可行区间"，再取指令与可行区间的较小值 |
| S3 | `0 < soc_min < soc_max < 1` | 模型校验（Pydantic）拒绝非法组合 |
| S4 | `soc_min` 默认 `0.10`；`soc_max` 默认 **`1.00`**（满仓） | 见 TIMESERIES_MODEL.md §4.5；可用区间由 `soc_min` 单独约束 |

**SOC 越界属于"设计缺陷级"错误**（V2 §11）：出现即说明约束未生效，必须报错，
**禁止**用 `min/max` 裁剪后照常出报告。

### 2.4 功率与能量约束（V2 §11）

每个小时的充放电能力**同时**受四个上限约束，取最小值：

```text
max_charge_energy(t) = min(
    max_charge_power_effective × Δt,                    ① 功率上限
    E_headroom(t) / (η_charge × Δt) × Δt,               ② SOC 上限
    E_capacity × max_c_rate × Δt,                       ③ 倍率上限（若配置）
    Σ 该小时可用电量（光伏盈余 + 电网可用）               ④ 电量来源上限
)

max_discharge_energy(t) = min(
    max_discharge_power_effective × Δt,                 ① 功率上限
    E_available(t) × η_discharge × Δt / Δt,             ② SOC 下限
    load_demand(t) + export_room(t),                    ③ 去向上限
    E_capacity × max_c_rate × Δt                        ④ 倍率上限（若配置）
)

max_charge_power_effective    = max_charge_power > 0 ? max_charge_power : storage_power_kw
max_discharge_power_effective = max_discharge_power > 0 ? max_discharge_power : storage_power_kw
```

> `max_charge_power = 0` 表示"按储能额定功率"，与 V1 `StorageConfig.storage_power_kw` 语义一致。

### 2.5 SOC 首末点闭合（V2 §11、§40）

V1 的年度模型隐含"能量按年平衡"；V2 逐时模型必须显式声明**首末 SOC 关系**：

| 模式 | 约束 | 本期状态 |
|---|---|---|
| 自由模式（默认） | 无首末约束；`soc_end` 由逐时递推决定 | ✅ 本期实现 |
| 闭合模式 | `soc_N = soc_0`（年度净吞吐为 0） | 🔶 设计预留（供经济优化使用） |

自由模式下，**年度净吞吐量**作为口径指标输出：

```text
年度净吞吐 = Σ_h storage_discharge_h − Σ_h storage_charge_h × η_round_trip
```

该值明显偏离 0 时，在报告中提示"储能年末存在净存留电量"。

---

## 3. 调度策略总览（V2 §12）

三种策略**互斥**，由 `StorageDispatchConfig.strategy` 指定，**一次只跑一种**：

| 策略 | 枚举值 | 目标 | 是否考虑电价 | 实现难度 |
|---|---|---|---|---|
| 峰谷套利 | `PEAK_VALLEY` | 最大化峰谷价差收益 | ✅ 仅用价格阈值 | 低 |
| 光伏自用优先 | `PV_SELF_CONSUMPTION` | 最大化光伏就地消纳 | ❌ 不看电价 | 低 |
| 经济优化 | `ECONOMIC_OPTIMIZATION` | 最大化逐时净收益 | ✅ 逐时比较 | 中 |

```text
共同前置：E_capacity > 0 且 storage_power_kw > 0，否则全部小时 action = IDLE（原因 R19）
共同顺序：先算光伏分配（pv_to_load）→ 再算储能指令 → 再算电网补足
```

**统一决策入口**（`dispatch_engine.py`）：

```text
for each hour t:                       ← 外层允许循环（TIMESERIES_MODEL.md §3.2 P3）
    instruction = strategy_decide(t)   ← 返回 (action, energy, reason_code)
    clamped     = clamp_by_constraints(t, instruction)   ← 第 2.4 节四个上限
    record DispatchDecision(t, action, reason_text, charge, discharge)
    update soc_array[t]                ← 向量化的 SOC 递推
```

---

## 4. 策略一：峰谷套利 `PEAK_VALLEY`（V2 §13）

**核心思想**：电价低于充电阈值就充，高于放电阈值就放，其余不动作。
**不使用** SOC 优化、**不预测**后续电价。

### 4.1 阈值定义（**已核实字段名**）

阈值优先取 `StorageDispatchConfig` 的**显式字段**；为 `0.0`（默认）时按峰谷比例**自动推算**：

```text
charge_threshold    = charge_price_threshold    > 0 ? charge_price_threshold
                                                    : 谷段电价 + (峰段电价 − 谷段电价) × 0.30
discharge_threshold = discharge_price_threshold > 0 ? discharge_price_threshold
                                                    : 谷段电价 + (峰段电价 − 谷段电价) × 0.70
```

| 配置字段 / 参数 | 默认 | 取值 | 说明 |
|---|---|---|---|
| `charge_price_threshold` | **`0.0`** | 元/kWh | **显式充电阈值**；`0` = 未指定，按下方 `0.30` 比例推算 |
| `discharge_price_threshold` | **`0.0`** | 元/kWh | **显式放电阈值**；`0` = 未指定，按下方 `0.70` 比例推算 |
| 推算用充电比例 | `0.30` | `0–1` | 仅当 `charge_price_threshold == 0` 时生效 |
| 推算用放电比例 | `0.70` | `0–1` | 仅当 `discharge_price_threshold == 0` 时生效 |

**约束**：推算模式下必须满足 `0.30 < 0.70`（固定值，无配置项）；
显式模式下必须满足 `charge_threshold < discharge_threshold`，否则无有效动作区间 → 模型校验报错。
深谷时段（`deep_valley_price > 0`）时，推算区间改为 `deep_valley_price ↔ peak_price`，比例不变。

> **优先级**：显式阈值**永远优先**，不得被推算值覆盖（TIMESERIES_MODEL.md §4.5）。

### 4.2 逐步判定流程（V2 §13）

```text
步骤 1  取该小时电价 price(t)（TIMESERIES_MODEL.md §4.4 解析）
步骤 2  若 allow_arbitrage == false        → IDLE，原因 R21
步骤 3  若 price(t) ≤ charge_threshold
        3.1  若 soc(t) ≥ soc_max          → IDLE，原因 R04
        3.2  若 charge_from_grid == false 且 allow_grid_charge == false
                                           → IDLE，原因 R12
        3.3  否则 → CHARGE，能量 = max_charge_energy(t)
                   原因 R01（谷段）；若为深谷时段用原因 R20
步骤 4  若 price(t) ≥ discharge_threshold
        4.1  若 soc(t) ≤ soc_min          → IDLE，原因 R05
        4.2  若 load 已由光伏覆盖          → IDLE，原因 R10
        4.3  否则 → DISCHARGE，能量 = min(max_discharge_energy(t), load 缺额)
                   原因 R03
步骤 5  其它（阈值之间）                    → IDLE，原因 R02
步骤 6  对步骤 3.3 / 4.3 的结果施加第 2.4 节四个上限
        6.1  若被功率上限截断              → 原因追加 R06 / R07
```

### 4.3 充电来源优先级（V2 §13）

```text
充电来源优先级：光伏盈余 → 电网（仅在 charge_from_grid 且 allow_grid_charge 时）
即：grid_to_storage = max(指令充电量 − pv_to_storage, 0)   再受开关限制
```

---

## 5. 策略二：光伏自用优先 `PV_SELF_CONSUMPTION`（V2 §14）

**核心思想**：优先让光伏就地消纳，储能只做"光伏的搬运工"。
**明确不看电价**——这是它与经济优化的根本区别。

### 5.1 优先级链（V2 §14）

```text
光伏电量去向优先级：PV → 负荷 → 储能 → 上网
```

| 优先级 | 去向 | 判定 |
|---|---|---|
| 1 | `pv_to_load` | `min(pv_generation, load)` |
| 2 | `pv_to_storage` | 剩余光伏 → 储能（`charge_from_pv == true` 且 SOC 未满） |
| 3 | `pv_to_grid` | 剩余光伏 → 上网（受 `allow_export` 约束） |
| 4 | `pv_curtailed` | 仍然剩余 → 弃光（受 `allow_export == false` 或电网限发导致） |

### 5.2 逐步判定流程（V2 §14）

```text
步骤 1  pv_surplus = max(pv_generation − load, 0)
步骤 2  若 pv_surplus > 0
        2.1  若 charge_from_pv == false       → 跳过充电，进入步骤 2.3
        2.2  若 soc(t) ≥ soc_max              → 跳过充电，原因 R04；进入步骤 2.3
        2.3  若 soc(t) < soc_max 且 charge_from_pv
              → CHARGE，能量 = min(pv_surplus, max_charge_energy(t))
                原因 R08（光伏盈余且 SOC 未满）
        2.4  剩余 pv_surplus 若 allow_export  → 计入 pv_to_grid
             否则                             → 计入 pv_curtailed，原因 R18
步骤 3  若 pv_surplus == 0（光伏不足以覆盖负荷）
        3.1  若 allow_arbitrage == true 且 price(t) ≥ discharge_threshold 且 soc(t) > soc_min
              → DISCHARGE 补足负荷缺额，原因 R11（峰段缺额放电）
        3.2  否则 → IDLE，原因 R09（无光伏盈余）/ R10（不主动放电）
步骤 4  【关键】负荷不足时储能不主动放电
        —— 即使电价处于峰段，若"光伏已覆盖负荷"或"allow_arbitrage == false"，
           储能也不放电。原因 R10。这是"光伏自用优先"的定义性行为。
步骤 5  对结果施加第 2.4 节四个上限（截断时追加 R06 / R07）
```

### 5.3 与"不看电价"的一致性

**自检项**：本策略下，把逐时电价整体乘以任意正数常数，`DispatchDecision` 序列
（`action` 与能量）**必须完全不变**（除步骤 3.1 的峰段判定外）。
若变化，说明实现里混入了电价逻辑，属于**策略串味**，判为缺陷。

---

## 6. 策略三：经济优化 `ECONOMIC_OPTIMIZATION`（V2 §15）

**定位**：**规则型**（rule-based）经济优化——逐时比较"动作 vs 不动作"的即时净收益，
**不是**全局最优、**不是**线性规划。真正的 LP 寻优属于 [OPTIMIZATION.md](OPTIMIZATION.md) 第 6 节。

### 6.1 逐时净收益判据（V2 §15）

```text
充电收益  Benefit_charge(t)    = (price_future_discharge_est − price(t) / η_charge) × 可充电量
放电收益  Benefit_discharge(t) = (price(t) × η_discharge − price_charge_est) × 可放电量
```

其中"未来/已发生价格估计"采用**本日峰谷价格的保守估计**：

```text
price_future_discharge_est = 本日峰段电价（若无峰段则取本日最高电价）
price_charge_est           = 本日谷段电价（若无谷段则取本日最低电价）
```

### 6.2 逐步判定流程（V2 §15）

```text
步骤 1  计算当日价格统计（峰段价、谷段价、最高价、最低价）——按 timestamp 的日期分组
步骤 2  若 allow_arbitrage == false → IDLE，原因 R21
步骤 3  计算 Benefit_charge(t) 与 Benefit_discharge(t)
步骤 4  若 Benefit_charge(t) > 0 且 soc(t) < soc_max 且充电来源可用
        → CHARGE，原因 R17（附净收益金额）
步骤 5  若 Benefit_discharge(t) > 0 且 soc(t) > soc_min 且存在去向（负荷缺额或允许上网）
        → DISCHARGE，原因 R17（附净收益金额）
步骤 6  若两者均 ≤ 0 → IDLE，原因 R16（附净收益金额）
步骤 7  若两者均 > 0（价格区间异常） → 取收益较大者，并在原因中说明
步骤 8  对结果施加第 2.4 节四个上限（截断时追加 R06 / R07）
```

### 6.3 与 LP 的边界

| 维度 | 规则型经济优化（本文档） | LP 优化（OPTIMIZATION.md） |
|---|---|---|
| 视野 | 逐时（当日价格统计） | 全周期 |
| 最优性 | 不保证全局最优 | 保证（在给定约束下） |
| 依赖 | 无（仅 NumPy） | `scipy`（**可选依赖**） |
| 输出 | `DispatchDecision` 序列 | 容量/功率寻优结果 + 调度序列 |
| 本期状态 | ✅ 本期实现 | 🔶 可选，缺 `scipy` 时降级 |

---

## 7. `DispatchDecision` 原因枚举（V2 §16）

> **规范 §16**：每小时都必须能解释"为什么充电 / 放电 / 不动作"。
> 下表为**完整原因清单**，实现时**不得**出现表外原因码。

| 编号 | 触发条件 | `action` | `reason` 模板（中文，数值为示例） |
|---|---|---|---|
| **R01** | 峰谷套利：`price ≤ charge_threshold`、SOC 未满、可充电 | `CHARGE` | 谷段电价 0.32 ≤ 充电阈值 0.40，且 SOC 45.0% < 上限 90.0% |
| **R02** | 峰谷套利：电价处于两阈值之间 | `IDLE` | 平段电价 0.62 处于充电阈值 0.40 与放电阈值 0.90 之间，不动作 |
| **R03** | 峰谷套利：`price ≥ discharge_threshold`、SOC 未空、有去向 | `DISCHARGE` | 峰段电价 1.05 ≥ 放电阈值 0.90，且 SOC 80.0% > 下限 10.0% |
| **R04** | 达到 SOC 上限，不再充电 | `IDLE` | SOC 已达上限 90.0%，不再充电 |
| **R05** | 达到 SOC 下限，不再放电 | `IDLE` | SOC 已达下限 10.0%，不再放电 |
| **R06** | 充电被最大充电功率截断 | `CHARGE` | 光伏盈余 120.0 kWh，受最大充电功率 500.0 kW 限制，实际充电 500.0 kWh |
| **R07** | 放电被最大放电功率截断 | `DISCHARGE` | 放电指令 600.0 kWh，受最大放电功率 500.0 kW 限制，实际放电 500.0 kWh |
| **R08** | 光伏盈余且 SOC 未满（自用优先） | `CHARGE` | 光伏盈余 35.0 kWh 且 SOC 40.0% 未满，优先充入储能 |
| **R09** | 无光伏盈余（自用优先，非峰段） | `IDLE` | 无光伏盈余（光伏 0.0 ≤ 负荷 80.0 kWh），储能不充电 |
| **R10** | 负荷已覆盖 / 策略不允许，储能不主动放电 | `IDLE` | 光伏已覆盖负荷，储能不主动放电 |
| **R11** | 光伏缺额且处于峰段（自用优先的放电分支） | `DISCHARGE` | 峰段且光伏缺额 60.0 kWh，储能放电补足 |
| **R12** | 电网充电开关关闭 | `IDLE` | 电网充电开关已关闭（allow_grid_charge = false） |
| **R13** | 电网充电开启但价格不满足阈值 | `IDLE` | 谷段电价 0.32 > 充电阈值 0.30，不启动电网充电 |
| **R14** | 储能上网开关关闭，余电不放电上网 | `IDLE` | 储能上网开关已关闭（allow_export = false），余电不放电上网 |
| **R15** | 达到日内等效循环次数上限 | `IDLE` | 今日等效循环已达配置上限 1.00 次，不再动作 |
| **R16** | 经济优化：动作净收益 ≤ 0 | `IDLE` | 放电净收益 −0.05 元 ≤ 0，不动作 |
| **R17** | 经济优化：动作净收益 > 0 | `CHARGE` / `DISCHARGE` | 充电净收益 +12.30 元 > 0，执行充电 |
| **R18** | 光伏盈余但储能已满且禁止上网 | `IDLE` | 光伏盈余 20.0 kWh，储能已满且禁止上网，弃光 20.0 kWh |
| **R19** | 未配置储能（容量或功率为 0） | `IDLE` | 未配置储能（容量 0.0 kWh），不动作 |
| **R20** | 深谷时段优先充电 | `CHARGE` | 深谷电价 0.28 ≤ 充电阈值 0.40，且为当前最低电价时段 |
| **R21** | 套利开关关闭 | `IDLE` | 峰谷套利开关已关闭（allow_arbitrage = false） |

**双轨输出**（**已核实**）：`DispatchDecision` 同时给出两个字段——

| 字段 | 用途 | 示例 |
|---|---|---|
| `reason_code` | **机器可判**：供程序判别与测试断言 | `"R01"` |
| `reason` | **人可读**：供界面与报告展示 | `谷段电价 0.32 ≤ 充电阈值 0.40，且 SOC 45.0% < 上限 90.0%` |

测试应断言 `reason_code`，用户看 `reason`（V2 §16）。

**原因文本构造规则**：

```text
reason = 模板中的占位符替换为**该小时的实际数值**（保留 2 位小数）
       + 数值单位与精度遵循核心 §1（电量 kWh、电价 元/kWh、SOC 百分比 1 位小数）
```

**可测试性要求**（V2 §16）：测试必须能在**任意一小时**断言"原因文本包含可核对的数值"，
即：正则 `\d+\.\d+` 至少命中一次。

---

## 8. 开关与统计口径（V2 §19–§21）

### 8.1 三个开关（V2 §19）

| 开关 | 默认 | 作用 | 关闭时的行为 |
|---|---|---|---|
| `charge_from_pv` | `True` | 允许光伏给储能充电 | `pv_to_storage = 0`，盈余全部走向上网/弃光 |
| `charge_from_grid` | `False` | 允许电网给储能充电（**第一道开关**） | `grid_to_storage = 0` |
| `allow_grid_charge` | `False` | **电网充电总开关（第二道开关）** | `grid_to_storage = 0` |
| `allow_export` | `False` | 允许储能放电上网 | 放电只能供负荷；余电上网仅限光伏 |
| `allow_arbitrage` | `True` | 允许峰谷套利 | 策略 1、3 全部退化为 IDLE（R21） |

**双开关设计说明**：`charge_from_grid` 与 `allow_grid_charge` **必须同时为真**才允许电网充电。
前者表达"技术上允许"，后者表达"商务上允许"（如 EMC 合同约定不得从电网充电）。
两者为**与**关系，任一为假即禁止。

```text
grid_to_storage 生效条件 = charge_from_grid AND allow_grid_charge
storage_to_grid 生效条件 = allow_export
```

### 8.2 统计口径（V2 §20、§21）

| 统计项 | 计算式 | 归属 |
|---|---|---|
| 电网充电量 | `Σ_h grid_to_storage_h` | 计入 `grid_import`（是真实的购电量） |
| 光伏充电量 | `Σ_h pv_to_storage_h` | **不计入**光伏自用收益（V1 §47） |
| 储能上网量 | `Σ_h storage_to_grid_h` | 计入 `grid_export`；本期默认 0 |
| 储能放电量 | `Σ_h storage_discharge_h` | 供负荷部分计入 `load_from_storage` |
| 弃光电量 | `Σ_h pv_curtailed_h` | 单独统计，用于评估消纳瓶颈 |
| 电网购入总量 | `Σ_h grid_import_h` | `grid_to_load + grid_to_storage` |
| 上网总量 | `Σ_h grid_export_h` | `pv_to_grid + storage_to_grid` |

**两条必须保持的口径纪律**（V2 §21，与 V1 §47 同源）：

| # | 纪律 |
|---|---|
| C1 | **一度电只能产生一条收益**：`pv_to_storage` 的电量**不得**再计入光伏自用收益 |
| C2 | **储能上网与光伏上网分开统计**，但都计入 `grid_export`；报告中必须能区分二者 |

### 8.3 需量电费口径（V2 §21）

若 `TariffProfile.demand_charge > 0`：

```text
月度最大需量 = max(该月各小时 grid_import)          ← 逐时取最大值（kWh/h = kW）
月度需量电费 = 月度最大需量 × demand_charge
年需量电费   = Σ_月 月度需量电费
```

> **设计取舍**：这是**简化口径**（用小时均值近似需量），真实需量按 15 分钟滑差计。
> 必须在报告口径页披露（TIMESERIES_MODEL.md §10.4 V2-L5）。

---

## 9. 等效循环次数（V2 §24、§25）

### 9.1 配置值 vs 实际值

| 指标 | 来源 | 公式 |
|---|---|---|
| **配置循环次数** | `StorageConfig.annual_cycles`（V1 字段，默认 `330`） | 用户输入，用于 V1 年度模型 |
| **实际等效循环（按放电量）** | 时序结果 | `C_actual = Σ_h storage_discharge_h / E_capacity` |
| **实际等效循环（按吞吐量）** | 时序结果 | `C_throughput = Σ_h storage_discharge_h / (E_capacity × DoD)` |
| **实际运行天数** | 时序结果 | 实际发生了充电或放电的天数 |

**DoD 关系**（与 V1 一致，核心 §4.2 式 9、§41）：

```text
DoD_actual = (soc_max − soc_min)          ← V2 中 DoD 由 SOC 上下限决定，不再单独配置
```

### 9.2 配置值与实际值的差异披露（V2 §24）

```text
偏离率 = (C_actual − annual_cycles) / annual_cycles
```

| 偏离率 | 报告处理 |
|---|---|
| `\|偏离率\| ≤ 10%` | 无需特别提示 |
| `10% < \|偏离率\| ≤ 30%` | 在储能章节提示"实际循环次数与配置值偏差 XX%" |
| `\|偏离率\| > 30%` | **必须**在口径说明中显著提示，并给出可能原因（策略阈值设置不当 / SOC 区间过窄 / 负荷与光伏不匹配） |

**规范 §25 要求**：报告中**必须同时显示**配置值与实际值，**不得**只显示其中一个。

### 9.3 日内循环上限（V2 §24）

```text
日内等效循环 = Σ_{t∈当日} storage_discharge_t / E_capacity
若日内等效循环 ≥ max_daily_cycles（默认 1.0）→ 当日后续小时 IDLE，原因 R15
```

该限制用于抑制"一天内反复充放"的非物理行为。

---

## 10. 衰减与更换年份（V2 §26）

### 10.1 容量衰减（与 V1 一致）

```text
E_capacity(n) = E_capacity_initial × (1 − d_storage)^(n−1)        n = 1..N
```

| 参数 | 来源 | 默认 |
|---|---|---|
| `d_storage` | `StorageConfig.annual_degradation_rate` | `0.02`（2%/年） |
| `E_capacity_initial` | `StorageConfig.storage_energy_kwh` | 用户输入 |

### 10.2 更换年份复位（V1 §45、核心 L12）

```text
若 n == StorageConfig.replacement_year:
    E_capacity(n) = E_capacity_initial          ← 容量恢复至初始值
    并按 (1 − d_storage)^(n−1) 重新起算衰减
```

> **V1 现状**（核心 L12）："更换电芯当年容量**恢复至初始值**并重新衰减"。
> V2 **沿用同一口径**，不引入"新电芯更低衰减率"的假设；若要建模新电芯参数，
> 必须作为显式参数新增并登记 `TODO(V2+)`。

### 10.3 SOC 与衰减的交互

衰减只影响 `E_capacity`，**不影响** `soc_min` / `soc_max`：

```text
E_available(t, n) = (soc_t − soc_min) × E_capacity(n)
E_headroom(t, n)  = (soc_max − soc_t) × E_capacity(n)
```

即：容量衰减后，同样的 SOC 区间对应的**绝对可用电量变小**，套利空间随之下降。

### 10.4 衰减口径披露

| 编号 | 口径 | 披露要求 |
|---|---|---|
| D1 | 容量按年线性衰减 `(1−d)^(n−1)`，年内不衰减 | 报告口径页 |
| D2 | 更换年份容量复位 | 报告口径页 |
| D3 | 不建模循环寿命与温度对衰减的影响 | 报告口径页（V1 同源限制） |

---

## 11. 储能收益口径（V2 §29、§40）

### 11.1 逐时套利收益（V2 §29）

```text
storage_revenue = Σ_h (storage_discharge_h × avoided_price_h)
                − Σ_h (storage_charge_h    × charge_price_h)
```

| 符号 | 含义 | 取值 |
|---|---|---|
| `avoided_price_h` | 该小时放电替代的电价 | 优先 `StorageConfig.discharge_avoided_price`；否则取该小时 `electricity_price` |
| `charge_price_h` | 该小时充电的电价 | 优先 `StorageConfig.charge_price`；否则取该小时 `electricity_price` |

### 11.2 **明确禁止**的简化（V2 §29）

| # | 禁止写法 | 为什么错 |
|---|---|---|
| X1 | `storage_revenue = 放电量 × (峰电价 − 谷电价)` | 峰谷价差是**单一常数**，无法表达逐时电价、深谷时段、需量时段，且会把"跨时段充电"算错 |
| X2 | `storage_revenue = 放电量 × 峰电价`（不减充电成本） | 只算收入不算成本，虚高 |
| X3 | 用**年度平均电价**替代逐时电价 | 丢失时点价值，正是 V2 要解决的问题 |
| X4 | 把 `storage_revenue` 再作为 `net_energy_cost` 的减项 | **重复计算**（放电已通过减少 `grid_import` 体现，见 TIMESERIES_MODEL.md §6.1.2 E2） |

### 11.3 四类收益的分列要求（V1 §44 沿用）

V1 把储能收益分为 **套利 / 容量 / 辅助服务 / 其他**四类（核心 §4.3 式 17）。
V2 的逐时模型**只精细化"套利"这一类**，另外三类沿用 V1 的固定值口径：

| 收益类型 | V1 | V2 |
|---|---|---|
| 套利 | 年度 `放电×替代价 − 充电×充电价` | **逐时同式求和**（本文档 §11.1） |
| 容量 | 固定值 | 固定值（V2 §29 不变；逐年模型属 `TODO(V2+)`，核心 L6） |
| 辅助服务 | 固定值 | 固定值（同上） |
| 其他 | 固定值 | 固定值（同上） |

**报告中必须分开列示**，不得只给一个"储能总收益"。

### 11.4 需量节省的归属

需量电费下降（§8.3）**不计入** `storage_revenue`，而是体现在
`baseline_results` 的节省额中（TIMESERIES_MODEL.md §7.1）。
理由：需量节省是"负荷侧"的贡献，与储能套利是两条不同的收益路径，合并会掩盖分析。

---

## 12. 与 V1 的口径对照

| 口径项 | V1（核心 §4.2、§4.3、L11–L14） | V2（本文档） | 是否改变 |
|---|---|---|---|
| 时间粒度 | 年度等效循环 | 逐时 SOC 递推 | 🔶 细化（V1 保留） |
| 放电量 | `可用容量 × DoD × 循环次数 × η_dis` | 逐时 `Σ storage_discharge` | 🔶 细化 |
| 充电量 | `放电量 / η_dis / η_chg` | 逐时 `Σ storage_charge` | 🔶 细化 |
| 效率方向 | `E_dis = Avail × DoD × Cycles × η_dis`；`E_chg = E_dis / η_dis / η_chg` | 充电乘、放电除（§2.2） | ❌ **方向一致** |
| 往返效率 | `η_chg × η_dis = 0.88` | 同一换算 | ❌ 不变 |
| 套利口径 | `放电×替代价 − 充电×充电价`（L10 披露：全部充电量按充电价计价） | **逐时同式**；光伏充电量仍按充电价计价（同 L10 口径） | ❌ 口径一致 |
| 电网充电 | 只展示与校验，不进收益公式（L14） | 计入 `grid_import`（真实购电），并进 `storage_revenue` 的充电项 | 🔶 **V2 变化**（须披露） |
| 容量复位 | 更换年复位（L12） | 同 | ❌ 不变 |

> ⚠️ **V2 必须显式披露的一处口径变化**：V1 的 `grid_charge` **不进入收益公式**（L14），
> 而 V2 的 `grid_to_storage` **会**进入 `electricity_cost` 与 `storage_revenue`。
> 该差异必须写入 `CalculationResult.notes`，且在 V1→V2 迁移时提示
> （TIMESERIES_MODEL.md §9.2 M7）。这是**唯一**会改变储能收益数值的口径变化。

---

## 13. 公式—条款—模块对照表（V2 §71）

| # | 公式 / 规则（简写） | 条款 | 模块（V2 布局） |
|---|---|---|---|
| 1 | `E_usable = E_capacity × (soc_max − soc_min)` | V2 §10 | `calculation/storage_soc.py` |
| 2 | `E_available = (soc − soc_min) × E_capacity` | V2 §10 | `calculation/storage_soc.py` |
| 3 | `E_headroom = (soc_max − soc) × E_capacity` | V2 §10 | `calculation/storage_soc.py` |
| 4 | `E_inc = P_charge × η_charge × Δt` | V2 §11 | `calculation/storage_soc.py` |
| 5 | `E_dec = P_discharge / η_discharge × Δt` | V2 §11 | `calculation/storage_soc.py` |
| 6 | `soc_end = soc_start + (E_inc − E_dec) / E_capacity` | V2 §11 | `calculation/storage_soc.py` |
| 7 | `η_round_trip = η_charge × η_discharge = 0.88` | 核心 §4.2 式 8 | `calculation/storage_soc.py` |
| 8 | `soc_min ≤ soc ≤ soc_max` 硬约束 | V2 §11 | `calculation/storage_soc.py`、`data/validator.py` |
| 9 | `max_charge_energy` 四上限取小 | V2 §11 | `calculation/storage_soc.py` |
| 10 | `max_discharge_energy` 四上限取小 | V2 §11 | `calculation/storage_soc.py` |
| 11 | 首末 SOC 闭合（可选模式） | V2 §11、§40 | `calculation/storage_soc.py` |
| 12 | 三策略互斥选择 | V2 §12 | `calculation/dispatch_engine.py` |
| 13 | 峰谷套利阈值与六步判定 | V2 §13 | `calculation/dispatch_engine.py` |
| 14 | 光伏自用优先级链（负荷→储能→上网） | V2 §14 | `calculation/dispatch_engine.py` |
| 15 | 负荷不足时储能不主动放电 | V2 §14 | `calculation/dispatch_engine.py` |
| 16 | 经济优化逐时净收益判据 | V2 §15 | `calculation/dispatch_engine.py` |
| 17 | `DispatchDecision` 21 条原因枚举 | V2 §16 | `calculation/dispatch_engine.py` |
| 18 | 电网充电双开关（与关系） | V2 §19 | `calculation/dispatch_engine.py` |
| 19 | 储能上网开关 `allow_export` | V2 §19 | `calculation/dispatch_engine.py` |
| 20 | 电网充电量统计口径 | V2 §20 | `calculation/energy_balance.py` |
| 21 | 一度电一条收益（C1、C2） | V2 §21、V1 §47 | `calculation/dispatch_engine.py` |
| 22 | 需量电费按月最大需量 | V2 §21 | `calculation/tariff_series.py` |
| 23 | `C_actual = Σ放电 / E_capacity` | V2 §24 | `calculation/storage_soc.py` |
| 24 | 日内循环上限 → R15 | V2 §24 | `calculation/dispatch_engine.py` |
| 25 | 配置值与实际值同时显示 | V2 §25 | `reports/`（V2 报表扩展） |
| 26 | `E_capacity(n) = E_init × (1−d)^(n−1)` | V2 §26 | `calculation/storage_soc.py` |
| 27 | 更换年容量复位 | V2 §26、V1 §45 | `calculation/storage_soc.py` |
| 28 | `storage_revenue = Σ(放电×替代价 − 充电×充电价)` | V2 §29 | `calculation/economic_v2.py` |
| 29 | 禁止简化为"放电量 × 峰谷价差" | V2 §29 | `calculation/economic_v2.py`（审查项） |
| 30 | 储能四类收益分列 | V1 §44 | `calculation/economic_v2.py` |
| 31 | B3 储能守恒残差 ≤ 1e-6 | V2 §95 | `calculation/energy_balance.py` |

---

## 14. 变更纪律

1. 改**效率方向**（§2.2）→ 属破坏性变更，必须同步改本文档、`notes`、测试，
   并在 [CHANGELOG.md](CHANGELOG.md) 的 `Changed` 段登记。
2. 改**策略判定流程** → 必须同步更新本文档第 4–6 节的步骤编号与原因枚举（第 7 节）。
3. **新增原因码** → 必须顺延编号（R22 起），**禁止**复用或删除既有编号（报表已引用）。
4. 改**收益口径** → 必须同步改第 11 节、[TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §6.1.2 与测试。
5. **V1 兼容性红线**：不得改变 V1 年度储能模型（核心 §4.2、L11–L14）的既有结果。
6. 本文档与 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) 的字段定义冲突时，以该文档为准；
   算法流程冲突时以本文档为准。
