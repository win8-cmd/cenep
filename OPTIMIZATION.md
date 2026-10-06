# OPTIMIZATION.md —— 优化与方案／参数扫描

> **当前状态**：**已交付（V2.0.0）**。本文档描述 CENEP V2 的三个优化器、
> 方案扫描与参数扫描机制，对应模块 `src/cenep/optimization/rule_based.py`、
> `greedy_optimizer.py`、`lp_optimizer.py` 与 `src/cenep/calculation/scenario_engine.py`
> **均已创建并通过测试**（`tests/test_optimization.py` 30 项、`tests/test_scenario_engine.py` 45 项）。
> 全文的「设计」「本期实现」现读作"**规范设计要求，已按此实现**"。
>
> **优先级**：受 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) 约束；
> 调度与 SOC 的算法细节以 [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) 为准，
> 时序模型与字段以 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) 为准。
>
> **记号**：`V2 §N` = V2 总规范第 N 条；`V1 §N` = V1 产品规范第 N 条；
> 【设计】= 规范设计要求（**已实现**）；【V1 沿用】= V1 已有实现。
>
> **适用范围**：工商业分布式光伏 / 工商业储能 / 工商业光储三类项目的
> **容量配置寻优**与**敏感性/情景扩展扫描**（V2 §44）。

---

## 1. 范围与设计原则（V2 §44）

### 1.1 优化要回答的问题

| # | 问题 | 输出 |
|---|---|---|
| 1 | 屋顶这么大，装多少 kWp 最划算？ | 最优光伏容量 |
| 2 | 要不要配储能？配多大容量、多大功率？ | 最优储能容量 / 功率 |
| 3 | 哪些参数一变，结论就翻？ | 参数扫描的敏感度排序 |
| 4 | **为什么**这个方案最优？ | 完整可解释过程（第 9 节） |

### 1.2 六条设计原则

| # | 原则 | 说明 |
|---|---|---|
| O1 | **不是黑盒** | 必须输出「输入参数 → 候选方案 → 约束条件 → 计算结果 → 最优方案」（V2 §48） |
| O2 | **复用唯一计算引擎** | 每个候选方案都走 `calculation/` 的同一套公式；优化器只负责**生成候选**与**比较** |
| O3 | **不改变 V1 结果** | 优化是外层循环，不得修改 V1 年度模型的任何公式 |
| O4 | **失败要降级，不要静默** | 优化器不可用时明确降级到下一级并记录原因（§2.2） |
| O5 | **可复现** | 同一输入、同一候选集，输出必须逐位一致（V1 §118 同源） |
| O6 | **不输出可行性结论** | 只输出指标与最优方案，**不得**输出"项目可行/不可行"（V1 §106） |

---

## 2. 三个优化器的分工与优先级（V2 §45）

### 2.1 分工矩阵

| 优化器 | 模块 | 方法 | 最优性 | 依赖 | 本期状态 |
|---|---|---|---|---|---|
| **规则型** `RuleBasedOptimizer` | `optimization/rule_based.py` | 按规则直接给出容量建议（面积上限 / 负荷匹配 / 经验配比） | 启发式 | 无 | ✅ 本期实现（**默认**） |
| **贪心经济优化** `GreedyEconomicOptimizer` | `optimization/greedy_optimizer.py` | 在候选网格上逐点评估，取最优 | 网格内最优 | 无 | ✅ 本期实现 |
| **线性规划** `LinearProgrammingOptimizer` | `optimization/lp_optimizer.py` | 连续变量 LP / MILP | 全局最优（凸松弛下） | **`scipy`（可选）** | 🔶 可选，缺依赖时降级 |

### 2.2 回退链（V2 §45）

```text
优先级（高 → 低）：RuleBasedOptimizer  →  GreedyEconomicOptimizer  →  LinearProgrammingOptimizer
```

> ⚠️ **注意方向**：这里的"优先级"指**执行顺序与稳定性优先级**——**先跑规则型**给出基准方案，
> 再用贪心在网格上搜优；LP 仅在用户显式选择且 `scipy` 可用时作为**精修**。
> 默认只跑前两级；**LP 永不被隐式调用**。

