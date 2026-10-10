# 报告规范（REPORT_SPEC）

> **当前状态（V2.1 阶段 2）：Excel **26 张工作表**（V1 §108 的 13 张 + V2 §67 新增 11 张 +
> V2.1 §8.1 新增 2 张账单表）与 PDF **17 部分**（V2 §66 的 16 部分 + V2.1 §8.1 新增
> 「账单事实与校验」，含 6 个时序章节与 3 张图表）均已实现。**
> **本文档为 Phase 6 / Phase 7 与 V2.1 阶段 2 的实现依据与现状记录。**
> **状态快照日期**：2026-10-06（V2.1 阶段 2 更新）
> **引用条款**：§35、§80、§81、§83、§87、§91、§105、§108–§111、§144、§148–§150、§154、§161
> （本文件正文中的表格与列表为紧凑起见以"§X"简写，凡引用产品规范条款处均指产品规范 §X）
>
> **三条铁律（§109、§148–§150）**
> 1. Excel 与 PDF 的**全部数据必须来自 `CalculationResult`**（以及 `Project` 的输入参数用于展示；
>    V2.1 起含 `Project.bills` 账单段与账单服务 `BillService` **已算好**的核对结果）；
> 2. Excel 与 PDF **一律不得重新计算**任何指标；
> 3. 可以展示**公式文本**，但展示的结果值必须与软件计算结果显示一致（`GUI = Excel = PDF = CalculationResult`）。

---

## 1. 文档信息与实现现状

| 项目 | 内容 |
|---|---|
| 文档名称 | Excel / PDF 报告规范 |
| 文档标识 | `REPORT_SPEC.md` |
| 目标阶段 | Phase 6（Excel，已实现）、Phase 7（PDF，已实现，快照进行中） |
| Excel 实现文件 | `src/cenep/reports/excel_exporter.py`（`ExcelExporter.export(project, result, path)`，26 张工作表） |
| PDF 实现文件 | `src/cenep/reports/pdf_exporter.py`（`PdfExporter.export(project, result, path)`，reportlab，17 部分 + 3 张图表） |
| 测试 | `tests/test_excel_export.py`、`tests/test_pdf_export.py`、`tests/test_report_v2.py`、`tests/test_bill_ui_report.py` |
| 数据源 | `CalculationResult`（唯一结果对象，`src/cenep/domain/results.py`）；账单事实取自 `Project.bills`，校验明细取自 `BillService.reconcile_all()` |

### 1.1 实现状态总览

| 输出 | 结构 | 当前状态 |
|---|---|---|
| Excel | **26 张**工作表（V1 §108 的 13 张 + V2 §67 的 11 张 + V2.1 §8.1 的 2 张账单表） | ✅ 已实现 |
| Excel 参数来源着色 | 按 `SourceType` 着色（§144） | ✅ 已实现 |
| Excel 一致性测试 | Excel ↔ `CalculationResult` | ✅ 已建立并全绿 |
| PDF | **17 部分** + 免责声明（V2 §66 + V2.1 §8.1、V1 §111） | ✅ 已实现 |
| PDF 页眉页脚与页码 | 见第 3 节 | ✅ **已实现（快照进行中）** |
| PDF 政策版本提示位 | 见 3.4 | ✅ **已实现（快照进行中）** |

---

## 2. Excel 输出设计（§108、§109）

### 2.1 工作表总览（V2.1 阶段 2 起为 26 张）

V2 §67 要求「至少包含」若干类内容，因此工作表在 V1 §108 的 13 张基础上**新增 11 张**；
V2.1 §8.1（阶段 2）再**新增 2 张账单表**（`账单原始数据`、`账单校验`），合计 **26 张**。

| V2 §67 要求 | 表名 | 与 V1 的关系 |
|---|---|---|
| Overview / Project / Base Parameters | 项目概况 / 基础参数 | V1 原有 |
| Storage / Dispatch Config | 技术参数 + **储能与调度** | 新增调度配置表 |
| Load Profile | **负荷曲线** | 新增 |
| PV Profile | **光伏曲线** | 新增 |
| Tariff Profile | 电价参数 + **分时电价** | 新增时段规则表 |
| Hourly Simulation | **8760时序仿真** | 新增（**抽样**，见 2.16） |
| Energy Balance | **能量平衡** | 新增 |
| Annual Summary | **年度汇总** | 新增 |
| Revenue | **收益分解** | 新增 |
| OPEX / CAPEX / Financing | 运维参数 / 投资参数 / 融资参数 | V1 原有 |
| Cash Flow / Financial Metrics | 年度现金流 / 财务指标 | V1 原有 |
| Scenario / Optimization | 情景分析 + **方案比较** + **方案寻优** | 新增两张 |
| Sensitivity | 敏感性分析 | V1 原有 |
| Policy Basis / Parameter Sources | 政策依据 / 参数来源 | V1 原有 |
| Data Quality | **数据质量** | 新增 |
| V2.1 §8.1 Bill Facts | **账单原始数据** | V2.1 阶段 2 新增 |
| V2.1 §8.1 Bill Check | **账单校验** | V2.1 阶段 2 新增 |

**V1 兼容性**：新增表在未启用时序仿真时输出「本项目未启用时序仿真」说明与启用方法，
在**没有账单**时输出「本项目尚未录入电费账单」与三种录入方法，**不缺表、不报错**；
配置类表（储能与调度、分时电价）与年度汇总表在 V1 项目下照常输出真实内容。

**以下为 V1 的 13 张表（V2 全部保留）**

| # | 工作表名（`SHEET_NAMES`） | 内容 | 主要数据来源 | 当前状态 |
|---|---|---|---|---|
| 1 | 项目概况 | 项目身份、规模、投资、导出时间 | `Project.basic_info`、`CalculationResult` | ✅ 已实现 |
| 2 | 基础参数 | 生命周期、折现率、负荷、首年概览 | `Project`、`CalculationResult` | ✅ 已实现 |
| 3 | 技术参数 | 光伏与储能全部技术参数 | `Project.pv`、`Project.storage`、`CalculationResult` | ✅ 已实现 |
| 4 | 电价参数 | 电价模式与全部价格、比例 | `Project.tariff` | ✅ 已实现 |
| 5 | 投资参数 | 投资明细、总投资、单位投资 | `CalculationResult.capex_breakdown` / `unit_investment` | ✅ 已实现 |
| 6 | 运维参数 | 运维费用输入与首年计算值 | `Project.opex`、`CalculationResult` | ✅ 已实现 |
| 7 | 融资参数 | 融资输入与贷款/资本金金额 | `Project.financing`、`CalculationResult` | ✅ 已实现 |
| 8 | 年度现金流 | 逐年完整现金流表（含 Year 0） | `CalculationResult.annual_results`、`project_cashflows` | ✅ 已实现 |
| 9 | 财务指标 | 全部财务指标 | `CalculationResult` 指标字段 | ✅ 已实现 |
| 10 | 敏感性分析 | 单因素敏感性明细 | `CalculationResult.sensitivity` | ✅ 已实现 |
| 11 | 情景分析 | 三情景对比 | `CalculationResult.scenarios` | ✅ 已实现 |
| 12 | 政策依据 | 政策版本、参数与出处 | `Project.policy`（`PolicyProfile`） | ✅ 已实现 |
| 13 | 参数来源 | 参数来源追溯表 | `CalculationResult.parameter_sources` | ✅ 已实现 |

### 2.2 工作表 1：项目概况

