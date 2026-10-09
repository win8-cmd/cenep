# DATA_MODEL.md —— 数据模型与字段字典

> **优先级**：低于 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) 与
> [CALCULATION_ENGINE.md](CALCULATION_ENGINE.md)，高于 `PROJECT_SPEC.md`、`UI_SPEC.md`、
> `REPORT_SPEC.md`、`README.md` 与代码注释（§157）。
> 参数默认值与取值区间的**权威定义**在 CORE 文档；本文档讲清"数据长什么样"：
> 字段、类型、结构、枚举、来源模型、校验分层与持久化。
> 所有字段名、类型、默认值均**逐个对照源码核实**（`src/cenep/domain/`、`infrastructure/`）。

---

## 1. 设计原则

1. **本层只描述数据，不含任何计算**（`domain/models.py` docstring：所有公式在
   `cenep.calculation`）。
2. **禁止多余字段**：所有输入模型继承 `_Model(BaseModel)`，
   配置 `ConfigDict(extra="forbid", validate_assignment=True)`
   —— 写错字段名立刻报错，且**赋值时**同样校验。
3. **能算出来的不落库**（如储能时长、单位投资只出现在结果对象里）。
4. **比例与利率统一小数**（§14：10% 存 `0.10`）。
5. **来源可追溯**：重要参数挂 `ParameterMeta`（§83–§85、§91）。

```text
Project                          ← 项目聚合根（§11；V2 §4）
├── schema_version : str = "2.0"      ← V2：V1 的 1.0/1.1 打开时自动迁移
├── basic_info     : BasicInfo        §12  ├── load      : LoadConfig      §16
├── pv             : PVConfig         §18  ├── storage   : StorageConfig   §38
├── tariff         : TariffConfig     §29  ├── investment: InvestmentConfig §48
├── opex           : OpexConfig       §53  ├── tax       : TaxConfig       §57
├── financing      : FinancingConfig  §62
├── policy         : PolicyProfile | None              §34
├── scenario       : ScenarioConfig   §92（conservative / optimistic : ScenarioDelta）
├── sensitivity    : SensitivityConfig §95
├── timeseries     : TimeSeriesConfig ★ V2 时序仿真配置（默认 enabled=False）
├── migration_notes: list[str]        ★ V2 V1→V2 迁移留痕
├── analysis_period: int = 25                          §15
├── discount_rate  : float = 0.08                      §72
└── parameter_registry : dict[str, ParameterMeta]      §83

CalculationResult                ← 唯一结果对象（§81）
├── 项目标识 / 规模 / 投资 / 融资 / 首年概览 / 指标 / 现金流序列
├── annual_results : list[AnnualResult]      每一年完整结果（§80）
├── scenarios      : list[ScenarioSummary]   情景分析（§92）
├── sensitivity    : list[SensitivityRow]    敏感性分析（§95）
├── parameter_sources : dict[str, dict]      参数来源（§83、§108）
├── ★ V2 新增 8 个字段：
│     time_series_results : TimeSeriesReport | None      （V2 §25）
│     baseline_results    : BaselineResult | None
│     annual_results / dispatch_results : list[...]
│     energy_balance      : EnergyBalance | None
│     data_quality        : DataQualityScore | None
│     scenario_results    : list[ScenarioResult]
│     optimization_results: OptimizationResult | None
└── notes          : list[str]               口径说明
```

---

## 2. 完整字段字典（输入模型）

单位：金额 `元`、电量 `kWh`、比例/利率 `小数`（§14）。取值约束详见 CORE 文档第 3 节。

### 2.1 `Project`（§11、§9）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `schema_version` | `str` | `"2.0"` | 数据模型版本（`.nep` 兼容性；V1 的 1.x 打开时自动迁移，见 `migration.py`） |
| `basic_info` | `BasicInfo` | `BasicInfo()` | 项目基本信息 |
| `load` | `LoadConfig` | `LoadConfig()` | 负荷参数 |
| `pv` | `PVConfig` | `PVConfig()` | 光伏参数 |
| `storage` | `StorageConfig` | `StorageConfig()` | 储能参数 |
| `tariff` | `TariffConfig` | `TariffConfig()` | 电价参数 |
| `investment` | `InvestmentConfig` | `InvestmentConfig()` | 投资参数 |
| `opex` | `OpexConfig` | `OpexConfig()` | 运维参数 |
| `tax` | `TaxConfig` | `TaxConfig()` | 折旧与税务参数 |
| `financing` | `FinancingConfig` | `FinancingConfig()` | 融资参数 |
| `policy` | `PolicyProfile \| None` | `None` | 政策 Profile |
| `scenario` | `ScenarioConfig` | `ScenarioConfig()` | 情景分析配置 |
| `sensitivity` | `SensitivityConfig` | `SensitivityConfig()` | 敏感性分析配置 |
| `timeseries` | `TimeSeriesConfig` | `TimeSeriesConfig()` | **V2 时序仿真配置**（默认 `enabled=False`，走 V1 年度模式；字段字典见 `TIMESERIES_MODEL.md`） |
| `bills` | `list[ElectricityBill]` | `[]` | **V2.1 月电费账单事实**（手动录入 / Excel 导入）。默认空列表：旧 `.nep` 打开后账单为空，不虚构数据。字段字典见 `bill_models.py`；**账单模拟结果另建模型**，不得混入本列表 |
| `migration_notes` | `list[str]` | `[]` | **V2**：V1→V2 迁移留痕（既有参数未被修改的说明；V2.1 补账单段时也会追加一条） |
| `analysis_period` | `int` | `25` | 项目生命周期（年，§15） |
| `discount_rate` | `float` | `0.08` | 折现率（§72） |
| `parameter_registry` | `dict[str, ParameterMeta]` | `{}` | 参数来源登记表 |

派生成员（非字段）：`project_type` 属性（透传 `basic_info.project_type`）、
`summary_line()`（`"名称 | 类型 | 省 市"`）。

### 2.2 `BasicInfo`（§12、§98）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `project_name` | `str` | `"未命名项目"` | 项目名称（`min_length=1`） |
| `province` | `str` | `"湖北"` | 省份 |
| `city` | `str` | `""` | 城市 |
| `project_type` | `ProjectType` | `COMMERCIAL_PV` | 项目类型（决定启用哪些模块） |
| `evaluation_date` | `date` | `date.today()` | 评价日期 |
| `customer_name` | `str \| None` | `None` | 业主名称（报告展示） |
| `industry` | `str \| None` | `None` | 所属行业（报告展示） |
| `notes` | `str \| None` | `None` | 备注 |

### 2.3 `LoadConfig`（§16、§17）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `annual_load_kwh` | `float` | `0.0` | 用户全年**实际用电量**（非发电量、非购电量，§17） |
| `working_days` | `int` | `300` | 年工作天数【保留：不参与计算】 |
| `daytime_load_ratio` | `float` | `0.5` | 白天负荷占比【保留】 |
| `nighttime_load_ratio` | `float` | `0.5` | 夜间负荷占比【保留】 |
| `annual_load_growth_rate` | `float` | `0.0` | 年用电量增长率（小数） |

**模型级校验**：`daytime_load_ratio + nighttime_load_ratio == 1`（容差 `1e-6`），
否则 `ValueError("白天负荷占比与夜间负荷占比之和必须等于 1")`。

