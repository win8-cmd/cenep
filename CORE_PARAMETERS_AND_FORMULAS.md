# CORE_PARAMETERS_AND_FORMULAS.md —— 核心参数与公式（最高优先级规范）

> **本文档是 CENEP V1 的最高优先级规范。** 依据规范 §157，任何文档、注释、界面文案
> 与本文档冲突时，**一律以本文档为准**。冲突优先级：
>
> `CORE_PARAMETERS_AND_FORMULAS.md` > `CALCULATION_ENGINE.md` > `DATA_MODEL.md` >
> `PROJECT_SPEC.md` > `UI_SPEC.md` > `REPORT_SPEC.md` > `README.md` > 代码注释
>
> 本文档中的字段名、默认值、取值区间、公式与源码位置**逐个对照源码核实**（`src/cenep/`），
> 章节末附「公式—条款—源码」对照表。适用范围：**工商业分布式光伏 / 工商业储能 /
> 工商业光储**三类项目（§13）；不做集中式电站、风电、水电，不做 8760 小时仿真、GIS、CAD。
>
> 记号：`§N` = 产品规范第 N 条；`文件:函数` = 源码位置；【登记】= 被计算引擎写入
> `ParameterRegistry`（§83）；【保留】= 已建模但未参与 V1 计算；`TODO(V2)` = 待办（§158）。

---

## 1. 全局单位（§14）

内部计算**统一使用基准单位**；单位换算只发生在展示层（GUI / Excel / PDF），
**禁止**出现在计算层。

| 物理量 | 单位 | 物理量 | 单位 |
|---|---|---|---|
| 功率 | `kW` | 金额 | `元` |
| 光伏容量 | `kWp` | 面积 | `m²` |
| 储能容量 | `kWh` | 时间 | `年` |
| 电量 | `kWh` | 利率 / 比例 | **小数（10% 存 `0.10`）** |
| 电价 | `元/kWh` | 单位投资指标 | `元/W`、`元/Wh`（派生） |

单位字典集中在 `domain/provenance.py:UNITS`，由 `unit_of(kind)` 读取；
GUI 与报表**不得**各自硬编码单位字符串：

```python
UNITS = {"power": "kW", "pv_capacity": "kWp", "storage_energy": "kWh", "energy": "kWh",
         "price": "元/kWh", "money": "元", "area": "m²", "time": "年",
         "rate": "小数", "ratio": "小数"}
```

单位投资换算（`investment.py:unit_investment`，§102）：

```text
YuanPerW  = TotalCAPEX / (PVCapacity_kWp × 1000)      容量为 0 → 0.0
YuanPerWh = TotalCAPEX / (StorageEnergy_kWh × 1000)   容量为 0 → 0.0
```

辅助量纲：`元/kWp`、`元/kWh`（`InvestmentConfig`）、`次/年`（储能循环）、`m²/kWp`、`h`。

---

## 2. 时间模型（§15）

```text
Year 0  = 建设 / 初始投资期（不发电、不运维；只有投资与提款）
Year 1  = 第一个运营年度
Year n  = 第 n 个运营年度（n = 1..N）
Year N  = 计算期最后一年，回收残值 ResidualValue
```

| 项 | 约定 | 源码依据 |
|---|---|---|
| 计算期 | `Project.analysis_period`，默认 25 年，可改（1–40） | `models.py:Project` |
| 现金流下标 | `t = 0..N`，`t = 0` 即 Year 0 | `financial_metrics.py:npv` |
| Year 0 现金流 | `ProjectCF_0 = -TotalCAPEX`；`EquityCF_0 = -EquityAmount` | `cashflow.py:year0_*`（§69） |
| 提款 | Year 0 一次性全额提款 `LoanAmount` | `financing.py` 模块说明 |
| 残值 | 仅第 `N` 年计入 | `engine.py:_calculate_core` |
| 衰减/增长 | 一律 `(1±x)^(n-1)`，Year 1 取基准值 | `pv.py:degradation_factor` 等 |
| 回收期起算 | 自 Year 0（含建设期） | `financial_metrics.py:payback_period` |

任何 `year < 1` 的入参都直接报错（Year 0 不属于运营期）：

```python
if year < 1:
    raise ValueError("year 必须从 1 开始（Year 0 为建设期）")
```

---

## 3. 参数总表（按模块）

**列含义**：字段名 / 中文名 / 单位 / 默认值 / 取值范围 / 含义 / 来源登记（`—` = 未登记）。

### 3.1 项目级（`Project`，§11、§15、§72）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `schema_version` | 数据模型版本 | — | `"1.0"` | str | — |
| `analysis_period` | 项目生命周期 | 年 | `25` | `>0, ≤40` | `USER_INPUT` |
| `discount_rate` | 折现率 | 小数 | `0.08` | `0–0.5` | `USER_INPUT` |
| `policy` | 政策 Profile | — | `None` | `PolicyProfile \| None` | `POLICY`（字段级） |
| `scenario` | 情景配置 | — | 默认启用 | `ScenarioConfig` | `ASSUMPTION` |
| `sensitivity` | 敏感性配置 | — | 默认启用 | `SensitivityConfig` | `SYSTEM_DEFAULT` |
| `parameter_registry` | 来源登记表 | — | `{}` | `dict[str, ParameterMeta]` | 计算后回写 |

派生成员：`project_type`（透传）、`summary_line()`（"名称 | 类型 | 省 市"）。

### 3.2 `BasicInfo`（§12、§98）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `project_name` | 项目名称 | — | `"未命名项目"` | 长度 ≥ 1 | — |
| `province` | 省份 | — | `"湖北"` | str | — |
| `city` | 城市 | — | `""` | str | — |
| `project_type` | 项目类型 | — | `COMMERCIAL_PV` | 枚举 | — |
| `evaluation_date` | 评价日期 | — | `date.today()` | `date` | — |
| `customer_name` | 业主名称 | — | `None` | `str \| None` | — |
| `industry` | 所属行业 | — | `None` | `str \| None` | — |
| `notes` | 备注 | — | `None` | `str \| None` | — |

### 3.3 `LoadConfig`（§16、§17）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 含义 / 来源登记 |
|---|---|---|---|---|---|
| `annual_load_kwh` | 用户全年实际用电量 | kWh | `0.0` | `≥0` | 实际用电量，非发电量也非购电量；`USER_INPUT` |
| `working_days` | 年工作天数 | 天 | `300` | `0–366` | 【保留】仅展示；`—` |
| `daytime_load_ratio` | 白天负荷占比 | 小数 | `0.5` | `0–1` | 【保留】；`—` |
| `nighttime_load_ratio` | 夜间负荷占比 | 小数 | `0.5` | `0–1` | 【保留】；`—` |
| `annual_load_growth_rate` | 年用电量增长率 | 小数 | `0.0` | `-0.5–1.0` | 负荷增长 g；`USER_INPUT` |

模型级校验：`daytime_load_ratio + nighttime_load_ratio == 1`（容差 `1e-6`），
否则 `ValueError("白天负荷占比与夜间负荷占比之和必须等于 1")`。

