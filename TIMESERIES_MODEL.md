# TIMESERIES_MODEL.md —— 时序数据模型与结果字典

> **当前状态**：**V2 开发中**。截至本文档修订时，**域层已落地**：
> `src/cenep/domain/timeseries.py`（输入时序模型）、`src/cenep/domain/timeseries_results.py`
> （结果模型）**已存在**，`domain/enums.py` 已含 `Resolution` / `DispatchStrategy` / `DispatchAction`。
> `data/`、`calculation/`（V2 部分）与 `optimization/` 下的模块**尚未创建**。
> 本文档描述**设计**：已有代码的部分以**实际实现**为准（第 4、6、7 节字段表逐字段对照源码核实），
> 尚无代码的部分用「设计」「V2 计划」表述，**不得**据此认为功能已可用。
>
> **优先级**：受 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md)（V1 最高优先级）约束；
> V2 新增内容以本文档为准。与 V1 冲突时，**V1 既有口径不得被静默改变**（V2 §65）。
>
> **记号**：`V2 §N` = V2 总规范第 N 条；`V1 §N` = V1 产品规范第 N 条；
> `核心 §N` = `CORE_PARAMETERS_AND_FORMULAS.md` 第 N 节；
> `文件:符号` = 源码位置（V2 模块尚未存在）；【设计】= 本期尚未实现；【V1 沿用】= V1 已有实现，V2 直接复用。
>
> **适用范围**：工商业分布式光伏 / 工商业储能 / 工商业光储三类项目（V2 §8）。
> V2 在 V1 年度模型之外新增**逐小时时序模型**，V1 年度模型继续保留并作为基准对照。

---

## 1. 设计目标与范围（V2 §6、§8）

V2 的时序模型要解决的，是 V1「年度等效」模型无法回答的问题：**电在一天里的时点价值**。

| 维度 | V1（【V1 沿用】） | V2（【设计】） |
|---|---|---|
| 时间粒度 | 年（`AnnualResult`） | **小时**（可扩展 15 分钟） |
| 负荷 | 单一 `annual_load_kwh` + 几何增长 | **逐时负荷曲线** |
| 光伏出力 | 年发电量 × 衰减因子 | **逐时出力曲线** |
| 电价 | 峰/平/谷单值 + 比例 | **逐时电价序列** |
| 储能 | 年度等效循环（§41、§42、核心 L11） | **逐时 SOC 递推 + 调度决策** |
| 自用比例 | 用户输入常数 | **由逐时匹配关系自然涌现** |
| 余电上网 | 年度比例 | **逐时判定** |

**范围红线**（V2 §8）：

1. V2 **不废弃** V1 年度模型；两者共用 `CalculationResult`，时序结果作为**新增分组**并列展示。
2. V2 **不做** GIS、CAD、组件排布、电气设计、潮流计算（与 V1 一致）。
3. V2 时序模型**不得**绕过 `calculation/` 计算，GUI / Excel / PDF 仍只能消费结果对象（V1 §5、§148–§150）。
4. 时序模型的任何新增简化，必须在本文档第 10 节与 `CalculationResult.notes` 中同步披露（V1 §158）。

### 1.1 模块布局（已冻结）

```text
src/cenep/domain/timeseries.py          输入时序模型          ✅ 域层已落地
src/cenep/domain/timeseries_results.py  结果模型              ✅ 域层已落地
src/cenep/data/importer.py              Excel/CSV 导入        【设计】
src/cenep/data/validator.py             时序校验              【设计】
src/cenep/data/quality.py               数据质量评分          【设计】
src/cenep/calculation/timeseries_engine.py  时间轴与分辨率     【设计】
src/cenep/calculation/load_profile.py       负荷              【设计】
src/cenep/calculation/pv_profile.py         光伏出力          【设计】
src/cenep/calculation/tariff_series.py      分时电价          【设计】
src/cenep/calculation/storage_soc.py        储能与 SOC        【设计】
src/cenep/calculation/dispatch_engine.py    调度三策略        【设计】
src/cenep/calculation/energy_balance.py     能量守恒          【设计】
src/cenep/calculation/economic_v2.py        时序 → 年度经济   【设计】
src/cenep/calculation/scenario_engine.py    方案与参数扫描    【设计】
src/cenep/optimization/rule_based.py        规则型优化        【设计】
src/cenep/optimization/greedy_optimizer.py  贪心经济优化      【设计】
src/cenep/optimization/lp_optimizer.py      线性规划（可选）  【设计】
```

**依赖方向**（不变式）：`domain/` ← `data/` ← `calculation/` ← `optimization/` ← `application/` ← `ui/`。
`optimization/` **不得**被 `calculation/` 反向依赖；`data/` **不得**包含任何财务公式。

### 1.2 已实现模型清单（对照源码核实）

除本文档重点描述的模型外，域层还实现了以下**配置类与容器类模型**（均位于
`domain/timeseries.py` / `domain/timeseries_results.py`），一并登记以免遗漏：

| 模型 | 字段数 | 所属 | 作用 |
|---|---|---|---|
| `TimeSeriesPoint` | 6 | 输入 | 单个时间点（§4.1） |
| `TimeSeriesProfile` | 7 | 输入 | Profile 基类（`profile_id` / `name` / `resolution` / `source` / `source_date` / `source_type` / `points`） |
| `LoadProfile` | 8 | 输入 | 负荷曲线（§4.2） |
| `PVProfile` | 9 | 输入 | 光伏出力曲线（§4.3） |
| `TariffProfile` | 14 | 输入 | 电价方案（§4.4） |
| `TimePeriodRule` | 4 | 输入 | 时段划分规则（§4.4） |
| `StorageDispatchConfig` | 15 | 输入 | 储能调度配置（§4.5） |
| `TimeSeriesConfig` | 9 | 输入 | 时序总配置（`enabled` / `resolution` / `base_year` / `load` / `pv` / `tariff` / `dispatch` / `holidays` / `balance_tolerance`） |
| `LoadProfileConfig` | 8 | 输入 | 负荷**生成**配置（`mode` / `hourly` / `typical_workday` / `typical_weekend` / `monthly_factors` / `annual_energy_kwh` / `annual_growth_rate` / `missing_data_policy`） |
| `PVProfileConfig` | 10 | 输入 | 光伏**生成**配置（`mode` / `hourly` / `typical_day` / `monthly_factors` / `hour_factors` / `equivalent_hours` / `performance_ratio` / `capacity_kwp` / `annual_growth_rate` / `missing_data_policy`） |
| `TariffSeriesConfig` | 4 | 输入 | 电价序列配置（`profile` / `annual_growth_rate` / `demand_charge_enabled` / `basic_charge_enabled`） |
| `TimeSeriesResultSet` | 27 | 结果 | **列式容器**（`timestamps` + 23 列 + `dispatch_actions` / `dispatch_reasons`） |
| `HourlyResult` | 25 | 结果 | 逐时行视图（§6.1） |
| `DispatchDecision` | 6 | 结果 | 调度决策与双轨原因（§6.2） |
| `EnergyBalance` | 20 | 结果 | 守恒（§7.2） |
| `BaselineResult` | 5 | 结果 | 基准对照（§7.1） |
| `DataQualityScore` | 6 | 结果 | 质量评分（§7.3） |
| `DataQualityIssue` | 5 | 结果 | 质量问题条目（§7.3） |
| `TimeSeriesMetrics` | 30 | 结果 | 汇总指标（§7.4） |
| `TimeSeriesReport` | 10 | 结果 | 时序总报告（§7.0） |
| `ScenarioResult` | 20 | 结果 | 方案扫描候选结果（[OPTIMIZATION.md](OPTIMIZATION.md) §7.3） |
| `OptimizationCandidate` | 7 | 结果 | 优化候选（`run_id` / `variables` / `objective` / `objective_value` / `feasible` / `note` / `scenario`） |
| `OptimizationResult` | 9 | 结果 | 优化结果（[OPTIMIZATION.md](OPTIMIZATION.md) §9.1） |