### 2.4 `PVConfig`（§18–§25、§99）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `pv_capacity_kwp` | `float \| None` | `None` | 直接输入装机容量 kWp（`gt=0`） |
| `roof_area_m2` | `float` | `0.0` | 屋顶总面积 m²（屋顶租金 `AREA` 模式基数） |
| `usable_roof_area_m2` | `float` | `0.0` | 可利用屋顶面积 m²（容量换算分子） |
| `area_per_kwp` | `float` | `6.0` | 单位容量占用面积 m²/kWp（换算分母） |
| `equivalent_hours` | `float` | `1100.0` | 年等效利用小时 h |
| `performance_ratio` | `float` | `1.0` | 系统综合损失修正系数 PR（`gt=0, ≤1`） |
| `annual_degradation_rate` | `float` | `0.005` | 年衰减率 |
| `curtailment_rate` | `float` | `0.0` | 限电率 C |
| `self_consumption_ratio` | `float` | `0.8` | 自发自用比例 |
| `loss_ratio` | `float` | `0.0` | 其他损失比例（默认 0） |
| `replacement_year` | `int \| None` | `None` | 光伏设备更换年份（如逆变器第 12 年，`1–60`） |
| `replacement_cost_per_kwp` | `float` | `0.0` | 光伏设备更换单价 元/kWp（仅在更换年份发生一次） |

### 2.5 `StorageConfig`（§38–§45、§100）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `storage_power_kw` | `float` | `0.0` | 储能功率 kW |
| `storage_energy_kwh` | `float` | `0.0` | 储能容量 kWh |
| `charge_efficiency` | `float \| None` | `None` | 充电效率 η_chg |
| `discharge_efficiency` | `float \| None` | `None` | 放电效率 η_dis |
| `round_trip_efficiency` | `float \| None` | `0.88` | 往返效率 |
| `annual_cycles` | `float` | `330.0` | 年等效循环次数（`0–1000`） |
| `depth_of_discharge` | `float` | `0.9` | 放电深度 DoD（`gt=0, ≤1`） |
| `annual_degradation_rate` | `float` | `0.02` | 年衰减率 |
| `replacement_year` | `int \| None` | `None` | 更换电芯年份（`1–60`） |
| `discharge_avoided_price` | `float \| None` | `None` | 放电替代电价（覆盖电价解析结果） |
| `charge_price` | `float \| None` | `None` | 充电电价（覆盖电价解析结果） |
| `annual_capacity_revenue` | `float` | `0.0` | 容量收益 元/年 |
| `annual_ancillary_revenue` | `float` | `0.0` | 辅助服务收益 元/年 |
| `annual_other_revenue` | `float` | `0.0` | 其他收益 元/年 |
| `replacement_capex` | `float` | `0.0` | 更换电芯投资 元 |

**模型级校验**：`charge_efficiency`、`discharge_efficiency`、`round_trip_efficiency`
**不能同时为 `None`**，否则 `ValueError("必须提供充电/放电效率，或往返效率")`。

> **储能单位投资只有一个数据源**：单位为 元/kWh 的储能投资参数是
> `InvestmentConfig.storage_capex_per_kwh`（投资参数页），CAPEX 计算只读它
> （`engine.py` 第 15 步 → `investment.compute_capex`）。
> **原先 `StorageConfig` 里同名的那份字段已在实现中删除**，以避免"两个同名字段、只有一个生效"
> 的隐患；储能参数页上的"储能单位投资"输入框现在直接绑定到 `investment.storage_capex_per_kwh`（同一数据源）。

### 2.6 `TariffConfig`（§29–§33、§101）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `tariff_mode` | `TariffMode` | `TOU` | 电价模式（决定解析分支） |
| `average_price` | `float` | `0.0` | 固定电价 元/kWh |
| `peak_price` | `float` | `1.0` | 峰电价 |
| `flat_price` | `float` | `0.7` | 平电价 |
| `valley_price` | `float` | `0.4` | 谷电价（`TOU` 的充电价） |
| `peak_ratio` | `float` | `0.3` | 峰电量比例 |
| `flat_ratio` | `float` | `0.4` | 平电量比例 |
| `valley_ratio` | `float` | `0.3` | 谷电量比例 |
| `market_price` | `float` | `0.0` | 市场电价 元/kWh |
| `custom_avoided_price` | `float \| None` | `None` | 自定义替代电价（`CUSTOM` 必填） |
| `custom_charge_price` | `float \| None` | `None` | 自定义充电电价（`CUSTOM` 必填） |
| `avoided_price_override` | `float \| None` | `None` | 替代电价用户覆盖（优先级最高，§85） |
| `charge_price_override` | `float \| None` | `None` | 充电电价用户覆盖（优先级最高） |
| `export_price` | `float` | `0.0` | 余电上网电价 元/kWh |
| `green_energy_price` | `float` | `0.0` | 绿电价格 元/kWh【保留：未参与计算】 |
| `green_environmental_value` | `float` | `0.0` | 绿色环境价值 元/kWh【保留】 |

**模型级校验**：`tariff_mode == TOU` 时 `peak_ratio + flat_ratio + valley_ratio == 1`
（容差 `1e-6`），否则 `ValueError("峰、平、谷电量比例之和必须等于 1")`。

### 2.7 `InvestmentConfig`（§48–§52、§102）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `mode` | `InvestmentMode` | `UNIT_PRICE` | 投资模式（两种互斥，§52） |
| `pv_capex_per_kw` | `float` | `3000.0` | 光伏单位投资 元/kWp（UNIT_PRICE 用） |
| `storage_capex_per_kwh` | `float` | `1000.0` | 储能单位投资 元/kWh（UNIT_PRICE 用） |
| `pv_capex` | `float` | `0.0` | DETAILED 模式下的光伏投资 |
| `storage_capex` | `float` | `0.0` | DETAILED 模式下的储能投资 |
| `grid_connection_cost` | `float` | `0.0` | 并网投资 |
| `roof_cost` | `float` | `0.0` | 屋顶加固/改造 |
| `development_cost` | `float` | `0.0` | 开发费用 |
| `engineering_cost` | `float` | `0.0` | 工程费用 |
| `construction_cost` | `float` | `0.0` | 施工费用 |
| `other_capex` | `float` | `0.0` | 其他投资 |
| `contingency` | `float` | `0.0` | 预备费 |
| `detailed_items` | `dict[str, float]` | `{}` | DETAILED 明细（**仅记录**，不计入九项之和） |

### 2.8 `OpexConfig`（§53–§56、§103）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `pv_opex` | `float` | `0.0` | 光伏运维（值按模式解释） |
| `pv_opex_mode` | `OpexMode` | `RATIO_OF_CAPEX` | 光伏运维模式 |
| `storage_opex` | `float` | `0.0` | 储能运维（值按模式解释） |
| `storage_opex_mode` | `OpexMode` | `RATIO_OF_CAPEX` | 储能运维模式 |
| `insurance` | `float` | `0.0` | 保险费（值按模式解释） |
| `insurance_mode` | `OpexMode` | `RATIO_OF_CAPEX` | 保险模式 |
| `management_cost` | `float` | `0.0` | 管理费 |
| `management_mode` | `OpexMode` | `FIXED` | 管理模式 |
| `other_opex` | `float` | `0.0` | 其他费用 |
| `other_opex_mode` | `OpexMode` | `FIXED` | 其他费用模式 |
| `roof_rent_mode` | `RoofRentMode` | `FIXED` | 屋顶租金模式 |
| `rent_per_m2` | `float` | `0.0` | 面积模式单价 元/m² |
| `rent_per_kw` | `float` | `0.0` | 容量模式单价 元/kWp |
| `annual_fixed_rent` | `float` | `0.0` | 固定租金 元/年 |
| `annual_opex_growth_rate` | `float` | `0.0` | 运维费用年增长率（`-0.2–0.5`） |