| 列/行标签 | 数据来源 | 单位 | 格式 |
|---|---|---|---|
| 项目名称 | `project.basic_info.project_name` | — | 文本 |
| 项目类型 | `project.basic_info.project_type.label` | — | 文本（工商业光伏 / 工商业储能 / 工商业光储） |
| 省份 | `project.basic_info.province` | — | 文本 |
| 城市 | `project.basic_info.city` | — | 文本 |
| 业主名称 | `project.basic_info.customer_name` | — | 文本 |
| 所属行业 | `project.basic_info.industry` | — | 文本 |
| 评价日期 | `project.basic_info.evaluation_date.isoformat()` | — | `YYYY-MM-DD` |
| 备注 | `project.basic_info.notes` | — | 文本 |
| 装机规模（分组标题） | — | — | 加粗 |
| 光伏装机容量 | `result.pv_capacity_kwp` | kWp | `#,##0.00` |
| 储能功率 | `result.storage_power_kw` | kW | `#,##0.00` |
| 储能容量 | `result.storage_energy_kwh` | kWh | `#,##0.00` |
| 储能时长 | `result.storage_duration_hours` | h | `#,##0.00` |
| 计算期 | `result.analysis_period` | 年 | `#` |
| 投资（分组标题） | — | — | 加粗 |
| 总投资 | `result.total_capex` | 元 | `#,##0.00` |
| 其中：贷款金额 | `result.loan_amount` | 元 | `#,##0.00` |
| 其中：资本金 | `result.equity_amount` | 元 | `#,##0.00` |
| 导出时间 | `datetime.now().isoformat(timespec="seconds")` | — | 文本 |

### 2.3 工作表 2：基础参数

| 参数 | 数据来源 | 单位 | 格式 |
|---|---|---|---|
| 项目生命周期 | `project.analysis_period` | 年 | `#,##0.0000` |
| 折现率 | `project.discount_rate` | 小数 | `#,##0.0000` |
| 折旧年限 | `project.tax.depreciation_years` | 年 | `#,##0.0000` |
| 折旧方法 | `project.tax.depreciation_method.label` | — | 文本 |
| 残值率 | `project.tax.residual_value_ratio` | 小数 | `#,##0.0000` |
| 可折旧投资占比 | `project.tax.depreciable_capex_ratio` | 小数 | `#,##0.0000` |
| 增值税率 | `project.tax.vat_rate` | 小数 | `#,##0.0000` |
| 所得税率 | `project.tax.income_tax_rate` | 小数 | `#,##0.0000` |
| 附加税费率 | `project.tax.surcharge_rate` | 小数 | `#,##0.0000` |
| 其他税费率 | `project.tax.other_tax_rate` | 小数 | `#,##0.0000` |
| 收入为含税口径 | `project.tax.revenue_is_vat_inclusive` | — | 是/否 |
| LCOE 增值税抵扣比例 | `project.tax.lcoe_vat_deductible_ratio` | 小数 | `#,##0.0000` |
| LCOE 抵减残值现值 | `project.tax.lcoe_residual_credit` | — | 是/否 |
| 年用电量 | `project.load.annual_load_kwh` | kWh | `#,##0.0000` |
| 年工作天数 | `project.load.working_days` | 天 | `#,##0.0000` |
| 白天负荷占比 | `project.load.daytime_load_ratio` | 小数 | `#,##0.0000` |
| 夜间负荷占比 | `project.load.nighttime_load_ratio` | 小数 | `#,##0.0000` |
| 年用电量增长率 | `project.load.annual_load_growth_rate` | 小数 | `#,##0.0000` |
| 首年光伏发电量 | `result.first_year_generation` | kWh | `#,##0.0000` |
| 首年自用电量 | `result.first_year_self_use_energy` | kWh | `#,##0.0000` |
| 首年上网电量 | `result.first_year_export_energy` | kWh | `#,##0.0000` |
| 首年收入 | `result.first_year_revenue` | 元 | `#,##0.0000` |
| 首年运维费 | `result.first_year_opex` | 元 | `#,##0.0000` |
| 经营期年均收入 | `result.annual_revenue` | 元 | `#,##0.0000` |
| 经营期年均运维费 | `result.annual_opex` | 元 | `#,##0.0000` |

### 2.4 工作表 3：技术参数

| 分组 | 参数 | 数据来源 | 单位 |
|---|---|---|---|
| 光伏 | 直接输入容量 | `project.pv.pv_capacity_kwp` | kWp |
| 光伏 | 屋顶总面积 | `project.pv.roof_area_m2` | m² |
| 光伏 | 可利用屋顶面积 | `project.pv.usable_roof_area_m2` | m² |
| 光伏 | 单位容量占用面积 | `project.pv.area_per_kwp` | m²/kWp |
| 光伏 | 年等效利用小时 | `project.pv.equivalent_hours` | h |
| 光伏 | 性能比 | `project.pv.performance_ratio` | 小数 |
| 光伏 | 年衰减率 | `project.pv.annual_degradation_rate` | 小数 |
| 光伏 | 限电率 | `project.pv.curtailment_rate` | 小数 |
| 光伏 | 自发自用比例 | `project.pv.self_consumption_ratio` | 小数 |
| 光伏 | 设备更换年份 | `project.pv.replacement_year`（为空显示"不更换"） | 年 |
| 光伏 | 设备更换单价 | `project.pv.replacement_cost_per_kwp` | 元/kWp |
| 光伏 | 设备更换支出（计算） | `pv.replacement_cost_per_kwp × pv_capacity_kwp` | 元 |
| 储能 | 功率 | `project.storage.storage_power_kw` | kW |
| 储能 | 容量 | `project.storage.storage_energy_kwh` | kWh |
| 储能 | 时长（计算） | `result.storage_duration_hours` | h |
| 储能 | 年循环次数 | `project.storage.annual_cycles` | 次/年 |
| 储能 | 放电深度 DoD | `project.storage.depth_of_discharge` | 小数 |
| 储能 | 年衰减率 | `project.storage.annual_degradation_rate` | 小数 |
| 储能 | 更换电芯年份 | `project.storage.replacement_year` | 年 |
| 储能 | 充电电价 | `project.storage.charge_price`（为空则显示"按电价参数推导"） | 元/kWh |
| 储能 | 放电替代电价 | `project.storage.discharge_avoided_price`（同上） | 元/kWh |
| 储能 | 年容量收益 | `project.storage.annual_capacity_revenue` | 元 |
| 储能 | 年辅助服务收益 | `project.storage.annual_ancillary_revenue` | 元 |
| 储能 | 年其他收益 | `project.storage.annual_other_revenue` | 元 |

> **口径说明行（必须增加）**：往返效率、充电效率、放电效率的取值与换算关系来自 `result.notes`，
> 原样写入本表下方，例如"储能往返效率：0.880000（充电 0.938083 × 放电 0.938083）"。

### 2.5 工作表 4：电价参数

| 参数 | 数据来源 | 单位 |
|---|---|---|
| 电价模式 | `project.tariff.tariff_mode.label` | — |
| 固定电价 | `project.tariff.average_price` | 元/kWh |
| 峰电价 | `project.tariff.peak_price` | 元/kWh |
| 平电价 | `project.tariff.flat_price` | 元/kWh |
| 谷电价 | `project.tariff.valley_price` | 元/kWh |
| 峰电量比例 | `project.tariff.peak_ratio` | 小数 |
| 平电量比例 | `project.tariff.flat_ratio` | 小数 |
| 谷电量比例 | `project.tariff.valley_ratio` | 小数 |
| 市场电价 | `project.tariff.market_price` | 元/kWh |
| 余电上网电价 | `project.tariff.export_price` | 元/kWh |
| 绿电价格 | `project.tariff.green_energy_price` | 元/kWh |
| 绿色环境价值 | `project.tariff.green_environmental_value` | 元/kWh |
| 替代电价（用户覆盖） | `project.tariff.avoided_price_override`（为空显示"未覆盖"） | 元/kWh |
| 充电电价（用户覆盖） | `project.tariff.charge_price_override`（为空显示"未覆盖"） | 元/kWh |

> **必须增加"电价解析口径"行**：取自 `result.notes` 中的"电价解析口径：…"，形如
> "峰平谷电价（替代价取综合电价，充电取谷价）；替代电价 0.723000 元/kWh，充电电价 0.400000 元/kWh，上网电价 0.350000 元/kWh。"

### 2.6 工作表 5：投资参数