### 3.4 `PVConfig`（§18–§25、§99）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `pv_capacity_kwp` | 直接输入装机容量 | kWp | `None` | `>0` 或 `None` | `USER_INPUT` |
| `roof_area_m2` | 屋顶总面积 | m² | `0.0` | `≥0` | — |
| `usable_roof_area_m2` | 可利用屋顶面积 | m² | `0.0` | `≥0` | — |
| `area_per_kwp` | 单位容量占用面积 | m²/kWp | `6.0` | `>0` | — |
| `equivalent_hours` | 年等效利用小时 `H` | h | `1100.0` | `>0` | `USER_INPUT` |
| `performance_ratio` | 综合损失修正系数 `PR` | 小数 | `1.0` | `>0, ≤1` | `USER_INPUT` |
| `annual_degradation_rate` | 年衰减率 `d` | 小数 | `0.005` | `0–0.5` | `USER_INPUT` |
| `curtailment_rate` | 限电率 `C` | 小数 | `0.0` | `0–1` | `USER_INPUT` |
| `self_consumption_ratio` | 自发自用比例 | 小数 | `0.8` | `0–1` | `USER_INPUT` |
| `loss_ratio` | 其他损失比例 | 小数 | `0.0` | `0–1` | — |

### 3.5 `StorageConfig`（§38–§45、§100）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `storage_power_kw` | 储能功率 | kW | `0.0` | `≥0` | `USER_INPUT` |
| `storage_energy_kwh` | 储能容量 | kWh | `0.0` | `≥0` | `USER_INPUT` |
| `charge_efficiency` | 充电效率 `η_chg` | 小数 | `None` | `>0, ≤1` | — |
| `discharge_efficiency` | 放电效率 `η_dis` | 小数 | `None` | `>0, ≤1` | — |
| `round_trip_efficiency` | 往返效率 | 小数 | `0.88` | `>0, ≤1` | — |
| `annual_cycles` | 年等效循环次数 | 次/年 | `330.0` | `0–1000` | `USER_INPUT` |
| `depth_of_discharge` | 放电深度 `DoD` | 小数 | `0.9` | `>0, ≤1` | `USER_INPUT` |
| `annual_degradation_rate` | 年衰减率 `d` | 小数 | `0.02` | `0–0.5` | `USER_INPUT` |
| `replacement_year` | 更换电芯年份 | 年 | `None` | `1–60` 且 `≤ analysis_period` | — |
| `discharge_avoided_price` | 放电替代电价（覆盖） | 元/kWh | `None` | `≥0` | — |
| `charge_price` | 充电电价（覆盖） | 元/kWh | `None` | `≥0` | — |
| `annual_capacity_revenue` | 容量收益 | 元/年 | `0.0` | `≥0` | — |
| `annual_ancillary_revenue` | 辅助服务收益 | 元/年 | `0.0` | `≥0` | — |
| `annual_other_revenue` | 其他收益 | 元/年 | `0.0` | `≥0` | — |
| `replacement_capex` | 更换电芯投资 | 元 | `0.0` | `≥0` | — |

模型级校验：`charge_efficiency` / `discharge_efficiency` / `round_trip_efficiency`
**不能同时为 `None`**，否则 `ValueError("必须提供充电/放电效率，或往返效率")`。

### 3.6 `TariffConfig`（§29–§33、§101）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 含义 |
|---|---|---|---|---|---|
| `tariff_mode` | 电价模式 | — | `TOU` | `FIXED/TOU/MARKET/CUSTOM` | 决定解析分支 |
| `average_price` | 固定电价 | 元/kWh | `0.0` | `≥0` | `FIXED` 的替代价与充电价 |
| `peak_price` | 峰电价 | 元/kWh | `1.0` | `≥0` | 峰段购电价 |
| `flat_price` | 平电价 | 元/kWh | `0.7` | `≥0` | 平段购电价 |
| `valley_price` | 谷电价 | 元/kWh | `0.4` | `≥0` | 谷段购电价，`TOU` 充电价 |
| `peak_ratio` | 峰电量比例 | 小数 | `0.3` | `0–1` | 综合电价权重 |
| `flat_ratio` | 平电量比例 | 小数 | `0.4` | `0–1` | 综合电价权重 |
| `valley_ratio` | 谷电量比例 | 小数 | `0.3` | `0–1` | 综合电价权重 |
| `market_price` | 市场电价 | 元/kWh | `0.0` | `≥0` | `MARKET` 的替代价与充电价 |
| `custom_avoided_price` | 自定义替代电价 | 元/kWh | `None` | `≥0` | `CUSTOM` 必填 |
| `custom_charge_price` | 自定义充电电价 | 元/kWh | `None` | `≥0` | `CUSTOM` 必填 |
| `avoided_price_override` | 替代电价（覆盖） | 元/kWh | `None` | `≥0` | 优先级最高（§85） |
| `charge_price_override` | 充电电价（覆盖） | 元/kWh | `None` | `≥0` | 优先级最高（§85） |
| `export_price` | 余电上网电价 | 元/kWh | `0.0` | `≥0` | 上网收益单价 |
| `green_energy_price` | 绿电价格 | 元/kWh | `0.0` | `≥0` | 【保留，见 L8】 |
| `green_environmental_value` | 绿色环境价值 | 元/kWh | `0.0` | `≥0` | 【保留，见 L8】 |

模型级校验：`TOU` 时三比例之和为 1（容差 `1e-6`）。本表除模式外**均未登记来源**。

### 3.7 `InvestmentConfig`（§48–§52、§102）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `mode` | 投资模式 | — | `UNIT_PRICE` | `UNIT_PRICE/DETAILED` | — |
| `pv_capex_per_kw` | 光伏单位投资 | 元/kWp | `3000.0` | `≥0` | `USER_INPUT` |
| `storage_capex_per_kwh` | 储能单位投资 | 元/kWh | `1000.0` | `≥0` | `USER_INPUT` |
| `pv_capex` | 光伏投资（DETAILED） | 元 | `0.0` | `≥0` | — |
| `storage_capex` | 储能投资（DETAILED） | 元 | `0.0` | `≥0` | — |
| `grid_connection_cost` | 并网投资 | 元 | `0.0` | `≥0` | — |
| `roof_cost` | 屋顶加固/改造 | 元 | `0.0` | `≥0` | — |
| `development_cost` | 开发费用 | 元 | `0.0` | `≥0` | — |
| `engineering_cost` | 工程费用 | 元 | `0.0` | `≥0` | — |
| `construction_cost` | 施工费用 | 元 | `0.0` | `≥0` | — |
| `other_capex` | 其他投资 | 元 | `0.0` | `≥0` | — |
| `contingency` | 预备费 | 元 | `0.0` | `≥0` | — |
| `detailed_items` | DETAILED 明细 | 元 | `{}` | `dict[str,float]` | — 【仅记录，见 L17】 |