### 2.9 `TaxConfig`（§57–§61）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `depreciation_method` | `DepreciationMethod` | `STRAIGHT_LINE` | 折旧方法（V1 只支持直线法） |
| `depreciation_years` | `int` | `20` | 折旧年限（`>0, ≤50`） |
| `residual_value_ratio` | `float` | `0.05` | 残值率 |
| `depreciable_capex_ratio` | `float` | `1.0` | 可折旧投资占总投资比例 |
| `vat_rate` | `float` | `0.13` | 增值税税率（`0–0.5`） |
| `income_tax_rate` | `float` | `0.25` | 所得税税率（`0–0.5`） |
| `surcharge_rate` | `float` | `0.0` | 附加税费率（`0–0.2`，以不含税收入为基数） |
| `other_tax_rate` | `float` | `0.0` | 其他税费率（`0–0.2`） |
| `revenue_is_vat_inclusive` | `bool` | `False` | 收入是否为含税口径 |
| `lcoe_vat_deductible_ratio` | `float` | `0.0` | LCOE/LCOS 口径的增值税进项抵扣比例（`0–1`，0 = 不抵减） |
| `lcoe_residual_credit` | `bool` | `False` | 是否将残值现值作为 LCOE/LCOS 成本抵减 |

> `depreciation_method` 当前**未**被引擎传给 `tax.depreciation_for_year`
> （该函数默认 `STRAIGHT_LINE`）；字段保留以便 V2 扩展。

### 2.10 `FinancingConfig`（§62–§65、§104）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `enabled` | `bool` | `True` | 是否启用融资 |
| `debt_ratio` | `float` | `0.0` | 贷款比例 |
| `equity_ratio` | `float` | `1.0` | 资本金比例 |
| `loan_amount` | `float \| None` | `None` | 贷款金额（覆盖按比例计算的结果） |
| `interest_rate` | `float` | `0.04` | 贷款利率 |
| `loan_term` | `int` | `10` | 贷款期限（**含宽限期**，`>0, ≤40`） |
| `grace_period` | `int` | `0` | 宽限期（只付息不还本，`0–39`） |
| `repayment_method` | `RepaymentMethod` | `EQUAL_PRINCIPAL` | 还款方式 |

**模型级校验**：`debt_ratio + equity_ratio == 1`（容差 `1e-6`）；
`enabled` 时 `grace_period < loan_term`。

### 2.11 `PolicyProfile`（§34–§36、§89、§90）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `policy_id` | `str` | `""` | 政策标识 |
| `policy_name` | `str` | `""` | 政策名称 |
| `policy_version` | `str` | `""` | 政策版本 |
| `effective_date` | `date \| None` | `None` | 生效日期 |
| `expiry_date` | `date \| None` | `None` | 失效日期 |
| `province` | `str` | `""` | 适用省份 |
| `pricing_mechanism` | `str` | `""` | 价格机制说明 |
| `market_price` | `float` | `0.0` | 市场电价 |
| `mechanism_price` | `float` | `0.0` | 机制电价 元/kWh |
| `mechanism_volume_ratio` | `float` | `0.0` | 机制电量比例（`0–1`） |
| `green_energy_price` | `float` | `0.0` | 绿电价格 |
| `green_environmental_value` | `float` | `0.0` | 绿色环境价值 |
| `source` | `str` | `""` | 来源 |
| `source_url` | `str` | `""` | 来源链接 |
| `notes` | `str` | `""` | 备注 |

派生属性：`display_version` → `f"{policy_name}（版本：{policy_version}）"`；无版本时仅 `policy_name`。

### 2.12 `PolicyTemplate`（政策模板层，§34、§36、§90、§159）

**为什么需要它**：`PolicyProfile` 的数值字段是 `float`（不可为 `None`）。若模板把"未填写"
写成 `0.0`，报告就会出现"机制电价 0 元/kWh"这种**看起来像事实的假数据**（违反 §91）。
因此模板用 `None` 表达"未填写"，与 `0.0`（用户确实填了 0）**严格区分**。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `policy_id` | `str` | `""` | 模板标识（必填，`save_template` 会校验） |
| `policy_name` | `str` | `""` | 模板名称 |
| `policy_version` | `str \| None` | `None` | 政策版本（发布前必须填写） |
| `effective_date` | `date \| None` | `None` | 生效日期 |
| `expiry_date` | `date \| None` | `None` | 失效日期 |
| `province` | `str` | `"湖北"` | 适用省份 |
| `pricing_mechanism` | `str \| None` | `None` | 价格机制说明 |
| `market_price` | `float \| None` | `None` | 市场电价 |
| `mechanism_price` | `float \| None` | `None` | 机制电价 |
| `mechanism_volume_ratio` | `float \| None` | `None` | 机制电量比例 |
| `green_energy_price` | `float \| None` | `None` | 绿电价格 |
| `green_environmental_value` | `float \| None` | `None` | 绿色环境价值 |
| `source` | `str \| None` | `None` | 来源 |
| `source_url` | `str \| None` | `None` | 来源链接 |
| `notes` | `str` | `""` | 备注 |

方法：`unfilled_numeric_fields()`、`unfilled_text_fields()`、`is_complete()`、
`missing_description()`、`to_profile(*, allow_unfilled=False)`、`from_profile(profile)`。
未填完时 `to_profile()` 抛 `PolicyTemplateError`；`allow_unfilled=True` 强制转换时，
`notes` 会自动追加「未填写警示」。

字段中文标签：`NUMERIC_FIELD_LABELS`（5 个数值字段）、`TEXT_FIELD_LABELS`
（`policy_version` / `source` / `pricing_mechanism`）。

内置模板：`policy/hubei.py:HUBEI_TEMPLATE`（`policy_id="HUBEI_NEW_ENERGY_TARIFF"`，
**所有数值字段均为 `None`**，`source_url=None`）；另有
`describe_startup_notice(project) -> str` 生成启动时的政策版本确认提示（§90）。

### 2.13 情景与敏感性配置（§92–§96）

`ScenarioDelta` 八个乘数（`gt=0`），默认值为"保守 / 乐观"两套：

| 字段 | 保守默认 | 乐观默认 |
|---|---|---|
| `capex_multiplier` | `1.05` | `0.95` |
| `electricity_price_multiplier` | `0.90` | `1.10` |
| `generation_multiplier` | `0.95` | `1.05` |
| `opex_multiplier` | `1.05` | `0.95` |
| `self_consumption_ratio_multiplier` | `0.90` | `1.10` |
| `storage_cycles_multiplier` | `0.90` | `1.10` |
| `storage_capex_multiplier` | `1.05` | `0.95` |
| `interest_rate_multiplier` | `1.10` | `0.90` |

`describe()` 只返回**不等于 1.0** 的项，例如
`ScenarioDelta(capex_multiplier=1.1).describe() == ["总投资 × 1.1"]`。

| 配置 | 字段 | 默认 |
|---|---|---|
| `ScenarioConfig` | `enabled` | `True` |
| `ScenarioConfig` | `conservative` | `default_conservative_delta()`（上表左列） |
| `ScenarioConfig` | `optimistic` | `default_optimistic_delta()`（上表右列） |
| `SensitivityConfig` | `enabled` | `True` |
| `SensitivityConfig` | `steps` | `[-0.2, -0.1, 0.0, 0.1, 0.2]` |
| `SensitivityConfig` | `variables` | 8 个变量全选 |

---