> **配置类模型说明**：`*ProfileConfig` / `TimeSeriesConfig` 用于**在缺时序数据时按规则生成曲线**
> （`mode` 区分"逐时输入"与"典型日/月度系数生成"），并携带 `missing_data_policy`
> （对应 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) §6 的缺失处理策略）。
> 该生成路径**必须在报告中披露**为合成数据（同文档 §6.3 G2）。

---

## 2. 时间模型（V2 §7）

### 2.1 唯一主时间索引

```text
timestamp  = 唯一主时间索引（ISO 8601，带时区无关的本地时间）
序号 1..8760 / 1..8784 只允许作为数组下标，禁止作为时间依据
```

**硬性规则**（V2 §7）：

| # | 规则 | 理由 |
|---|---|---|
| T1 | 所有时序对象**必须**携带 `timestamp` 序列，长度与各数据列一致 | 序号无法表达跨年、闰年、15 分钟分辨率 |
| T2 | **禁止**用 1~8760 序号充当时间键 | V1 审计中已出现"按序号对齐"导致错位的先例 |
| T3 | 时间轴起点为 `1月1日 00:00`，终点为 `12月31日 23:00`（小时分辨率） | 全年连续、无缺 |
| T4 | 时间戳**不含时区偏移**，按项目所在地本地时间解释 | 分布式项目不跨时区；避免 DST 歧义 |
| T5 | 排序、去重、缺失判定**一律**基于 `timestamp` | 与 T1/T2 一致 |
| T6 | 同一 `timestamp` 在一条曲线中**只能出现一次** | 重复即报错，见 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) §5 |

### 2.2 分辨率与点数（V2 §7）

| 分辨率 | 枚举值 | 单日点数 | **平年点数** | **闰年点数** | 本期状态 |
|---|---|---|---|---|---|
| 小时 | `HOURLY` | 24 | **8760** | **8784** | ✅ 本期实现（内部唯一计算分辨率） |
| 15 分钟 | `QUARTER_HOURLY` | 96 | **35040** | **35136** | 🔶 预留接口，本期不做换算与计算 |
| 日 | `DAILY` | 1 | 365 | 366 | 🔶 预留（降采样展示） |
| 月 | `MONTHLY` | — | 12 | 12 | 🔶 预留（月度汇总展示） |

```text
平年 = year 满足 (year % 4 != 0) 或 (year % 100 == 0 且 year % 400 != 0)
闰年 = 2 月 29 天 → 全年 366 天
HOURLY 点数 = 366 × 24 = 8784（闰年）｜ 365 × 24 = 8760（平年）
```

**内部计算分辨率固定为 `HOURLY`。** 15 分钟分辨率本期只做两件事：
① 时间轴生成与点数校验；② 降采样为小时后再进入计算。
**禁止**在未定义 15 分钟级调度规则的情况下直接计算。

> **与 V1 的关系**：V1 明确「不做 8760 小时仿真」（核心 L11、V1 §137）。
> V2 新增时序模型**不改变** V1 年度模型的既有路径；V1 项目在未提供时序数据时，
> 仍按年度模型计算（V2 §65 兼容性承诺）。

### 2.3 派生时间字段

`TimeSeriesPoint` 中的时间属性**全部由 `timestamp` 派生**，导入时忽略用户提供的同名列（避免自相矛盾）：

```text
year       = timestamp.year
month      = timestamp.month          1..12
day        = timestamp.day            1..31
hour       = timestamp.hour           0..23
weekday    = timestamp.weekday()      0=周一 .. 6=周日
is_weekend = weekday >= 5
is_holiday = timestamp.date() ∈ 法定节假日表（来自 PolicyProfile，V1 §34；本期可为空集）
```

`is_holiday` 依赖政策数据，**不得**在代码中硬编码节假日表（V1 §5「政策不硬编码」）。

---

## 3. 存储结构：列式数组 vs 行视图（V2 §87、§6）

这是 V2 时序模型**最关键的一处工程取舍**，必须在文档层说清楚。

### 3.1 问题

- V2 §6 用**对象模型**定义结果：`HourlyResult` 逐字段可读、可序列化、可直接喂给报表。
- V2 §86、§87 要求**性能与内存**：8760 点 ×（负荷 + 光伏 + 电价 + 储能 + 收益）≈ 十万级数值，
  若逐小时创建 Python 对象，仅对象开销就是数十 MB 级，且方案扫描（OPTIMIZATION.md 第 7 节）
  要重复计算**数百个候选方案**。

### 3.2 取舍（已冻结）

```text
引擎内部：NumPy 列式数组（columnar）
            ↓  按需物化
HourlyResult：行视图（row view），不是内部存储结构
            ↓
JSON / Excel / PDF：由行视图序列化
```

| 层 | 结构 | 说明 |
|---|---|---|
| **引擎内部** | `numpy.ndarray`（float64 / int32 / bool），**一列一个数组** | 名称与 `HourlyResult` 字段**同名同序**；不创建 Python 对象 |
| **列式容器**（已落地） | `TimeSeriesResultSet`（Pydantic，27 字段） | `timestamps` + 21 个与 `HourlyResult` 同名的**列** + `dispatch_actions` / `dispatch_reasons`；这是"列式数组"的**模型化表达** |
| **结果对象** | `HourlyResult`（Pydantic） | **按需生成**的行视图：报表要哪些时段就物化哪些；不参与计算 |
| **序列化** | `list[dict]` / JSON | 由行视图转换，见第 8 节 |

> **命名对照**：`TimeSeriesResultSet` 的 21 个数值列与 `HourlyResult` 的 21 个数值字段**一一同名**
> （另加 `electricity_price`、`export_price` 两个价格列）。行视图物化 = 按 `timestamp` 把列切片成行。

**硬性约束**（V2 §87）：

| # | 约束 |
|---|---|
| P1 | 引擎内部**必须**使用 NumPy 列式数组；**禁止**逐小时创建大量 Python 对象 |
| P2 | 计算过程中**禁止**出现 `for i in range(8760): HourlyResult(...)` 这类模式 |
| P3 | 允许的循环粒度：**按策略/按方案**（外层），向量化操作在**逐时维度**（内层） |
| P4 | `HourlyResult` 对象的**数量**由**展示需求**决定，与内部数组长度解耦 |
| P5 | 任何"为了好看"而在引擎内部预先生成全量行对象的实现，视为**违反 §87** |

### 3.3 为什么仍然保留 `HourlyResult`

| 原因 | 说明 |
|---|---|
| 规范 §6 定义了模型 | 模型定义是**对外契约**，不能因为内部实现而取消 |
| 可解释性 | 用户要能查到"第 3 天 14:00 为什么充电"（V2 §16） |
| 序列化 | `.nep` / Excel / PDF 需要**可读**的数据形态 |
| 兼容性 | V1 的 `AnnualResult` 是行模型，V2 保持同一消费方式，GUI 改造成本最低 |

**一句话**：**列式数组是引擎的"内存"，`HourlyResult` 是引擎的"接口"。**
两者字段一一对应，转换代码只允许存在于 `domain/timeseries_results.py` 的一处物化函数中。

### 3.4 内存与耗时预算（设计目标）

| 项目 | 目标 | 备注 |
|---|---|---|
| 单方案逐时数组内存 | ≤ 8 MB | 8760 × 约 20 列 × 8 B ≈ 1.4 MB（float64），含中间量留 5 倍余量 |
| 单方案时序计算耗时 | ≤ 0.5 s | 纯向量化；不含方案扫描 |
| 方案扫描（100 候选） | ≤ 60 s | 见 OPTIMIZATION.md 第 7、10 节 |
| 全量行视图物化 | **默认不物化** | 仅当报表/界面请求时按年或按月物化 |

