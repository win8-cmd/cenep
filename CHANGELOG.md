# 变更日志（CHANGELOG）

> 本文档记录 CENEP 的所有**重要变更**。
>
> **格式约定**：遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
> **版本号**遵循[语义化版本](https://semver.org/lang/zh-CN/)（`主版本.次版本.修订号`）。
>
> **状态说明**：`[1.0.0]` 与 `[Unreleased] — V2.0.0` **均为已交付事实**
> （代码在磁盘上、测试可运行、EXE 可启动）。V2 的 `domain/`、`calculation/`（V2 部分）、
> `optimization/`、`data/`、`ui/charts.py` 与 `infrastructure/migration.py` **全部落地**，
> 全量测试 **885 passed / 0 failed（32 个测试文件）**，`dist\CENEP\CENEP.exe` 已用
> V2 依赖重建并启动验证通过。少数条目仍标 ⬜ —— 它们是 V2 **有意不在本版实现**的
> 设计延伸（例如把版本元数据放进 `.nep` 载荷），不得据此认为 V2 未实现。
>
> **分类**：`Added` 新增 / `Changed` 变更 / `Deprecated` 弃用 / `Removed` 移除 /
> `Fixed` 修复 / `Security` 安全。

---

## [Unreleased] — V2.2（阶段 4：月账单估算与光伏消纳计算）

> **定位**：《CENEP V2.1–V2.4 增量开发执行规格书》§10 阶段 4——
> 「实现可编辑典型负荷模板和月电量回归；实现消纳率纯计算模块；复用 PV profile 和
> 能量平衡引擎；完成 UI 曲线和指标；完成 Excel/PDF 输出和测试」。
>
> **状态**：**已交付并可运行**。全部改动为**附加式**：既有 9 个页面标题与相对顺序、
> 既有 26 张工作表与 17 个 PDF 部分的**名目与内容一项未删**；
> 新增页面 / 工作表 / 章节均为追加（PDF 章节编号顺延，内容不变）。
> 每阶段契约与实测记录见 **`V2.2_SELF_CONSUMPTION_SPEC.md`**。
>
> **红线遵守**：核心公式全部在 `calculation/`（`self_consumption.py`、`load_estimate.py`、
> `load_portrait.py`），界面**零计算**（静态扫描锁定）；`policy/hubei.py` 未改动（仍全 `None`）；
> 估算曲线与实测曲线在**类型层**强制区分（`LoadDataSourceType` + `estimated` + `is_estimate`），
> 估算结果始终携带"基于估算"标记。

### Added（V2.2 阶段 4 新增）

- **消纳率纯计算模块** `calculation/self_consumption.py`：按 §3.3 实现
  `E_self=min(负荷,光伏)`、`E_export=max(光伏−负荷,0)`、`E_import=max(负荷−光伏,0)` 与
  四项指标（光伏自用率 / 负荷覆盖率 / 光伏上网率 / 电网依赖率），
  每项含**公式、分子、分母、单位、边界、统计口径**（`CALIBERS`，界面/Excel/PDF 三处同源）；
  分母为 0 返回 `None`（"不适用"，不显示 0%）。
- **月账单估算引擎** `calculation/load_estimate.py`：`E_i = E_month × w_i ÷ Σw_i`
  （权重不是 kW），**每月严格回归**到输入月电量（残差 0.0，容差 `1e-6` kWh）；
  4 种月电量来源、日类型优先级、周末/节假日/停产比例、班次裁剪、白天/夜间电量比例、
  子小时均分假设，全部写入 `assumptions`。
- **可编辑典型负荷模板** `domain/load_estimate.py`：内置 5 套（单班/双班/连续/工作日为主/办公型），
  明确声明"模板是权重曲线，不是行业实测事实"。
- **消纳结果模型** `domain/self_consumption_result.py`：§2.4 字段 + 逐月明细 +
  口径列表 + 来源标签 + `assumption_lines()`；非实测来源而 `is_based_on_estimate=False` 直接报错。
- **负荷画像** `calculation/load_portrait.py`：逐月电量/峰值/均值/负荷率（向量化分组）、
  典型工作日/休息日/全部日曲线、年化估算。
- **应用服务** `application/load_profile_service.py`：数据集登记/切换（**保留历史版本**）、
  账单电量 → 估算输入、月账单估算、高频导入复用、光伏出力解析（复用 §9 引擎）、
  消纳分析装配、逐月覆盖提示；`ProjectService.load_profile_service()` 只做装配。
- **界面「负荷与消纳」页** `ui/load_pages.py`（A 数据来源 / B 负荷画像 / C 消纳分析）+
  `ui/charts.py` 的 `LoadPvChart`、`MonthlyRateChart`；估算横幅**橙色**、实测横幅**绿色**；
  输入变更即标记"结果已过期，请重新计算"。
- **时间轴工具** `calculation/timeseries_engine.py::axis_from_timestamps`（纯追加）：
  由任意时间戳序列构造 `TimeAxis`，支持部分年度真实数据。
- **Excel「消纳率分析」表**（第 27 张）与 **PDF「五、负荷估算与光伏消纳」章节**（第 18 部分）：
  四项指标 + 口径 + 逐月明细 + 假设 + 估算徽标；无数据时输出中文说明，不缺表/章节。
- **测试**：`tests/test_self_consumption.py`、`tests/test_load_estimate.py`、
  `tests/test_load_profile_service.py`、`tests/test_load_ui_report.py`。
- **文档**：`V2.2_SELF_CONSUMPTION_SPEC.md`（口径、估算规则、真实资料实测与差异归因）。

### Changed（V2.2 阶段 4，全部为追加式扩展）

- `domain/models.py`：`Project` **追加** `load_datasets` / `active_load_dataset_id`
  （默认空列表/空串，旧项目反序列化不受影响）。
- `domain/enums.py`：**追加** `LoadEstimateSource`（月电量来源），既有成员未动。
- `reports/pdf_exporter.py`：插入「负荷估算与光伏消纳」，后续章节编号顺延（内容不变）；
  `build_story` / `export` 追加可选关键字参数 `self_consumption` / `load_portrait`。
- `reports/excel_exporter.py`：`SHEET_NAMES` 追加「消纳率分析」；`export` 追加同样两个可选参数。
- `ui/main_window.py`：追加「负荷与消纳」页签与服务装配；导出时把已算好的结果传给报告层。
- `selftest.py` 与 6 个既有测试文件的"表数/章节数/页面数"断言同步（26→27、17→18、9→10 页）。

---

## [Unreleased] — V2.1（阶段 2：账单页面与报告）

> **定位**：把 V2.1 阶段 1 交付的账单能力（`domain/bill_models.py`、
> `calculation/bill_calculator.py`、`data/bill_importer.py`、`application/bill_service.py`）
> **接进现有 GUI 与 Excel/PDF 报告**，即《CENEP V2.1–V2.4 增量开发执行规格书》§10 阶段 2：
> 「用户无需修改代码即可导入并查看账单」。
>
> **状态**：**已交付并可运行**。全部改动为**附加式**：既有 8 个页面的标题与相对顺序、
> 既有 24 张工作表与 16 个 PDF 部分的**名目与内容一项未删**；新增页面 / 工作表 / 章节均为追加。
> 全量测试由 **1126 passed** 增至 **>1126 passed / 0 failed / 0 skipped**
> （新增 `tests/test_bill_ui_report.py`，并同步 6 个既有测试文件的表数 / 章节数 / 页面数断言）。
>
> **红线遵守**：账单页与报告**不含任何计算**——列表、汇总、差异、平均电价、校验级别
> 全部来自 `BillService`（其内部再委托 `calculation/bill_calculator.py`）；
> `policy/hubei.py` 未改动（仍全 `None`）；界面无动画、无 AI 聊天入口。

### Added（V2.1 阶段 2 新增）

- **「月度账单」页面**（`ui/pages.py::BillsPage`，注册于 `ui/main_window.py::_build_ui`）：
  账单列表（按账期排序、按月份筛选）、录入 / 编辑表单、月度汇总、年度汇总、
  分级校验提示、导入向导五个页签。
  - 表单**复用** `ui/field_spec.py` 的 `SectionSpec` / `FieldSpec` / `SectionForm`，
    字段规格新增于 `ui/sections.py` 的 `BILL_SECTIONS`（**不进** `ALL_SECTIONS`，
    参数页仍为既有 14 组）；中文名与单位统一取自 `domain/bill_models.py::BILL_FIELD_LABELS`。
  - `field_spec.py` **附加式**新增 `Kind.DATE`（账期用 `QDateEdit`）与 `FieldSpec.optional_tooltip`
    （既有字段的可留空提示文案一字未改）。
  - **六步导入向导**（`ui/pages.py::BillImportWizard`）：选文件 → 选表 → 映射列 → 预览 → 校验 →
    确认导入；自动列映射失败时自动切换为**手工列映射**（支持用户重命名列）；
    重复账单按「跳过 / 替换 / 保留两条」选择；错误行定位到「工作表 / 行号 / 字段 / 原因」。
  - 界面显式写明**账单事实与模拟结果的分界**（复算与光储方案模拟属于阶段 5/6，标注
    「待确认 / 未建模」）、**数据来源标签**（手动录入 / Excel 导入 / 估算）、
    **平均综合电价只是统计口径**，以及跨月账期 / 缺月 / 不可直接相加等提示。
  - 电量与金额为 `None` 时显示「账单未提供」（**绝不显示 0**，§2.1）；校验问题按
    ERROR（红）/ WARNING（橙）/ INFO（蓝）分级着色显示。
  - 模板下载走 `BillService.template_bytes()`；导出文件名用 `with_name(name + 后缀)`。
- **Excel 新增两张表**：`账单原始数据`（账单事实逐条明细，按账期排序）与
  `账单校验`（ΔE / ΔC / 二层核对差异 + `assumptions` 口径假设）。
  `SHEET_NAMES` 由 24 张增至 **26 张**；无账单时两张表照常生成，写
  「本项目尚未录入电费账单」及三种录入方法，**不缺表、不报错**；
  工作簿内公式数仍为 **0**。
- **PDF 新增章节**「三、账单事实与校验」（位于「输入参数」之后、「负荷分析」之前）：
  概况与月度趋势、账单事实明细、ΔE / ΔC 校验、口径假设，以及无账单时的说明段落。
  `REPORT_SECTIONS` 由 16 部分增至 **17 部分**，既有章节编号整体顺延一位（内容未变）。
- **新增测试** `tests/test_bill_ui_report.py`（47 例）：页面构造 / 填入 / 清空 / 空状态指引、
  六步向导状态转换、ERROR/WARNING/INFO 分级与配色、`None` → 「账单未提供」（页面 + Excel + PDF
  三处专测）、Excel 两张表有 / 无账单两种情况与公式数为 0、PDF 有 / 无账单与章节数、
  §105 页面显示 = `BillService` 返回、主窗口保存 / 重开后账单恢复。

### Changed（V2.1 阶段 2 变更）

- `ui/main_window.py`：`_build_ui()` 的 `(widget, title)` 元组表**追加** `(self.bills_page, "月度账单")`
  （紧跟「时序仿真」）；`_reload_all()` 经 `ProjectService.bill_service(project)` 装配账单服务。
  页面数 8 → **9**（既有 8 个页面的标题与顺序未变）。
- `src/cenep/selftest.py`：工作表数断言 24 → 26、PDF 章节数断言 16 → 17
  （**唯一一处允许清单之外的改动**：该文件把两个数量硬编码为结构自检，
  任务书要求同步表数断言，且不改则打包前自检必然失败；无任何行为变化）。
- 同步既有测试断言（正当变更，均带「V2.1 新增账单页 / 账单表 / 账单章节」注释）：
  `tests/test_gui.py`（`test_eight_tabs` → `test_nine_tabs`）、
  `tests/test_excel_export.py`（24 → 26）、`tests/test_report_v2.py`（24 → 26、章节序号 + 图表所在章节）、
  `tests/test_pdf_export.py`（16 → 17）、`tests/test_selftest.py`（24 → 26）。
- 文档同步：`UI_SPEC.md`（9 个页面 + 账单页设计）、`REPORT_SPEC.md`（26 张表 / 17 部分）。

---

> **定位**：在 V1 年度模型之外新增**逐小时（8760）时序仿真**、**储能 SOC 调度与三策略**、
> **需量电费与削峰**、**方案比较与三级寻优**与**时序数据导入／质量评分**
> （即 V1 §139 的 V2 候选功能：8760 模型、储能优化、需量电费、更复杂分时电价）。
>
> **状态**：**已交付并可运行**。本节条目全部对照 `src/` 与 `tests/` 逐条核实，
> 全量测试 `pytest tests` → **885 passed / 0 failed（32 个测试文件）**；
> 打包产物 `dist\CENEP\CENEP.exe`（V2.0.0 版本资源）已实际启动验证。
> **V1 兼容性红线未破**：V1 项目（`timeseries.enabled = False`）继续走同一条 V1 年度
> 代码路径，`test_engine_golden.py` 全部用例保持全绿；报表在 V1 项目下仍导出 V1 的
> 13 张工作表（24 表中新表输出占位说明），**不缺表、不报错**。
>
> **文档索引**：时序模型见 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md)，
> 储能与调度见 [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md)，
> 寻优见 [OPTIMIZATION.md](OPTIMIZATION.md)，
> 导入见 [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md)，
> 架构与模块布局见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 3.4 / 3.5 节，
> 测试清单见 [TEST_PLAN.md](TEST_PLAN.md) 第 8 节。