## 3. 完整字段字典（结果模型）

### 3.1 `AnnualResult`（§80）—— 45 个字段

`model_config = ConfigDict(extra="forbid")`；除 `year` 外全部默认 `0.0`（`dscr` 默认 `None`）。

| # | 字段 | 类型 | 含义 |
|---|---|---|---|
| 1 | `year` | `int` | 年份，**1 = 第一个运营年度**（必填） |
| 2 | `load_kwh` | `float` | 当年负荷 |
| 3 | `pv_generation_kwh` | `float` | 光伏发电量 |
| 4 | `pv_self_use_kwh` | `float` | 光伏直接自用电量 |
| 5 | `pv_export_kwh` | `float` | 余电上网电量 |
| 6 | `pv_to_storage_kwh` | `float` | 光伏转入储能的电量 |
| 7 | `pv_loss_kwh` | `float` | 光伏分配损耗 |
| 8 | `storage_available_kwh` | `float` | 储能可用容量 |
| 9 | `storage_charge_kwh` | `float` | 储能年充电量 |
| 10 | `storage_discharge_kwh` | `float` | 储能年放电量 |
| 11 | `storage_grid_charge_kwh` | `float` | 电网补充充电量 |
| 12 | `pv_self_use_revenue` | `float` | 光伏自用收益 |
| 13 | `pv_export_revenue` | `float` | 光伏上网收益 |
| 14 | `pv_other_revenue` | `float` | 光伏其他收益（V1 恒 0） |
| 15 | `storage_arbitrage_revenue` | `float` | 储能套利收益 |
| 16 | `storage_capacity_revenue` | `float` | 储能容量收益 |
| 17 | `storage_ancillary_revenue` | `float` | 储能辅助服务收益 |
| 18 | `storage_other_revenue` | `float` | 储能其他收益 |
| 19 | `total_revenue` | `float` | 总收入 |
| 20 | `revenue_net` | `float` | 参与利润表的不含税收入口径 |
| 21 | `opex` | `float` | 运维费用 |
| 22 | `depreciation` | `float` | 折旧 |
| 23 | `ebitda` | `float` | EBITDA |
| 24 | `ebit` | `float` | EBIT |
| 25 | `interest` | `float` | 利息 |
| 26 | `ebt` | `float` | 税前利润 |
| 27 | `taxable_income` | `float` | 应纳税所得额 |
| 28 | `income_tax` | `float` | 所得税 |
| 29 | `surcharge` | `float` | 附加税费 |
| 30 | `other_tax` | `float` | 其他税费 |
| 31 | `cash_tax` | `float` | 现金税费合计 |
| 32 | `capex` | `float` | 当年投资（运营年恒 0） |
| 33 | `replacement_capex` | `float` | 更换电芯投资（仅更换年） |
| 34 | `residual_value` | `float` | 残值（仅末年） |
| 35 | `project_cashflow` | `float` | 项目现金流 |
| 36 | `equity_cashflow` | `float` | 资本金现金流 |
| 37 | `debt_begin` | `float` | 期初贷款余额 |
| 38 | `debt_drawdown` | `float` | 当年提款（运营年恒 0） |
| 39 | `principal_repayment` | `float` | 还本额 |
| 40 | `debt_end` | `float` | 期末贷款余额 |
| 41 | `cfads` | `float` | 可用于偿债的现金流 |
| 42 | `debt_service` | `float` | 还本付息合计 |
| 43 | `dscr` | `float \| None` | 当年偿债备付率（`None` = 无法计算） |
| 44 | `cumulative_project_cashflow` | `float` | 累计项目现金流 |
| 45 | `cumulative_equity_cashflow` | `float` | 累计资本金现金流 |

字段分组与源码注释分区一致：负荷与光伏 / 储能 / 收入 / 成本与利润 / 投资 / 现金流 /
债务 / 偿债 / 累计。

### 3.2 `CalculationResult`（§81）—— 唯一结果对象

`model_config = ConfigDict(extra="forbid")`。GUI、Excel、PDF **只能**消费本对象，
不得重新计算（§109、§148–§150）。

| 分组 | 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|---|
| 项目标识 | `project_name` | `str` | `""` | 项目名称 |
| | `project_type` | `str` | `""` | 项目类型值（如 `"PV_STORAGE"`） |
| | `province` | `str` | `""` | 省份 |
| | `city` | `str` | `""` | 城市 |
| 规模 | `pv_capacity_kwp` | `float` | `0.0` | 解析后的光伏容量 |
| | `storage_power_kw` | `float` | `0.0` | 储能功率 |
| | `storage_energy_kwh` | `float` | `0.0` | 储能容量 |
| | `storage_duration_hours` | `float` | `0.0` | 储能时长 |
| | `analysis_period` | `int` | `0` | 计算期 |
| 投资 | `total_capex` | `float` | `0.0` | 总投资 |
| | `unit_investment` | `dict[str, float]` | `{}` | 单位投资 |
| | `capex_breakdown` | `dict[str, float]` | `{}` | 九项中文名 → 金额 |
| 融资 | `loan_amount` | `float` | `0.0` | 贷款金额 |
| | `equity_amount` | `float` | `0.0` | 资本金 |
| 首年概览 | `first_year_generation` | `float` | `0.0` | 首年发电量 |
| | `first_year_self_use_energy` | `float` | `0.0` | 首年自用电量 |
| | `first_year_export_energy` | `float` | `0.0` | 首年上网电量 |
| | `first_year_revenue` | `float` | `0.0` | 首年收入 |
| | `first_year_opex` | `float` | `0.0` | 首年运维费 |
| | `first_year_cashflow` | `float` | `0.0` | 首年项目现金流 |
| | `annual_revenue` | `float` | `0.0` | 经营期年均收入 |
| | `annual_opex` | `float` | `0.0` | 经营期年均运维费 |
| 指标 | `project_irr` | `float \| None` | `None` | 项目 IRR（§70） |
| | `equity_irr` | `float \| None` | `None` | 资本金 IRR（§70） |
| | `project_npv` | `float` | `0.0` | 项目 NPV（§71） |
| | `equity_npv` | `float` | `0.0` | 资本金 NPV |
| | `static_payback` | `float \| None` | `None` | 静态回收期（§73） |
| | `discounted_payback` | `float \| None` | `None` | 动态回收期（§74） |
| | `lcoe` | `float \| None` | `None` | LCOE（§75） |
| | `lcos` | `float \| None` | `None` | LCOS（§77） |
| | `roi` | `float \| None` | `None` | ROI（§78） |
| | `min_dscr` | `float \| None` | `None` | 最低 DSCR（§79） |
| 现金流序列 | `project_cashflows` | `list[float]` | `[]` | 含 Year 0，长度 `N+1` |
| | `equity_cashflows` | `list[float]` | `[]` | 含 Year 0，长度 `N+1` |
| | `cumulative_cashflow` | `list[float]` | `[]` | 累计项目现金流 |
| 明细 | `annual_results` | `list[AnnualResult]` | `[]` | 长度 `N` |
| 情景/敏感性 | `scenarios` | `list[ScenarioSummary]` | `[]` | §92 |
| | `sensitivity` | `list[SensitivityRow]` | `[]` | §95 |
| 参数来源 | `parameter_sources` | `dict[str, dict]` | `{}` | §83、§108 |
| 口径说明 | `notes` | `list[str]` | `[]` | 口径披露（实测 ≥ 15 条） |

固定键（顺序即 Excel 行序）：