---

## 4. 输入时序模型字段字典（V2 §9、§17）

单位一律使用 V1 基准单位（核心 §1）：电量 `kWh`、功率 `kW`、电价 `元/kWh`、比例 `小数`。
所有模型继承 `_Model(BaseModel)`，`extra="forbid"`、`validate_assignment=True`（V1 §112）。

### 4.1 `TimeSeriesPoint`（V2 §17）

**单点**的完整记录。既是导入模板的目标结构，也是 `HourlyResult` 的时间骨架。

> **实现现状（已核实）**：`TimeSeriesPoint` 实际只有 **6 个 Pydantic 字段**（见下表 A），
> 时间属性全部是**只读 `property`**（见下表 B），**不落库**。这样既满足"时间可查"，
> 又避免 8760 点重复存储 6 个可从 `timestamp` 推出的值（V2 §87 内存要求）。

**表 A：字段（6 个，会序列化）**

| 字段 | 类型 | 单位 | 默认 | 取值 | 含义 |
|---|---|---|---|---|---|
| `timestamp` | `datetime` | — | 必填 | 全年连续、唯一 | **唯一主时间索引**（第 2.1 节 T1） |
| `is_holiday` | `bool` | — | `False` | — | 是否法定节假日（来自 `PolicyProfile`，V1 §34） |
| `load_kwh` | `float` | kWh | `0.0` | `≥0` | 该小时**用电量**（非功率；1 小时 = kWh 与 kW 数值相同） |
| `pv_generation_kwh` | `float` | kWh | `0.0` | `≥0` | 该小时**光伏发电量** |
| `electricity_price` | `float` | 元/kWh | `0.0` | `≥0` | 该小时**购电电价**（用户侧到户电价） |
| `export_price` | `float` | 元/kWh | `0.0` | `≥0` | 该小时**上网电价** |

**表 B：派生属性（6 个 `property`，由 `timestamp` 推出，不入库）**

| 属性 | 类型 | 取值 | 含义 |
|---|---|---|---|
| `year` | `int` | — | 年 |
| `month` | `int` | `1–12` | 月 |
| `day` | `int` | `1–31` | 日 |
| `hour` | `int` | `0–23` | 时 |
| `weekday` | `int` | `0–6` | 星期（0 = 周一） |
| `is_weekend` | `bool` | — | 是否周末（`weekday >= 5`） |

> **单位约定**：小时分辨率下，功率与电量数值相同（`1 kW × 1 h = 1 kWh`）。
> 15 分钟分辨率下**不得**直接沿用：`E = P × Δt`，`Δt = 0.25 h`（见 STORAGE_DISPATCH.md §2.2）。

### 4.2 `LoadProfile`（V2 §9）

| 字段 | 类型 | 单位 | 默认 | 含义 |
|---|---|---|---|---|
| `profile_id` | `str` | — | 必填 | 曲线标识（同一项目内唯一） |
| `name` | `str` | — | 必填 | 中文显示名（如「2025 年实际负荷」） |
| `resolution` | `Resolution` | — | `HOURLY` | 分辨率（第 2.2 节） |
| `annual_energy` | `float` | kWh | `0.0` | 全年用电量合计（**由 `points` 求和得出**，用于与 V1 `annual_load_kwh` 对齐校对） |
| `points` | `list[TimeSeriesPoint]` | — | `[]` | 逐时点；长度必须等于该年点数 |
| `source` | `str` | — | `""` | 数据来源名称（如「供电公司计量导出」） |
| `source_date` | `date \| None` | — | `None` | 数据日期 |
| `source_type` | `SourceType` | — | `HISTORICAL` | 来源类型（见 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) §9） |

**一致性校验**（`data/validator.py`，V2 §54）：

```text
|annual_energy − Σ points.load_kwh| ≤ max(1e-6, 1e-6 × annual_energy)
```

### 4.3 `PVProfile`（V2 §9）

| 字段 | 类型 | 单位 | 默认 | 含义 |
|---|---|---|---|---|
| `profile_id` | `str` | — | 必填 | 曲线标识 |
| `name` | `str` | — | 必填 | 中文显示名 |
| `resolution` | `Resolution` | — | `HOURLY` | 分辨率 |
| `capacity_kwp` | `float` | kWp | `0.0` | 该曲线对应的直流侧装机容量（用于容量换算与方案扫描缩放） |
| `annual_generation` | `float` | kWh | `0.0` | 全年发电量合计（由 `points` 求和） |
| `points` | `list[TimeSeriesPoint]` | — | `[]` | 逐时点（只使用其 `pv_generation_kwh`） |
| `source` | `str` | — | `""` | 数据来源（如「PVsyst 仿真导出」） |
| `source_date` | `date \| None` | — | `None` | 数据日期 |
| `source_type` | `SourceType` | — | `HISTORICAL` | 来源类型 |

**容量缩放规则**（用于方案扫描）：

```text
pv_point(target) = pv_generation_kwh × (target_capacity_kwp / capacity_kwp)     capacity_kwp > 0
```

即：曲线形状不变，**按容量等比缩放**。若 `capacity_kwp = 0`，禁止缩放并报错。

### 4.4 `TariffProfile`（V2 §9）—— **14 个字段（已核实）**

| 字段 | 类型 | 单位 | 默认 | 含义 |
|---|---|---|---|---|
| `tariff_id` | `str` | — | `""` | 电价方案标识 |
| `name` | `str` | — | `""` | 中文显示名 |
| `effective_date` | `date \| None` | — | `None` | 生效日期（政策时效性，V1 §35） |
| `region` | `str` | — | **`"湖北"`** | 适用地区（与 V1 `BasicInfo.province` 默认一致） |
| `time_periods` | `list[TimePeriodRule]` | — | 默认工厂 | 时段划分规则（见下表） |
| `sharp_peak_price` | `float` | 元/kWh | `0.0` | **尖峰电价**（V2 新增，比峰段更高） |
| `peak_price` | `float` | 元/kWh | `0.0` | 峰段电价 |
| `flat_price` | `float` | 元/kWh | `0.0` | 平段电价 |
| `valley_price` | `float` | 元/kWh | `0.0` | 谷段电价 |
| `deep_valley_price` | `float` | 元/kWh | `0.0` | 深谷电价（V2 新增时段） |
| `custom_price` | `float` | 元/kWh | `0.0` | **自定义电价**（V2 新增；时段标记为「自定义」时使用） |
| `export_price` | `float` | 元/kWh | `0.0` | 上网电价 |
| `demand_charge` | `float` | 元/kW·月 | `0.0` | 需量电价（按最大需量计收，V2 新增） |
| `basic_charge` | `float` | 元/kVA·月 | `0.0` | 基本电费（按容量计收，V2 新增） |

**`TimePeriodRule`（4 字段）**——时段划分的结构化表达：

| 字段 | 类型 | 含义 |
|---|---|---|
| `period` | `TariffPeriod` | 时段（枚举：`SHARP_PEAK` / `PEAK` / `FLAT` / `VALLEY` / `DEEP_VALLEY` / `CUSTOM`） |
| `months` | `list[int]` | 适用月份（`1–12`；空 = 全年） |
| `day_types` | `list[DayType]` | 适用日类型（枚举：`WORKDAY` / `WEEKEND` / `HOLIDAY`；空 = 全部） |
| `hours` | `list[int]` | 适用小时（`0–23`） |

**时段解析规则**（`calculation/tariff_series.py`，V2 §9）：

```text
price(t) = 遍历 time_periods，取第一个满足 (t.month ∈ months) 且 (t 的日类型 ∈ day_types)
           且 (t.hour ∈ hours) 的规则 → 按其 period 取对应电价
           未命中任何规则 → 使用 flat_price 并记入告警（不得静默取 0）
```