### 3.8 `OpexConfig`（§53–§56、§103）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 |
|---|---|---|---|---|
| `pv_opex` / `pv_opex_mode` | 光伏运维（值 / 模式） | 按模式 | `0.0` / `RATIO_OF_CAPEX` | `≥0` / 枚举 |
| `storage_opex` / `storage_opex_mode` | 储能运维（值 / 模式） | 按模式 | `0.0` / `RATIO_OF_CAPEX` | `≥0` / 枚举 |
| `insurance` / `insurance_mode` | 保险费（值 / 模式） | 按模式 | `0.0` / `RATIO_OF_CAPEX` | `≥0` / 枚举 |
| `management_cost` / `management_mode` | 管理费（值 / 模式） | 元/年 | `0.0` / `FIXED` | `≥0` / 枚举 |
| `other_opex` / `other_opex_mode` | 其他费用（值 / 模式） | 元/年 | `0.0` / `FIXED` | `≥0` / 枚举 |
| `roof_rent_mode` | 屋顶租金模式 | — | `FIXED` | `AREA/CAPACITY/FIXED` |
| `rent_per_m2` | 面积单价 | 元/m² | `0.0` | `≥0` |
| `rent_per_kw` | 容量单价 | 元/kWp | `0.0` | `≥0` |
| `annual_fixed_rent` | 固定租金 | 元/年 | `0.0` | `≥0` |
| `annual_opex_growth_rate` | 运维年增长率 `g` | 小数 | `0.0` | `-0.2–0.5` |

`OpexMode` = `FIXED`（固定金额）| `RATIO_OF_CAPEX`（按投资比例）。本模块**均未登记来源**。
`RATIO_OF_CAPEX` 的基数：光伏运维取 `capex.pv_capex`，储能运维取 `capex.storage_capex`，
保险 / 管理 / 其他取 `total_capex`（见 §4.5）。

### 3.9 `TaxConfig`（§57–§61）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `depreciation_method` | 折旧方法 | — | `STRAIGHT_LINE` | 仅此一项 | — 【未传入，见 L5】 |
| `depreciation_years` | 折旧年限 | 年 | `20` | `>0, ≤50` | `USER_INPUT` |
| `residual_value_ratio` | 残值率 | 小数 | `0.05` | `0–1` | `USER_INPUT` |
| `depreciable_capex_ratio` | 可折旧投资占比 | 小数 | `1.0` | `0–1` | — |
| `vat_rate` | 增值税税率 | 小数 | `0.13` | `0–0.5` | `USER_INPUT` |
| `income_tax_rate` | 所得税税率 | 小数 | `0.25` | `0–0.5` | `USER_INPUT` |
| `surcharge_rate` | 附加税费率 | 小数 | `0.0` | `0–0.2` | — |
| `other_tax_rate` | 其他税费率 | 小数 | `0.0` | `0–0.2` | — |
| `revenue_is_vat_inclusive` | 收入是否含税 | — | `False` | bool | — |

### 3.10 `FinancingConfig`（§62–§65、§104）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `enabled` | 是否融资 | — | `True` | bool | — |
| `debt_ratio` | 贷款比例 | 小数 | `0.0` | `0–1` | `USER_INPUT` |
| `equity_ratio` | 资本金比例 | 小数 | `1.0` | `0–1` | — |
| `loan_amount` | 贷款金额（覆盖） | 元 | `None` | `≥0` 或 `None` | — |
| `interest_rate` | 贷款利率 | 小数 | `0.04` | `0–0.5` | `USER_INPUT` |
| `loan_term` | 贷款期限（含宽限期） | 年 | `10` | `>0, ≤40` | `USER_INPUT` |
| `grace_period` | 宽限期 | 年 | `0` | `0–39` 且 `< loan_term` | — |
| `repayment_method` | 还款方式 | — | `EQUAL_PRINCIPAL` | `EQUAL_PRINCIPAL/EQUAL_INSTALLMENT` | — |

模型级校验：`debt_ratio + equity_ratio == 1`（容差 `1e-6`）；
`enabled` 时 `grace_period < loan_term`。
实际还本年数 = `loan_term - grace_period`。

### 3.11 `PolicyProfile`（§34–§36、§89、§90）

| 字段名 | 中文名 | 单位 | 默认值 | 取值 | 来源登记 |
|---|---|---|---|---|---|
| `policy_id` | 政策标识 | — | `""` | str | — |
| `policy_name` | 政策名称 | — | `""` | str | — |
| `policy_version` | 政策版本 | — | `""` | str | `POLICY`（`policy.version`） |
| `effective_date` | 生效日期 | — | `None` | `date \| None` | — |
| `expiry_date` | 失效日期 | — | `None` | `date \| None` | — |
| `province` | 适用省份 | — | `""` | str | — |
| `pricing_mechanism` | 价格机制说明 | — | `""` | str | — |
| `market_price` | 市场电价 | 元/kWh | `0.0` | `≥0` | `POLICY` |
| `mechanism_price` | 机制电价 | 元/kWh | `0.0` | `≥0` | `POLICY` |
| `mechanism_volume_ratio` | 机制电量比例 | 小数 | `0.0` | `0–1` | `POLICY` |
| `green_energy_price` | 绿电价格 | 元/kWh | `0.0` | `≥0` | `POLICY` |
| `green_environmental_value` | 绿色环境价值 | 元/kWh | `0.0` | `≥0` | `POLICY` |
| `source` | 来源 | — | `""` | str | — |
| `source_url` | 来源链接 | — | `""` | str | — |
| `notes` | 备注 | — | `""` | str | — |

派生属性 `display_version`：`f"{policy_name}（版本：{policy_version}）"`；无版本时仅 `policy_name`。

**§34 要求政策独立建模，不得写死**。V1 中政策参与**来源登记与报告披露**；
政策**模板**层由 `src/cenep/policy/` 提供（`PolicyTemplate` 的数值字段可为 `None`，
未填完时 `to_profile()` **拒绝转换**，避免把"未填写"当成 0 写进报告，§159）。
V1 仍未实现"政策数值自动折算为电价"（见 L7）。

### 3.12 情景与敏感性（§92–§96）

`ScenarioDelta` 八个乘数（默认全 `1.0`，约束 `gt=0`）：

| 字段名 | 中文名 | 保守默认 | 乐观默认 |
|---|---|---|---|
| `capex_multiplier` | 总投资 | `1.05` | `0.95` |
| `electricity_price_multiplier` | 电价 | `0.90` | `1.10` |
| `generation_multiplier` | 发电量 | `0.95` | `1.05` |
| `opex_multiplier` | 运维成本 | `1.05` | `0.95` |
| `self_consumption_ratio_multiplier` | 自用比例 | `0.90` | `1.10` |
| `storage_cycles_multiplier` | 储能循环次数 | `0.90` | `1.10` |
| `storage_capex_multiplier` | 储能投资 | `1.05` | `0.95` |
| `interest_rate_multiplier` | 贷款利率 | `1.10` | `0.90` |

| 配置 | 字段名 | 默认值 | 来源登记 |
|---|---|---|---|
| `ScenarioConfig` | `enabled` | `True` | — |
| `ScenarioConfig` | `conservative` | `default_conservative_delta()` | `ASSUMPTION`（`scenario.conservative`） |
| `ScenarioConfig` | `optimistic` | `default_optimistic_delta()` | `ASSUMPTION`（`scenario.optimistic`） |
| `SensitivityConfig` | `enabled` | `True` | — |
| `SensitivityConfig` | `steps` | `[-0.2, -0.1, 0.0, 0.1, 0.2]` | `SYSTEM_DEFAULT`（`sensitivity.steps`） |
| `SensitivityConfig` | `variables` | 8 个变量全选 | — |

`BASE` 情景不读乘数，直接用 `ScenarioDelta()`（全 1）。默认乘数标注为**假设值**，
可在界面修改（§91、§158）。实际参与计算的敏感性变量会按项目类型过滤（§4.8）。