```text
capex_breakdown : 光伏投资、储能投资、并网投资、屋顶费用、开发费用、
                  工程费用、施工费用、其他投资、预备费                （9 个）
unit_investment : yuan_per_w  = TotalCAPEX / (PVCapacity_kWp × 1000)
                  yuan_per_wh = TotalCAPEX / (StorageEnergy_kWh × 1000)   （容量为 0 → 0.0）
```

### 3.3 `SensitivityRow`（§95、§96）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `variable` | `str` | 必填 | 变量枚举值（如 `"CAPEX"`） |
| `variable_label` | `str` | 必填 | 中文标签（如 `"总投资"`） |
| `change` | `float` | 必填 | 参数变化率（`-0.2` = −20%） |
| `project_irr` | `float \| None` | `None` | 该变化下的项目 IRR |
| `equity_irr` | `float \| None` | `None` | 该变化下的资本金 IRR |
| `project_npv` | `float` | `0.0` | 该变化下的项目 NPV |
| `static_payback` | `float \| None` | `None` | 该变化下的静态回收期 |
| `irr_change` | `float \| None` | `None` | IRR 相对基准的变化率 |
| `coefficient` | `float \| None` | `None` | 敏感度系数 = `irr_change / change` |

### 3.4 `ScenarioSummary`（§92、§93）

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `scenario` | `str` | 必填 | 情景枚举值（`BASE`/`CONSERVATIVE`/`OPTIMISTIC`） |
| `label` | `str` | 必填 | 中文标签（基准/保守/乐观） |
| `total_capex` | `float` | `0.0` | 该情景总投资 |
| `first_year_revenue` | `float` | `0.0` | 该情景首年收入 |
| `project_irr` | `float \| None` | `None` | 项目 IRR |
| `equity_irr` | `float \| None` | `None` | 资本金 IRR |
| `project_npv` | `float` | `0.0` | 项目 NPV |
| `static_payback` | `float \| None` | `None` | 静态回收期 |
| `deltas` | `list[str]` | `[]` | 情景乘数可读描述（BASE 为空列表） |

---

## 4. 枚举表（`domain/enums.py`）

所有枚举继承 `StrEnum`，**值等于名字**，便于 JSON 序列化与字典键。

### 4.1 带中文 `label` 的枚举

| 枚举 | 成员 → 值 | 中文标签 |
|---|---|---|
| `ProjectType`（§13） | `COMMERCIAL_PV` | 工商业光伏 |
| | `COMMERCIAL_STORAGE` | 工商业储能 |
| | `PV_STORAGE` | 工商业光储 |
| `TariffMode`（§29） | `FIXED` / `TOU` / `MARKET` / `CUSTOM` | 固定电价 / 峰平谷电价 / 市场电价 / 自定义电价 |
| `RepaymentMethod`（§62） | `EQUAL_PRINCIPAL` / `EQUAL_INSTALLMENT` | 等额本金 / 等额本息 |
| `ScenarioType`（§92） | `CONSERVATIVE` / `BASE` / `OPTIMISTIC` | 保守 / 基准 / 乐观 |
| `SensitivityVariable`（§94） | `CAPEX` | 总投资 |
| | `ELECTRICITY_PRICE` | 电价 |
| | `GENERATION` | 发电量 |
| | `OPEX` | 运维成本 |
| | `SELF_CONSUMPTION_RATIO` | 自用比例 |
| | `STORAGE_CYCLES` | 储能循环次数 |
| | `STORAGE_CAPEX` | 储能投资 |
| | `INTEREST_RATE` | 贷款利率 |
| `RiskLevel`（§106） | `EXCELLENT` / `NORMAL` / `WATCH` / `RISK` | 优秀 / 正常 / 关注 / 风险 |
| `SourceType`（§84） | 见 4.3 | 见 4.3 |

`ProjectType` 另有 `has_pv` / `has_storage` 两个属性，是**引擎分支的唯一依据**：
无光伏 → 强制容量 0；无储能 → 强制充放电 0。

> `RiskLevel` 的源码约束原文：**仅作提示，不得据此直接判定项目"可行/不可行"**（§106）。
> 该枚举已在 `domain/__init__.py` 导出，但 V1 计算层与报表层**尚未使用**（预留）。

### 4.2 不带 `label` 属性的枚举

| 枚举 | 成员 | 值 |
|---|---|---|
| `InvestmentMode`（§52） | `UNIT_PRICE` / `DETAILED` | 同名字符串（**无 `label`**） |
| `DepreciationMethod`（§57） | `STRAIGHT_LINE` | 同名字符串（**无 `label`**，V1 唯一取值） |
| `OpexMode`（§54） | `FIXED` / `RATIO_OF_CAPEX` | 同名字符串（**无 `label`**） |
| `RoofRentMode`（§56） | `AREA` / `CAPACITY` / `FIXED` | 同名字符串（**无 `label`**） |

### 4.3 `SourceType` 与配色（§84、§144）

| 成员 | 值 | `label` | `ui_color` |
|---|---|---|---|
| `USER_INPUT` | `"USER_INPUT"` | 用户输入 | `blue` |
| `POLICY` | `"POLICY"` | 政策参数 | `gray` |
| `CONTRACT` | `"CONTRACT"` | 合同参数 | `blue` |
| `HISTORICAL` | `"HISTORICAL"` | 历史数据 | `blue` |
| `EXPERIENCE` | `"EXPERIENCE"` | 行业经验 | `yellow` |
| `ASSUMPTION` | `"ASSUMPTION"` | 假设值 | `yellow` |
| `SYSTEM_DEFAULT` | `"SYSTEM_DEFAULT"` | 系统默认 | `yellow` |
| `CALCULATED` | `"CALCULATED"` | 系统计算 | `green` |

§144 的四色语义：**蓝 = 用户输入，绿 = 系统计算，黄 = 假设，灰 = 政策模板**；
合同/历史数据复用蓝色。Excel 侧的实际填充色见
`reports/excel_exporter.py:SOURCE_FILL`（`DDEBF7` / `E2EFDA` / `FFF2CC` / `EDEDED`）。

### 4.4 导出清单

`domain/__init__.py` 的 `__all__`：

```text
枚举：DepreciationMethod, InvestmentMode, OpexMode, ProjectType, RepaymentMethod,
      RiskLevel, RoofRentMode, ScenarioType, SensitivityVariable, SourceType, TariffMode
模型：BasicInfo, FinancingConfig, InvestmentConfig, LoadConfig, OpexConfig,
      PolicyProfile, Project, PVConfig, ScenarioConfig, ScenarioDelta,
      SensitivityConfig, StorageConfig, TariffConfig, TaxConfig
来源：UNITS, ParameterMeta, ParameterRegistry, SOURCE_PRIORITY, unit_of
结果：AnnualResult, CalculationResult
```

> `SensitivityRow` / `ScenarioSummary` **未**在 `domain/__init__.py` 导出，
> 需从 `cenep.domain.results` 直接导入。

---

## 5. 参数来源模型（§83–§85、§91）

### 5.1 `ParameterMeta`

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `value` | `float \| str \| bool \| None` | `None` | 参数值 |
| `unit` | `str` | `""` | 单位（取自 `UNITS`） |
| `source_type` | `SourceType` | `SYSTEM_DEFAULT` | 来源类型（§84） |
| `source_name` | `str` | `""` | 来源名称 |
| `source_date` | `date \| None` | `None` | 来源日期 |
| `source_url` | `str` | `""` | 来源链接 |
| `is_assumption` | `bool` | `False` | 是否假设值 |
| `note` | `str` | `""` | 备注 |
| `override_reason` | `str` | `""` | 用户覆盖系统默认值的原因（§85 要求记录） |