各时段电价与 `TariffPeriod` 的对应：
`SHARP_PEAK→sharp_peak_price`、`PEAK→peak_price`、`FLAT→flat_price`、
`VALLEY→valley_price`、`DEEP_VALLEY→deep_valley_price`、`CUSTOM→custom_price`。
`deep_valley_price > 0` 时才参与解析；为 `0.0` 表示该地区无深谷时段（同 `sharp_peak_price`、`custom_price`）。

### 4.5 `StorageDispatchConfig`（V2 §9）—— **15 个字段（已核实）**

| 字段 | 类型 | 单位 | 默认 | 含义 |
|---|---|---|---|---|
| `strategy` | `DispatchStrategy` | — | `PV_SELF_CONSUMPTION` | 调度策略（第 5 节） |
| `charge_from_pv` | `bool` | — | `True` | 允许光伏给储能充电 |
| `charge_from_grid` | `bool` | — | `False` | 允许电网给储能充电 |
| `allow_grid_charge` | `bool` | — | `False` | **电网充电总开关**（与 `charge_from_grid` 同时为真才生效） |
| `allow_export` | `bool` | — | `False` | 允许储能放电上网 |
| `allow_arbitrage` | `bool` | — | `True` | 允许峰谷套利 |
| `soc_min` | `float` | 小数 | `0.10` | SOC 下限（`0–1`） |
| `soc_max` | `float` | 小数 | **`1.00`** | SOC 上限（`0–1`；**默认满仓**，可用区间由 `soc_min` 约束） |
| `initial_soc` | `float` | 小数 | `0.10` | **SOC 初值**（V2 新增；默认取 `soc_min`） |
| `charge_efficiency` | `float` | 小数 | **`0.938`** | 充电效率 `η_charge` |
| `discharge_efficiency` | `float` | 小数 | **`0.938`** | 放电效率 `η_discharge` |
| `max_charge_power` | `float` | kW | `0.0` | 最大充电功率（`0` = 按储能额定功率） |
| `max_discharge_power` | `float` | kW | `0.0` | 最大放电功率（`0` = 按储能额定功率） |
| `charge_price_threshold` | `float` | 元/kWh | `0.0` | **充电价格阈值**（V2 新增；`0` = 由策略按峰谷自动推算） |
| `discharge_price_threshold` | `float` | 元/kWh | `0.0` | **放电价格阈值**（V2 新增；`0` = 由策略按峰谷自动推算） |

**效率默认值说明**：

```text
V1 往返效率默认 0.88（核心 §4.2 式 8）
V2 实现取 η_charge = η_discharge = 0.938
⚠️ 精确值应为 √0.88 = 0.938083152…；取 0.938 时 0.938² = 0.879844 ≠ 0.88（偏差 0.018%）
```

> **口径提示**：若要求往返效率**严格等于** 0.88，应把方向效率显式设为 `0.938083`；
> 取默认 `0.938` 时往返效率为 `0.879844`。该差异已在
> [CHANGELOG.md](CHANGELOG.md) 的 V2 条目中登记，最终取值以评审结论为准。

**两个价格阈值的语义**：`0.0` 表示"未显式指定"，由策略按 `PEAK_VALLEY` 的
峰谷比例推算（见 [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) §4.1）；
显式赋值（`> 0`）时**优先使用显式阈值**，不得被推算值覆盖。

---

## 5. 枚举定义（V2 §9）—— 对照 `domain/enums.py` 核实

V2 在 `domain/enums.py` 中新增/扩展了以下枚举（**共 7 个 V2 专用 + 2 个扩展**）：

```python
class Resolution(StrEnum):              # V2 新增
    HOURLY = "HOURLY"                  # 小时（内部唯一计算分辨率）
    QUARTER_HOURLY = "QUARTER_HOURLY"  # 15 分钟（预留）
    DAILY = "DAILY"                    # 日（预留）
    MONTHLY = "MONTHLY"                # 月（预留）

class DispatchStrategy(StrEnum):        # V2 新增
    PEAK_VALLEY = "PEAK_VALLEY"                      # 峰谷套利
    PV_SELF_CONSUMPTION = "PV_SELF_CONSUMPTION"      # 光伏自用优先
    ECONOMIC_OPTIMIZATION = "ECONOMIC_OPTIMIZATION"  # 经济优化（规则型）

class DispatchAction(StrEnum):          # V2 新增
    CHARGE = "CHARGE"                  # 充电
    DISCHARGE = "DISCHARGE"            # 放电
    IDLE = "IDLE"                      # 不动作

class TariffPeriod(StrEnum):            # V2 新增
    SHARP_PEAK / PEAK / FLAT / VALLEY / DEEP_VALLEY / CUSTOM

class DayType(StrEnum):                 # V2 新增
    WORKDAY / WEEKEND / HOLIDAY

class MissingDataPolicy(StrEnum):       # V2 新增
    REJECT / LINEAR_INTERPOLATION / FORWARD_FILL / TYPICAL_DAY_FILL

class LoadProfileMode(StrEnum):         # V2 新增
    HOURLY / TYPICAL_DAY / ANNUAL_SIMPLE

class PVProfileMode(StrEnum):           # V2 新增
    HOURLY / TYPICAL_DAY / MONTHLY_HOUR_FACTOR / EQUIVALENT_HOURS

class OptimizationObjective(StrEnum):   # V2 新增（见 OPTIMIZATION.md §3.3）
    MAX_IRR / MAX_NPV / MIN_PAYBACK / MIN_LCOE / MIN_LCOS / MIN_ANNUAL_COST

class ScanVariable(StrEnum):            # V2 新增（见 OPTIMIZATION.md §8）
    PV_CAPACITY / STORAGE_CAPACITY / STORAGE_POWER / STORAGE_PRICE /
    TARIFF / PEAK_VALLEY_SPREAD / LOAD / CAPEX
```

| 枚举 | 取值数 | 引用条款 |
|---|---|---|
| `Resolution` | 4 | V2 §7 |
| `DispatchStrategy` | 3 | V2 §12–§15 |
| `DispatchAction` | 3 | V2 §16 |
| `TariffPeriod` | 6 | V2 §9 |
| `DayType` | 3 | V2 §9 |
| `MissingDataPolicy` | 4 | V2 §55（见 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) §6.2） |
| `LoadProfileMode` | 3 | V2 §9（曲线生成模式） |
| `PVProfileMode` | 4 | V2 §9（曲线生成模式） |
| `OptimizationObjective` | 6 | V2 §14（见 [OPTIMIZATION.md](OPTIMIZATION.md) §3.3） |
| `ScanVariable` | 8 | V2 §48（见 [OPTIMIZATION.md](OPTIMIZATION.md) §8） |
| `SensitivityVariable` | **8**（V1 为 5，V2 扩展） | V1 §95、V2 §48 |

> **两套单变量机制的区别**见 [OPTIMIZATION.md](OPTIMIZATION.md) §8.1：
> `SensitivityVariable`（含融资/循环次数）回答"结论稳不稳"；
> `ScanVariable`（容量相关）回答"哪些容量参数最敏感"。

---

## 6. 结果模型字段字典（V2 §22、§23、§16）

### 6.1 `HourlyResult`（V2 §22、§23）—— 逐时结果行视图

> **提醒**：本对象是**按需生成的行视图 + JSON 序列化载体**，**不是**引擎内部存储结构（第 3 节）。
> 字段顺序 = 列式数组的列顺序，物化函数只允许存在于 `domain/timeseries_results.py`。

**列含义记号**：`Δt = 1 h`（小时分辨率）。所有电量单位 `kWh`、电价 `元/kWh`、金额 `元`。