---

## 4. 公式清单（按模块）

每个公式给出数学式、规范条款与源码位置（相对 `src/cenep/calculation/`）。

### 4.1 光伏（`pv.py`，§18–§25）

**（1）容量解析（§19）**

```text
若 pv_capacity_kwp > 0:                      PVCapacity = pv_capacity_kwp,          依据 "INPUT"
否则若 usable_roof_area > 0 且 area_per_kwp > 0: PVCapacity = usable_roof_area / area_per_kwp, 依据 "AREA"
否则:                                        PVCapacity = 0,                        依据 "NONE"
```

源码：`pv.py:resolve_pv_capacity` → `(容量 kWp, 依据)`。项目类型不含光伏时引擎强制置 0。

**（2）首年发电量（§20）**

```text
G1 = Ppv × H × PR × (1 - C)
     Ppv = 装机容量 kWp；H = 年等效小时；PR = performance_ratio；C = curtailment_rate
```

源码：`pv.py:first_year_generation`。
§21：若用户输入的等效小时已是最终可利用小时，则 `PR = 1`，本函数**不做额外扣减**。

**（3）逐年衰减（§22）**

```text
deg(n) = (1 - d)^(n-1) ,  Gn = G1 × deg(n)      n = 1..N
```

源码：`pv.py:degradation_factor`、`pv.py:generation_for_year`。

**（4）电量分配（§23–§25、§46）**

```text
loss          = Generation × loss_ratio
usable        = max(Generation - loss, 0)
target_direct = usable × self_consumption_ratio
direct_use    = min(target_direct, Load)                  # §24：不超过用户负荷
remaining     = max(usable - direct_use, 0)
to_storage    = min(remaining, storage_charge_headroom)   # 受储能年充电能力限制
export        = max(remaining - to_storage, 0)

守恒（§23）：Generation = direct_use + to_storage + export + loss
误差（§113）：balance_error = Generation - (direct_use + to_storage + export + loss)
              is_balanced = |balance_error| ≤ 1e-6
```

分配顺序（§46）：**直接自用 → 储能 → 余电上网**。
源码：`pv.py:allocate_pv_energy` → `PvAllocation(generation, direct_use, to_storage, export, loss)`；
误差见 `pv.py:PvAllocation.balance_error`，阈值 `pv.py:ENERGY_BALANCE_TOLERANCE = 1e-6`。

> **实现与 docstring 的差异**：`allocate_pv_energy` 的 docstring 把 `loss` 列为第 6 步，
> 而**代码是"先扣损耗再分配"**（`export = remaining - to_storage`）。两种写法数值等价
> （都满足 §23 守恒），**本文档以代码实现为准**。

**（5）电网补充充电量（引擎派生量）**

```text
GridCharge = max(storage_charge - pv_to_storage, 0)
```

源码：`engine.py:_calculate_core`（逐年循环）。该量用于说明储能充电量中有多少来自电网，
**不改变** §43 的套利口径。

### 4.2 储能（`storage.py`，§37–§45）

**（1）储能时长（§39）**

```text
StorageDuration = StorageEnergy / StoragePower      （StoragePower ≤ 0 时返回 0.0）
```

源码：`storage.py:storage_duration_hours`。

**（2）效率换算（§40）**

```text
给出 η_chg 且给出 η_dis :  (η_chg, η_dis, η_chg × η_dis)
只给出 round_trip      :  (√round_trip, √round_trip, round_trip)
只给出 η_chg           :  (η_chg, η_chg, η_chg²)
只给出 η_dis           :  (η_dis, η_dis, η_dis²)
全为 None              :  raise ValueError("必须提供充电效率/放电效率，或往返效率")
恒等关系                :  round_trip = charge × discharge
```

源码：`storage.py:resolve_efficiencies`。

**（3）年放电量与年充电量（§41、§42）**

```text
Edis = AvailableEnergy × DoD × Cycles × η_dis          # 交流侧口径 kWh
Echg = Edis / η_dis / η_chg                            # 效率 ≤ 0 抛 ValueError
```

源码：`storage.py:annual_discharge_energy`、`storage.py:annual_charge_energy`。

**（4）逐年可用容量（§45、§38）**

```text
未设更换年 R:  Available_n = Initial × (1 - d)^(n-1)
设了更换年 R:  n <  R → Available_n = Initial × (1 - d)^(n-1)
              n ≥ R → Available_n = Initial × (1 - d)^(n-R)     ← 更换当年恢复至初始值
              is_replacement_year = (n == R)
```

源码：`storage.py:available_energy_for_year` → `(可用容量, 是否更换年)`。
更换年同时计入 `ReplacementCAPEX`（§38 的 V1 明确口径）。

**（5）串联结果**

```text
storage_year_result(...) → StorageYear(year, available_energy_kwh,
                                       discharge_energy_kwh, charge_energy_kwh,
                                       is_replacement_year)
```

源码：`storage.py:storage_year_result`。引擎把当年 `Echg` 作为光伏分配的
`storage_charge_headroom` 入参。V1 采用**年度等效循环模型**，不做 8760 小时仿真（§37）。

### 4.3 电价与收益（`revenue.py`，§26–§33、§43、§44）

**（1）峰平谷综合电价（§32）**

```text
AverageTOUPrice = Peak × PeakRatio + Flat × FlatRatio + Valley × ValleyRatio
三比例之和必须为 1，否则 ValidationError(field="tou_ratios")   （§31）
```

源码：`revenue.py:average_tou_price`。

**（2）电价解析（§29–§33）**

| `tariff_mode` | 替代电价 avoided | 充电电价 charge | `basis` 文案 |
|---|---|---|---|
| `FIXED` | `average_price` | `average_price` | 固定电价 |
| `TOU` | `AverageTOUPrice` | `valley_price`（谷充） | 峰平谷电价（替代价取综合电价，充电取谷价） |
| `MARKET` | `market_price` | `market_price` | 市场电价 |
| `CUSTOM` | `custom_avoided_price` | `custom_charge_price` | 自定义电价 |
| 其他 | `ValidationError(field="tariff_mode")` | — | — |

显式覆盖（§85）：`avoided_price_override` / `charge_price_override` 不为 `None` 时覆盖上述结果。
源码：`revenue.py:resolve_tariff` → `TariffResolution(avoided_electricity_price, charge_price,
export_price, average_tou_price, basis)`。

储能侧可再单独覆盖（§43，`engine.py` 第 10 步之后）：

```text
DischargeAvoidedPrice = storage.discharge_avoided_price  若不为 None，否则 tariff.avoided_electricity_price
ChargePrice           = storage.charge_price             若不为 None，否则 tariff.charge_price
```

**（3）三项光伏/储能收益（§27、§28、§43）**

```text
PVSelfUseRevenue        = PVSelfUse × AvoidedElectricityPrice          （§27）
PVExportRevenue         = PVExport × ExportPrice                       （§28）
StorageArbitrageRevenue = Edis × DischargeAvoidedPrice − Echg × ChargePrice   （§43）
```

源码：`revenue.py:self_use_revenue`、`export_revenue`、`storage_arbitrage_revenue`。

> **V1 口径（写入报告）**：按 §43 原文，**全部充电量按 `ChargePrice` 计价**，包含由光伏
> 转入储能的电量；该部分电量在光伏侧**不计**自用收益（§47），因此不存在重复计算。