| 项目 | 数据来源 | 单位 | 格式 |
|---|---|---|---|
| 光伏投资 | `result.capex_breakdown["光伏投资"]` | 元 | `#,##0.00` |
| 储能投资 | `result.capex_breakdown["储能投资"]` | 元 | `#,##0.00` |
| 并网投资 | `result.capex_breakdown["并网投资"]` | 元 | `#,##0.00` |
| 屋顶费用 | `result.capex_breakdown["屋顶费用"]` | 元 | `#,##0.00` |
| 开发费用 | `result.capex_breakdown["开发费用"]` | 元 | `#,##0.00` |
| 工程费用 | `result.capex_breakdown["工程费用"]` | 元 | `#,##0.00` |
| 施工费用 | `result.capex_breakdown["施工费用"]` | 元 | `#,##0.00` |
| 其他投资 | `result.capex_breakdown["其他投资"]` | 元 | `#,##0.00` |
| 预备费 | `result.capex_breakdown["预备费"]` | 元 | `#,##0.00` |
| **总投资** | `result.total_capex` | 元 | `#,##0.00`（加粗，标注"各项之和"） |
| 光伏单位投资 | `result.unit_investment["yuan_per_w"]` | 元/W | `#,##0.00`（标注"总投资 ÷ 光伏容量"） |
| 储能单位投资 | `result.unit_investment["yuan_per_wh"]` | 元/Wh | `#,##0.00`（标注"总投资 ÷ 储能容量"） |
| 投资模式 | `project.investment.mode.value` | — | 文本（UNIT_PRICE / DETAILED） |

> ⚠️ **实现注意**：`unit_investment` 的分母是**总投资**（含非光伏/非储能项），
> 因此其口径须在"说明"列中如实写明，避免用户误认为"光伏单位造价"。

### 2.7 工作表 6：运维参数

| 参数 | 数据来源 | 单位 |
|---|---|---|
| 光伏运维（输入值） | `project.opex.pv_opex` | 按模式 |
| 光伏运维模式 | `project.opex.pv_opex_mode.value` | — |
| 储能运维（输入值） | `project.opex.storage_opex` | 按模式 |
| 储能运维模式 | `project.opex.storage_opex_mode.value` | — |
| 保险费（输入值） | `project.opex.insurance` | 按模式 |
| 管理费 | `project.opex.management_cost` | 元/年 |
| 其他费用 | `project.opex.other_opex` | 元/年 |
| 屋顶租金模式 | `project.opex.roof_rent_mode.value` | — |
| 屋顶租金单价 | `project.opex.rent_per_m2` | 元/m² |
| 屋顶容量租金单价 | `project.opex.rent_per_kw` | 元/kWp |
| 屋顶固定租金 | `project.opex.annual_fixed_rent` | 元/年 |
| 运维费用年增长率 | `project.opex.annual_opex_growth_rate` | 小数 |
| 首年运维费合计（计算） | `result.first_year_opex` | 元 |
| 首年折旧（计算） | `result.annual_results[0].depreciation` | 元 |

### 2.8 工作表 7：融资参数

| 参数 | 数据来源 | 单位 |
|---|---|---|
| 是否融资 | `project.financing.enabled`（显示"是"/"否"） | — |
| 贷款比例 | `project.financing.debt_ratio` | 小数 |
| 资本金比例 | `project.financing.equity_ratio` | 小数 |
| 贷款利率 | `project.financing.interest_rate` | 小数 |
| 贷款期限 | `project.financing.loan_term` | 年 |
| 宽限期 | `project.financing.grace_period` | 年 |
| 还款方式 | `project.financing.repayment_method.label` | — |
| 贷款金额（计算） | `result.loan_amount` | 元 |
| 资本金金额（计算） | `result.equity_amount` | 元 |

### 2.9 工作表 8：年度现金流（核心表）

> **Year 0 行必须存在**（建设/初始投资期，§69）。当前实现已插入 Year 0 行：
> 第 1–12 列为 0，第 13–15 列取 `result.project_cashflows[0]`、`result.equity_cashflows[0]`、`result.cumulative_cashflow[0]`。

| 列 | 列名 | 数据来源（`AnnualResult` 字段） | 单位 | 格式 |
|---|---|---|---|---|
| 1 | 年份 | `year`（Year 0 行固定为 0） | — | `#,##0` |
| 2 | 负荷（kWh） | `load_kwh` | kWh | `#,##0.00` |
| 3 | 发电量（kWh） | `pv_generation_kwh` | kWh | `#,##0.00` |
| 4 | 自用电量（kWh） | `pv_self_use_kwh` | kWh | `#,##0.00` |
| 5 | 上网电量（kWh） | `pv_export_kwh` | kWh | `#,##0.00` |
| 6 | 储能放电量（kWh） | `storage_discharge_kwh` | kWh | `#,##0.00` |
| 7 | 总收入（元） | `total_revenue` | 元 | `#,##0.00` |
| 8 | 运维费（元） | `opex` | 元 | `#,##0.00` |
| 9 | 折旧（元） | `depreciation` | 元 | `#,##0.00` |
| 10 | EBITDA（元） | `ebitda` | 元 | `#,##0.00` |
| 11 | 利息（元） | `interest` | 元 | `#,##0.00` |
| 12 | 税费（元） | `cash_tax` | 元 | `#,##0.00` |
| 13 | 项目现金流（元） | `project_cashflow` | 元 | `#,##0.00` |
| 14 | 资本金现金流（元） | `equity_cashflow` | 元 | `#,##0.00` |
| 15 | 累计项目现金流（元） | `cumulative_project_cashflow` | 元 | `#,##0.00` |

**建议扩展列（Phase 6 增强，**仍未实现**；V2 也未加入 —— 年度现金流表保持 V1 的 15 列）**

| 建议列 | 数据来源 | 说明 |
|---|---|---|
| 转入储能电量 | `pv_to_storage_kwh` | 电量分配完整可见 |
| 光伏损耗 | `pv_loss_kwh` | 守恒校验可核对 |
| 储能充电量 / 电网充电量 | `storage_charge_kwh` / `storage_grid_charge_kwh` | 储能套利口径可核对 |
| 自用收益 / 上网收益 / 套利收益 / 容量收益 / 辅助服务收益 / 其他收益 | 对应的 6 个收入字段 | 收入构成可追溯 |
| 不含税收入 | `revenue_net` | 税务口径可核对 |
| EBIT / 利润总额 / 应纳税所得额 / 所得税 / 附加税费 / 其他税费 | `ebit` / `ebt` / `taxable_income` / `income_tax` / `surcharge` / `other_tax` | 利润表完整 |
| 更换投资 / 残值 | `replacement_capex` / `residual_value` | 现金流权益完整 |
| 期初债务 / 提款 / 还本 / 期末债务 | `debt_begin` / `debt_drawdown` / `principal_repayment` / `debt_end` | 还本付息计划表 |
| CFADS / 还本付息 / DSCR | `cfads` / `debt_service` / `dscr` | 偿债能力可核对 |
| 累计资本金现金流 | `cumulative_equity_cashflow` | 资本金视角 |

> **公式列（可选）**：可在"说明"列或独立"公式"列以**文本形式**展示公式（如
> `EBITDA = RevenueNet − OPEX`），**不得写成 Excel 公式**（否则 Excel 会自行计算，违反 §149）。

### 2.10 工作表 9：财务指标

| 指标 | 数据来源 | 单位 | 格式 | 说明列内容 |
|---|---|---|---|---|
| 总投资 | `result.total_capex` | 元 | `#,##0.00` | 各项投资之和 |
| 首年发电量 | `result.first_year_generation` | kWh | `#,##0.00` | 首年光伏发电量 |
| 首年收入 | `result.first_year_revenue` | 元 | `#,##0.00` | 含光伏与储能收益 |
| 项目财务内部收益率（所得税后） | `result.project_irr` | % | `0.000000`（文本化） | 融资前现金流 IRR |
| 资本金财务内部收益率 | `result.equity_irr` | % | `0.000000`（文本化） | 资本金现金流 IRR |
| 项目财务净现值 | `result.project_npv` | 元 | `#,##0.00` | 折现率 `project.discount_rate` |
| 资本金财务净现值 | `result.equity_npv` | 元 | `#,##0.00` | 折现率同上 |
| 静态投资回收期 | `result.static_payback` | 年 | `0.00`（文本化） | 自 Year 0 起算；未回收显示"未回收" |
| 动态投资回收期 | `result.discounted_payback` | 年 | `0.00`（文本化） | 同上 |
| LCOE | `result.lcoe` | 元/kWh | `0.0000` | 光伏口径，成本不含融资利息 |
| LCOS | `result.lcos` | 元/kWh | `0.0000` | 储能口径 |
| ROI | `result.roi` | 倍 | `0.0000` | **生命周期累计净收益 ÷ 初始总投资（软件自定义口径）** |
| 最低 DSCR | `result.min_dscr` | 倍 | `0.0000` | CFADS ÷ 还本付息 |
| 生命周期累计净现金流 | `result.cumulative_cashflow[-1]` | 元 | `#,##0.00` | 项目现金流累计 |