| # | 字段 | 类型 | 单位 | 计算式（V2 §23） |
|---|---|---|---|---|
| 1 | `timestamp` | `datetime` | — | 取自时间轴（第 2.1 节） |
| 2 | `load` | `float` | kWh | `LoadProfile.points[t].load_kwh` |
| 3 | `pv_generation` | `float` | kWh | `PVProfile.points[t].pv_generation_kwh`（可经容量缩放） |
| 4 | `pv_to_load` | `float` | kWh | `min(pv_generation, load)` |
| 5 | `pv_to_storage` | `float` | kWh | `min(pv_generation − pv_to_load, charge_power_room)`（见 §6.1.1） |
| 6 | `pv_to_grid` | `float` | kWh | `min(pv_generation − pv_to_load − pv_to_storage, export_room)` 受 `allow_export` 约束 |
| 7 | `pv_curtailed` | `float` | kWh | `pv_generation − (pv_to_load + pv_to_storage + pv_to_grid)` |
| 8 | `grid_to_load` | `float` | kWh | `max(load − pv_to_load − load_from_storage, 0)` |
| 9 | `grid_to_storage` | `float` | kWh | `charge_from_grid 且 allow_grid_charge ? min(剩余充电空间, 策略允许值) : 0` |
| 10 | `storage_charge` | `float` | kWh | `pv_to_storage + grid_to_storage` |
| 11 | `storage_discharge` | `float` | kWh | `min(可用放电电量, 策略放电指令, max_discharge_power × Δt)` |
| 12 | `storage_soc_start` | `float` | 小数 | `上一小时 storage_soc_end`；首点 = `soc_min`（或配置初值） |
| 13 | `storage_soc_end` | `float` | 小数 | `soc_start + (storage_charge × η_charge × Δt − storage_discharge / η_discharge × Δt) / E_capacity` |
| 14 | `load_from_storage` | `float` | kWh | `storage_discharge`（本期：放电全部供负荷；上网需 `allow_export`） |
| 15 | `grid_import` | `float` | kWh | `grid_to_load + grid_to_storage` |
| 16 | `grid_export` | `float` | kWh | `pv_to_grid + storage_to_grid`（本期 `storage_to_grid` 默认 0） |
| 17 | `electricity_cost` | `float` | 元 | `grid_import × electricity_price` |
| 18 | `export_revenue` | `float` | 元 | `grid_export × export_price` |
| 19 | `storage_revenue` | `float` | 元 | `storage_discharge × avoided_price − storage_charge × charge_price`（**归因指标**，见 §6.1.2） |
| 20 | `total_revenue` | `float` | 元 | `export_revenue + storage_revenue` |
| 21 | `net_energy_cost` | `float` | 元 | `electricity_cost − export_revenue`（**实际购电净支出**） |
| 22 | `electricity_price` | `float` | 元/kWh | 该小时购电电价（**结果回写**，便于报表直接展示，无需回查 `TariffProfile`） |
| 23 | `export_price` | `float` | 元/kWh | 该小时上网电价（同上） |
| 24 | `dispatch_action` | `DispatchAction` | — | 该小时调度动作（`CHARGE` / `DISCHARGE` / `IDLE`），与 `DispatchDecision.action` **同源** |
| 25 | `dispatch_reason` | `str` | — | 该小时中文原因句，与 `DispatchDecision.reason` **同源**（V2 §16） |

> **共 25 个字段**（已核实）。第 22–25 项是 V2 实现为使逐时报表**自解释**而增加的冗余列：
> 有它们，报表不必再去关联 `TariffProfile` 与 `DispatchDecision`；代价是每点多 4 个值
> （`TimeSeriesResultSet` 中相应增加 4 列），仍在第 3.4 节内存预算内。

#### 6.1.1 `pv_to_storage` 的充电空间

```text
charge_power_room = min(max_charge_power_effective × Δt,
                        (soc_max − soc_start) × E_capacity / (η_charge × Δt) × Δt)
max_charge_power_effective = max_charge_power > 0 ? max_charge_power : storage_power_kw
```

即：**同时**受功率上限与 SOC 上限约束，取小者。

#### 6.1.2 收益口径的关键说明（避免重复计算）

`storage_revenue` 按 V2 §29 定义为**逐时**套利：

```text
storage_revenue = Σ_h (storage_discharge_h × avoided_price_h)
                − Σ_h (storage_charge_h    × charge_price_h)
```

**两条必须遵守的口径纪律**：

| # | 纪律 | 理由 |
|---|---|---|
| E1 | **禁止**把套利简化为「放电量 × 峰谷价差」 | 峰谷价差是**单一常数**，无法表达逐时电价、深谷、需量；V2 §29 明确禁止 |
| E2 | `storage_revenue` 是**归因指标**，**不得**再作为 `net_energy_cost` 的减项 | 放电已通过减少 `grid_import` 体现为成本下降；再减一次即**重复计算** |

因此：**`net_energy_cost`（实际支出）与 `total_revenue`（归因收益）口径不同，禁止相加或相减后当作实际支出。**
基准对照（无光伏无储能的原始电费）与节省额的算法见第 10 节。

### 6.2 `DispatchDecision`（V2 §16）

> **规范 §16 的要求**：**每小时都要能解释「为什么充电 / 放电 / 不动作」**。
> 因此 `reason` 是**人类可读的中文句子**，不是错误码。

| 字段 | 类型 | 单位 | 计算式 / 取值 |
|---|---|---|---|
| `timestamp` | `datetime` | — | 与 `HourlyResult.timestamp` 一一对应 |
| `action` | `DispatchAction` | — | `CHARGE` / `DISCHARGE` / `IDLE` |
| `reason` | `str` | — | 中文原因句（枚举见 STORAGE_DISPATCH.md 第 7 节），**含实际数值** |
| `reason_code` | `str` | — | **原因码**（`"R01"`–`"R21"`），供程序判别与测试断言；`reason` 供人读 |
| `charge_energy` | `float` | kWh | `action == CHARGE` 时为 `storage_charge`，否则 `0.0` |
| `discharge_energy` | `float` | kWh | `action == DISCHARGE` 时为 `storage_discharge`，否则 `0.0` |

> **共 6 个字段**（已核实）。`reason_code` 与 `reason` 是"机器可判 + 人可读"的双轨设计：
> 测试断言 `reason_code`，用户看 `reason`（V2 §16）。

**一致性约束**：

```text
action == CHARGE    ⟺ charge_energy > 0 且 discharge_energy == 0
action == DISCHARGE ⟺ discharge_energy > 0 且 charge_energy == 0
action == IDLE      ⟺ charge_energy == 0 且 discharge_energy == 0
```

`dispatch_results` 的长度**必须**等于时序点数；缺一条即视为计算失败。

---

## 7. `CalculationResult` V2 新增字段（V2 §25、§62）

V1 的 `CalculationResult` 字段保持**语义不变**（DATA_MODEL.md §3.2）。
V2 **新增 8 个字段**，全部默认空值，**不破坏** V1 项目与既有报表：

| # | 字段 | 类型（**已核实**） | 默认 | 含义 | 引用 |
|---|---|---|---|---|---|
| 1 | `time_series_results` | `TimeSeriesReport \| None` | `None` | **时序结果总报告**（含列式逐时数据、汇总指标、守恒、基准、质量） | V2 §22 |
| 2 | `baseline_results` | `BaselineResult \| None` | `None` | 基准对照（无光伏无储能的原始电费与需量） | V2 §25 |
| 3 | `annual_results` | `list[AnnualResult]` | 必填 | **时序汇总**得到的年度结果（沿用 V1 的 `AnnualResult` 类型） | V2 §62 |
| 4 | `dispatch_results` | `list[DispatchDecision]` | 必填 | 逐时调度决策与中文原因 | V2 §16 |
| 5 | `energy_balance` | `EnergyBalance \| None` | `None` | 能量守恒（年度合计 + 最大逐时残差 + 容差 + 判定） | V2 §95 |
| 6 | `data_quality` | `DataQualityScore \| None` | `None` | 数据质量评分（总分 + 四维度 + 问题清单） | V2 §56 |
| 7 | `scenario_results` | `list[ScenarioResult]` | 必填 | 方案扫描结果（20 字段/候选） | V2 §48 |
| 8 | `optimization_results` | `OptimizationResult \| None` | `None` | 优化结果（9 字段，含可解释过程与候选明细） | V2 §48 |