```text
optimize(project, objective):
    result_rule = RuleBasedOptimizer().run(project)              # 始终执行
    if not greedy_enabled:
        return result_rule
    result_greedy = GreedyEconomicOptimizer().run(project, seed=result_rule.best)
    if not lp_enabled:
        return better_of(result_rule, result_greedy)
    if scipy is None:
        record_degradation("线性规划不可用：未安装 scipy，已降级到贪心经济优化")   # 不静默
        return better_of(result_rule, result_greedy)
    result_lp = LinearProgrammingOptimizer().run(project, seed=result_greedy.best)
    return better_of(result_rule, result_greedy, result_lp)
```

**降级必须留痕**（原则 O4）：

| 降级原因 | 记录位置 | 文案示例 |
|---|---|---|
| 未安装 `scipy` | `OptimizationResult.explanation` 开头 + `notes` | 「线性规划不可用：未安装 scipy，已降级到贪心经济优化」 |
| 候选方案全部不可行 | 同上 | 「候选方案全部违反约束，已降级到规则型建议」 |
| LP 未在迭代上限内收敛 | 同上 | 「线性规划未在 500 次迭代内收敛，已降级到贪心结果」 |
| 某候选不可行 | `OptimizationCandidate.note` | 「C1：第 4128 小时 SOC 1.02 > 上限 1.00」 |

### 2.3 选择规则

| 用户意图 | 推荐优化器 | 理由 |
|---|---|---|
| 只要一个"合理"的容量建议 | 规则型 | 秒出，可解释 |
| 要"在给定候选里最优" | 贪心经济优化 | 网格内严格最优，无依赖 |
| 要连续最优解、且已装 `scipy` | 线性规划 | 解质量最高 |
| 做敏感性/情景报告 | **不使用优化器** | 用 `scenario_engine.py` 的参数扫描（第 8 节） |

---

## 3. 目标函数与约束（V2 §12–§16）

### 3.1 目标函数（V2 §12）

```text
min  F = 购电成本 − 上网收入 + 储能运行成本

其中（全周期，逐时求和后再按 V1 折现口径汇总）：
    购电成本     = Σ_h (grid_import_h × electricity_price_h)          ← TIMESERIES_MODEL.md §6.1 第 17 行
    上网收入     = Σ_h (grid_export_h × export_price_h)               ← 第 18 行
    储能运行成本 = Σ_h (storage_discharge_h × c_op) + 储能年运维分摊   ← 见下方说明
```

| 项 | 符号 | 取值 | 说明 |
|---|---|---|---|
| 储能度电运行成本 | `c_op` | 默认 `0.0` 元/kWh | 若未配置则不计；配置后计入目标函数 |
| 储能年运维分摊 | — | `OpexConfig.storage_opex`（V1 字段） | 按年计入，再折算到逐时 |

> **与 V1 的关系**：V1 的持仓成本（投资、折旧、税、融资）**不进**目标函数中的"运行成本"，
> 而是通过 `objective` 的**评价指标**（NPV / IRR 等）体现。目标函数是**运行层**的，
> 指标是**投资层**的——两者不可混淆。

### 3.2 约束条件（V2 §13、§14）

```text
C1  SOC 上下限：        soc_min ≤ soc_t ≤ soc_max                    ∀t
C2  充电功率上限：      0 ≤ storage_charge_t      ≤ max_charge_energy(t)      ∀t
C3  放电功率上限：      0 ≤ storage_discharge_t   ≤ max_discharge_energy(t)   ∀t
C4  能量平衡 = 0：      pv_residual_t = 0 且 load_residual_t = 0 且 soc_residual_t = 0   ∀t
C5  容量非负：          PVCapacity ≥ 0、StorageEnergy ≥ 0、StoragePower ≥ 0
C6  容量上限：          PVCapacity ≤ 屋顶可装上限（由面积换算）
C7  策略一致性：        同一方案只跑一种 DispatchStrategy
```