派生属性：`priority`（`SOURCE_PRIORITY[source_type]`，未知返回 0）、`color`（= `ui_color`）。

### 5.2 `SOURCE_PRIORITY`（§85，源码实际取值）

| `SourceType` | 优先级 |
|---|---|
| `SYSTEM_DEFAULT` | `0` |
| `EXPERIENCE` | `1` |
| `USER_INPUT` | `2` |
| `HISTORICAL` | `3` |
| `ASSUMPTION` | `3` |
| `POLICY` | `4` |
| `CONTRACT` | `5` |
| `CALCULATED` | `6` |

> **与概要口径的差异**（以源码为准）：① `HISTORICAL` 与 `ASSUMPTION` **并列** 3；
> ② `CALCULATED` 被赋予**最高**优先级 6。

### 5.3 `ParameterRegistry`

| 方法 | 说明 |
|---|---|
| `register(key, meta)` | 登记一条 |
| `register_value(key, value, unit="", source_type=SYSTEM_DEFAULT, source_name="", source_url="", is_assumption=False, note="", source_date=None)` | 快捷登记（**不设置** `override_reason`） |
| `get(key)` | 取一条 |
| `items()` | 返回 `dict` 副本 |
| `assumptions()` | `is_assumption` 或来源为 `ASSUMPTION`/`SYSTEM_DEFAULT`/`EXPERIENCE` |
| `policy_items()` | `source_type == POLICY` |
| `to_dict()` | 供结果对象与报表使用 |

`to_dict()` 每条的结构：

```text
value, unit, source_type, source_type_label, source_name,
source_date("YYYY-MM-DD" 或 ""), source_url, is_assumption, note, override_reason
```

`engine.py:_register_parameters` 实际登记的内容：24 个 `USER_INPUT` 键
（`analysis_period`、`discount_rate`、负荷 2 项、光伏 6 项、储能 5 项、投资 2 项、
税 4 项、融资 3 项）、6 个 `POLICY` 键（含 `policy.version`）、2 个 `ASSUMPTION`
（`scenario.conservative` / `scenario.optimistic`）、1 个 `SYSTEM_DEFAULT`
（`sensitivity.steps`）。**其余参数当前未登记**。

### 5.4 `UNITS` 与 `unit_of`

```python
UNITS = {"power": "kW", "pv_capacity": "kWp", "storage_energy": "kWh", "energy": "kWh",
         "price": "元/kWh", "money": "元", "area": "m²", "time": "年",
         "rate": "小数", "ratio": "小数"}
def unit_of(kind: str) -> str: ...      # 未知类型返回 ""，不抛异常（报表容错）
```

---

## 6. 校验分层

### 6.1 两道防线

```text
用户输入
 ├─ 第一道：Pydantic（domain/models.py）
 │    类型 / 取值范围(ge,gt,le,min_length) / 模型级交叉校验(@model_validator)
 │    extra="forbid" 拒绝未定义字段；validate_assignment=True 让赋值同样受校验
 │    → pydantic.ValidationError（英文，面向开发者）
 └─ 第二道：calculation/validator.py
      业务规则（按项目类型的必填项、非负性、比例一致性）
      面向用户的中文说明 + field 字段定位（§132）
      → cenep.calculation.errors.ValidationError（中文）
```

源码注释原文（`domain/models.py`）：Pydantic 做**类型与取值范围**的底线约束（§112）；
中文报错由 `cenep.calculation.validator` 统一产生（§132），保证 GUI 能提示
"哪个参数有问题"而不是抛裸异常。

### 6.2 Pydantic 承担的约束（摘要）

| 模型 | 约束 |
|---|---|
| `LoadConfig` | 数值范围；`daytime + nighttime == 1` |
| `PVConfig` | `pv_capacity_kwp > 0` 或 `None`；`area_per_kwp > 0`；`0 < PR ≤ 1`；比例 0–1 |
| `StorageConfig` | 效率 `0 < η ≤ 1`；三者不能全为 `None`；`0 < DoD ≤ 1`；循环 0–1000 |
| `TariffConfig` | 价格 `≥ 0`；比例 0–1；`TOU` 三比例和为 1 |
| `InvestmentConfig` | 全部金额 `≥ 0` |
| `OpexConfig` | 全部金额 `≥ 0`；增长率 `-0.2–0.5` |
| `TaxConfig` | 税率上限 0.5；残值率与可折旧比例 0–1 |
| `FinancingConfig` | `debt + equity == 1`；`grace < loan_term`（enabled 时） |
| `PolicyTemplate` | 数值字段可为 `None`，给了值则 `≥ 0`（比例 `≤ 1`） |
| `_Model` / 结果模型 | `extra="forbid"`、`validate_assignment=True` |

典型用例（`tests/test_validation.py::TestModelBackstop`）：

```python
StorageConfig(charge_efficiency=1.5)                             # ValidationError
TariffConfig(peak_ratio=0.5, flat_ratio=0.4, valley_ratio=0.3)   # ValidationError
FinancingConfig(debt_ratio=0.6, equity_ratio=0.6)                # ValidationError
StorageConfig(storage_energy_kwh=1.0, unknown_field=1)           # ValidationError（extra=forbid）
```

### 6.3 validator 承担的检查项

`validate_project(project)`（全部带 `field`）：

| 检查 | 报错（中文） | `field` |
|---|---|---|
| `analysis_period > 0` | 项目生命周期必须大于 0 | `analysis_period` |
| （含光伏）容量 > 0 | 光伏装机容量必须大于 0（可直接输入容量，或输入可利用屋顶面积与单位面积容量） | `pv.pv_capacity_kwp` |
| （含光伏）等效小时 > 0 | 年等效利用小时必须大于 0 | `pv.equivalent_hours` |
| （含光伏）`0 < PR ≤ 1` | 性能比必须大于 0 且不超过 100% | `pv.performance_ratio` |
| （含光伏）衰减 0–0.5 | 光伏年衰减率必须在 0~50% 之间 | `pv.annual_degradation_rate` |
| （含储能）容量 > 0 | 储能容量必须大于 0 | `storage.storage_energy_kwh` |
| （含储能）功率 > 0 | 储能功率必须大于 0 | `storage.storage_power_kw` |
| （含储能）`0 < DoD ≤ 1` | 放电深度 DoD 必须大于 0 且不超过 100% | `storage.depth_of_discharge` |
| （含储能）循环 ≥ 0 | 年循环次数不能为负数 | `storage.annual_cycles` |
| （含储能）衰减 0–0.5 | 储能年衰减率必须在 0~50% 之间 | `storage.annual_degradation_rate` |
| （含储能）效率 `0 < η ≤ 1` | 储能充电/放电/往返效率不能大于 100% 或小于等于 0 | `storage.efficiency` |
| （含储能）`η_chg × η_dis > 0` | 储能充电效率与放电效率的乘积必须大于 0 | `storage.efficiency` |
| （含储能）更换年 ≤ 计算期 | 更换电芯年份不能超过项目生命周期 | `storage.replacement_year` |
| 负荷 ≥ 0 | 年用电量不能为负数 | `load.annual_load_kwh` |
| 六项电价 ≥ 0 | `{标签}不能为负数` | `tariff.*` |
| `CUSTOM` 必填两个价格 | 自定义电价模式必须填写替代电价 / 充电电价 | `tariff.custom_avoided_price`、`tariff.custom_charge_price` |
| 税率/利率 0–1 | 增值税率/所得税率/贷款利率必须在 0~100% 之间 | `tax.vat_rate` 等 |
| 融资比例一致 | 贷款比例与资本金比例之和必须等于 1（当前为 …） | `financing.debt_ratio` |
| `loan_term > 0` | 贷款期限必须大于 0 | `financing.loan_term` |
| `grace < term` | 宽限期必须小于贷款期限，否则无法还本 | `financing.grace_period` |
| 单位投资 ≥ 0 | 光伏/储能单位投资不能为负数 | `investment.*` |