**字段名与冻结定义完全一致**（8 个名称均未改名）；类型由 `dict` 具体化为**专用模型**，
便于序列化时保持结构稳定。`CalculationResult` 当前共 **47 个字段**（V1 的 39 个 + V2 的 8 个）。

### 7.0 `TimeSeriesReport` 内部结构（10 字段，已核实）

`time_series_results` 是本 V2 分组的总入口，内部层次如下：

```text
TimeSeriesReport
├── enabled             : bool                 是否启用时序测算
├── resolution          : Resolution           分辨率（默认 HOURLY）
├── base_year           : int                  基准年（默认 2025）
├── dispatch_strategy   : DispatchStrategy     本次使用的调度策略
├── as_of               : date | None          结果生成日期
├── hourly              : TimeSeriesResultSet | None    ← 列式逐时数据（第 3 节）
├── metrics             : TimeSeriesMetrics              ← 30 个汇总指标（§7.4）
├── balance             : EnergyBalance | None           ← 守恒（§7.2）
├── baseline            : BaselineResult | None          ← 基准（§7.1）
└── data_quality        : DataQualityScore | None        ← 质量（§7.5）
```

> **实现观察**：`baseline` / `balance` / `data_quality` 在 `TimeSeriesReport` 内部**与**
> `CalculationResult` 顶层**同时存在**（顶层为规范 §25 要求的 8 个字段，报告内为自包含副本）。
> 二者必须**同源写入**，不得各自计算——否则会出现"顶层与报告内不一致"。
> 该冗余已在 [CHANGELOG.md](CHANGELOG.md) 的 V2 条目登记。

### 7.1 `BaselineResult`（5 字段，已核实）

基准 = **同一负荷、同一电价、同一时间轴下，不投光伏也不投储能**的原始电费：

| 字段 | 类型 | 单位 | 含义 |
|---|---|---|---|
| `annual_load` | `float` | kWh | 年用电量合计（`Σ load`） |
| `annual_electricity_cost` | `float` | 元 | 原始电度电费（`Σ load × price`） |
| `annual_demand_cost` | `float` | 元 | 原始需量电费（`Σ_月 月最大需量 × demand_charge`） |
| `peak_demand_kw` | `float` | kW | 全年最大需量 |
| `monthly_max_demand` | `list[float]` | kW | 12 个月的月最大需量（长度 12） |

**节省额不放在 `BaselineResult` 里**，而是由 `TimeSeriesMetrics` 给出（§7.4）：

```text
electricity_cost_saving = baseline_electricity_cost − actual_electricity_cost
demand_cost_saving      = baseline_demand_cost      − actual_demand_cost
```

**口径**：基准与方案的负荷、电价、时间轴**完全一致**，只改变光伏/储能是否参与。

### 7.2 `EnergyBalance`（20 字段，已核实）

**实现口径是"年度合计 + 最大逐时残差"**，而不是逐小时残差清单：

| 分组 | 字段 | 含义 |
|---|---|---|
| 光伏侧 | `pv_generation` | 年发电量合计 |
| | `pv_to_load` / `pv_to_storage` / `pv_to_grid` / `pv_curtailed` | 四条去向的年合计 |
| 电网侧 | `grid_to_load` / `grid_to_storage` / `grid_import` | 购电分解与合计 |
| | `grid_export` | 上网合计 |
| 储能侧 | `storage_charge` / `storage_discharge` | 充放电年合计 |
| | `storage_to_load` / `storage_to_grid` | 放电去向分解 |
| 平衡量 | `load_total` | 负荷合计（`Σ load`） |
| | `supply_total` | 供给侧合计 |
| | `demand_total` | 需求侧合计 |
| 判定 | `error` | **年度**守恒残差（`supply_total − demand_total`） |
| | `max_hourly_error` | **逐时**残差的最大绝对值 |
| | `tolerance` | 容差，默认 `1e-06`（与 V1 §113 一致） |
| | `is_balanced` | `max_hourly_error ≤ tolerance` 且 `\|error\| ≤ tolerance` |

**三条守恒关系**（对应第 10.1 节 B1 / B2 / B3）：

```text
B1 光伏侧：pv_generation = pv_to_load + pv_to_storage + pv_to_grid + pv_curtailed
B2 负荷侧：load_total   = pv_to_load + storage_to_load + grid_to_load
B3 供电侧：supply_total = demand_total            （error 即其差）
```

> **SOC 残差（B3 原表述）不在本模型内**：实现把 SOC 守恒交给 `storage_soc.py` 的递推
> 自身保证（`soc_end` 由 `soc_start` 与充放电推出），并用 `dispatch_engine.py` 的
> **SOC 越界即失败**（[STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) §2.3）替代独立残差。
> 本表按**实际实现**描述；第 10.1 节已同步修订。

### 7.3 `DataQualityScore` / `DataQualityIssue`（已核实）

```text
DataQualityScore（6 字段）
├── score               : float      总分 0–100
├── completeness        : float      完整性 0–40
├── continuity          : float      连续性 0–25
├── outlier             : float      异常值 0–20      ← 命名用 outlier（非 anomaly）
├── source_credibility  : float      来源可信度 0–15
└── issues              : list[DataQualityIssue]

DataQualityIssue（5 字段）
├── level     : str    严重度（"WARNING" / "ERROR"）
├── category  : str    类别
├── message   : str     中文说明
├── count     : int     命中数量
└── samples   : list    样本（时间点或行号）
```

计分规则的完整定义见 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) 第 8 节
（四维度权重与 §8.2 来源可信度分值表）。

### 7.4 `TimeSeriesMetrics`（30 字段，已核实）—— 时序汇总指标

`TimeSeriesReport.metrics` 承载全部"从逐时数据汇总出来的指标"，是报表的主要数据源：

| 分组 | 字段（30 个） |
|---|---|
| 电量 | `annual_pv_generation`、`annual_pv_curtailment`、`annual_grid_purchase`、`annual_grid_export`、`annual_load`、`annual_storage_charge`、`annual_storage_discharge`、`annual_grid_charge` |
| 循环 | `configured_cycles`（配置值）、`equivalent_cycles`（实际值） |
| 比例 | `self_consumption_rate`（自用率）、`self_sufficiency_rate`（自给率） |
| 需量 | `peak_demand_before`、`peak_demand_after`、`demand_saving`、`monthly_max_demand_before`、`monthly_max_demand_after` |
| 电费 | `baseline_electricity_cost`、`actual_electricity_cost`、`electricity_cost_saving` |
| 需量费 | `baseline_demand_cost`、`actual_demand_cost`、`demand_cost_saving` |
| 收益归因 | `pv_self_consumption_saving`、`pv_export_revenue`、`storage_arbitrage_revenue`、`storage_capacity_revenue`、`storage_ancillary_revenue`、`other_revenue`、`total_benefit` |

> **储能四类收益分列**（V1 §44）由 `storage_arbitrage_revenue` / `storage_capacity_revenue` /
> `storage_ancillary_revenue` / `other_revenue` 四个字段承担，满足
> [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) §11.3 的分列要求。

### 7.5 与 V1 年度结果的关系

```text
V1 路径：Project(年度参数) → engine.calculate() → annual_results（年度模型）
V2 路径：Project(时序参数) → 时序引擎 → economic_v2.py → annual_results（时序汇总）
```

两条路径都产出 `annual_results`（同一 `AnnualResult` 类型），**但分属不同计算口径**；
执行哪条路径由 `Project` 是否携带时序数据决定（V2 §65）。