### Added（V2.0.0 新增）

#### V2 能力总览（13 项，均可运行）

| # | 能力 | 落地位置 | 验证 |
|---|---|---|---|
| A1 | **8760 小时时序仿真**（逐时能量流 + SOC 递推 + 逐年仿真） | `calculation/timeseries_engine.py`、`calculation/economic_v2.py` | `test_timeseries_engine.py`、`test_v2_integration.py` |
| A2 | **负荷曲线引擎**（小时形状／典型日×月度系数／年度简单三模式，按年电量归一化） | `calculation/load_profile.py` | `test_profile_engines.py` |
| A3 | **光伏出力曲线引擎**（等效小时／典型日／月度小时系数／逐时导入，含夜间零出力语义） | `calculation/pv_profile.py` | `test_profile_engines.py` |
| A4 | **分时电价曲线引擎**（尖峰/高峰/平/谷/深谷 + 上网电价，独立于购电电价的增长率） | `calculation/tariff_series.py` | `test_profile_engines.py` |
| A5 | **储能 SOC 模型**（充放电能力四上限取小、SOC 硬约束、往返效率口径） | `calculation/storage_soc.py` | `test_storage_soc.py` |
| A6 | **三策略调度**（峰谷套利 `PEAK_VALLEY`／光伏自用优先 `PV_SELF_CONSUMPTION`／经济优化 `ECONOMIC_OPTIMIZATION`） | `calculation/dispatch_engine.py` | `test_dispatch_engine.py`、`test_dispatch_semantics.py` |
| A7 | **能量守恒校验**（年度残差 `error` + 最差逐时残差 `max_hourly_error` + `tolerance`） | `calculation/energy_balance.py` | `test_v2_integration.py`、`test_v2_performance.py` |
| A8 | **需量电费与削峰**（按月最大需量计费、储能削峰量与实际需量削减） | `calculation/dispatch_engine.py`、`calculation/economic_v2.py` | `test_dispatch_engine.py`、`test_report_v2.py` |
| A9 | **时序经济评价**（自用率／自给率／电费节省／储能收益分解／逐时套利与 LCOS） | `calculation/economic_v2.py` | `test_v2_integration.py`、`test_result_wiring.py` |
| A10 | **方案比较与三级寻优**（规则型 → 贪心经济 → 线性规划，含优雅降级与留痕；参数扫描八维度） | `calculation/scenario_engine.py`、`optimization/rule_based.py`、`optimization/greedy_optimizer.py`、`optimization/lp_optimizer.py` | `test_scenario_engine.py`、`test_optimization.py` |
| A11 | **PyQtGraph 交互图表**（滚轮缩放／拖拽平移／右键复位／十字光标读数／时间筛选／导出 PNG） | `ui/charts.py` | `test_gui_v2.py` |
| A12 | **Excel 24 表 / PDF 16 部分**（V1 的 13 表与 15 章全部保留并重组） | `reports/excel_exporter.py`、`reports/pdf_exporter.py` | `test_report_v2.py`、`test_excel_export.py`、`test_pdf_export.py` |
| A13 | **数据导入校验与质量评分**（三类模板 + 18 条校验 + 14 条告警 + 0~100 四维度评分） | `data/importer.py`、`data/validator.py`、`data/quality.py` | `test_data_layer.py` |
| A14 | **V1 → V2 迁移**（`schema_version` 1.0/1.1 自动迁移为 2.0，只补 `timeseries` 默认值并留痕） | `infrastructure/migration.py` | `test_migration.py` |