`validate_ratios(project)` 可单独调用，覆盖：`TOU` 三比例、`self_consumption_ratio`、
`curtailment_rate`、`residual_value_ratio`、`debt_ratio`、`discount_rate`。

### 6.4 守恒校验

```python
ENERGY_BALANCE_TOLERANCE = 1e-6          # pv.py
validate_energy_balance(annual_results, tolerance=ENERGY_BALANCE_TOLERANCE)
validate_storage_balance(annual_results, tolerance=1e-6)
```

- 前者：`PVGeneration == PVDirectUse + PVToStorage + PVExport + PVLoss`，
  误差 > 1e-6 抛 `EnergyBalanceError(field="pv.energy_balance")`（§113）；
- 后者：`pv_to_storage_kwh - storage_charge_kwh ≤ 1e-6`，否则抛
  `EnergyBalanceError(field="storage.charge_energy")`。

---

## 7. 持久化：`.nep` 项目文件

实现位置 `src/cenep/infrastructure/project_file.py`，测试 `tests/test_project_file.py`。

### 7.1 文件信封

```json
{
  "format": "cenep-project",
  "schema_version": "2.0",
  "app_version": "1.0.0",
  "calculation_version": "2.0.0",
  "policy_version": "<政策 profile_id，无政策时为空串>",
  "tariff_version": "<电价版本，无时为空串>",
  "saved_at": "2026-10-07T02:02:06",
  "project": { "...": "Project 的完整 JSON" }
}
```

| 信封字段 | 来源 | 说明 |
|---|---|---|
| `format` | 常量 `FILE_FORMAT` | 固定 `"cenep-project"`，用于识别本软件文件 |
| `schema_version` | 常量 `SCHEMA_VERSION`（= `migration.CURRENT_SCHEMA_VERSION`） | **V2 为固定 `"2.0"`**；`"1.0"` / `"1.1"` 的文件**自动迁移**后加载，其它版本拒绝加载 |
| `app_version` | `cenep.__version__` | 写文件时的软件版本 |
| `calculation_version` | 常量 `CALCULATION_ENGINE_VERSION` | **V2 §95**：`"2.0.0"`，供历史结果追溯（V2 §94） |
| `policy_version` | `project.policy.profile_id` | 无政策时为空串 |
| `tariff_version` | `project.tariff.tariff_version` | 电价参数版本 |
| `bills_schema_version` | 常量 `migration.BILL_SECTION_SCHEMA_VERSION` | **V2.1 §8.2**：账单段结构版本（`"1.0"`）。与 `calculation_version` 同级；旧读方忽略未知信封键，不受影响 |
| `saved_at` | `datetime.now().isoformat(timespec="seconds")` | 保存时间（**不参与幂等比较**，见 7.4） |
| `project` | `project.model_dump(mode="json")` | `Project` 的完整 JSON 化字典 |

> **实现要点**：序列化用 `project.model_dump(mode="json")` 再
> `json.dumps(payload, ensure_ascii=False, indent=2)`，**不是** `model_dump_json()`。
> `mode="json"` 负责把 `date`、枚举等转为 JSON 原生类型；编码固定 UTF-8，中文可读。
>
> **`computed_field` 的往返约定（V2）**：`domain/timeseries.py` 里的派生时间字段
> （`year` / `month` / `day` / `hour` / `weekday` / `is_weekend`）是 `@computed_field`，
> `model_dump()` 会**输出**它们（满足序列化要求 J1–J8），但读回时会被**剥离**
> 再交给 Pydantic —— 否则 `extra="forbid"` 会因"多余字段"拒绝加载，造成
> "存得进读不回"（V2 已修复缺陷 ②）。

### 7.2 `project` 字段构成

`project` 是 `Project` 的完整字典（见 2.1 节全部字段，V2.0 共 18 个字段；V2.1 追加 `bills` 后共 19 个）。序列化时的典型形态：

| 子对象 | 序列化形态 |
|---|---|
| `basic_info.project_type` | 字符串（如 `"COMMERCIAL_PV"`）；**启用时序后由容量归一化后写回**（V2 §105） |
| `basic_info.evaluation_date` | `"YYYY-MM-DD"` |
| `tariff.tariff_mode`、`investment.mode`、`tax.depreciation_method`、`financing.repayment_method`、`opex.*_mode`、`opex.roof_rent_mode` | 枚举字符串 |
| `scenario` / `sensitivity` | 嵌套字典；`sensitivity.variables` 为字符串列表（V2 为 8 个变量） |
| `timeseries` | **V2 新增段**：`enabled` / `resolution` / `base_year` / `load` / `pv` / `tariff` / `dispatch` / `holidays` / `balance_tolerance` |
| `migration_notes` | **V2 新增**：字符串列表；从 1.x 迁移时写入"既有参数未被修改"的中文留痕 |
| `policy` | `null` 或完整 `PolicyProfile` 字典 |
| `parameter_registry` | `{key: ParameterMeta 字典}`；计算后由引擎回写，保存时通常已含来源信息 |

### 7.3 常量与 API

```python
NEP_SUFFIX = ".nep"  ;  FILE_FORMAT = "cenep-project"
SCHEMA_VERSION = migration.CURRENT_SCHEMA_VERSION      # "2.0"（V2）
MIGRATABLE_SCHEMA_VERSIONS = ("1.0", "1.1")            # 可自动迁移的旧版本

ensure_suffix(path) -> Path          # 补 .nep 后缀（大小写不敏感比较）
save_project(project, path) -> Path  # 原子写入，返回实际路径
load_project(path) -> Project        # 读取 + 版本校验（或自动迁移）
read_file_info(path) -> ProjectFileInfo   # 只读信封（最近文件列表用）
autosave_path(path) -> Path          # "<stem>.autosave.nep"
autosave(project, path) -> Path      # 计算前自动保存（§134）
has_autosave(path) -> bool
recover_autosave(path) -> Project
```

迁移 API（`infrastructure/migration.py`）：

```python
CURRENT_SCHEMA_VERSION = "2.0"
CALCULATION_ENGINE_VERSION = "2.0.0"
LEGACY_SCHEMA_VERSIONS = ("1.0", "1.1")
SUPPORTED_SCHEMA_VERSIONS = ("2.0", "1.0", "1.1")

migrate_project_payload(payload, from_version) -> MigrationOutcome
migrate_v1_to_v2(payload) -> tuple[dict, list[str]]
# MigrationOutcome: payload / notes / from_version / to_version / migrated
```

`ProjectFileInfo`（frozen dataclass）：`path`、`project_name`、`project_type`、
`saved_at`、`schema_version`。

### 7.4 写入的原子性

```python
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(text, encoding="utf-8")
os.replace(tmp, path)        # 原子替换；finally 中清理残留 .tmp
```

> **幂等性断言的口径（V2 修复缺陷 ⑩）**：重复保存同一个项目时，
> 文件内容**只有 `saved_at` 会变**（精确到秒）。因此"存盘幂等"的回归断言必须
> 比较**去掉 `saved_at` 后的有效载荷**（`format` + 四个版本号 + `project`），
> 而不是比较包含时间戳的全文 —— 否则跨秒执行会偶发假失败。