---

## 8. JSON 可序列化要求（V2 §63）

`.nep` 项目文件与自检报告都是 JSON，因此**所有 V2 结果对象必须可直接 `model_dump(mode="json")`**。

| # | 要求 | 说明 |
|---|---|---|
| J1 | `datetime` 一律序列化为 **ISO 8601 字符串** | 如 `"2025-01-01T00:00:00"`；禁止 Unix 时间戳 |
| J2 | `date` 一律序列化为 `"YYYY-MM-DD"` | 与 V1 `ParameterMeta` 一致 |
| J3 | 枚举一律序列化为**字符串值** | 如 `"CHARGE"`；禁止整数码 |
| J4 | `float` 保留**完整精度**，展示层再做四舍五入 | 与 V1 §114「不得用 0 冒充无法计算」一致 |
| J5 | `None` 与 `0.0` **语义不同**，序列化后必须可区分 | `None` = 无法计算 / 未提供 |
| J6 | **禁止**在结果对象中出现 NumPy 类型（`np.float64` 等） | 物化函数负责转换为 Python 原生类型 |
| J7 | 结果对象**不得**含不可序列化的引用（函数、文件句柄、DataFrame） | — |
| J8 | 大数组默认**不写入** `.nep`；仅在用户显式勾选"保存时序结果"时写入 | 控制文件体积（8760 点约 2~5 MB JSON） |

自检（`python -m cenep --selftest`，V1 §131）在 V2 中**新增**一项：对含时序的结果对象执行
`json.dumps(result.model_dump(mode="json"))`，失败即判定自检不通过。

---

## 9. `.nep` `schema_version` 2.0 与 V1 → V2 迁移（V2 §64、§65）

### 9.1 版本矩阵

| `schema_version` | 产生者 | V2 软件的处理 |
|---|---|---|
| `"1.0"` | CENEP V1.0.0 | ✅ 打开 → **自动迁移**为 2.0 |
| `"1.1"` | V1 后续修订 | ✅ 打开 → **自动迁移**为 2.0 |
| `"2.0"` | CENEP V2.0.0 | ✅ 直接打开 |
| 其它（如 `"9.9"`）/ 缺失 | — | ❌ 抛 `MigrationError`，中文报错（**不猜测**） |

> **✅ 已落地**：`src/cenep/infrastructure/migration.py` 已实现上述策略，导出常量：
> `CURRENT_SCHEMA_VERSION = "2.0"`、`LEGACY_SCHEMA_VERSIONS = ("1.0", "1.1")`、
> `SUPPORTED_SCHEMA_VERSIONS = ("2.0", "1.0", "1.1")`、`CALCULATION_ENGINE_VERSION = "2.0.0"`；
> 入口函数 `migrate_project_payload(payload, from_version)`、`migrate_v1_to_v2(payload)`。
>
> V1 现状是「版本不匹配即拒载」（核心 L26）；V2 把 `1.0` / `1.1` 改为「迁移」，
> 这是**唯一放宽**，其它未知版本仍拒载。

### 9.2 迁移规则（V2 §65）

| # | 规则 | 具体要求 |
|---|---|---|
| M1 | **缺失字段用明确默认值补齐** | 逐字段登记默认值（见第 9.3 节），**禁止**用 `None` 糊过 |
| M2 | **不得静默改动原参数** | V1 已有字段的值、单位、含义**一律照搬**；迁移只**新增**，不修改 |
| M3 | 迁移结果必须**可复现 V1 结果** | 迁移后按 V1 年度模型计算，指标必须与原 V1 结果**逐位一致**（容差 `1e-9`） |
| M4 | 必须记录 `policy_version` | 迁移时写入当前政策 Profile 版本（V1 §34–§36） |
| M5 | 必须记录 `tariff_version` | 电价方案版本（V2 新增） |
| M6 | 必须记录 `calculation_version` | 计算口径版本（V2 新增）；用于区分「年度模型」与「时序模型」结果 |
| M7 | 迁移必须**留痕** | 在 `CalculationResult.notes` 追加一条中文说明，例：`「项目文件由 schema 1.0 迁移至 2.0：新增 8 个时序字段，原参数未改动」` |
| M8 | **禁止**在迁移中补造时序数据 | 无时序数据的 V1 项目，V2 中仍按年度模型计算，**不得**用「典型日 × 365」静默合成 |

### 9.3 V1 → V2 新增内容与默认值（**已核实**）

迁移只做一件事：**为 `Project` 新增的 `timeseries` 段写入显式默认值**（不留 Pydantic 隐式默认，
使文件自解释），并更新版本号与留痕。**既有参数一个都不改。**

**`Project.timeseries`（`TimeSeriesConfig`，9 字段）迁移后写入的值**：

| 字段 | 迁移写入的默认值 | 语义 |
|---|---|---|
| `enabled` | **`False`** | **时序仿真默认关闭** → V1 项目继续走年度模式，结果与 V1 一致 |
| `resolution` | `"HOURLY"` | 分辨率 |
| `base_year` | `2025` | 基准年 |
| `load` | `{"mode": "ANNUAL_SIMPLE"}` | 负荷模式：年度简单模式（等价于 V1 年度模型） |
| `pv` | `{"mode": "EQUIVALENT_HOURS"}` | 光伏模式：等效小时模式（等价于 V1） |
| `tariff` | `{"profile": {}, "annual_growth_rate": 0.0}` | 电价序列配置 |
| `dispatch` | `{}` | 调度配置（取 `StorageDispatchConfig` 默认值） |
| `holidays` | `[]` | 节假日（空集） |
| `balance_tolerance` | `1e-06` | 守恒容差（与 V1 §113 一致） |

**载荷级字段**：

| 字段 | 迁移写入 | 说明 |
|---|---|---|
| `schema_version` | `"2.0"` | 由 `1.0` / `1.1` 升级 |
| `migration_notes` | 追加一条中文说明 | 例：「已从 schema_version 1.0 迁移到 2.0；既有参数未被修改，计算结果口径不变。」 |

**信封级字段**（**不在** `Project` 载荷内，由 `save_project` 写入 `.nep` 外层）：

| 字段 | 说明 |
|---|---|
| `calculation_version` | 计算引擎版本（`CALCULATION_ENGINE_VERSION = "2.0.0"`） |
| `policy_version` | 政策 Profile 版本 |
| `tariff_version` | 电价方案版本 |

> ⚠️ **重要区别（已核实）**：`policy_version` / `tariff_version` / `calculation_version`
> 是 **`.nep` 信封级**字段，**不能**放进项目载荷——因为 `Project` 配置为 `extra="forbid"`，
> 多写字段会直接校验失败。迁移模块对此有显式注释说明。
> 早期设计稿曾把三者列为 `Project` 字段，此处已按实现更正。

### 9.4 迁移流程（**已核实**）

```text
load_project(path)
  ↓ 读取信封 schema_version
  ├─ "2.0"            → 直接构造 V2 模型（MigrationOutcome.migrated == False）
  ├─ "1.0" / "1.1"    → migrate_project_payload(payload, from_version)
  │                       ├─ _MIGRATION_STEPS[from_version] = migrate_v1_to_v2
  │                       ├─ 补 timeseries 段显式默认值（§9.3）
  │                       ├─ schema_version → "2.0"
  │                       ├─ 追加 migration_notes
  │                       └─ _record_version_metadata（记录 calculation_version）
  └─ 其它 / 缺失       → raise MigrationError（中文报错，含 from_version / to_version
                                              与可迁移版本列表）

返回 MigrationOutcome{ payload, notes, from_version, to_version, migrated }
```

> **实现要点（已核实）**：
> 1. `migrate_v1_to_v2` **只新增 `timeseries` 段**，不触碰任何既有参数值；
> 2. 项目**已含** `timeseries` 段时，只 `setdefault("enabled", False)` 并留痕，不覆盖用户值；
> 3. `migration_notes` 为**追加式**，多次迁移不丢历史；
> 4. 迁移函数通过 `_MIGRATION_STEPS` 注册表分派，新增版本只需加一条映射。