#### 报表升级到 V2 §66 / §67（已完成）

- Excel 由 13 张扩展为 **24 张**：新增「储能与调度」「负荷曲线」「光伏曲线」「分时电价」
  「8760时序仿真」「能量平衡」「年度汇总」「收益分解」「方案比较」「方案寻优」「数据质量」；
  V1 的 13 张**全部保留且相对顺序不变**；「8760时序仿真」表按**抽样**输出
  （每月 1 日 + 夏冬两个典型日，共 336 行），并注明全量数据的获取方式；
  未启用时序时新表输出占位说明，**不缺表、不报错**（V1 项目仍可导出）。
- PDF 由 15 章重组为 **16 部分**：项目概况 / 输入参数 / 负荷分析 / PV时序分析 /
  储能SOC分析 / 能源流 / 电费分析 / 储能收益 / 投资 / 现金流 / 经济指标 /
  方案比较 / 敏感性 / 风险 / 参数来源 / 免责声明；V1 内容全部保留，仅重新归并。
- PDF 新增 **3 张图表**（reportlab.graphics 直接绘制，不引入新依赖）：
  典型日负荷与光伏出力曲线、逐月上网电量柱状图、典型日储能 SOC 曲线。
- 新增 **结果一致性测试**（§105）：断言 Excel「财务指标」「收益分解」的数字与
  `CalculationResult` 同名字段逐位一致，且 Excel 与 PDF **同时**包含 V2 新增的
  自用率、自给率、等效循环、需量削减四项指标。
- 报表口径披露：收益分解表与 PDF 均写明「电费节省按现金口径，其分解项之和与之严格相等；
  光伏给储能充电的电量按 0 计价以避免重复计算」。

#### 时序模型与结果（[TIMESERIES_MODEL.md](TIMESERIES_MODEL.md)）

- **✅ 已落地** `domain/timeseries.py`：输入时序模型（逐字段对照源码核实）
  - `TimeSeriesPoint`（6 字段：`timestamp`、`is_holiday`、`load_kwh`、`pv_generation_kwh`、
    `electricity_price`、`export_price`；另有 6 个只读 `property`：
    `year` / `month` / `day` / `hour` / `weekday` / `is_weekend`，不落库）
  - `TimeSeriesProfile`（7）、`LoadProfile`（8）、`PVProfile`（9）
  - `TariffProfile`（14，含新增 `sharp_peak_price`、`deep_valley_price`、
    `custom_price`、`demand_charge`、`basic_charge`）与 `TimePeriodRule`（4）
  - `StorageDispatchConfig`（15，含新增 `initial_soc`、
    `charge_price_threshold`、`discharge_price_threshold`）
  - 配置类：`TimeSeriesConfig`（9）、`LoadProfileConfig`（8）、
    `PVProfileConfig`（10）、`TariffSeriesConfig`（4）
- **✅ 已落地** `domain/timeseries_results.py`：结果模型
  - `TimeSeriesResultSet`（27，**列式容器**：`timestamps` + 23 列 + 调度动作/原因）
  - `HourlyResult`（**25** 字段，逐字段给出计算式）
  - `DispatchDecision`（6 字段：含**人类可读中文原因** `reason` + 机器可判别 `reason_code`）
  - `TimeSeriesReport`（10）、`TimeSeriesMetrics`（30）、`EnergyBalance`（20）、
    `BaselineResult`（5）、`DataQualityScore`（6）、`DataQualityIssue`（5）
  - `ScenarioResult`（20）、`OptimizationCandidate`（7）、`OptimizationResult`（9）
- **✅ 已落地** 10 个 V2 枚举：`Resolution`（4）、`DispatchStrategy`（3）、
  `DispatchAction`（3）、`TariffPeriod`（6）、`DayType`（3）、`MissingDataPolicy`（4）、
  `LoadProfileMode`（3）、`PVProfileMode`（4）、`OptimizationObjective`（6）、
  `ScanVariable`（8）；`SensitivityVariable` 由 5 扩为 **8**
- **✅ 已落地** `CalculationResult` **新增 8 个字段**（V1 字段语义不变，当前共 47 字段）：
  `time_series_results`（`TimeSeriesReport | None`）、`baseline_results`（`BaselineResult | None`）、
  `annual_results`（`list[AnnualResult]`）、`dispatch_results`（`list[DispatchDecision]`）、
  `energy_balance`（`EnergyBalance | None`）、`data_quality`（`DataQualityScore | None`）、
  `scenario_results`（`list[ScenarioResult]`）、`optimization_results`（`OptimizationResult | None`）
- **✅ 已落地** 时间模型（`calculation/timeseries_engine.py`）：内部 **1 小时**一个周期；
  `timestamp` 为**唯一主时间索引**，禁止用 1~8760 序号当时间依据；
  平年 8760 点 / 闰年 8784 点；`Resolution` 同时提供
  `QUARTER_HOURLY`（15 分钟，35040 / 35044 点）、`DAILY`、`MONTHLY`，
  `delta_hours` 随分辨率取值（1.0 / 0.25 …）
- **✅ 已落地** 引擎内部使用 **NumPy 列式数组**（`TimeAxis` 一次算好年月日时分/星期/日类型）；
  `HourlyResult` 为**按需生成的行视图 + JSON 序列化载体**