**（4）储能四类收益与总收益（§44）**

```text
StorageTotalRevenue = Arbitrage + Capacity + Ancillary + Other          # 四类必须分开记录
TotalRevenue = PVSelfUseRevenue + PVExportRevenue + PVOtherRevenue
             + StorageArbitrage + StorageCapacity + StorageAncillary + StorageOther
```

源码：`revenue.py:storage_total_revenue`；总收益在 `engine.py` 第 14 步。
`pv_other_revenue` 当前恒为 `0.0`【保留，L9】。

### 4.4 投资（`investment.py`，§48–§52）

```text
TotalCAPEX = PV_CAPEX + Storage_CAPEX + GridConnection + Roof + Development
           + Engineering + Construction + Other + Contingency            （§49，九项之和）

UNIT_PRICE：PV_CAPEX      = PVCapacity × PV_CAPEX_PerKW                   （§50）
            Storage_CAPEX = StorageEnergy × StorageCAPEXPerKWh            （§51）
DETAILED  ：PV_CAPEX / Storage_CAPEX 直接取用户填写金额，容量不参与计算     （§52）
非法模式 → ValidationError(field="investment_mode")；投资为负 → ValidationError(field="capex")
```

源码：`investment.py:compute_capex`、`investment.py:CapexBreakdown.total`。

### 4.5 运维（`opex.py`，§53–§56）

```text
单项：FIXED → amount = value
      RATIO_OF_CAPEX → amount = capex_base × value
      其他模式 → ValidationError；amount < 0 → ValidationError("运维费用不能为负数")

屋顶租金：AREA     → RoofArea × RentPerM2          （RoofArea = PVConfig.roof_area_m2）
          CAPACITY → PVCapacity × RentPerKW
          FIXED    → AnnualFixedRent

首年合计：Opex_1 = PVOpex + StorageOpex + RoofRent + Insurance + Management + Other   （§54）
逐年增长：Opex_n = Opex_1 × (1 + g)^(n-1)                                            （§55）
```

源码：`opex.py:resolve_opex_item`、`opex.py:roof_rent`、`opex.py:OpexBreakdown.first_year_total`、
`opex.py:opex_for_year`（`year < 1` 抛 `ValueError`）。
`capex_base`：光伏运维 → `capex.pv_capex`；储能运维 → `capex.storage_capex`；
保险 / 管理 / 其他 → `total_capex`（`engine.py` 第 16 步）。

### 4.6 折旧与税务（`tax.py`，§57–§61）

```text
DepreciableCAPEX = TotalCAPEX × depreciable_capex_ratio
DepreciableBase  = DepreciableCAPEX × (1 - ResidualValueRatio)              （§58）
AnnualDepreciation = DepreciableBase / DepreciationYears
Depreciation_n   = AnnualDepreciation  当 n ≤ depreciation_years，否则 0      （§58）
ResidualValue    = DepreciableCAPEX × ResidualValueRatio                    （§67，仅末年）

RevenueNet = Revenue / (1 + vat_rate)  当 revenue_is_vat_inclusive，否则 Revenue

EBITDA = RevenueNet - Opex          （§60）
EBIT   = EBITDA - Depreciation      （§60）
EBT    = EBIT - Interest            （§60）
TaxableIncome = max(EBT, 0)         （§61）
IncomeTax     = TaxableIncome × IncomeTaxRate                                （§61）
Surcharge     = RevenueNet × surcharge_rate        （V1 简化：以不含税收入为基数）
OtherTax      = RevenueNet × other_tax_rate        （V1 简化）
CashTax       = IncomeTax + Surcharge + OtherTax
```

源码：`tax.py:depreciable_base`、`annual_depreciation`、`depreciation_for_year`、
`residual_value`、`revenue_net`、`ebitda_of`、`ebit_of`、`ebt_of`、`taxable_income_of`、
`income_tax_of`、`surcharge_of`、`other_tax_of`、`tax_year_result`、`TaxYear.cash_tax`。
非直线法抛 `ValueError`（V1 只支持 `STRAIGHT_LINE`）。

### 4.7 融资（`financing.py`，§62–§65）

```text
LoanAmount   = TotalCAPEX × DebtRatio            （§63；loan_amount 不为 None 时覆盖；enabled=False → 0）
EquityAmount = TotalCAPEX - LoanAmount           （§63）

n_repay = loan_term - grace_period               （宽限期内还本为 0）

EQUAL_PRINCIPAL：per_year = LoanAmount / n_repay，逐年归还，末年轧平余额
EQUAL_INSTALLMENT：r ≤ 0 → 退化为等额本金
                   r > 0 → factor = (1+r)^n_repay
                           annuity = LoanAmount × r × factor / (factor - 1)
                           逐年：interest_decomp = 期初余额 × r
                                 principal = min(annuity - interest_decomp, 余额)
                                 末年 principal = 余额（轧平）

EndingDebt         = BeginningDebt + Drawdown - PrincipalRepayment ≥ 0     （§64）
AverageDebtBalance = (BeginningDebt + EndingDebt) / 2
Interest           = AverageDebtBalance × InterestRate                     （§65）
DebtService        = PrincipalRepayment + Interest
```

源码：`financing.py:loan_amount_of`、`equity_amount_of`、`principal_schedule`、
`build_loan_schedule`、`LoanYear.debt_service`。
运营期 `drawdown` 恒为 `0.0`（提款只在 Year 0）。
**严禁**用「原始贷款 × 利率」作为所有年份的利息（§65）。

### 4.8 现金流（`cashflow.py`，§66–§69）

```text
ProjectCF = EBITDA - CashTax - CAPEX - ReplacementCAPEX + ResidualValue    （§67）
EquityCF  = EBITDA - CashTax - Interest - PrincipalRepayment - EquityCAPEX
            - ReplacementCAPEX + DebtDrawdown + ResidualValue             （§68）
ProjectCF_0 = -TotalCAPEX ;  EquityCF_0 = -EquityAmount                    （§69）
Cum_t = Σ_{i=0..t} CF_i                                                    （累计）
```

源码：`cashflow.py:project_cashflow`、`equity_cashflow`、`year0_project_cashflow`、
`year0_equity_cashflow`、`cumulative`。运营年 `CAPEX = 0`、`EquityCAPEX = 0`、`DebtDrawdown = 0`。

### 4.9 财务指标（`financial_metrics.py`，§70–§79）