**V1 结果一致性如何保证**：迁移写入 `timeseries.enabled = False`，使项目继续走
**V1 年度模型代码路径**——因此 V1 结果一致是**结构性保证**（同一条代码路径），
而不是靠"迁移后重算比对"来事后校验。
（早期设计稿曾写"迁移后重算并比对差异 ≤ 1e-9，超出则拒载"；该自检**未实现**，
也**不需要**，因为路径相同。此处已按实现更正。）

---

## 10. 口径契约（V2 §95）

### 10.1 能量守恒（V2 §95）

V2 在 V1「年度能量守恒」（核心 §5、§113）之外，新增**逐时守恒校验**，容差统一 `1e-6`。
按**实际实现**（`EnergyBalance`，见 §7.2），守恒分三条关系、四个判定字段：

```text
B1 光伏侧：pv_generation = pv_to_load + pv_to_storage + pv_to_grid + pv_curtailed
B2 负荷侧：load_total    = pv_to_load + storage_to_load + grid_to_load
B3 供电侧：supply_total  = demand_total

判定字段：
    error            = supply_total − demand_total        （年度残差）
    max_hourly_error = max_t | 第 t 小时残差 |             （最差逐时残差）
    tolerance        = 1e-6                               （与 V1 §113 一致）
    is_balanced      = max_hourly_error ≤ tolerance 且 |error| ≤ tolerance
```

**SOC 守恒**不单独设残差字段：由 `storage_soc.py` 的递推式自身保证
（[STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) §2.2），并由
**SOC 越界即判定失败**（同文档 §2.3）作为等价约束。

**失败处理**：`is_balanced == False` 即判定本次计算失败，
**不得**带着不守恒的结果出报告（V2 §95）。中文提示需给出 `max_hourly_error` 与容差。

### 10.2 时点价值与基准对照

```text
实际购电净支出 NetCost = Σ_h net_energy_cost_h = Σ_h (grid_import_h × price_h) − Σ_h (grid_export_h × export_price_h)
基准（无光伏无储能）     Baseline = Σ_h (load_h × price_h)
节省额                   Saving = Baseline − NetCost
```

**注意**：`NetCost` 已隐含储能的削峰作用，因此**不得**再减去 `Σ storage_revenue`（见 §6.1.2 E2）。

### 10.3 与 V1 口径的继承与差异

| 口径项 | V1（核心 §5） | V2 | 是否改变 |
|---|---|---|---|
| 单位 | 基准单位，比例小数 | **相同** | ❌ 不变 |
| 时间轴 | Year 0 建设期，Year 1 起运营 | **相同**；时序结果按年汇总后接回 V1 现金流 | ❌ 不变 |
| 一度电一条收益 | §47 | **相同**（`pv_to_storage` 不计自用收益） | ❌ 不变 |
| 储能收益 | 年度 `放电×替代价 − 充电×充电价` | **逐时同式**，禁止简化为峰谷价差 | 🔶 细化 |
| 不可计算指标 | 返回 `None`，不得用 0 冒充 | **相同** | ❌ 不变 |
| 结果对象 | 唯一 `CalculationResult` | **相同**（仅新增字段） | ❌ 不变 |
| 政策参数 | 来自 `PolicyProfile`，带版本出处 | **相同**；并新增 `policy_version` 落盘 | 🔶 强化 |

### 10.4 必须披露的 V2 简化（同步写入 `notes`）

| 编号 | 简化 | 披露要求 |
|---|---|---|
| V2-L1 | 15 分钟分辨率本期只降采样，不做 15 分钟级调度 | 在报告口径页说明 |
| V2-L2 | 储能放电本期默认全部供负荷；上网需显式开启 `allow_export` | 在储能章节说明 |
| V2-L3 | SOC 初值默认取 `soc_min`；不建模跨日 SOC 优化 | 在储能章节说明 |
| V2-L4 | 无时序数据时按 V1 年度模型计算，**不合成**曲线 | 在项目概况说明 |
| V2-L5 | 需量电费按每小时 `grid_import` 的**月最大值**估算 | 在电价章节说明 |

---

## 11. 字段—条款—模块对照表

| # | 项目 | 条款 | 模块（V2 布局） |
|---|---|---|---|
| 1 | 唯一主时间索引 `timestamp` | V2 §7 | `domain/timeseries.py:TimeSeriesPoint` |
| 2 | 分辨率与点数（8760 / 8784 / 35040） | V2 §7 | `calculation/timeseries_engine.py` |
| 3 | 闰年判定 | V2 §7 | `calculation/timeseries_engine.py` |
| 4 | 列式数组存储 | V2 §86、§87 | `calculation/timeseries_engine.py` |
| 5 | `HourlyResult` 行视图物化 | V2 §6、§22 | `domain/timeseries_results.py` |
| 6 | `TimeSeriesPoint` 时间派生字段 | V2 §17 | `domain/timeseries.py` |
| 7 | `LoadProfile` 与其一致性校验 | V2 §9 | `domain/timeseries.py`、`data/validator.py` |
| 8 | `PVProfile` 与容量等比缩放 | V2 §9 | `domain/timeseries.py`、`calculation/pv_profile.py` |
| 9 | `TariffProfile` 与逐时电价解析 | V2 §9 | `domain/timeseries.py`、`calculation/tariff_series.py` |
| 10 | `StorageDispatchConfig` | V2 §9 | `domain/timeseries.py` |
| 11 | `HourlyResult` 21 个字段的计算式 | V2 §22、§23 | `calculation/energy_balance.py` |
| 12 | `DispatchDecision` 与中文原因 | V2 §16 | `calculation/dispatch_engine.py` |
| 13 | `CalculationResult` 8 个新增字段 | V2 §25、§62 | `domain/timeseries_results.py` |
| 14 | 基准对照 `baseline_results` | V2 §25 | `calculation/economic_v2.py` |
| 15 | 逐时能量守恒 B1/B2/B3 | V2 §95 | `calculation/energy_balance.py` |
| 16 | JSON 可序列化 | V2 §63 | `domain/timeseries_results.py`、`infrastructure/project_file.py` |
| 17 | `.nep` schema 2.0 | V2 §64 | `infrastructure/project_file.py` |
| 18 | V1 → V2 迁移 | V2 §65 | `infrastructure/project_file.py:migrate_v1_to_v2` |
| 19 | 时序 → 年度经济汇总 | V2 §62 | `calculation/economic_v2.py` |
| 20 | 数据质量评分 | V2 §56 | `data/quality.py` |

---

## 12. 变更纪律

1. 改任何**字段名** → 必须同步改本文档、[DATA_MODEL.md](DATA_MODEL.md) 的 V2 章节、
   `domain/timeseries.py`、`domain/timeseries_results.py`、对应测试；`pytest` 必须全绿。
2. 改任何**计算式** → 必须同步改本文档第 6 节、`CalculationResult.notes` 与测试（V1 §158）。
3. **列式数组的列顺序**必须与 `HourlyResult` 字段顺序一致；改顺序视为破坏性变更。
4. 新增简化 → 必须登记到第 10.4 节，并在报告中披露。
5. **V1 兼容性红线**：任何 V2 改动都不得改变 V1 年度模型的既有口径与结果
   （V1 Golden Case 必须继续通过，见 [CHANGELOG.md](CHANGELOG.md)）。
6. 本文档优先级：低于 `CORE_PARAMETERS_AND_FORMULAS.md`，与 `CALCULATION_ENGINE.md` 同级；
   与 [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md)、[OPTIMIZATION.md](OPTIMIZATION.md)、
   [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) 冲突时，以本文档的**模型与字段定义**为准。