- **✅ 已落地** 逐时**能量守恒**（`calculation/energy_balance.py`）：`EnergyBalance` 以
  `error`（年度残差）+ `max_hourly_error`（最差逐时残差）+ `tolerance = 1e-6`
  + `is_balanced` 判定（§73 的随机 100 组守恒测试见 TEST_PLAN 第 8 节）
- **✅ 已落地** 时序结果**全部可直接 `model_dump(mode="json")`**（8 条序列化要求 J1–J8），
  由 `test_timeseries_models.py` 逐条断言
- **✅ 已落地** `Project` 新增 **`timeseries` 段**（`TimeSeriesConfig`，9 字段：
  `enabled` / `resolution` / `base_year` / `load` / `pv` / `tariff` / `dispatch` /
  `holidays` / `balance_tolerance`）；**默认 `enabled = False`**，不影响 V1 项目
- **✅ 已落地** `.nep` **`schema_version` 2.0** 与 **V1 → V2 自动迁移**
  （`infrastructure/migration.py`）：支持 `1.0` / `1.1` → `2.0`；
  只补 `timeseries` 段显式默认值、更新版本号、追加 `migration_notes`，**不改动任何既有参数**；
  未知版本抛 `MigrationError`（中文报错，含可迁移版本列表）
- **✅ 已落地** 版本元数据 `calculation_version` / `policy_version` / `tariff_version`
  由 `.nep` **信封层**承载（**不放进项目载荷**，因 `Project` 为 `extra="forbid"`）：
  `save_project()` 同时写入 `calculation_version = "2.0.0"`、`policy_version`
  （取自 `policy.profile_id`）与 `tariff_version`（取自 `tariff.tariff_version`）

#### 储能 SOC 与调度（[STORAGE_DISPATCH.md](STORAGE_DISPATCH.md)）

- 新增 `calculation/storage_soc.py`：SOC 递推与约束
  - 充电 `E_inc = P_charge × η_charge × Δt`；放电 `E_dec = P_discharge / η_discharge × Δt`
  - `soc_min` / `soc_max` **硬约束**（越界即判定计算失败，不得裁剪后继续）
  - 充放电能力**四个上限取小**（功率 / SOC / 倍率 / 电量来源）
- 新增 `calculation/dispatch_engine.py`：三种调度策略
  - **峰谷套利** `PEAK_VALLEY`：价格阈值 + 六步判定
  - **光伏自用优先** `PV_SELF_CONSUMPTION`：`PV → 负荷 → 储能 → 上网` 优先级链，
    **负荷不足时储能不主动放电**
  - **经济优化** `ECONOMIC_OPTIMIZATION`：**规则型**逐时净收益判据
- `DispatchDecision.reason` 提供 **21 条原因枚举**（R01–R21）中文原因句，
  满足"每小时都能解释为什么充电/放电/不动作"
- 电网充电**双开关**（`charge_from_grid` 与 `allow_grid_charge` 为**与**关系）、
  储能上网开关 `allow_export`
- 等效循环次数**同时输出配置值与实际值**，偏离率 > 30% 强制提示
- 储能套利按**逐时** `Σ(放电×替代电价 − 充电×充电电价)` 计算；
  **明确禁止**简化为"放电量 × 峰谷价差"
- 需量电费按月最大需量估算（简化口径，须在报告披露）

#### 优化与方案／参数扫描（[OPTIMIZATION.md](OPTIMIZATION.md)）

- 新增 `optimization/rule_based.py`（默认）、`greedy_optimizer.py`（网格搜索）、
  `lp_optimizer.py`（线性规划，**`scipy` 可选依赖**）
- 三者**分工与回退链**：规则型 → 贪心 → 线性规划；不可用时**降级但必须留痕**
- 目标函数 `min(购电成本 − 上网收入 + 储能运行成本)` 与约束 C1–C7
- **✅ 已落地** 优化目标枚举 `OptimizationObjective`（**6 种，默认 `MAX_NPV`**）：
  `MAX_NPV` / `MAX_IRR` / `MIN_PAYBACK` / `MIN_LCOE` / `MIN_LCOS` / `MIN_ANNUAL_COST`；
  并列时按确定性打破规则取唯一解
- **方案扫描四维度**（候选值由规范给定）：
  光伏 **500/750/1000/1250/1500/2000 kWp**、
  储能容量 **0/500/1000/1500/2000 kWh**、
  储能功率 **250/500/750/1000 kW**、电价与峰谷价差
- **✅ 已落地** 参数扫描枚举 `ScanVariable`（**8 个维度**）：`PV_CAPACITY`、
  `STORAGE_CAPACITY`、`STORAGE_POWER`、`STORAGE_PRICE`、`TARIFF`、
  `PEAK_VALLEY_SPREAD`、`LOAD`、`CAPEX`（仍遵守 V1 §95「一次只改一个参数」）
- **✅ 已落地** 敏感性变量 `SensitivityVariable` 由 V1 的 5 个扩为 **8 个**
  （新增 `STORAGE_CYCLES`、`STORAGE_CAPEX`、`INTEREST_RATE`）
- **禁止黑盒**：优化结果按五段式输出「输入参数 → 候选方案 → 约束条件 → 计算结果 → 最优方案」，
  `OptimizationResult.explanation` 必须含至少一个数值比较；不可行候选须保留 `note` 原因

#### 数据导入与质量（[DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md)）

- 新增 `data/importer.py`、`data/validator.py`、`data/quality.py`
- **三种导入模板**：负荷 `timestamp,load_kwh`；光伏 `timestamp,pv_kwh`；
  电价 `timestamp,price,export_price`；支持 `.xlsx` 与 `.csv`
- 列名等价表、时间格式白名单（5 种）/ 黑名单（4 种）、单位约定（**不做单位换算，不符即报错**）
- **18 条校验规则**（V01–V18）：时间重复/缺失/间隔错误/未排序/跨年/闰年点数、
  负值/单位异常/全零/空值/异常值/电价异常，含**固定校验顺序**与确定性要求
- **默认禁止静默填充**；必须报告「发现 XXX 个时间点缺失」；
  四种可选策略（枚举 `MissingDataPolicy`）：`LINEAR_INTERPOLATION` / `FORWARD_FILL` /
  **`TYPICAL_DAY_FILL`** / `REJECT`，
  且填充后必须留痕（典型日填充须把 `source_type` 降级为 `ASSUMPTION`）
- **14 条异常值告警**（R-LOAD-*、R-PV-*、R-PRICE-*、R-SOC-*、R-BALANCE、R-DEMAND），
  含负负荷、**光伏夜间发电**、**SOC > 100%**、**SOC < 0%**、能量不守恒
- `DataQualityScore` **0~100 四维度**（字段名 `completeness` 40 / `continuity` 25 /
  **`outlier`** 20 / `source_credibility` 15）+ `issues`（`DataQualityIssue` 5 字段）；
  四级评定（优秀 / 良好 / 一般 / 较差，由 `score` 派生展示）；**只评分不阻断**
- 参数来源**六元组**：`source_type` / `source_name` / `source_date` / `source_url` /
  **`version`** / `note`；`version` 为 V2 新增字段（V1 `ParameterMeta` 无此字段）
- 统一**错误报告契约** E1–E6（中文、位置、原因、后果、建议、`field`）

#### 文档

- 新增 [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md)、[STORAGE_DISPATCH.md](STORAGE_DISPATCH.md)、
  [OPTIMIZATION.md](OPTIMIZATION.md)、[DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md)、
  本 CHANGELOG（V2 §102、§103）