```text
NPV = Σ_{t=0..N} CF_t / (1 + r)^t          t = 0 即除以 1，Year 0 不额外折现      （§71）

IRR：求 r 使 NPV(r) = 0
     可解性：现金流至少一个正值与一个负值，否则 None                            （§114）
     算法：二分法，区间 [-0.9999, 10.0]，固定 300 次迭代（确定性，§118）；
           区间内无符号变化时先以 2001 点网格扫描，仍找不到则 None
     常量：_IRR_LOW = -0.9999、_IRR_HIGH = 10.0、_IRR_ITERATIONS = 300

静态回收期：Payback = (n-1) + |Cum_(n-1)| / CF_n
            n = 首次满足 Cum_(n-1) < 0 ≤ Cum_n 的年份；始终未回收或 CF_n ≤ 0 → None   （§73）
动态回收期：对 DiscountedCF_t = CF_t/(1+r)^t 用同一插值法                          （§74）

LCOE = Σ_t (Cost_t/(1+r)^t) / Σ_t (Energy_t/(1+r)^t)   能量折现值 ≤ 0 → None      （§75、§76）
LCOS = Σ_t (StorageCost_t/(1+r)^t) / Σ_t (DischargeEnergy_t/(1+r)^t)              （§77）
       —— LCOE/LCOS 的成本均不含融资利息（§76）
ROI  = 生命周期累计净收益 / 初始总投资；初始总投资 ≤ 0 → None                      （§78）
       生命周期累计净收益 = Σ_n (EBITDA_n - CashTax_n)
CFADS = EBITDA - CashTax - MaintenanceCAPEX      （此处 = 该年 replacement_capex） （§79）
DSCR_n = CFADS_n / DebtService_n，DebtService ≤ 0 → None；最低 DSCR = min(可算值) （§79）
```

源码：`financial_metrics.py:npv`、`irr`、`has_sign_change`、`payback_period`、
`discounted_cashflows`、`discounted_payback_period`、`lcoe`、`lcos`、`roi`、
`cfads_of`、`dscr_series`、`minimum_dscr`。

引擎构造的 LCOE / LCOS 序列（`engine.py`）：

| 序列 | `t = 0` | `t = n`（n ≥ 1） |
|---|---|---|
| LCOE 成本 | `(总投资 − 储能投资) × (1 − 增值税抵扣比例)` | `光伏相关运营成本 × (1+g)^(n-1) + 该年光伏设备更换支出` |
| LCOE 电量 | `0.0` | 第 n 年光伏发电量 |
| LCOS 成本 | `储能投资 × (1 − 增值税抵扣比例)` | `storage_opex_1 × (1+g)^(n-1) + 该年储能更换支出` |
| LCOS 电量 | `0.0` | 第 n 年储能放电量 |

其中 **光伏相关运营成本 = `pv_opex + roof_rent + insurance + management_cost + other_opex`**
（即除 `storage_opex` 外的全部年运营成本，与现金流中的 `AnnualResult.opex` 口径一致；
纯光伏项目两者相等）。**光伏设备更换**由 `pv.replacement_year` +
`pv.replacement_cost_per_kwp × 装机` 产生，仅在该年发生一次。

**可选抵减项（默认关闭，规范 §75 口径开关）**

| 参数 | 默认 | 作用 |
|---|---|---|
| `tax.lcoe_vat_deductible_ratio` | `0.0` | 按该比例抵减增值税进项（`t = 0` 的投资基数） |
| `tax.lcoe_residual_credit` | `False` | 为真时将残值现值记入第 N 年作为成本抵减，残值按光伏/储能投资比例分摊 |

开启两者后，LCOE 即等价于行业/招标通行公式
`LCOE = [I₀ − It − VR/(1+i)^N + Σ Mₙ/(1+i)ⁿ] / Σ Yₙ/(1+i)ⁿ`
（已用公开招标文件案例验证为 0 差异，见 `tests/test_lcoe_caliber.py`）。

### 4.10 负荷（引擎内联，§16、§17）

```text
Load_n = Load_1 × (1 + g)^(n-1)        Load_1 = load.annual_load_kwh，g = load.annual_load_growth_rate
```

源码：`engine.py:_calculate_core` 逐年循环首行。

---

## 5. 口径契约

**单位**：内部统一基准单位；比例与利率一律小数；单位字符串只从 `UNITS` 取；
单位换算只发生在展示层。

**时间轴**：Year 0 只含投资与提款（无发电、运维、折旧、税、残值）；Year 1 为第一个运营年；
`(1±x)^(n-1)` 在 n=1 时等于基准值；残值只在末年；回收期自 Year 0 起算；
现金流数组长度固定 `N+1`。

**税负顺序**（不可调整）：
`RevenueNet → EBITDA → EBIT → EBT → TaxableIncome = max(EBT,0) → IncomeTax → CashTax`。
折旧与利息都在所得税之前扣除；应纳税所得额不得为负（§61）。

**融资**：贷款额与资本金按 §63；`loan_amount` 覆盖优先；利息必须按平均余额（§65）；
余额恒 `≥ 0`（§64），出现负数立即报错；宽限期内只付息不还本。

**收入**：一度电只能产生一条收益（§47，进储能电量不计光伏自用收益）；
储能四类收益分开列示（§44）；任何市场价格必须来自参数或政策 Profile（§33）；
政策参数必须带版本与出处并在报告中显示（§35、§36、§90）。

**指标**：项目 IRR 与资本金 IRR 同时显示（§70）；NPV 从 t=0 起算（§71）；
LCOE/LCOS 成本不含融资利息（§76）；ROI 必须注明"生命周期累计净收益 ÷ 初始总投资"（§78）；
不可计算的指标返回 `None`，展示层显示"无法计算"/"未回收"，**不得用 0 冒充**（§114）。

**校验**：Pydantic 负责类型与取值范围（§112），`validator.py` 负责中文友好报错（§132）；
能量守恒误差 `≤ 1e-6`（§113）；报错必须带 `field`；同一输入重复计算 100 次结果一致（§118）。

---

## 6. V1 简化口径与已知限制（TODO(V2)）

报告中必须原样披露本节内容（§59、§158）；**不得**把简化模型表述为正式可研 / 审计 / 税务结论。

### 6.1 税务（`tax.py`）

| 编号 | 限制 | TODO |
|---|---|---|
| L1 | 增值税进项抵扣与留抵退税**不建模**；`vat_rate` 仅用于含税↔不含税换算 | `TODO(V2)`：进项抵扣与留抵退税 |
| L2 | **不建模亏损跨年弥补** | `TODO(V2)`：亏损结转 5 年 |
| L3 | 三免三减半、西部大开发等优惠**不内置**，只能由 `income_tax_rate` 体现 | `TODO(V2)`：税收优惠期 |
| L4 | 附加税费与其他税费以**不含税收入**为基数（非实际应纳增值税额） | `TODO(V2)`：以应纳增值税为基数 |
| L5 | 只支持直线法；且引擎**未把** `depreciation_method` 传入 `depreciation_for_year`（默认即直线法） | `TODO(V2)`：加速折旧 |

### 6.2 收入与政策

| 编号 | 限制 | TODO |
|---|---|---|
| L6 | 储能容量 / 辅助 / 其他三类收益按**固定值**计取，**不随年份增长** | `TODO(V2)`：三类收益逐年模型 |
| L7 | 政策参与**登记与披露**，尚未自动折算为电价；未实现"政策→电价"推导 | `TODO(V2)`：政策数值参与电价推导 |
| L8 | `tariff.green_energy_price`、`green_environmental_value` 及 `policy.*` 同名参数**未参与**收益计算 | `TODO(V2)`：绿电与环境价值收益 |
| L9 | `AnnualResult.pv_other_revenue` 恒为 `0.0` | `TODO(V2)`：光伏其他收益 |
| L10 | 储能套利把**全部充电量**按充电价计价（含光伏转入电量） | 口径已披露；其它口径应做成可选模式，不改默认公式 |

### 6.3 储能