**无值文案规则（§114）**

| 情形 | 展示 |
|---|---|
| `project_irr` / `equity_irr` / `roi` / `lcoe` / `lcos` 为 `None` | "无法计算" |
| `static_payback` / `discounted_payback` 为 `None` | "未回收" |
| `min_dscr` 为 `None` | "不适用"（无融资或无还本付息年份） |

### 2.11 工作表 10：敏感性分析

| 列 | 列名 | 数据来源（`SensitivityRow`） | 格式 |
|---|---|---|---|
| 1 | 变化因素 | `variable_label` | 文本 |
| 2 | 变化率 | `change` | `0.00%` |
| 3 | 项目IRR | `project_irr`（`None` → "无法计算"） | `0.00%` |
| 4 | 资本金IRR | `equity_irr`（同上） | `0.00%` |
| 5 | 项目NPV | `project_npv` | `#,##0.00` |
| 6 | 静态回收期 | `static_payback`（`None` → "未回收"） | `#,##0.0000` |
| 7 | 敏感度系数 | `coefficient`（`None` → 空） | `#,##0.0000` |

**建议增加列**：`irr_change`（IRR 变化率）。表头下须加一行说明：
"一次只改变一个参数，其余参数保持基准值；敏感度系数 = IRR 变化率 ÷ 参数变化率。"

### 2.12 工作表 11：情景分析

| 列 | 列名 | 数据来源（`ScenarioSummary`） | 格式 |
|---|---|---|---|
| 1 | 情景 | `label`（保守 / 基准 / 乐观） | 文本 |
| 2 | 总投资 | `total_capex` | `#,##0.00` |
| 3 | 首年收入 | `first_year_revenue` | `#,##0.00` |
| 4 | 项目IRR | `project_irr`（`None` → "无法计算"） | `0.00%` |
| 5 | 资本金IRR | `equity_irr`（同上） | `0.00%` |
| 6 | 项目NPV | `project_npv` | `#,##0.00` |
| 7 | 静态回收期 | `static_payback`（`None` → "未回收"） | `#,##0.0000` |
| 8 | 情景乘数 | `deltas`（`"、".join(...)`，基准显示"基准"） | 文本 |

> 情景乘数是**显式可修改参数**，不是隐藏逻辑（§158）；基准情景的 `deltas` 为空，显示"基准"。

### 2.13 工作表 12：政策依据

| 项目 | 数据来源 | 说明 |
|---|---|---|
| 政策名称 | `project.policy.policy_name` | — |
| 政策版本 | `project.policy.policy_version` | — |
| 生效日期 | `project.policy.effective_date` | `YYYY-MM-DD` |
| 失效日期 | `project.policy.expiry_date`（空 → "未标注"） | — |
| 适用范围（省份） | `project.policy.province` | — |
| 价格机制 | `project.policy.pricing_mechanism` | 文本 |
| 市场电价 | `project.policy.market_price` | 元/kWh |
| 机制电价 | `project.policy.mechanism_price` | 元/kWh |
| 机制电量比例 | `project.policy.mechanism_volume_ratio` | 小数 |
| 绿电价格 | `project.policy.green_energy_price` | 元/kWh |
| 绿色环境价值 | `project.policy.green_environmental_value` | 元/kWh |
| 来源 | `project.policy.source` | 政策文件名称/文号 |
| 来源链接 | `project.policy.source_url` | URL |
| 备注 | `project.policy.notes` | 文本 |
| **报告披露** | `f"本测算采用政策：{project.policy.display_version}"` | 必须存在 |

**未关联政策时（`project.policy is None`）必须输出**

| 项目 | 内容 |
|---|---|
| 政策文件 | 未关联政策 Profile |
| 提示 | 本测算未采用内置政策参数，所有电价均为用户输入，请自行核对现行政策。 |

### 2.14 工作表 13：参数来源（可追溯性核心表，§83、§91）

| 列 | 列名 | 数据来源（`parameter_sources` 字典的每一项） | 说明 |
|---|---|---|---|
| 1 | 参数 | 字典键（如 `investment.pv_capex_per_kw`） | 字段路径 |
| 2 | 取值 | `value` | 原始值 |
| 3 | 单位 | `unit` | 取自 `ParameterMeta.unit` |
| 4 | 来源类型 | `source_type_label`（回退 `source_type`） | 中文标签，按 `SourceType` **着色**（§144） |
| 5 | 来源名称 | `source_name` | 文件/合同/政策名 |
| 6 | 来源日期 | `source_date` | `YYYY-MM-DD` |
| 7 | 来源链接 | `source_url` | URL |
| 8 | 是否假设值/备注 | `"假设值；" + note`（`is_assumption` 为真时） | 披露假设 |

**来源类型着色（§144，与界面一致）**

| `source_type` | 中文 | 填充色 |
|---|---|---|
| `USER_INPUT` | 用户输入 | 🔵 浅蓝 `DDEBF7` |
| `CONTRACT` | 合同参数 | 🔵 浅蓝 `DDEBF7` |
| `HISTORICAL` | 历史数据 | 🔵 浅蓝 `DDEBF7` |
| `CALCULATED` | 系统计算 | 🟢 浅绿 `E2EFDA` |
| `EXPERIENCE` | 行业经验 | 🟡 浅黄 `FFF2CC` |
| `ASSUMPTION` | 假设值 | 🟡 浅黄 `FFF2CC` |
| `SYSTEM_DEFAULT` | 系统默认 | 🟡 浅黄 `FFF2CC` |
| `POLICY` | 政策参数 | ⚪ 浅灰 `EDEDED` |

**假设值披露要求（§91）**

- [ ] `is_assumption = True` 的行，第 8 列必须以"假设值；"开头；
- [ ] 情景乘数（`scenario.conservative`、`scenario.optimistic`）与敏感性步长（`sensitivity.steps`）必须出现在本表中；
- [ ] 未在代码中登记来源的关键参数，Phase 6 需补齐登记（当前 `_register_parameters` 登记了 24 个用户输入参数 + 政策参数 + 情景/敏感性参数）。

### 2.16 V2 新增的 11 张工作表（§67）

| # | 工作表名 | 内容 | 主要数据来源 | V1 项目下的行为 |
|---|---|---|---|---|
| 4 | 储能与调度 | 调度策略、SOC 上下限、充放电效率与往返效率、功率上限、价格阈值、5 个开关（§10–§15、§20、§21） | `Project.timeseries.dispatch`、`Project.storage` | 输出配置值 + 「未启用」说明 |
| 5 | 负荷曲线 | 取得方式、年电量、增长率、缺失策略；逐时统计与**逐月负荷表** | `result.time_series_results.hourly` | 占位说明 |
| 6 | 光伏曲线 | 取得方式、容量、等效小时、PR、衰减；逐时统计与**逐月发电量表** | 同上 | 占位说明 |
| 8 | 分时电价 | 各时段电价、上网电价、需量电价、**时段规则表**（月份/日类型/小时） | `Project.timeseries.tariff` | 输出模板配置 + 说明 |
| 12 | 8760时序仿真 | **抽样**逐时明细（见下） | `result.time_series_results.hourly` | 占位说明 |
| 13 | 能量平衡 | 供给侧/需求侧分项、平衡误差、最大逐时误差、容差、是否平衡（§19、§70） | `result.energy_balance` | 占位说明 |
| 14 | 年度汇总 | 逐年电量、收入、运维、EBITDA、现金流、累计现金流 | `result.annual_results` | 照常输出（V1 年度数据） |
| 15 | 收益分解 | 基准/实际电费、电费节省及其分解、上网收入、储能各类收益、需量节省、**关键运行指标**（自用率/自给率/等效循环/需量削减） | `result.time_series_results.metrics` | 占位说明 |
| 20 | 方案比较 | 各候选方案的容量、投资、IRR/NPV/回收期/LCOE/LCOS、年节省、自用率、自给率、等效循环 | `result.scenario_results` | 「未执行方案比较」说明 |
| 21 | 方案寻优 | 目标、扫描维度、约束、最优方案与**「为什么最优」**、候选明细（§45–§48） | `result.optimization_results` | 「未执行方案寻优」说明 |
| 22 | 数据质量 | 总分与四个分项、等级、问题清单（§55） | `result.data_quality` | 「不适用」说明（未导入外部数据） |
| 23 | **账单原始数据**（V2.1 §8.1） | 账单事实逐条明细（账期、计量点、电量、费用分项、来源、质量状态），**按账期排序** | `Project.bills` | 「本项目尚未录入电费账单」+ 录入方法 |
| 24 | **账单校验**（V2.1 §8.1） | ΔE（分时电量合计差）、ΔC（费用分项合计差）、电度电费二层核对、三态一致性、问题清单、**`assumptions` 口径假设** | `BillService.reconcile_all()`（已算好的核对结果） | 同上 |