#### 界面升级（V2 §5、§49–§50、§68–§71、§88）

- **参数页新增 5 个分组 / 39 个字段**（全部走声明式 `FieldSpec`，不手写控件）：
  「时序仿真总开关」（启用开关 / 计算粒度 / 基准年 / 能量平衡容差）、
  「负荷曲线」（来源模式 / 年用电量 / 年增长率 / 缺失处理）、
  「光伏出力曲线」（来源模式 / 等效小时 / 性能比 / 容量覆盖项 / 缺失处理）、
  「分时电价」（尖峰·高峰·平·谷·深谷·上网电价 / 电价年增长率 /
  需量与基本电费开关及单价）、
  「储能调度策略」（策略 / SOC 上下限与起始 / 充放电效率 / 最大充放电功率 /
  电网充电与储能上网开关 / 充放电价格阈值）；
  `CHOICE` 选项直接由枚举成员生成（`(member.value, member.label)`），**杜绝手写字符串**
- **主窗口新增「时序仿真」标签页**（位于「结果」之后），内含 5 个子页：
  - **结果概要**（§68）：项目投资额、PV 容量、储能容量、首年发电量、首年自用率、
    首年自给率、首年电费节省、首年储能收益、项目/股东 IRR、NPV、
    静态/动态回收期、LCOE、LCOS；并列出 §41 的 22 项时序指标
  - **时序数据**（§69）：全年 / 按月 / 按日切换，逐时明细含 13 列
    （负荷、PV、PV 三路去向、购电、上网、充放电、SOC、电价、**动作 / 原因**）
  - **能源平衡**（§70）：九条能量流 + 平衡式 + 平衡误差；
    **超出容差时红色告警**并提示"计算应判定失败"
  - **储能**（§71）：容量 / 功率 / 时长 / SOC 区间 / 充放电效率与往返效率 /
    最大功率 / 年充放电量 / 电网充电量 / **配置循环 vs 实际等效循环** /
    套利与容量收益 / LCOS
  - **图表**（§49–§50）：6 张 PyQtGraph 交互图
- **交互式图表**（新增 `ui/charts.py`）：
  - 全年负荷、PV 出力（Load + PV 双线）、储能 SOC、典型日四线（Load/PV/Grid/Storage）、
    电价曲线、方案比较柱状图（无方案时提示"未执行方案比较"）
  - 支持**滚轮缩放 / 拖拽平移 / 右键复位**、**十字光标悬停读数**、
    **时间筛选（全年 / 按月 / 按日）**、**导出 PNG**
  - 数据只从 `TimeSeriesResultSet.column()` 取列，**界面不做任何计算**（§61）
- **未启用时序仿真时**：时序页给出明确提示而不是报错，图表清空并显示原因（最常见用户路径）

### Changed（V2.0.0 变更）

> **口径红线**：下列变更**只影响启用时序仿真（`timeseries.enabled = true`）的项目**；
> V1 项目继续走同一条年度模型代码路径，数值结果不因 V2 而改变（兼容性承诺 C2、C4）。

- **储能单向效率由 `0.938` 改为 `√0.88`（往返效率严格等于 V1 的 0.88）**（缺陷 ①）：
  充电与放电效率各取 `√0.88 = 0.938083152…`，使
  `η_charge × η_discharge = 0.88` 与 V1 完全一致。旧的 `0.938` 往返为
  `0.938² = 0.879844`，带来 0.018% 的口径漂移，会同时污染储能套利收益与 LCOS。
  落地位置：`domain/timeseries.py`（`V1_ROUND_TRIP_EFFICIENCY = 0.88` +
  单向效率默认值），回归用例见 `tests/test_storage_soc.py`、`tests/test_dispatch_engine.py`。
- **`ProjectType` 按容量自动归一化**（缺陷 ⑬）：启用时序仿真后，项目类型**不再信任用户
  手填值**，而是按实际容量重算 —— 口径为
  `timeseries.pv.capacity_kwp → pv.pv_capacity_kwp → 0` 与 `storage.storage_energy_kwh`：

  | 光伏容量 | 储能容量 | 归一化后的类型 |
  |---|---|---|
  | > 0 | > 0 | `PV_STORAGE` |
  | > 0 | = 0 | `COMMERCIAL_PV` |
  | = 0 | > 0 | `COMMERCIAL_STORAGE` |
  | = 0 | = 0 | 保留原类型（§42 基准方案） |

  归一化会在 `notes` 中留中文痕；归一化后若**年度模型与实际容量口径仍然矛盾**
  （例如时序在用储能、项目类型却不含储能），**直接抛 `CalculationError` 拒绝计算**，
  而不是静默出结果 —— "计入储能收益却不计储能造价"正是项目 IRR 被高估的成因。
  落地位置：`application/calculation_service.py`、
  `calculation/scenario_engine.py::normalize_project_type / effective_pv_capacity`，
  回归用例见 `tests/test_project_type_alignment.py`（含 20.03% → 15.00% 的端到端断言）。

其它 V2 口径变更（均只影响时序路径）：

- **`.nep` 版本策略放宽**：`schema_version = "1.0"` / `"1.1"` 由 V1 的「版本不匹配即拒载」
  改为「**自动迁移**为 2.0」；其它未知版本仍拒载（唯一放宽项）
- **电网充电计入收益**：V1 的 `grid_charge` 只用于展示与校验、**不进入收益公式**
  （核心 L14）；V2 的 `grid_to_storage` **计入** `electricity_cost` 与 `storage_revenue`。
  **这是 V2 唯一会改变储能收益数值的口径变化**，迁移时必须提示并在 `notes` 披露
- **储能收益由年度细化为逐时**：套利口径不变（`放电×替代价 − 充电×充电价`），
  但由年度聚合改为逐时求和；容量/辅助/其他三类仍为固定值（沿用 V1）
- **储能 SOC 区间取代 DoD 配置**：V2 用 `soc_min` / `soc_max` 表达可用区间，
  `DoD_actual = soc_max − soc_min`（V1 的 `depth_of_discharge` 继续服务于年度模型）
- **敏感性维度由 5 个扩展为 8 个**（V1 的 5 个变量必须继续可用，V2 为其超集）
- **应折旧/税务/投资等 V1 参数语义保持不变**，V2 只**新增**字段

### ⚠️ 待评审差异（设计 vs 已落地实现）

以下差异在文档定稿时由**逐字段对照源码**发现；V2.0.0 交付前的处置结果如下：