| 编号 | 限制 | TODO |
|---|---|---|
| L11 | **年度等效循环模型**，不做 8760 小时仿真；不建模日内时序、SOC 曲线、需量管理 | `TODO(V2)`：8760 小时仿真 |
| L12 | 更换电芯当年容量**恢复至初始值**并重新衰减 | 口径已明确；"新电芯参数衰减"需显式建模 |
| L13 | ~~`StorageConfig.storage_capex_per_kwh` 不参与 CAPEX~~ | **已修复**：删除 `StorageConfig` 中的同名字段，储能单位投资统一由 `InvestmentConfig.storage_capex_per_kwh` 承担（唯一数据源）；储能参数页的输入框改为绑定该字段 |
| L14 | `grid_charge`（电网补充充电量）只用于展示与校验，不进入收益公式 | 口径已披露 |

### 6.4 负荷

| 编号 | 限制 | TODO |
|---|---|---|
| L15 | 只用 `annual_load_kwh` 与 `annual_load_growth_rate`；`working_days`、昼/夜比例**不参与计算** | `TODO(V2)`：分时段负荷曲线 |
| L16 | 负荷为几何增长，无分月/分季波动 | `TODO(V2)`：分月负荷模型 |

### 6.5 投资

| 编号 | 限制 | TODO |
|---|---|---|
| L17 | `detailed_items` 只被**原样记录**，**不计入**九项之和 | `TODO(V2)`：明细项参与合计 |
| L18 | 投资在 Year 0 一次性发生，不建模分期投入与建设期利息 | `TODO(V2)`：分期投资与建设期利息 |

### 6.6 指标

| 编号 | 限制 | TODO |
|---|---|---|
| L19 | LCOE 用**光伏口径**、LCOS 用**储能口径**（见 §4.9 序列表） | 口径已披露 |
| L20 | 等额本息：还本按年金+**期初余额**分解，利息入账按 §65 **平均余额**，差异在报告口径说明中披露 | `TODO(V2)`：统一分解口径 |
| L21 | IRR 固定区间 `[-0.9999, 10.0]`；无符号变化或区间外返回 `None` | 口径已披露 |
| L22 | `RiskLevel`（优秀/正常/关注/风险）仅作提示，**不得**据此判定项目"可行/不可行"（§106）；V1 计算层与报表层尚未使用该枚举 | `TODO(V2)`：风险分级展示 |

### 6.7 实现状态总表（对照磁盘真实代码，§157）

| 编号 | 能力 | 状态 | 位置 |
|---|---|---|---|
| — | 数据模型（Pydantic） | ✅ 已实现 | `src/cenep/domain/` |
| — | 统一计算引擎（§82 的 31 步） | ✅ 已实现 | `src/cenep/calculation/engine.py` |
| — | 校验与中文报错 | ✅ 已实现 | `src/cenep/calculation/validator.py` |
| — | 情景 / 敏感性分析 | ✅ 已实现 | `calculation/scenario.py`、`sensitivity.py` |
| — | 参数来源与可追溯 | ✅ 已实现 | `src/cenep/domain/provenance.py` |
| — | 应用服务（编排 / 自动保存 / 日志） | ✅ 已实现 | `src/cenep/application/` |
| — | `.nep` 项目文件 | ✅ 已实现 | `src/cenep/infrastructure/project_file.py` |
| — | SQLite（政策版本 / 政策模板 / 项目模板 / 参数字典 / 历史索引） | ✅ 已实现 | `src/cenep/infrastructure/db.py` |
| — | 日志（轮转文件 + 控制台） | ✅ 已实现 | `src/cenep/infrastructure/logging_setup.py` |
| — | 政策模板与版本机制（**刻意不预填数值**） | ✅ 已实现 | `src/cenep/policy/` |
| — | Excel 导出（13 张工作表） | ✅ 已实现 | `src/cenep/reports/excel_exporter.py` |
| — | PDF 导出（15 章 + 免责声明） | ✅ 已实现 | `src/cenep/reports/pdf_exporter.py` |
| — | GUI（PySide6，只绑定字段与展示结果） | ✅ 已实现 | `src/cenep/ui/` |
| — | `python -m cenep` 入口与 `--selftest` 自检 | ✅ 已实现 | `src/cenep/__main__.py`、`selftest.py` |
| — | PyInstaller 打包配置 | ✅ 已实现（配置） | `build/CENEP.spec`、`build/entry.py` |
| L23 | 示例项目与导出产物 | ✅ 已实现 | `examples/`、`tools/make_examples.py` |
| L24 | `utils/` 通用工具包 | ❌ **尚未实现**（目录不存在） | — |
| L25 | `TEST_PLAN.md`（黄金案例手工推导过程） | ❌ **尚未创建** | — |
| L26 | `.nep` schema 迁移器 | ❌ 尚未实现（当前策略：版本不匹配即拒载） | `infrastructure/project_file.py` |
| L27 | 政策数值自动折算为电价 | ❌ 尚未实现（见 L7） | — |
| L28 | 多省份政策模板数据集 | ⚠️ 仅湖北占位模板 | `policy/hubei.py`（数值全为 `None`） |

> **与早期项目概要的差异**：概要曾称"GUI、Excel/PDF 导出、`.nep` 项目文件、政策模板、
> 打包尚未实现"。经逐文件核实，上述能力**均已实现**；真正尚未实现的是 `utils/`、
> `TEST_PLAN.md`、`.nep` 迁移器与政策数值自动折算。本文档以磁盘真实代码为准。

### 6.8 §158 执行约束

1. 公式算不出来时**不得自创公式**：必须在 §6 登记 `TODO(V2)`，采用最简单、最透明、
   最可替换的模型（如 L6 固定值、L12 容量复位），并在 `CalculationResult.notes` 中披露口径。
2. 任何新增简化都必须同步更新：本节、`engine.py:_CALIBER_NOTES`、报告口径页。
3. 简化模型不得在界面或报告中表述为"正式可研 / 审计 / 税务结论"。

---

## 7. 公式—条款—源码对照表