### 2.17 V2.1 新增的两张账单表（§8.1；阶段 2）

**`账单原始数据`**（列名取自 `BILL_FIELD_LABELS`，含单位）

| 列 | 数据来源 | 规则 |
|---|---|---|
| 序号 / 账单编号 / 账单月份 / 账期起 / 账期止 / 跨月账期 | `ElectricityBill` | 按 `billing_period_start → billing_period_end → 计量点` 排序 |
| 计量点编号 / 客户名称 / 电压等级 / 计费方式 / 合同容量 / 账单计费需量 | `ElectricityBill` | 空值写「账单未提供」；`billing_demand_kw` 是**账单事实**，不是曲线最大值 |
| 总购电量 + 五个分时时段电量 | `ElectricityBill` | `None` → **「账单未提供」（绝不写 0）** |
| 13 个费用分项 + 账单总额 | `ElectricityBill` | 同上；`power_factor_adjustment_yuan` / `adjustment_charge_yuan` 允许负值 |
| 数据来源 / 来源文件名 / 来源行号 / 备注 | `ElectricityBill` | 来源中文标签：手动录入 / Excel 导入 / 估算 |
| 数据质量状态 / 数据质量说明 | `ElectricityBill` | 中文说明逐条拼接 |

**`账单校验`**

| 区块 | 内容 | 数据来源 |
|---|---|---|
| 汇总 | 核对条数、有效/警告/无效条数、ΔE 与 ΔC 的口径说明 | `BillService.reconcile_all()` + `ElectricityBill.quality_status` |
| 明细 | ΔE、电量容差、电量是否一致（三态）、未提供时段、ΔC、金额容差、金额是否一致、电度电费二层差、二层是否一致、质量状态、问题清单（含 ERROR/WARNING/INFO 与规则编号） | `BillReconciliation` |
| 口径假设 | `assumptions` 逐条披露（含"平均综合电价只是统计口径"、"未提供的分项不按 0 计入"） | `BillReconciliation.assumptions` |

**无账单时**：两张表照常生成，写「本项目尚未录入电费账单」＋三种录入方法（手动录入 /
模板导入 / 直接导入 Excel·CSV）＋「账单事实与模拟结果分开保存」的说明，
**不缺表、不报错**（V2.1 §8.2）；由 `tests/test_bill_ui_report.py::TestExcelBillSheets` 断言。

#### 2.17.1 账单数据分类口径的**报告披露义务**（V2.5 §5）

口径全文见 `DATA_MODEL.md` §10。报告侧必须满足：

| 报告位置 | 必须出现的内容 |
|---|---|
| Excel「账单校验」→「口径与假设」区块 / PDF「三、账单事实与校验（四）口径假设」 | ① **运营费用只留档**：`operation_fee_detail`（第 5 页 B/C 类）已包含在总电费里，**只留档、不进入任何电价或费用计算口径**，重复计入即重复计算；② **逐时电价优先**：消纳率电价首选账单 24 小时电量电价表，市场化直购客户**不得**套用湖北政府峰谷系数（尖峰 200% / 高峰 150% / 低谷 45%），该系数仅适用于代理购电客户；③ 计量分组明细只用于校验与追溯 |
| 数据来源 | 三条口径均来自 `BillReconciliation.assumptions`（`calculation/bill_calculator.reconcile_bill` 产出），**报告层不自行推导**（§0.2） |
| 电价口径 | 使用 `calculation/bill_price_source.resolve_bill_energy_price()` 的 `source_label` / `messages` / `assumptions` 原样披露**取值来源与降级过程**；降级到平均电价时必须写明"精度低于逐时口径" |
| 禁止 | 报告**不得**出现把运营费用加进费用合计、把增值税发票金额计入电费、或用政府峰谷系数给市场化直购客户定价的任何数值或结论 |

上述披露由 `tests/test_v25_data_caliber.py` 断言（含"运营费用取任意值数值结果逐位不变"）。

**「8760时序仿真」表的抽样规则（重要）**

完整时序有 8760 点 × 25 列，直接写入会让工作簿大到无法打开。因此该表只写：

* **每月 1 日 24 小时**（12 × 24 = 288 行）
* **1 月 15 日与 7 月 15 日两个典型日**（2 × 24 = 48 行）

合计 **336 行**，并在表头注明「完整 8760 逐时数据可通过 .nep 或 JSON 导出获取」。
测试 `tests/test_report_v2.py::TestHourlySheetSampling` 会断言抽样行数与数值一致性。

**铁律**：工作簿内**不得出现任何公式**（V1 §109、V2 §61）——全部写计算好的数值，
由 `test_excel_export.py::test_no_formulas_written` 与
`test_report_v2.py::TestNoFormulasInV2Sheets` 双重保证。

### 2.15 Excel 通用规格

| 项目 | 规格 |
|---|---|
| 文件扩展名 | `.xlsx`（非 `.xlsx` 时自动补后缀） |
| 工作表数量 | **固定 26 张**（V2.1 阶段 2 起；V1 的 13 张全部保留且相对顺序不变，第一张默认空表已被移除） |
| 标题行 | 第 1 行合并单元格，加粗 14 号；数据从第 3 行开始 |
| 表头行 | 加粗 + 浅蓝填充 `DDEBF7` + 细边框 + 居中换行 |
| 冻结窗格 | 「年度现金流」冻结在首列之后；「敏感性分析」冻结首列 |
| 列宽 | 自适应，最小 10、最大 42（来源表 48、政策表 60、现金流表 20） |
| 数字格式 | 金额 `#,##0.00`；整数 `#,##0`；比例 `0.00%`；四位小数 `#,##0.0000` |
| 无值文案 | "无法计算" / "未回收" / "不适用" / "未覆盖" / "未标注" |
| 日志 | 导出后写日志："导出 Excel：`<path>`（26 张工作表）" |

---

## 3. PDF 报告设计（§110、§111）

> **当前状态：已实现（快照进行中）。** 实现文件为 `src/cenep/reports/pdf_exporter.py`
> （`PdfExporter.export(project, result, path)`，reportlab，17 部分 + 3 张图表；中文字体优先系统 TTF，
> 回退 `STSong-Light`，再回退 Helvetica）。数据来源同样只能是 `CalculationResult` 与 `Project`。

### 3.1 17 部分结构（V2 §66 + V2.1 §8.1）

V2 §66 把 V1 §110 的 15 章**重组为 16 部分**：V1 的「测算条件 / 技术参数 / 电价参数」
并入「二、输入参数」，「运营成本」并入「十一、现金流」，「收益测算」并入「十二、经济指标」，
「政策依据」并入「十六、参数来源」；并**新增 6 个时序章节**。
**V2.1 §8.1（阶段 2）在「二、输入参数」之后插入「三、账单事实与校验」**，
此后各章编号整体顺延一位（内容与名目一项未丢），共 **17 部分**。V1 的内容一项未丢。