### 7.5 加载错误（`ProjectFileError`）

| 情形 | 中文报错 |
|---|---|
| 文件不存在 | 项目文件不存在 |
| 不是合法 JSON | 项目文件不是合法的 JSON：{exc} |
| `OSError` | 读取项目文件失败：{exc} |
| `format` 不符 | 不是本软件的项目文件（缺少 format 标识） |
| `schema_version` 不受支持 | 项目文件版本不兼容：文件为 {文件版本}，当前软件支持 {SCHEMA_VERSION}（可自动迁移：1.0、1.1） |
| 缺少 `project` | 项目文件缺少 project 内容 |
| `project` 校验失败 | 项目内容校验失败：{exc} |
| 保存失败 | 保存项目文件失败：{exc} |
| **迁移失败（V2）** | 项目文件版本不受支持：文件为 {版本}，当前软件支持 2.0，可迁移版本 1.0、1.1（`MigrationError`） |

### 7.6 自动保存与恢复（§134）

```text
当前文件     ...\项目A.nep
自动保存文件 ...\项目A.autosave.nep
```

`CalculationService.calculate_with_outcome(project, current_path)` 在计算前调用
`ProjectService.autosave`；`autosave_enabled=False` 时返回 `None`。
恢复用 `recover_autosave(path)`；原文件**不会**被自动保存覆盖。

### 7.7 版本策略（V2）

| 项 | 现状 |
|---|---|
| 当前版本 | 写入 `schema_version = "2.0"`（`migration.CURRENT_SCHEMA_VERSION`） |
| `1.0` / `1.1` | ✅ **自动迁移**为 `2.0`：只补 `timeseries` 段的**显式默认值**、更新版本号、追加 `migration_notes`；**不改动任何既有参数、不重算、不删字段**。V2.1 起再补一次空 `bills` 段（`ensure_bill_section()`） |
| `2.0` | ✅ 直接打开；缺 `bills` 键时补**显式空列表**并追加 `migration_notes`（幂等：已有 `bills` 时不写任何说明） |
| V2.1 账单段 | ✅ **不升 `schema_version`**：账单是可选新增段（默认 `[]`），段结构版本写在信封 `bills_schema_version`（`"1.0"`）。因此 V2.0 与 V2.1 共用 `schema_version="2.0"`，双向可读 |
| 其它 / 缺失 | ❌ 拒绝加载，给中文提示并列出可迁移版本（`MigrationError`，**不猜测**） |
| 迁移留痕 | `Project.migration_notes: list[str]`，报告与界面均可展示"这个项目从哪个版本迁过来" |
| 未知字段 | `extra="forbid"` 导致加载失败；因此新增字段**必须给默认值** |

> **兼容性红线（V2 C2）**：迁移写入 `timeseries.enabled = False`，V1 项目
> 继续走**同一条 V1 年度模型代码路径**，因此"V1 结果可复现"是**结构性保证**，
> 不是事后比对。详见 [CHANGELOG.md](CHANGELOG.md) 的兼容性承诺表。

---

## 8. 持久化：SQLite（`infrastructure/db.py`）

项目完整数据仍在 `.nep`；SQLite 只做**索引与模板**，避免单点损坏。

### 8.1 表结构（5 张表）

| 表 | 列 | 主键 |
|---|---|---|
| `policy_profiles` | `policy_id`, `policy_version`, `province`, `payload`, `updated_at` | `(policy_id, policy_version)` |
| `policy_templates` | `policy_id`, `name`, `payload`, `updated_at` | `policy_id` |
| `project_templates` | `name`, `project_type`, `payload`, `updated_at` | `name` |
| `parameter_dictionary` | `key`, `value`, `unit`, `source_type`, `note`, `updated_at` | `key` |
| `project_history` | `id`(AUTOINCREMENT), `path`, `project_name`, `project_type`, `opened_at` | `id` |

> 模块 docstring 写的是"四类数据"，实际有 **5 张表**（`policy_templates` 为政策模板层新增）。
> `policy_profiles.payload` / `policy_templates.payload` / `project_templates.payload`
> 存 `model_dump_json()` 字符串；所有写入均为 `INSERT OR REPLACE`（幂等 upsert）。
> 政策主键含版本号，因此**新增版本不会覆盖旧版本**（§35）。

### 8.2 方法清单

| 方法 | 说明 |
|---|---|
| `Database(path="cenep.db")` | 建库建表；支持 `with Database(...) as db:` |
| `save_policy(policy)` | 政策版本 upsert；`policy_id` 空则回退 `policy_name`，版本空则存 `"未标注版本"` |
| `list_policies(province=None)` | 按 `updated_at DESC`；可按省份过滤 |
| `get_policy(policy_id, policy_version)` | 精确取一条 |
| `list_policy_versions(policy_id)` | 同一政策全部版本，按 `updated_at, rowid` 升序（§35） |
| `latest_policy(policy_id)` | 最近一次保存的版本 |
| `save_policy_template(policy_id, name, payload_json)` | 政策模板 upsert |
| `list_policy_templates()` | `[(policy_id, name)]`，按 `name` 排序 |
| `load_policy_template(policy_id)` | 返回 payload JSON 字符串或 `None` |
| `save_template(name, project)` / `list_templates()` / `load_template(name)` | 项目模板 |
| `set_parameter(key, value, unit, source_type, note)` / `get_parameter(key)` | 参数字典（值 JSON 编码） |
| `touch_history(path, project_name, project_type)` | 追加历史记录 |
| `recent_projects(limit=10)` | 按路径分组取最近一条，`ORDER BY MAX(id) DESC`（同秒内也稳定） |
| `close()` | 关闭连接 |

`recent_projects` 返回：

```python
[{"path": ..., "project_name": ..., "project_type": ..., "opened_at": ...}, ...]
```

### 8.3 政策模板与版本的配合（`policy/store.py`）

```text
PolicyStore(db, seed_builtin=True)
  ├── seed_builtin_templates()   幂等写入 HUBEI_TEMPLATE（已有则跳过，不覆盖用户修改）
  ├── save_template(t)           要求 policy_id 非空，否则 PolicyTemplateError
  ├── load_template(policy_id)   找不到抛 PolicyTemplateError
  ├── list_templates()           → [(policy_id, name)]
  ├── publish(t)                 to_profile() → 校验 policy_version 非空 → db.save_policy
  ├── versions(policy_id)        → db.list_policy_versions
  ├── latest(policy_id)          → db.latest_policy
  ├── attach(project, policy)    深拷贝项目并挂载政策，**不改原对象**
  └── startup_notice(project)    → describe_startup_notice（§90）
```

---

## 9. 变更纪律

1. **改字段必改三处**：`domain/models.py` / `domain/results.py`、本文档对应表格、
   必要时 CORE 文档的参数表与公式对照表。
2. **新增字段必须给默认值**：否则旧 `.nep` 文件会因缺字段加载失败。
3. **枚举新增成员**要同步：`label`、`ui_color`（若属 `SourceType`）、情景/敏感性处理、
   报表展示分支。
4. **改 `schema_version`** 必须同时给出迁移方案或明确拒绝策略，并更新 7.7 节。
5. **结果对象只增不改语义**：GUI / Excel / PDF 都消费 `CalculationResult`，
   改语义会同时影响三处展示与全部测试（§109）。
6. **政策模板永不预填具体数值**（§159）：新增省份模板时所有数值字段保持 `None`。