| # | 公式（简写） | 条款 | 源码位置（`src/cenep/calculation/`） |
|---|---|---|---|
| 1 | `PVCapacity = UsableRoofArea / AreaPerKWp` | §19 | `pv.py:resolve_pv_capacity` |
| 2 | `G1 = Ppv × H × PR × (1-C)` | §20、§21 | `pv.py:first_year_generation` |
| 3 | `Gn = G1 × (1-d)^(n-1)` | §22 | `pv.py:generation_for_year`、`degradation_factor` |
| 4 | `G = 自用 + 进储能 + 上网 + 损耗`（≤1e-6） | §23、§113 | `pv.py:allocate_pv_energy`、`PvAllocation.balance_error` |
| 5 | `PVSelfUse = min(G × ratio, Load)` | §24 | `pv.py:allocate_pv_energy` |
| 6 | `to_storage = min(remaining, headroom)` | §23、§25、§46 | `pv.py:allocate_pv_energy` |
| 7 | `StorageDuration = Energy / Power` | §39 | `storage.py:storage_duration_hours` |
| 8 | `round_trip = charge × discharge` | §40 | `storage.py:resolve_efficiencies` |
| 9 | `Edis = Available × DoD × Cycles × η_dis` | §41 | `storage.py:annual_discharge_energy` |
| 10 | `Echg = Edis / η_dis / η_chg` | §42 | `storage.py:annual_charge_energy` |
| 11 | `Available_n = Initial × (1-d)^(n-1)`（更换年复位） | §38、§45 | `storage.py:available_energy_for_year` |
| 12 | `AverageTOUPrice = Σ(价格 × 比例)` | §31、§32 | `revenue.py:average_tou_price` |
| 13 | 四种电价模式的替代价/充电价解析 | §29–§33 | `revenue.py:resolve_tariff` |
| 14 | `PVSelfUseRevenue = PVSelfUse × AvoidedPrice` | §27 | `revenue.py:self_use_revenue` |
| 15 | `PVExportRevenue = PVExport × ExportPrice` | §28 | `revenue.py:export_revenue` |
| 16 | `StorageArbitrage = Edis × 放电替代价 − Echg × 充电价` | §43 | `revenue.py:storage_arbitrage_revenue` |
| 17 | `StorageTotal = 套利 + 容量 + 辅助 + 其他` | §44 | `revenue.py:storage_total_revenue` |
| 18 | 禁止重复计算（进储能电量不计自用收益） | §47 | `pv.py:allocate_pv_energy`、`engine.py:_CALIBER_NOTES` |
| 19 | `TotalCAPEX = 九项之和` | §49 | `investment.py:CapexBreakdown.total` |
| 20 | `PV_CAPEX = 容量 × 单价`（UNIT_PRICE） | §50 | `investment.py:compute_capex` |
| 21 | `Storage_CAPEX = 容量 × 单价`（UNIT_PRICE） | §51 | `investment.py:compute_capex` |
| 22 | UNIT_PRICE 与 DETAILED 互斥 | §52 | `investment.py:compute_capex` |
| 23 | 单位投资 `元/W`、`元/Wh` | §102 | `investment.py:unit_investment` |
| 24 | 单项 OPEX（FIXED / RATIO_OF_CAPEX） | §54 | `opex.py:resolve_opex_item` |
| 25 | `Opex_n = Opex_1 × (1+g)^(n-1)` | §55 | `opex.py:opex_for_year` |
| 26 | 屋顶租金（AREA / CAPACITY / FIXED） | §56 | `opex.py:roof_rent` |
| 27 | `DepreciableBase = 可折旧投资 × (1-残值率)` | §58 | `tax.py:depreciable_base` |
| 28 | `年折旧 = 基数 / 折旧年限` | §58 | `tax.py:annual_depreciation`、`depreciation_for_year` |
| 29 | `ResidualValue = 可折旧投资 × 残值率` | §67 | `tax.py:residual_value` |
| 30 | `RevenueNet = Revenue / (1+VAT)` | §59 | `tax.py:revenue_net` |
| 31 | `EBITDA = Revenue − Opex` | §60 | `tax.py:ebitda_of` |
| 32 | `EBIT = EBITDA − Depreciation` | §60 | `tax.py:ebit_of` |
| 33 | `EBT = EBIT − Interest` | §60 | `tax.py:ebt_of` |
| 34 | `TaxableIncome = max(EBT, 0)` | §61 | `tax.py:taxable_income_of` |
| 35 | `IncomeTax = TaxableIncome × 税率` | §61 | `tax.py:income_tax_of` |
| 36 | `CashTax = 所得税 + 附加 + 其他` | §61 | `tax.py:TaxYear.cash_tax` |
| 37 | `LoanAmount = TotalCAPEX × DebtRatio` | §63 | `financing.py:loan_amount_of` |
| 38 | `EquityAmount = TotalCAPEX − LoanAmount` | §63 | `financing.py:equity_amount_of` |
| 39 | `EndingDebt = Beginning + Drawdown − Repayment ≥ 0` | §64 | `financing.py:build_loan_schedule` |
| 40 | `Interest = AverageDebtBalance × Rate` | §65 | `financing.py:build_loan_schedule` |
| 41 | 等额本金 / 等额本息还本计划 | §62 | `financing.py:principal_schedule` |
| 42 | `ProjectCF = EBITDA − CashTax − CAPEX − Replace + Residual` | §67 | `cashflow.py:project_cashflow` |
| 43 | `EquityCF = EBITDA − CashTax − Interest − 还本 − 资本金 − Replace + 提款 + Residual` | §68 | `cashflow.py:equity_cashflow` |
| 44 | `ProjectCF_0 = -TotalCAPEX`；`EquityCF_0 = -EquityAmount` | §69 | `cashflow.py:year0_*` |
| 45 | `IRR`（项目 / 资本金，确定性二分法） | §70、§114、§118 | `financial_metrics.py:irr` |
| 46 | `NPV = Σ CF_t/(1+r)^t` | §71 | `financial_metrics.py:npv` |
| 47 | 静态回收期 `(n-1) + \|Cum_(n-1)\|/CF_n` | §73 | `financial_metrics.py:payback_period` |
| 48 | 动态回收期（折现后同法） | §74 | `financial_metrics.py:discounted_payback_period` |
| 49 | `LCOE = Σ(Cost/(1+r)^t) / Σ(Energy/(1+r)^t)` | §75、§76 | `financial_metrics.py:lcoe` |
| 50 | `LCOS` 同法（储能口径） | §77 | `financial_metrics.py:lcos` |
| 51 | `ROI = 生命周期累计净收益 / 初始总投资` | §78 | `financial_metrics.py:roi` |
| 52 | `DSCR = CFADS / DebtService` | §79 | `financial_metrics.py:dscr_series`、`minimum_dscr` |
| 53 | `CFADS = EBITDA − CashTax − MaintenanceCAPEX` | §79 | `financial_metrics.py:cfads_of` |
| 54 | `Load_n = Load_1 × (1+g)^(n-1)` | §16、§17 | `engine.py:_calculate_core` |
| 55 | §82 的 31 步执行顺序 | §82 | `engine.py:calculate`、`_calculate_core`（见 CALCULATION_ENGINE.md） |
| 56 | 参数来源登记 | §83–§85、§91 | `engine.py:_register_parameters`、`domain/provenance.py` |
| 57 | 情景乘数施加（深拷贝 + 乘数 + 重算） | §92–§94 | `scenario.py:apply_delta`、`delta_for` |
| 58 | 敏感性单变量施加 | §95、§96 | `sensitivity.py:apply_variable`、`applicable_variables` |
| 59 | 能量守恒校验（≤ 1e-6） | §113 | `validator.py:validate_energy_balance` |
| 60 | 储能充放电口径校验 | §113 | `validator.py:validate_storage_balance` |
| 61 | 输入校验与中文报错 | §112、§114、§132 | `validator.py:validate_project`、`validate_ratios` |
| 62 | 参数配色（蓝/绿/黄/灰） | §144 | `domain/enums.py:SourceType.ui_color`、`reports/excel_exporter.py:SOURCE_FILL` |

---

## 8. 变更纪律

1. 改任何公式 → 同步改本文档、`engine.py:_CALIBER_NOTES`、对应测试；`pytest` 必须全绿。
2. 新增参数 → 补齐 Pydantic 约束、本文档参数表、来源登记（如需）。
3. 新增政策参数 → 必须走 `PolicyProfile` / `PolicyTemplate`，带版本与出处（§34、§36），
   模板**永不预填具体数值**（§159）。
4. 本文档为最高优先级：若代码与本文档不一致，**以本文档为准并立即修正代码**（§157）。