| # | 部分名 | 主要内容 | 数据来源 | V1 对应 |
|---|---|---|---|---|
| — | 封面 | 项目名称、类型、地点、业主、评价日期、政策版本、免责提示 | `Project.basic_info`、`Project.policy` | 原第 1 章 |
| 一 | 项目概况 | 项目身份表 + 关键结论指标摘要 | `Project.basic_info`、`CalculationResult` | 原第 2 章 |
| 二 | 输入参数 | （一）技术参数　（二）电价参数，以及计算期、折现率、口径说明 | `Project`、`result.notes` | 原第 3–5 章 |
| **三** | **账单事实与校验** | **账单概况、月度趋势、账单事实明细、ΔE/ΔC 校验、口径假设；无账单时输出说明段落** | **`Project.bills`、`BillService`** | **V2.1 新增（§8.1）** |
| 四 | 负荷分析 | 年电量、最大/最小/平均负荷、取得方式、增长率；**图 1** 典型日负荷与光伏出力 | `time_series_results.hourly` | **新增** |
| 五 | PV时序分析 | 年发电量、等效小时、最大出力、弃光、自用率、自给率；**图 2** 逐月上网电量 | 同上 | **新增** |
| 六 | 储能SOC分析 | 容量/功率、SOC 上下限与实际区间、充放电量、电网充电量、等效循环；**图 3** 典型日 SOC | `time_series_results.hourly`、`metrics` | **新增** |
| 七 | 能源流 | 供给侧/需求侧全部路径分项、平衡误差、容差、是否平衡（§19） | `result.energy_balance` | **新增** |
| 八 | 电费分析 | 基准/实际电费、电费节省及其分解、需量电费节省、最大需量前后 | `metrics`、`baseline_results` | **新增** |
| 九 | 储能收益 | 套利/容量/辅助/其他收益、等效循环、上网收入、首年收益合计 | `metrics` | **新增** |
| 十 | 投资 | 投资明细、总投资、单位投资 | `result.capex_breakdown`、`unit_investment` | 原第 6 章 |
| 十一 | 现金流 | （一）运营成本　（二）年度现金流表（含 Year 0） | `Project.opex`、`annual_results` | 原第 7、9 章 |
| 十二 | 经济指标 | （一）收益测算　（二）11 个首页指标 + ROI + 资本金 NPV | `AnnualResult`、`CalculationResult` | 原第 8、10 章 |
| 十三 | 方案比较 | 三情景对比表；有 V2 方案扫描结果时并列表 | `result.scenarios`、`scenario_results` | 原第 12 章 + V2 |
| 十四 | 敏感性 | 敏感性表（IRR/NPV/回收期） | `result.sensitivity` | 原第 11 章 |
| 十五 | 风险 | 敏感性最高的因素、假设值、政策时效性提示（**不含"可行/不可行"结论**） | `result.sensitivity`、`parameter_sources` | 原第 13 章 |
| 十六 | 参数来源 | （一）测算说明：政策版本、参数来源与链接、核实状态、全部口径说明 | `Project.policy`、`result.notes` | 原第 14、15 章 |
| 十七 | 免责声明 | 免责声明全文（两段，原文照抄） | §111 原文 | 原第 15 章末 |

**V2.1 §8.1「三、账单事实与校验」的内容与口径**

| 小节 | 内容 | 数据来源 | 关键约束 |
|---|---|---|---|
| 概况 | 账单条数、覆盖月份、数据来源构成、质量状态构成、年度覆盖率 / 总电量 / 总额 / 平均综合电价、是否可直接相加、缺失月份、跨月与非自然月账期 | `Project.bills`、`BillService.annual_summary()` | 平均综合电价**只是账单统计口径**，不得当作边际节省电价（§3.1） |
| （一）月度趋势 | 逐月条数、总购电量、账单总额、平均综合电价、跨月标记、质量状态 | `BillService.monthly_summary()` | `None` → 「账单未提供」，**不显示 0**（§2.1） |
| （二）账单事实明细 | 逐条账单的月份、账期（跨月标注）、计量点、总购电量、账单总额、数据来源、质量状态 | `Project.bills` | 按账期排序；数据来源标签（手动录入 / Excel 导入 / 估算）必须显示 |
| （三）校验与差异 | ΔE、电量一致性三态、ΔC、金额一致性三态、电度电费二层核对、质量状态、问题清单（ERROR / WARNING / INFO + 规则编号） | `BillService.reconcile_all()` | 差异数值一律披露，不因未超容差而隐藏（§2.1） |
| （四）口径假设 | `assumptions` 与年度汇总 `messages` 逐条列出 + 「账单事实 vs 模拟结果」分界说明 | `BillReconciliation.assumptions`、`BillAnnualSummary.messages` | 必须写明账单复算与光储方案模拟属于阶段 5/6，当前为「待确认 / 未建模」 |
| 无账单时 | 「本项目尚未录入电费账单」＋三种录入方法＋分界说明 | 固定中文文案 | **不缺章节、不报错**（§8.2） |

**新增图表（V2 §66，用 `reportlab.graphics` 直接绘制，不引入新依赖）**

| 图 | 位置 | 内容 | 数据来源 |
|---|---|---|---|
| 图 1 | 四、负荷分析 | 典型日（7 月 15 日）负荷与光伏出力双折线 | `hourly.load`、`hourly.pv_generation` |
| 图 2 | 五、PV时序分析 | 逐月上网电量柱状图（12 柱） | `hourly.grid_export` 按月汇总 |
| 图 3 | 六、储能SOC分析 | 典型日（7 月 15 日）储能 SOC 曲线（%） | `hourly.storage_soc_end` |

**绘图约束**：只画**抽样**数据（典型日 24 点或 12 个月），不把 8760 点画进报告，
以免 PDF 体积与渲染时间失控；未启用时序仿真时**不绘制任何图表**（V1 报告保持无图）。
测试 `tests/test_report_v2.py::TestPdfCharts` 会断言图表数量、所在章节与体积上限。

### 3.2 逐章内容设计

#### 第 1 章 封面

| 要素 | 数据来源 | 版式 |
|---|---|---|
| 报告标题 | 固定文案："工商业新能源项目经济评价测算报告（前期测算）" | 居中，20–24pt 加粗 |
| 项目名称 | `project.basic_info.project_name` | 居中，16pt |
| 项目类型 | `project.basic_info.project_type.label` | 居中，12pt |
| 省份 / 城市 | `project.basic_info.province` / `city` | 居中，12pt |
| 业主名称 | `project.basic_info.customer_name` | 左对齐，11pt |
| 评价日期 | `project.basic_info.evaluation_date` | 左对齐，11pt |
| 政策版本 | `project.policy.display_version`（无则"未关联政策 Profile"） | 左对齐，11pt，加框提示 |
| 装机规模摘要 | `result.pv_capacity_kwp` / `storage_power_kw` / `storage_energy_kwh` | 左对齐，11pt |
| 关键指标摘要（可选） | 项目IRR、项目NPV、静态回收期 | 左对齐，11pt |
| 免责提示（封面底部） | "本报告为前期测算材料，不替代正式可行性研究。" | 底部居中，9pt 灰字 |
| 编制单位 / 编制人 / 日期 | 报告配置（✅ 已实现（快照进行中）） | 底部左对齐 |

#### 第 2 章 项目概况

| 内容块 | 数据来源 |
|---|---|
| 项目基本信息表 | `project.basic_info` 全部字段 |
| 建设规模表 | `result.pv_capacity_kwp`、`storage_power_kw`、`storage_energy_kwh`、`storage_duration_hours` |
| 投资与融资摘要 | `result.total_capex`、`loan_amount`、`equity_amount` |
| 关键指标摘要 | `project_irr`、`equity_irr`、`project_npv`、`static_payback`、`discounted_payback`、`lcoe`、`lcos`、`min_dscr` |

> **禁止**：本章不得出现"项目可行 / 不可行"的结论性表述（§106、§144）。

#### 第 3 章 测算条件