**C4 的严格性**（V2 §95）：能量平衡残差容差 `1e-6`；违反即该候选方案**判为不可行**，
不得带着残差进入比较（[TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §10.1）。

### 3.3 六种优化目标（V2 §14）

| # | 枚举值（`OptimizationObjective`） | 中文名 | 优化方向 | 评价指标来源 |
|---|---|---|---|---|
| 1 | `MAX_NPV` | **最大化项目 NPV** | **默认** | 核心 §4.9 式 46 |
| 2 | `MAX_IRR` | 最大化项目 IRR | 最大 | 核心 §4.9 式 45 |
| 3 | `MIN_PAYBACK` | 最小化静态回收期 | 最小 | 核心 §4.9 式 47 |
| 4 | `MIN_LCOE` | 最小化 LCOE | 最小 | 核心 §4.9 式 49 |
| 5 | `MIN_LCOS` | 最小化 LCOS | 最小 | 核心 §4.9 式 50 |
| 6 | `MIN_ANNUAL_COST` | 最小化年购电成本 | 最小 | 本文档 §3.1 目标函数 |

> **已核实**：`domain/enums.py:OptimizationObjective` 共 **6 个取值**，与上表逐一对应；
> `OptimizationResult.objective` 为该枚举的字符串值，**默认 `MAX_NPV`**。

**并列打破规则**（确定性要求，原则 O5）：

```text
指标完全相同 → 依次比较：① 总投资较小者  ② 储能容量较小者  ③ 光伏容量较小者  ④ 候选编号较小者
```

> 该规则保证**同一输入必然得到同一结果**，不受候选遍历顺序影响（V1 §118 同源要求）。

---

## 4. 规则型优化 `RuleBasedOptimizer`（V2 §46）

**定位**：不做搜索，直接依据**工程规则**给出推荐容量。秒级返回，用于给搜索提供起点。

### 4.1 规则清单

| # | 规则 | 公式 | 适用 |
|---|---|---|---|
| R1 | 屋顶可装上限 | `PV_max = 可利用屋顶面积 / 单位容量占用面积`（核心 §4.1 式 1） | 全部 |
| R2 | 负荷匹配（自用率目标） | `PV_rec = 年用电量 × 目标自用率 / 年等效利用小时` | 全部 |
| R3 | 变压器容量约束 | `PV_rec ≤ 变压器容量 × 允许渗透率` | 全部 |
| R4 | 逆变器容配比 | `AC = PV_dc / 容配比`（默认 `1.1~1.2`） | 全部 |
| R5 | 储能容量（负荷侧） | `E_rec = 典型日峰段用电量 × 目标削峰比例` | 储能 / 光储 |
| R6 | 储能功率 | `P_rec = E_rec / 目标时长`（默认 `2 h`） | 储能 / 光储 |
| R7 | 储能时长下限 | `P_rec ≥ E_rec / 4 h`（避免超长时储能） | 储能 / 光储 |

```text
最终建议：PV_rec  = min(R1 上限, R2 负荷匹配, R3 变压器约束)
          E_rec   = R5，P_rec = max(R6, R7)
```

### 4.2 输出

```text
RuleBasedResult = {
  "pv_capacity_kwp":  float,
  "storage_energy_kwh": float,
  "storage_power_kw":  float,
  "rule_trace": [
      { "rule": "R1", "formula": "屋顶可装上限", "value": 2061.8, "unit": "kWp",
        "inputs": { "可利用屋顶面积": 12370.8, "单位容量占用面积": 6.0 } },
      ...
  ],
  "binding_rule": "R2",         # 哪个规则起了决定作用
}
```

`rule_trace` 是**可解释性要求**的一部分（第 9 节）：必须给出每条规则的**输入值、公式、结果**。

---

## 5. 贪心经济优化 `GreedyEconomicOptimizer`（V2 §46）

**定位**：在候选网格上**逐点评估**，取目标函数最优者。网格内严格最优，无外部依赖。

### 5.1 算法

```text
输入：候选集合 C（由第 7 节的四个维度做笛卡尔积生成，可去重、可剪枝）
      objective（第 3.3 节，默认 MAX_NPV）
      seed（来自 RuleBasedOptimizer 的建议，用于排序剪枝）

步骤 1  对每个候选 c ∈ C：跑一次完整 V2 时序计算 → 得到指标向量
步骤 2  过滤不可行候选（违反 C1–C7 任一）→ 记入 infeasible 列表与原因
步骤 3  在可行候选上按 objective 排序
步骤 4  应用并列打破规则（§3.3）→ 得到唯一最优
步骤 5  输出：最优候选 + 全部候选的指标表 + 不可行候选及原因
```

### 5.2 加速策略（不改变结果）

| # | 策略 | 说明 | 是否影响最优性 |
|---|---|---|---|
| G1 | 按 `seed` 距离排序遍历 | 尽早遇到好解，便于提前终止 | ❌ 不影响（除非启用 G3） |
| G2 | 缓存同参数计算结果 | 候选去重后只算一次 | ❌ 不影响 |
| G3 | 提前终止（可选） | 连续 `K` 个候选未改善即停止 | ⚠️ **是近似**，须在输出中标注 `early_stopped: true` |

**默认不启用 G3**（保证网格内严格最优）。启用时必须在 `optimization_results` 中显式标注。

### 5.3 计算量预估

```text
候选数 N_c = |PV| × |E| × |P| × |价格情景|      ← 第 7 节四个维度
总计算量   = N_c × 单方案时序耗时
```

以第 7 节的候选值全组合为例：`6 × 5 × 4 × 1 = 120` 个候选，
单方案 ≤ 0.5 s（[TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §3.4）→ **≤ 60 s**。
超过 200 个候选时必须提示用户并建议启用剪枝。

---

## 6. 线性规划 `LinearProgrammingOptimizer`（V2 §47）

### 6.1 定位与可选性

| 项 | 说明 |
|---|---|
| 依赖 | **`scipy`（可选依赖）**；未安装时**降级**到贪心，并留痕（§2.2） |
| 变量 | 容量变量（连续）+ 逐时充放电变量 |
| 最优性 | 在给定的**线性化假设**下为全局最优 |
| 本期状态 | 🔶 可选实现 |

### 6.2 变量与目标

```text
决策变量：
    x_pv      : 光伏容量（kWp，连续，0 ≤ x_pv ≤ PV_max）
    x_e       : 储能容量（kWh，连续，0 ≤ x_e ≤ E_max）
    x_p       : 储能功率（kW，连续，0 ≤ x_p ≤ P_max）
    chg_t     : 第 t 小时充电量（kWh，连续，≥0）                t = 1..T
    dis_t     : 第 t 小时放电量（kWh，连续，≥0）                t = 1..T
    soc_t     : 第 t 小时末 SOC（kWh 绝对量，连续）              t = 1..T

目标函数（线性）：
    min  Σ_t (grid_import_t × price_t) − Σ_t (grid_export_t × export_price_t)
         + Σ_t (dis_t × c_op)
```

### 6.3 约束（线性化）

```text
L1  SOC 递推：      soc_t = soc_{t−1} + chg_t × η_charge − dis_t / η_discharge
L2  SOC 上下限：     soc_min × x_e ≤ soc_t ≤ soc_max × x_e
L3  功率上限：       chg_t ≤ x_p × Δt ；dis_t ≤ x_p × Δt
L4  能量平衡：       pv_t + dis_t + grid_import_t = load_t + chg_t + grid_export_t
L5  上网限制：       grid_export_t ≤ pv_t + dis_t × allow_export
L6  容量上限：       x_pv ≤ PV_max ；x_e ≤ E_max ；x_p ≤ P_max
L7  非负：           全部变量 ≥ 0
```

> **线性化注意事项**：`chg_t × η_charge` 与 `dis_t / η_discharge` 均为**常数系数**乘积，
> 保持线性；**禁止**引入"效率依赖于 SOC"的非线性项（若要做，必须转 MILP 并单独登记 `TODO(V2+)`）。

### 6.4 收敛与失败处理

| 情形 | 处理 |
|---|---|
| 求解成功 | 输出连续解，并按"就近取整"映射回候选网格，用于与贪心结果**同口径比较** |
| 未收敛 | 记降级原因，返回贪心结果 |
| 无可行解 | 记录不可行原因（哪条约束冲突），返回贪心结果 |
| 未安装 `scipy` | 记降级原因（§2.2），返回贪心结果 |

---

## 7. 方案扫描：四个维度（V2 §46、§48）

**方案扫描**回答"容量组合"问题，**四个维度**与规范给定的候选值如下（**候选值为规范给定，不得自行改动**）：

| # | 维度 | 参数路径 | **候选值** | 个数 |
|---|---|---|---|---|
| 1 | **光伏容量** | `pv.pv_capacity_kwp` | **500 / 750 / 1000 / 1250 / 1500 / 2000** kWp | 6 |
| 2 | **储能容量** | `storage.storage_energy_kwh` | **0 / 500 / 1000 / 1500 / 2000** kWh | 5 |
| 3 | **储能功率** | `storage.storage_power_kw` | **250 / 500 / 750 / 1000** kW | 4 |
| 4 | **电价与峰谷价差** | `tariff.*` | 见 §7.2 | 1（基准）+ 情景 |

### 7.1 组合规则

```text
候选总数（未剪枝）= 6 × 5 × 4 × 1 = 120

剪枝规则（默认启用，必须在输出中声明）：
    PR1  储能容量 = 0 时，储能功率必须为 0（避免"有功率无容量"的无效组合）
    PR2  储能时长 P/E 必须落在 [0.5 h, 4 h]，否则剔除
    PR3  光伏容量超过屋顶可装上限 → 剔除并记录原因
    PR4  （可选）仅保留 E ∈ {0, 500, 1000, 2000}，剔除 1500（当 E_max < 1500）
```

### 7.2 第 4 维度：电价与峰谷价差

| 情景 | 电价处理 | 用途 |
|---|---|---|
| 基准 | 用户输入的 `TariffProfile` 原值 | 默认 |
| 峰谷价差收窄 | `peak_price` 下调、`valley_price` 上调，价差 × `0.7` | 政策收紧测试 |
| 峰谷价差扩大 | 价差 × `1.3` | 储能收益上行情景 |
| 无峰谷价差 | 全部时段取 `flat_price` | 储能套利归零的边界测试 |

```text
价差情景的施加方式：保持 flat_price 不变，按比例调整 peak/valley
    peak'    = flat + (peak − flat) × k
    valley'  = flat − (flat − valley) × k        k ∈ {0.7, 1.0, 1.3, 0}
```

> **禁止**直接给 `peak_price` 乘系数——那会同时改变电价水平与价差，无法分离两者影响。

### 7.3 输出：`ScenarioResult`（20 字段，**已核实**）

方案扫描的每个候选产出一条 `ScenarioResult`：

| 分组 | 字段 | 含义 |
|---|---|---|
| 标识 | `name` | 候选标识（如 `"PV1000_E500_P250"`，编码四个维度取值） |
| | `label` | 中文显示名 |
| | `is_baseline` | 是否为基准方案 |
| 容量 | `pv_capacity_kwp` / `storage_power_kw` / `storage_energy_kwh` | 三个容量维度取值 |
| 投资 | `total_capex` | 该候选的总投资 |
| 指标 | `project_irr` / `equity_irr` / `project_npv` | 两套 IRR 与项目 NPV |
| | `static_payback` / `discounted_payback` | 静态 / 动态回收期 |
| | `lcoe` / `lcos` | 光伏与储能度电成本 |
| 运行 | `annual_saving` | 年节省额 |
| | `self_consumption_rate` / `self_sufficiency_rate` | 自用率 / 自给率 |
| | `equivalent_cycles` | 实际等效循环次数 |
| | `demand_saving` | 需量节省 |
| 明细 | `metrics` | 完整指标字典（供报表与二次分析） |

**每个候选都必须可追溯**（原则 O1）：`name` 编码四个维度的取值，便于人工核对。

### 7.4 输出：`OptimizationCandidate`（7 字段，**已核实**）

优化器内部逐个评估的候选记录：

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` | `str` | 候选唯一编号（与 `ScenarioResult.name` 对应） |
| `variables` | `dict` | 该候选的参数取值（扫描变量 → 值） |
| `objective` | `str` | 本次优化目标 |
| `objective_value` | `float` | 目标函数取值 |
| `feasible` | `bool` | 是否满足全部约束（C1–C7） |
| `note` | `str` | 不可行原因 / 备注（**中文**） |
| `scenario` | `ScenarioResult \| None` | 对应的完整方案结果 |

---

## 8. 参数扫描：8 个维度（V2 §48）

**参数扫描**回答"结论稳不稳"问题，在**固定容量方案**上逐一改变参数。

**8 个维度由枚举 `ScanVariable` 定义（已核实，共 8 个取值）**：

| # | 枚举值 | 中文名 | 参数路径 | 扫描范围（默认） | 步长 |
|---|---|---|---|---|---|
| 1 | `PV_CAPACITY` | 光伏容量 | `pv.pv_capacity_kwp` | `±20%` | `10%` |
| 2 | `STORAGE_CAPACITY` | 储能容量 | `storage.storage_energy_kwh` | `±20%` | `10%` |
| 3 | `STORAGE_POWER` | 储能功率 | `storage.storage_power_kw` | `±20%` | `10%` |
| 4 | `STORAGE_PRICE` | 储能单价 | `investment.storage_capex_per_kwh` | `±20%` | `10%` |
| 5 | `TARIFF` | 电价水平 | `tariff.*`（整体） | `−20% ~ +20%` | `10%` |
| 6 | `PEAK_VALLEY_SPREAD` | 峰谷价差 | `peak/valley`（按 §7.2 施加） | `k ∈ {0.7, 0.85, 1.0, 1.15, 1.3}` | — |
| 7 | `LOAD` | 负荷水平 | `load.*`（年用电量） | `−20% ~ +20%` | `10%` |
| 8 | `CAPEX` | 投资单价 | `investment.pv_capex_per_kw` | `−20% ~ +20%` | `10%` |

> ⚠️ **与早期设计的差异（已核实并修正）**：本表按 `ScanVariable` **实际取值**给出——
> 第 4 项为**储能单价**（`STORAGE_PRICE`）而非"储能容量"的重复；
> **不包括折现率**（折现率属于敏感性分析侧，见 §8.1）；共 8 个，与规范"8 个维度"一致。

### 8.1 与 V1 敏感性分析的关系（**已核实：两套独立机制**）

V2 中并存**两套**单变量机制，均由枚举定义、互不替代：

| 机制 | 枚举 | 取值数 | 取值 | 用途 |
|---|---|---|---|---|
| **敏感性分析** | `SensitivityVariable` | **8** | `CAPEX`、`ELECTRICITY_PRICE`、`GENERATION`、`OPEX`、`SELF_CONSUMPTION_RATIO`、`STORAGE_CYCLES`、`STORAGE_CAPEX`、`INTEREST_RATE` | 回答"结论稳不稳"（含融资与循环次数） |
| **参数扫描** | `ScanVariable` | **8** | `PV_CAPACITY`、`STORAGE_CAPACITY`、`STORAGE_POWER`、`STORAGE_PRICE`、`TARIFF`、`PEAK_VALLEY_SPREAD`、`LOAD`、`CAPEX` | 回答"容量相关的哪些参数最敏感" |

| 项 | V1（【V1 沿用】） | V2 |
|---|---|---|
| 模块 | `calculation/sensitivity.py` | `calculation/scenario_engine.py`（新增） |
| 变量数 | 5 个（总投资 / 电价 / 发电量 / 运维 / 自用比例） | `SensitivityVariable` 扩为 **8**（新增储能循环、储能单价、利率） |
| 结果对象 | `CalculationResult.sensitivity`（`list[SensitivityRow]`） | 保留；另新增 `CalculationResult.scenario_results`（`list[ScenarioResult]`） |
| 是否覆盖 V1 | V1 的 5 个变量必须**继续可用** | 是**超集**（新增 3 个，不改既有 5 个） |

**V1 的 `SensitivityRow` 结构（9 字段，继续沿用）**：

```text
variable / variable_label / change / project_irr / equity_irr /
project_npv / static_payback / irr_change / coefficient
```

> **V1 §95 红线**：敏感性分析与参数扫描都**一次只改变一个参数**，其余保持基准值。
> V2 不得把"多参数同时变动"混入其中——那属于方案扫描（第 7 节）或独立的多因素分析。

### 8.2 输出结构

```text
参数扫描行 = {
  "variable": "PV_CAPACITY",
  "variable_label": "光伏容量",
  "change": -0.10,
  "project_irr": 0.0974, "project_npv": 744486.0, "static_payback": 9.12,
  "irr_change": -0.116,      # 相对基准的变化率
  "coefficient": 1.16,        # 敏感度系数 = irr_change / change
  "elasticity_rank": 2,       # 敏感度排序（1 = 最敏感）
}
```

`elasticity_rank` 用于直接回答"哪些参数一变结论就翻"。

---

## 9. 禁止黑盒：可解释输出契约（V2 §48）

> **规范 §48 的核心要求**：用户要能知道**为什么该方案最优**。
> 因此优化结果**必须**按下列五段式输出，缺一段即判为不合规。

```text
输入参数            ① 本次优化用到的全部参数（含来源类型与单位）
    ↓
候选方案            ② 候选集合的生成规则与完整清单（含被剪枝的及原因）
    ↓
约束条件            ③ 生效的约束清单（C1–C7）与各候选的违反情况
    ↓
计算结果            ④ 每个可行候选的完整指标向量
    ↓
最优方案            ⑤ 最优候选 + 选出它的判据 + 与次优的差距
```

### 9.1 `OptimizationResult`（9 字段，**已核实**）与五段式的对应

实际模型字段与五段式的**逐段对应关系**如下（**不得缺段**）：

| 五段式 | `OptimizationResult` 字段 | 类型 | 说明 |
|---|---|---|---|
| ① **输入参数** | `scan_variables` | `list` | 本次扫描/寻优的变量定义（路径、候选值、单位） |
| ③ **约束条件** | `constraints` | `list` | 生效的约束清单（C1–C7 的可读描述） |
| ② **候选方案** | `candidates` | `list[OptimizationCandidate]` | 每个候选含 `variables`（取值）、`feasible`（是否可行）、`note`（不可行原因，中文） |
| ④ **计算结果** | `candidates[].objective_value` | `float` | 目标函数取值 |
| | `candidates[].scenario` | `ScenarioResult \| None` | 该候选的完整方案结果（20 字段，含全部指标） |
| ⑤ **最优方案** | `best_run_id` | `str` | 最优候选编号 |
| | `best_variables` | `dict` | 最优候选的参数取值 |
| | `best_scenario` | `ScenarioResult \| None` | 最优候选的完整结果 |
| | `explanation` | `str` | **为什么最优**（中文，必须含数值比较） |
| 过程元数据 | `objective` | `str` | 本次优化目标 |
| | `elapsed_seconds` | `float` | 耗时（满足第 10 节 P3/P4 的可核验性） |

```text
OptimizationResult
├── objective          : str                 优化目标（默认 "MAX_NPV"）
├── scan_variables     : list                ① 输入参数
├── constraints        : list                ③ 约束条件
├── candidates         : list[OptimizationCandidate]
│      └── run_id / variables / objective / objective_value / feasible / note / scenario
│                                            ② 候选方案 + ④ 计算结果
├── best_run_id        : str                 ┐
├── best_variables     : dict                │ ⑤ 最优方案
├── best_scenario      : ScenarioResult|None │
├── explanation        : str                 ┘
└── elapsed_seconds    : float
```

**`explanation` 的强制内容**（规范 §48「用户要能知道为什么该方案最优」）：

```text
explanation = "在 <N_feasible> 个可行候选中，<objective 中文名> 最优的是 <best_run_id>"
            + "（<关键指标> = <数值>），较次优方案 <second_run_id> 高/低 <差额>（<百分比>）"
            + （若施加了并列打破规则）"；存在指标并列，已按＜总投资较小者＞规则选定"
```

示例：

```text
在 66 个可行候选中，项目 NPV 最高的是 PV1000_E500_P250（NPV = 1,196,357.86 元），
较次优方案 PV1000_E1000_P500 高 184,223.86 元（+18.2%）。
```

### 9.1.1 降级信息的承载

`OptimizationResult` **没有**独立的 `degradation` 字段，降级原因按下列约定承载：

| 情形 | 承载位置 |
|---|---|
| 优化器降级（如未装 `scipy`） | `explanation` 开头追加中文说明 |
| 候选层面的不可行 | `OptimizationCandidate.note`（逐条） |
| 需要全局提示的降级 | 同时写入 `CalculationResult.notes`（口径说明） |

> **纪律**：降级**必须**在上述至少一处可见，禁止静默降级（原则 O4）。

### 9.2 可解释性的三条硬性纪律

| # | 纪律 | 检查方式 |
|---|---|---|
| X1 | **必须**给出 `explanation`，且其中**至少含一个数值比较** | 正则 `\d` 命中，且含比较词（"较…高/低"） |
| X2 | **必须**保留不可行候选及其原因 | `candidates` 中 `feasible == false` 的条目，`note` 非空且为中文 |
| X3 | **禁止**只输出 `best_scenario` 而省略 `candidates` | `candidates` 为空或仅 1 条 判为黑盒 |

### 9.3 报告中的呈现

优化结果在报告中的呈现顺序**必须**与第 9 节的五段式一致，
**不得**只放"最优方案"一张表（V2 §48）。

---

## 10. 性能与内存约束（V2 §86）

| # | 约束 | 目标值 |
|---|---|---|
| P1 | 单候选方案时序计算 | ≤ 0.5 s |
| P2 | 引擎内部**必须**用 NumPy 列式数组，禁止逐时 Python 对象 | 见 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §3.2 |
| P3 | 方案扫描（120 候选） | ≤ 60 s |
| P4 | 参数扫描（8 维度 × 5 档 = 40 次重算） | ≤ 20 s |
| P5 | 优化过程峰值内存 | ≤ 512 MB |
| P6 | 候选间**不得**持有全量逐时行对象 | 只保留指标向量；逐时明细按需重算 |

```text
内存预算校验（设计目标）：
    单方案逐时数组            约 1.4 MB（8760 × 20 列 × 8 B）
    同时驻留方案数            1（串行评估）
    指标缓存（120 候选）      < 0.1 MB
    → 峰值内存主要来自 Qt 界面与报表，优化本身 < 50 MB
```

**并行化说明**：本期**不引入**多进程并行（避免 Windows 打包下的 `multiprocessing`
复杂性与结果不确定性）。若后续引入，必须保证**结果与串行逐位一致**（原则 O5）。

---

## 11. 与 V1 的关系

| 项 | V1（【V1 沿用】） | V2 | 是否改变 V1 |
|---|---|---|---|
| 情景分析 | `calculation/scenario.py`，保守/基准/乐观三情景 | 保留；新增容量方案扫描 | ❌ 不变 |
| 敏感性分析 | `sensitivity.py`，5 个变量 | 保留；新增 8 维度参数扫描 | ❌ 不变 |
| 优化 | **不做** | 新增三个优化器 | ❌ 不变（纯新增） |
| 结果对象 | `CalculationResult.scenarios` / `.sensitivity` | 新增 `.scenario_results` / `.optimization_results` | ❌ 不变（只增字段） |
| 可行性判定 | 不输出（V1 §106） | **同样不输出**（原则 O6） | ❌ 不变 |

**V1 兼容性红线**：优化器**只允许**通过 `apply_variable` / `apply_delta` 一类**参数施加**
函数构造候选，**禁止**在优化器内部直接改写公式或调用 `calculation/` 之外的算术。

---

## 12. 公式—条款—模块对照表

| # | 内容（简写） | 条款 | 模块（V2 布局） |
|---|---|---|---|
| 1 | 目标函数 `min(购电成本 − 上网收入 + 储能运行成本)` | V2 §12 | `optimization/greedy_optimizer.py` |
| 2 | 约束 C1–C7 | V2 §13、§14 | `calculation/energy_balance.py`、`optimization/*` |
| 3 | 优化目标枚举（6 种，默认 `MAX_NPV`） | V2 §14 | `optimization/*`、`domain/enums.py`（V2 扩展） |
| 4 | 规则型优化 R1–R7 | V2 §46 | `optimization/rule_based.py` |
| 5 | 贪心网格搜索与加速策略 | V2 §46 | `optimization/greedy_optimizer.py` |
| 6 | 线性规划变量/目标/约束 | V2 §47 | `optimization/lp_optimizer.py` |
| 7 | `scipy` 可选依赖与降级留痕 | V2 §45、§47 | `optimization/lp_optimizer.py` |
| 8 | 方案扫描四维度与候选值 | V2 §46、§48 | `calculation/scenario_engine.py` |
| 9 | 价差情景施加方式 | V2 §48 | `calculation/scenario_engine.py` |
| 10 | 参数扫描 8 维度 | V2 §48 | `calculation/scenario_engine.py` |
| 11 | 一次只改一个参数 | V1 §95 | `calculation/scenario_engine.py` |
| 12 | 并列打破规则（确定性） | V2 §48、V1 §118 | `optimization/*` |
| 13 | 五段式可解释输出 | V2 §48 | `optimization/*`、`domain/timeseries_results.py` |
| 14 | 性能与内存约束 P1–P6 | V2 §86 | 全部 `calculation/`、`optimization/` |
| 15 | 不输出可行性结论 | V1 §106 | 全部优化器与报表 |

---

## 13. 变更纪律

1. **候选值不得自行改动**：第 7 节的候选值由规范给定；确需变更必须同步改本文档与
   [CHANGELOG.md](CHANGELOG.md) 并在评审中说明理由。
2. 新增优化目标 → 必须顺延枚举、补入第 3.3 节表格，并补测试。
3. 新增降级路径 → 必须补入第 2.2 节表格，且**必须留痕**（原则 O4）。
4. 引入并行化 → 必须证明结果与串行**逐位一致**（原则 O5），否则不得合并。
5. **V1 兼容性红线**：优化器不得改变 V1 情景/敏感性分析的既有结果
   （V1 Golden Case 必须继续通过）。
6. 本文档与 [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) 冲突时，
   调度算法以后者为准；优化流程以本文档为准。