| # | 项 | 设计/规范口径 | 当前实现 | 影响 | 登记位置 |
|---|---|---|---|---|---|
| N1 | ~~储能充放电效率默认值~~ | `√0.88 = 0.938083`（往返恰为 0.88） | **✅ 已按设计修正**：单向效率取 `√0.88`，往返严格 `0.88`（不再使用 `0.938`） | — | [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) §2.2、[TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §4.5 |
| N2 | 质量分异常值维度字段名 | 文档曾用 `anomaly` | **`outlier`** | 仅命名 | [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) §8.1 |
| N3 | `baseline` / `balance` / `data_quality` 存放位置 | 规范 §25 要求位于 `CalculationResult` 顶层 | 顶层**与** `TimeSeriesReport` 内**同时存在** | 冗余，必须**同源写入**，否则两处不一致 | [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §7.0 |
| N4 | ~~`.nep` 迁移器~~ | V1 现状为"版本不匹配即拒载"（核心 L26） | **✅ 已落地**：`infrastructure/migration.py`，支持 `1.0`/`1.1` → `2.0`，未知版本抛 `MigrationError` | — | [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) §9 |

> **处置结论**：N1 与 N4 已在**实现侧**解决（往返效率严格 0.88、迁移器落地）；
> N2、N3 为文档与实现的**表述/结构**差异，已按实现更新文档（N3 由
> `test_result_wiring.py` 断言"两处同源写入"）。

### Fixed（V2.0.0 修复的 13 个缺陷）

> **可追溯性核心**：以下 13 条是 V2 开发与收尾阶段**实际定位并修复**的缺陷，
> 每条均已补回归测试。**不得省略、不得合并**。

| # | 缺陷 | 根因 | 影响 | 修复 | 回归用例 |
|---|---|---|---|---|---|
| ① | **单向效率 `0.938` 使往返效率偏离 V1 的 `0.88`** | 默认值取三位小数 `0.938`，而非 `√0.88` | `0.938² = 0.879844`，往返效率偏离 V1 口径 0.018%，储能套利收益与 LCOS 出现系统性漂移 | 单向效率改为 `√0.88 = 0.938083152…`，往返严格 `0.88`；常量 `V1_ROUND_TRIP_EFFICIENCY = 0.88` 落地于 `domain/timeseries.py` 并加注释说明**为什么不能取 0.938** | `test_storage_soc.py`、`test_dispatch_engine.py` |
| ② | **`computed_field` 派生时间字段"存得进 `.nep` 读不回"** | `model_dump()` 会输出 `@computed_field`（年/月/日/时/星期/是否周末），但读回时 `extra="forbid"` 拒绝这些字段 | `.nep` 保存成功、加载直接报"多余字段"——项目文件**不可往返** | 为派生字段设计**读回时忽略**的入口（把 computed 字段在反序列化前剥离），并明确 `model_dump()` 仍输出它们，满足 J1–J8 序列化要求 | `test_timeseries_models.py`（序列化往返）、`test_project_file.py` |
| ③ | **`TYPICAL_DAY` 负荷模式未按 `annual_energy_kwh` 归一化** | 典型日曲线只定义了 24 点**形状**，漏了"按年电量定水平"的整年缩放 | 该模式下的年用电量与用户输入的年电量不符，误差可达数倍，进而扭曲自用率、电费与全部经济指标 | 典型日曲线**只定形状**，`annual_energy_kwh` **定水平**：整体缩放使 `Σ Load ≡ annual_energy_kwh`；`≤ 0` 时抛带 `field` 的中文错误 | `test_profile_engines.py` |
| ④ | **调度原因与实际动作不符（报"充电"但电量为 0）** | 先按价格阈值写死 `reason`，再算实际充放电量；量为 0 时未回退原因 | 逐时明细自相矛盾，用户在"时序数据"页看到"充电 0 kWh"，破坏"每小时都能解释为什么"的承诺 | 原因改为**由实际动作量反推**：动作量为 0 时必须给"不动作"类原因（含具体数值理由）；`DispatchDecision.reason / reason_code` 与实际充放电量强绑定 | `test_dispatch_semantics.py` |
| ⑤ | **电价列映射错位 / `export_price` 从未解析 / 空值静默变 0** | 表头等价表命中顺序有误；电价模板的可选列 `export_price` 未登记；空单元格被直接 `float("")→0` | 电价曲线整体错位（把上网电价当购电电价），或上网电价全为 0；缺失值静默变成 `0.0` 参与计算，违反"默认零静默行为" | 修正列名等价表与匹配顺序；新增 `_find_optional_column` 支持 `export_price`（并按模板"沿用上一非空值"）；空值一律走缺失数据策略并留痕，**禁止静默填 0** | `test_data_layer.py` |
| ⑥ | **光伏夜间出力为 0 被全体中位数误判为"单位疑似 MWh"** | 用**全体数据**的中位数做单位量级判定，光伏曲线有一半时间是夜间 `0`，中位数被拉到接近 0 | 正常的光伏曲线被误报"单位疑似 MWh"，用户被迫忽略告警，告警体系失效 | 单位量级判定改为**只用非零样本**（并按数据类型区分阈值）；夜间 `0` 被视为**正常语义**而非异常 | `test_data_layer.py` |
| ⑦ | **`peak_load` / `peak_power` 把 Δt 钳到 `max(Δt, 1)`，致 15 分钟粒度功率低估 4 倍** | 功率 = 单周期电量 ÷ Δt，但实现把 Δt 下限钳成 1 小时 | 15 分钟分辨率下功率被低估 4 倍，需量电费、削峰量与储能功率约束全部偏小 | 新增 `_delta_hours_of()`：从 `TimeAxis` 或 `Resolution` 取**真实 Δt**（仅当 `≤ 0` 时才退回 1 h），**不做 `max(Δt, 1)` 钳位** | `test_profile_engines.py`、`test_v2_integration.py` |
| ⑧ | **`YearSimulation` 预物化 25×8760 个对象（约 110 MB）** | 逐年仿真把每个小时的输入输出都建成 Python 对象再聚合 | 25 年仿真内存峰值约 110 MB，且构造耗时随年数线性增长 | `YearSimulation` 改为 **NumPy 列式数组**容器；`HourlyResult` 只在**需要逐时明细时按需生成行视图**，序列化时才落 JSON | `test_v2_performance.py`、`test_timeseries_engine.py` |
| ⑨ | **`PVConfig.pv_capacity_kwp` 不允许 0，与 §78 退化要求冲突** | 字段约束为 `gt=0` | 无法表达"本项目不装光伏"的纯负荷/纯储能退化场景，用户被迫填一个假的非零容量，全部指标失真 | 约束改为 `ge=0.0` 且允许 `None`，并在 `description` 写明"允许 0 —— 表示本项目不装光伏（§78 要求 PV 容量为 0 时退化为纯电网负荷项目）" | `test_project_type_alignment.py`、`test_data_layer.py` |
| ⑩ | **存盘幂等断言比较全文含 `saved_at`，跨秒偶发失败** | 断言"两次保存的文件内容完全相同"，但信封里的 `saved_at` 精确到秒 | 跨秒执行时偶发失败（假失败），污染 CI 信号 | 幂等断言改为比较**去掉 `saved_at` 的有效载荷**（比较 `format` / 四个版本号 / `project` 载荷），时间戳不参与等价判定 | `test_project_file.py` |
| ⑪ | **性能用例单次墙钟计时，并行负载下假失败** | 只跑一次并断言墙钟耗时 | 并行负载或冷缓存时偶发超时（假失败），无法区分真实性能退化 | 性能用例改为**多次取样取最小值**（如 5 次）或放宽为**预算 + 明确标注**，并显式说明"并行负载下的计时不作为判定依据" | `test_v2_performance.py`、`test_application.py` |
| ⑫ | **峰谷套利逐时近视贪心致套利收益低估约 60%** | `PEAK_VALLEY` 逐时判定"电价 ≤ 充电阈值就充电"，在傍晚平价时段（19–20 点）就把电池充满，挤掉了次日凌晨更便宜的谷段 | 全年套利收益**被低估约 60%**；经济优化策略同理，谷段被挤掉 | 改为**逐日价格排序窗口调度**：先按当日电价排序定位真正的最低/最高价时段再配对充放。**只改变"哪一小时充放"，不改变"充放多少"**，窗口内价格与原贪心相同，因此其它指标口径不变 | `test_dispatch_engine.py`（含修复前/后的 SOC 轨迹对比）、`test_dispatch_semantics.py` |
| ⑬ | **项目类型口径不一致致项目 IRR 高估 5 个百分点（20.03% → 15.00%）** | 时序侧按真实容量计算（含储能收益），年度模型侧却按**用户手填的 `project_type`** 取造价与设备（不含储能） | "计入储能收益却不计储能造价"，项目 IRR 被高估到 **20.03%**，而正确口径为 **15.00%**（差 5 个百分点），足以让不可行项目看起来可行 | 启用时序后**按容量自动归一化项目类型**（见上节 Changed），并在归一化后仍矛盾时**抛错拒绝计算**；`ui` 与报表一律展示归一化后的类型口径 | `test_project_type_alignment.py`（断言 15.00% 而非 20.03%）、`test_gui_v2.py`、`test_result_wiring.py` |

### V1 登记在案的 `TODO(V2)` 限制

以下 V1 限制在 V2 中**部分解决**：8760 小时仿真、分时段负荷曲线（L15/L16）、
储能逐时收益（L6 的一部分）已落地；其余项（税务进项抵扣 L1、亏损跨年弥补 L2、
政策数值自动折算电价 L7、分期投资与建设期利息 L18 等）**明确不在 V2.0.0 范围内**，
完整清单见 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) 第 6 节。

### 兼容性承诺（V2.0.0 硬性红线）

| # | 承诺 | 验证方式 |
|---|---|---|
| C1 | **V1 项目文件可直接打开** | `.nep` `schema_version = "1.0"` / `"1.1"` **自动迁移**为 2.0，迁移写入 `migration_notes` 留痕（✅ 已落地：`infrastructure/migration.py`） |
| C2 | **V1 计算结果可复现** | 迁移写入 `timeseries.enabled = False`，项目继续走 **V1 年度模型代码路径** → 结果一致为**结构性保证**（同一条代码路径），非事后比对 |
| C3 | **V1 Golden Case 必须继续通过** | `tests/test_engine_golden.py` 全部用例必须保持全绿 |
| C4 | **V1 公式不被改写** | V2 只**新增**模块；`calculation/` 既有模块的公式与口径不得变更 |
| C5 | **唯一结果对象不变** | 仍是 `CalculationResult`；GUI / Excel / PDF 仍只消费结果对象、不得计算（V1 §148–§150） |
| C6 | **一次只改一个参数** | 参数扫描继续遵守 V1 §95 |
| C7 | **不输出可行性结论** | 优化器与报表均不得输出"项目可行/不可行"（V1 §106） |
| C8 | **默认零静默行为** | 缺失不静默填充、降级不静默发生、政策不硬编码（V1 §5、§159） |

### 未纳入本版

以下 V1 已登记的 `TODO(V2)` 项，经评估**不在 V2.0.0 范围内**，留待后续版本：

| 项 | 说明 | 参考 |
|---|---|---|
| 加速折旧 | 只支持直线法 | 核心 L5 |
| 税收优惠期 | 三免三减半等 | 核心 L3 |
| 亏损结转 5 年 | — | 核心 L2 |
| 明细项参与投资合计 | `detailed_items` 目前只被记录 | 核心 L17 |
| 多用户 / 多计量点 | — | ROADMAP 第 3 节 |
| 多项目对比 / 项目数据库扩展 | — | ROADMAP 第 3 节 |

---

## [1.0.0] — 2026-10-06

> **首个正式版本**。定位：面向工商业新能源项目（分布式光伏 / 储能 / 光储）
> 开发阶段的**快速经济评价计算器 + 投资分析工具**。
> **不是**设计院正式可研软件，不替代可行性研究、工程设计、造价咨询、审计、税务咨询与政府审批。

### Added

#### 计算能力

- 支持**三类项目**：工商业分布式光伏 `COMMERCIAL_PV`、工商业储能 `COMMERCIAL_STORAGE`、
  工商业光储 `PV_STORAGE`
- **25 年全周期现金流**：`Year 0 = 建设/初始投资期`，`Year 1..N = 运营期`，
  残值只在末年，现金流数组固定长度 `N+1`
- **统一计算引擎**（`calculation/engine.py`）：按规范 §82 的 **31 步**固定顺序执行，
  模块级单例 `calculation_engine` + 快捷函数 `calculate`
- **财务指标（两套口径）**
  - 项目 IRR / 资本金 IRR（确定性二分法，固定区间 `[-0.9999, 10.0]`）
  - 项目 NPV / 资本金 NPV（从 `t=0` 起算）
  - 静态回收期 / 动态回收期（插值法）
  - **LCOE**（光伏口径）/ **LCOS**（储能口径）
  - **最低 DSCR** 与逐年 DSCR（`CFADS / DebtService`）
  - ROI（生命周期累计净收益 ÷ 初始总投资）
- **敏感性分析**：5 个变量（总投资 / 电价 / 发电量 / 运维成本 / 自用比例），
  一次只改一个参数，输出敏感度系数与排序
- **情景分析**：保守 / 基准 / 乐观三情景，**深拷贝 + 显式乘数 + 重算**
- **能量守恒校验**：误差 ≤ `1e-6`；**同一输入重复计算 100 次结果一致**
- **不可计算指标返回 `None`**，展示层显示"无法计算 / 未回收"，**不用 0 冒充**

#### 数据模型与可追溯

- 完整 Pydantic v2 数据模型（`domain/`）：`Project`、`BasicInfo`、`LoadConfig`、`PVConfig`、
  `StorageConfig`、`TariffConfig`、`InvestmentConfig`、`OpexConfig`、`TaxConfig`、
  `FinancingConfig`、`PolicyProfile`、`ScenarioConfig`、`SensitivityConfig`
- **唯一结果对象** `CalculationResult`（含 `AnnualResult`、`ScenarioSummary`、`SensitivityRow`）
- **参数来源与可追溯性**（`domain/provenance.py`）：`ParameterMeta` + `ParameterRegistry`，
  **8 种 `source_type`**（用户输入 / 政策 / 合同 / 历史 / 经验 / 假设 / 系统默认 / 计算得出）
  与优先级、界面配色（蓝 / 绿 / 黄 / 灰）
- **政策 Profile 版本机制**（`policy/`）：可版本化、带出处，
  模板**刻意不预填任何数值**

#### 界面与交付物

- **PySide6 图形界面**（`ui/`）：声明式字段绑定（`field_spec.py` + `sections.py`），
  只绑定字段与展示结果，**不做任何计算**
- **Excel 导出 13 张工作表**：项目概况 / 基础参数 / 技术参数 / 电价参数 / 投资参数 /
  运维参数 / 融资参数 / 年度现金流 / 财务指标 / 敏感性分析 / 情景分析 / 政策依据 / 参数来源
- **PDF 报告 15 章 + 免责声明**：封面 / 项目概况 / 测算条件 / 技术参数 / 电价参数 /
  投资估算 / 运营成本 / 收益测算 / 现金流 / 经济指标 / 敏感性分析 / 情景分析 /
  风险提示 / 政策依据 / 测算说明
- **工作簿内无任何公式**：报表只写入数值，不重算（GUI / Excel / PDF 只消费 `CalculationResult`）
- **`.nep` 项目文件**：原子写入、自动保存（`.autosave.nep`）、崩溃恢复、版本校验
- **SQLite**（政策版本 / 政策模板 / 项目模板 / 参数字典 / 历史索引）
- **日志**：轮转文件 + 控制台
- **`python -m cenep` 入口**与 **`--selftest` 自检**（三个黄金案例 + Excel + PDF 全链路）
- **PyInstaller 打包配置**（`build/CENEP.spec`、`build/entry.py`），产物为免安装 EXE

#### 校验与测试

- 分层校验：Pydantic 负责类型与取值区间，`validator.py` 负责**中文友好报错**并携带 `field`
- **测试体系：16 个测试文件、280 个测试用例，全部通过（280 passed / 0 failed）**
- 黄金案例（`tests/conftest.py`）：工商业光伏 1000 kWp、工商业储能 500 kW/1000 kWh、
  工商业光储；关键期望值均为**手工独立推算**，非从程序输出反抄
- **静态扫描测试**：断言 GUI / 报表源码中不出现 `npv(` / `irr(` / `lcoe(` 等计算调用
- **LCOE 口径测试**：含按**公开招标文件给定公式**的端到端复算（逐位一致）

#### 文档

- 12 份设计规范：`PROJECT_SPEC.md`、`CORE_PARAMETERS_AND_FORMULAS.md`（最高优先级）、
  `DATA_MODEL.md`、`ARCHITECTURE.md`、`CALCULATION_ENGINE.md`、`REGULATIONS_AND_POLICY.md`、
  `HUBEI_POLICY_MODEL.md`、`UI_SPEC.md`、`REPORT_SPEC.md`、`TEST_PLAN.md`、`ROADMAP.md`、
  `AI_EXECUTION_GUIDE.md`

### Fixed

本版开发过程中修复的缺陷（均已补回归测试）：

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | LCOE 成本序列只取 `pv_opex` + 屋顶租金，**漏计保险费、管理费、其他费用** | 同一份报表中"运维成本"与 LCOE 口径不一致，LCOE 被低估 | LCOE 成本改为覆盖除储能专项费用外的**全部**年运营成本 |
| 2 | LCOE 投资基数只取 `capex.pv_capex`，**漏计并网/开发/设计/施工等共享投资** | LCOE 偏低 | 改为 `总投资 − 储能投资`（纯光伏项目两者相等） |
| 3 | LCOE **不认**增值税进项抵扣与残值抵减 | 与行业/招标通行口径不一致，LCOE 偏高约 10% | 新增两个**默认关闭**的可选开关 `lcoe_vat_deductible_ratio`、`lcoe_residual_credit` |
| 4 | **无法表达光伏设备更换**（如逆变器第 12 年更换） | 25 年评价缺少一次性支出 | `PVConfig` 新增 `replacement_year`、`replacement_cost_per_kwp` |
| 5 | 导出文件名使用 `Path.with_suffix()`，把文件名中最后一个点之后的内容当后缀截掉（`全屋面2061.8kWp_经济评价` → `全屋面2061.xlsx`） | 行业常见命名（`2061.8kWp`、`V1.2`、`10.5MW`）**必然踩到** | 改为字符串拼接追加后缀，补 5 个回归用例 |
| 6 | `resolve_pv_capacity(None, 0.0, 6.0)` 在屋顶面积为 0 时仍返回依据 `"AREA"` | 容量取值依据错误 | 增加 `usable_roof_area_m2 > 0` 前置条件 |
| 7 | `available_energy_for_year` 未在 `replacement_year` 复位容量 | 更换电芯后容量不恢复 | 改为 `year >= replacement_year` |
| 8 | `StorageConfig` 与 `InvestmentConfig` **重复定义** `storage_capex_per_kwh` | 存在两个数据源，易不一致 | 删除 `StorageConfig` 中的同名字段，储能单位投资**唯一数据源**为 `InvestmentConfig` |

### 已知限制

V1 明确披露的简化口径与限制，**报告中必须原样披露**（完整清单见
[CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) 第 6 节，共 28 条）；
其中 **8760 小时仿真、日内时序与 SOC 曲线、分时段负荷曲线、储能逐时收益**已在
**V2.0.0 的时序路径**解决（见本文件 `[Unreleased] — V2.0.0` 一节），
其余项仍登记为 `TODO(V2)`。下表为 **V1.0.0 的年度模型口径**：

| 类别 | 限制（V1.0.0 年度模型；✅ 标注者 V2 已解决） |
|---|---|
| 税务 | 增值税进项抵扣与留抵退税**不建模**；**不建模亏损跨年弥补**；附加税费以不含税收入为基数；只支持直线法 |
| 储能 | **年度等效循环模型**；~~不做 8760 小时仿真~~ ✅ V2 已实现 8760 时序仿真；~~不建模日内时序与 SOC 曲线~~ ✅ V2 已实现 SOC 逐步递推；~~电网充电不进收益公式~~ ✅ V2 时序路径计入 |
| 负荷 | 只用年用电量与增长率；~~分月/分季波动不建模~~ ✅ V2 已实现小时级／典型日×月度系数负荷曲线 |
| 投资 | 投资在 Year 0 一次性发生；不建模分期投入与建设期利息 |
| 指标 | IRR 固定区间；ROI 仅提示不做可行性判定 |
| 范围 | 不做集中式电站、风电、水电；不做 GIS、CAD、组件排布、电气设计、短路/潮流计算 |

> **免责声明**（`reports/pdf_exporter.py` 逐字出现）：
> 本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，不替代项目正式可行性研究、
> 工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。

---

## 版本兼容性总表

| `.nep` `schema_version` | 由哪个版本写入 | V1.0.0 处理 | V2.0.0 处理（已交付） |
|---|---|---|---|
| `"1.0"` | CENEP V1.0.0 | ✅ 直接打开 | ✅ **自动迁移**为 `2.0`（只补 `timeseries` 显式默认值 + 更新版本号 + 追加 `migration_notes`，**不改动任何既有参数**） |
| `"1.1"` | CENEP V1.1（迁移链预留） | ❌ 拒载 | ✅ **自动迁移**为 `2.0`（与 `1.0` 同一条迁移函数） |
| `"2.0"` | CENEP V2.0.0 | ❌ 版本不兼容，拒载并提示升级 | ✅ 直接打开 |
| 其它 / 缺失 | — | ❌ 拒载 | ❌ 拒载（不猜测） |

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) | 核心参数与公式（最高优先级）、V1 简化口径清单 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 分层架构；**V2 模块布局与数据流见第 3.4 / 3.5 节** |
| [DATA_MODEL.md](DATA_MODEL.md) | 数据模型与 `.nep` 信封（`schema_version = "2.0"` + 迁移） |
| [TEST_PLAN.md](TEST_PLAN.md) | 测试计划；**V2 测试清单与用例数见第 8 节** |
| [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) | V2 时序数据模型与结果字典 |
| [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) | V2 储能 SOC 模型与调度策略 |
| [OPTIMIZATION.md](OPTIMIZATION.md) | V2 优化与方案／参数扫描 |
| [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) | V2 时序数据导入规范 |
| [REPORT_SPEC.md](REPORT_SPEC.md) | Excel 24 表 / PDF 16 部分报告规范 |
| [UI_SPEC.md](UI_SPEC.md) | 界面规范（含 V2 时序仿真页与 PyQtGraph 图表） |
| [V2_ACCEPTANCE.md](V2_ACCEPTANCE.md) | V2 验收清单 |
| [ROADMAP.md](ROADMAP.md) | 阶段计划与 V2/V3 候选功能 |