| 内容块 | 数据来源 |
|---|---|
| 计算期与折现率 | `project.analysis_period`、`project.discount_rate` |
| 电价模式 | `project.tariff.tariff_mode.label` |
| 口径说明清单 | `result.notes`（**逐条列出，不得删减**） |
| 政策版本 | `result.notes` 中"本测算采用政策：…"或"未关联政策 Profile" |
| 软件版本与导出时间 | `cenep.__version__`、导出时间戳 |

#### 第 4 章 技术参数

内容同 Excel 工作表 3（第 2.4 节）。版式：两列表格（参数 / 取值+单位），光伏与储能分块。

#### 第 5 章 电价参数

内容同 Excel 工作表 4（第 2.5 节），并增加"各电价模式推导规则"小表：

| 模式 | 替代电价 | 充电电价 |
|---|---|---|
| 固定电价 | 固定电价 | 固定电价 |
| 峰平谷电价 | 峰平谷综合电价 | 谷电价 |
| 市场电价 | 市场电价 | 市场电价 |
| 自定义电价 | 自定义替代电价 | 自定义充电电价 |

#### 第 6 章 投资估算

内容同 Excel 工作表 5，并增加投资构成**饼图或百分条**（数据来自 `capex_breakdown`）。

#### 第 7 章 运营成本

| 内容块 | 数据来源 |
|---|---|
| 运维参数表 | `project.opex.*` |
| 首年运维费合计 | `result.first_year_opex` |
| 经营期年均运维费 | `result.annual_opex` |
| 逐年运维费曲线（图） | `annual_results[].opex` |

#### 第 8 章 收益测算

| 内容块 | 数据来源 |
|---|---|
| 首年收入构成表 | `annual_results[0]` 的 6 个收入字段 + `total_revenue` |
| 首年发电量与电量分配 | `pv_generation_kwh`、`pv_self_use_kwh`、`pv_export_kwh`、`pv_to_storage_kwh`、`pv_loss_kwh` |
| 储能收益四类分解 | `storage_arbitrage_revenue`、`storage_capacity_revenue`、`storage_ancillary_revenue`、`storage_other_revenue` |
| 年均收入 | `result.annual_revenue` |
| 逐年收入曲线（图） | `annual_results[].total_revenue` |

#### 第 9 章 现金流

| 内容块 | 数据来源 |
|---|---|
| 年度现金流表（含 Year 0） | 同 Excel 工作表 8（可用精简列版） |
| 累计现金流图 | `result.cumulative_cashflow` |
| 年度现金流图 | `result.project_cashflows`、`equity_cashflows` |
| 回收期标注 | `result.static_payback`、`discounted_payback` |

#### 第 10 章 经济指标

| 指标 | 字段 | 单位 | 口径说明（必须同行显示） |
|---|---|---|---|
| 总投资 | `total_capex` | 元 | 各项投资之和 |
| 项目IRR | `project_irr` | % | 融资前现金流 IRR（所得税后） |
| 资本金IRR | `equity_irr` | % | 资本金现金流 IRR |
| 项目NPV | `project_npv` | 元 | 折现率 `discount_rate` |
| 资本金NPV | `equity_npv` | 元 | 同上 |
| 静态回收期 | `static_payback` | 年 | 自 Year 0 起算 |
| 动态回收期 | `discounted_payback` | 年 | 同上 |
| LCOE | `lcoe` | 元/kWh | 光伏口径，成本不含融资利息 |
| LCOS | `lcos` | 元/kWh | 储能口径 |
| ROI | `roi` | 倍 | **生命周期累计净收益 ÷ 初始总投资** |
| 最低DSCR | `min_dscr` | 倍 | CFADS ÷ 还本付息 |

#### 第 11 章 敏感性分析

内容同 Excel 工作表 10，并增加两张图：
- 项目IRR 敏感性图（`sensitivity[].change` × `project_irr`，按变量分组）
- NPV 敏感性图（`sensitivity[].change` × `project_npv`，按变量分组）

#### 第 12 章 情景分析

内容同 Excel 工作表 11。

#### 第 13 章 风险提示

| 提示类型 | 生成依据 | 文案模板 |
|---|---|---|
| 敏感性最高的因素 | `sensitivity[].coefficient` 绝对值最大的变量 | "本项目对【X】最为敏感，敏感度系数为 Y。" |
| 假设值清单 | `parameter_sources` 中 `is_assumption = True` 的项 | "以下参数为假设值，请核实：【列表】" |
| 系统默认值清单 | `source_type = SYSTEM_DEFAULT` 的项 | "以下参数采用系统默认值：【列表】" |
| 政策时效性 | `project.policy.expiry_date` 与评价日期比较 | "本测算采用的政策版本生效于【X】，请注意政策时效性。" |
| 简化口径提示 | `result.notes` 中的税务简化条目 | "本软件的税务为简化模型，不建模进项抵扣、留抵退税与亏损跨年弥补。" |
| 不可计算项 | `None` 的指标 | "【X】无法计算，原因：现金流不存在符号变化。" |

> **禁止**：本章不得输出"项目风险等级为高/中/低"这类量化结论（§106、§144）；
> 只做**因素提示**与**假设披露**。

#### 第 14 章 政策依据

内容同 Excel 工作表 12，并附 `REGULATIONS_AND_POLICY.md` 中实际使用的依据条目与**核实状态**（✅已核实 / ⚠️待核实）。

#### 第十五部分（一）测算说明

| 内容块 | 数据来源 |
|---|---|
| 全部口径说明 | `result.notes` 逐条 |
| 参数来源汇总 | `parameter_sources` 概览（可选附录形式） |
| **免责声明（原文，必须完整出现）** | §111 |
| 软件版本与导出时间 | `cenep.__version__`、时间戳 |

### 3.3 页面版式建议

| 项目 | 规格 |
|---|---|
| 纸张 | A4 纵向（年度现金流与敏感性表可用 A4 横向或缩放至一页宽） |
| 页边距 | 上下 2.0 cm；左右 2.2 cm（需容纳表格时用 1.5 cm） |
| 字体 | 中文黑体系（标题）/ 宋体系（正文）；正文 10.5pt，表格 8.5–9pt，脚注 8pt |
| 行距 | 正文 1.4 倍；表格单倍 |
| 章标题 | 一级标题 16pt 加粗；二级 13pt 加粗 |
| 表格样式 | 表头浅蓝底 `DDEBF7`；数字右对齐；单位列左对齐；边框细线 |
| 图 | 宽度不超过版心宽度；图题在图下方居中（"图 X-Y 图名"，8.5pt） |
| 表 | 表题在表上方居中（"表 X-Y 表名"，8.5pt） |
| 数字格式 | 与 Excel 一致（金额 `#,##0.00`、比例 `0.00%`、四位小数 `0.0000`） |
| 无值文案 | 与 Excel 一致（无法计算 / 未回收 / 不适用） |

### 3.4 页眉、页脚、页码、政策版本提示位

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ 页眉（左）：工商业新能源项目经济评价测算报告（前期测算）                        │
│ 页眉（右）：项目名称 | 项目类型                                                │
│ 页眉（次行，右，小字）：本测算采用政策：{policy.display_version}                │
│                          （未关联政策时显示："未关联政策 Profile"）             │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   正文                                                                       │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ 页脚（左）：本报告为前期测算材料，不替代正式可行性研究                          │
│ 页脚（中）：导出时间 yyyy-MM-dd HH:mm                                         │
│ 页脚（右）：第 X 页 / 共 Y 页                                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

| 元素 | 内容 | 数据来源 |
|---|---|---|
| 页眉左 | 报告标题 | 固定文案 |
| 页眉右 | `project.basic_info.project_name`、`project_type.label` | `Project` |
| 页眉次行右（小字） | **政策版本提示位**："本测算采用政策：`{policy.display_version}`" | `project.policy.display_version` |
| 页脚左 | "本报告为前期测算材料，不替代正式可行性研究" | 固定文案 |
| 页脚中 | 导出时间 | 时间戳 |
| 页脚右 | "第 X 页 / 共 Y 页" | 页码 |
| 封面页 | **不显示**页眉页脚 | — |

**免责声明位置（§111）**

| 位置 | 形式 |
|---|---|
| 封面底部 | 一句话提示："本报告为前期测算材料，不替代正式可行性研究。" |
| 第十七部分「免责声明」 | **免责声明全文（两段，原文照抄）** |
| 每页页脚（左） | 一句话提示 |
| PDF 元数据 / 尾页 | 完整免责声明再次出现（可选） |

### 3.5 PDF 生成流程（Phase 7 建议）

```text
project + result
      │
      ▼
PDFExporter.export(project, result, path)
      │
      ├─ 构建文档（A4、页眉页脚模板）
      ├─ 第 1 章 封面
      ├─ 第 1–17 部分 逐章渲染
      │      └─ 所有数值直接取自 CalculationResult / Project（禁止重算）
      ├─ 图表：matplotlib 生成图片后插入（数据来自 result 序列）
      ├─ 免责声明原文校验（断言两段文本均存在）
      ├─ 保存 PDF
      └─ 日志："导出 PDF：<path>（17 部分）"
```

---

## 4. 输出一致性与验收（§109、§154）

### 4.1 一致性要求

| 校验 | 内容 | 当前状态 |
|---|---|---|
| Excel ↔ `CalculationResult` | 「年度现金流」每行 = `annual_results[i]`；「财务指标」每行 = 对应字段 | ✅ 已实现且测试全绿（`test_excel_export.py` 19 项） |
| PDF ↔ `CalculationResult` | 指标部分 = 对应字段；文本提取后比对 | ✅ 已实现且测试全绿（`test_pdf_export.py` 19 项） |
| Excel ↔ PDF | 同一指标数值一致（格式化归一化后） | ✅ 已实现且测试全绿（`test_report_v2.py` 41 项） |
| GUI ↔ Excel ↔ PDF | 三层互等 | ⚠️ **间接覆盖**：`test_gui.py` / `test_gui_v2.py` 断言"界面显示值 = `CalculationResult`"，`test_report_v2.py::test_both_excel_and_pdf_expose_v2_indicators` 断言"Excel 与 PDF 同时含 V2 指标"，二者合起来等价于三层同源；**尚无单一的三层互等用例** |
| 禁止 Excel 公式 | 单元格只写数值；公式仅以文本展示 | ✅ 已按此实现 |
| 禁止导出前重算 | 参数变更后必须重新计算再导出 | ✅ 已实现：报表只消费传入的 `CalculationResult`，且 `test_excel_export.py::test_no_formulas_written` 断言工作簿内公式单元格数为 0 |

### 4.2 报告验收 checklist

- [x] Excel 恰好 26 张工作表（V1 §108 的 13 张 + V2 §67 新增 11 张 + V2.1 §8.1 新增 2 张账单表），表名与 `SHEET_NAMES` 一致 —— ✅已实现
- [x] Excel 数据全部来自 `CalculationResult` / `Project` —— ✅已实现
- [x] 年度现金流表包含 Year 0 行 —— ✅已实现
- [x] 参数来源表按 `SourceType` 着色，假设值带"假设值；"前缀 —— ✅已实现
- [x] 未关联政策时的提示文案 —— ✅已实现
- [x] Excel 全部测试通过（**V2.0.0 全量 885 passed / 0 failed**） —— ✅已完成
- [ ] 年度现金流表补齐建议列（电量分配、收入构成、利润表、债务、偿债） —— ⬜未实现
- [x] PDF 17 部分齐备且部分名与 V2 §66 / V2.1 §8.1 一致 —— ✅已实现
- [x] PDF 封面要素完整（含政策版本与免责提示） —— ✅已实现（快照进行中）
- [x] PDF 页眉页脚、页码、政策版本提示位 —— ✅已实现（快照进行中）
- [x] PDF 第十七部分含 §111 免责声明原文（两段） —— ✅已实现
- [x] V2.1 §8.1 两张账单表（`账单原始数据` / `账单校验`）在**有 / 无账单**两种情况下均生成，无账单时写明三种录入方法 —— ✅已实现（`tests/test_bill_ui_report.py::TestExcelBillSheets`）
- [x] V2.1 §8.1 PDF「三、账单事实与校验」章节（概况 / 月度趋势 / 事实明细 / ΔEΔC / assumptions / 无账单说明） —— ✅已实现（`tests/test_bill_ui_report.py::TestPdfBillSection`）
- [x] 账单电量 / 金额为 `None` 时 Excel 与 PDF 均写「账单未提供」，**绝不写 0**（V2.1 §2.1） —— ✅已实现（`TestMissingValueDisplay::test_excel_and_pdf_never_show_zero_for_missing`）
- [ ] PDF 图表齐全（**规范原列的三张：累计现金流、IRR 敏感性、NPV 敏感性**） —— ⬜**仍未实现**；V2 新增的 3 张图是**典型日负荷与光伏出力曲线、逐月上网电量柱状图、典型日储能 SOC 曲线**，与这三张不是同一组
- [x] PDF ↔ `CalculationResult` 一致性测试（`tests/test_pdf_export.py`、`tests/test_report_v2.py`） —— ✅已实现且测试全绿
- [~] 三层互等测试（GUI = Excel = PDF） —— ⚠️ **间接覆盖**（GUI↔结果 与 Excel/PDF↔结果 分别有断言），**尚无单一的三层互等用例**

---

## 5. 免责声明（§111，原文，必须完整出现在 PDF 第十七部分）

> **本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，不替代项目正式可行性研究、工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。**
>
> **电价、市场交易、税务、储能收益等政策参数具有时效性，应以项目实施时的最新正式政策及实际合同为准。**

---

## 6. Phase 6 / Phase 7 实现任务清单

**Phase 6（Excel，已基本完成）**

- [x] 建立 `src/cenep/reports/excel_exporter.py`
- [x] 实现 26 张工作表（V1 13 张 + V2 新增 11 张 + V2.1 新增 2 张账单表）
- [x] 实现来源类型着色与假设值前缀
- [x] 实现标题/表头/边框/列宽/数字格式
- [ ] 修复 `tests/test_excel_export.py` 中的失败项
- [ ] 年度现金流表补齐建议列（见 2.9）
- [ ] 敏感性表增加 `irr_change` 列
- [ ] 增加"公式说明"列（文本形式，不写 Excel 公式）
- [ ] 增加 Excel 工作表保护提示（只读建议）

**Phase 7（PDF，已实现，快照进行中）**

- [x] 建立 `src/cenep/reports/pdf_exporter.py`（reportlab）
- [x] 实现封面与 17 部分结构（`REPORT_SECTIONS`）
- [x] 实现页眉页脚模板、页码、政策版本提示位（`_decorate`）
- [x] 实现中文字体注册（系统 TTF → `STSong-Light` → Helvetica 回退）
- [x] 实现表格渲染组件（表头样式、数字格式、无值文案）
- [x] 实现第 13 章「风险提示」的生成（`_risk_text`）
- [ ] 实现图表渲染（累计现金流、IRR 敏感性、NPV 敏感性等，数据来自 `result` 序列）——⬜ 当前以表格为主
- [ ] 实现免责声明原文校验断言（当前为人工核对）
- [x] 编写测试 `tests/test_pdf_export.py`：章数、章名、指标值一致性、免责声明存在性 —— ✅已建立，⬜ 待全绿
- [ ] 编写三层一致性测试 `tests/test_output_consistency.py`
- [ ] **验收标准**：`GUI = Excel = PDF = CalculationResult`（§154）——⬜ 待 GUI 实现后补齐

---

## 7. 关联文档

| 文档 | 关系 |
|---|---|
| `PROJECT_SPEC.md` | 验收 checklist ③输出一致性的判定标准 |
| `UI_SPEC.md` | 报告页的导出入口与预览设计 |
| `ROADMAP.md` | Phase 6 / Phase 7 的交付物与验收标准 |
| `REGULATIONS_AND_POLICY.md` | 第 14 章政策依据的条目与核实状态 |
| `HUBEI_POLICY_MODEL.md` | 政策版本展示文案与字段来源 |
| `src/cenep/domain/results.py` | 本文件所有数据来源字段的权威定义 |
| `src/cenep/reports/excel_exporter.py` | Excel 的现有实现 |
| `src/cenep/reports/pdf_exporter.py` | PDF 实现（17 部分 + 3 张图表 + 块级重排由 V2 §66 / V2.1 §8.1 决定） |
