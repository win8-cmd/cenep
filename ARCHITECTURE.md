# ARCHITECTURE.md —— 分层架构与依赖规则

> **优先级**：本文档优先级低于 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md)、
> [CALCULATION_ENGINE.md](CALCULATION_ENGINE.md)、[DATA_MODEL.md](DATA_MODEL.md)，
> 高于 `PROJECT_SPEC.md`、`UI_SPEC.md`、`REPORT_SPEC.md`、`README.md` 与代码注释（规范 §157）。
>
> **本文档描述的是磁盘上真实存在的代码**：目录、模块、类、函数名均可与源码逐一对上。
> 凡规划中但尚未落地的目录/模块，一律用「尚未实现」标注（§158）。

---

## 1. 架构目标与铁律

| # | 铁律 | 落地方式 |
|---|---|---|
| 1 | **只有一套公式** | 全部位于 `src/cenep/calculation/` |
| 2 | **只有一套数据模型** | 全部位于 `src/cenep/domain/` |
| 3 | **只有一个结果对象** | `CalculationResult`；GUI / Excel / PDF 都是它的展示方式（§81） |
| 4 | **展示层不得自行计算** | GUI / Excel / PDF 只调 `CalculationService` → `CalculationEngine`（§8、§109、§148–§150） |
| 5 | **政策不硬编码** | 政策参数来自 `PolicyProfile` / `PolicyTemplate`，带版本与出处（§34–§36） |
| 6 | **每个数字可追溯** | `ParameterRegistry` + `ParameterMeta`（§83–§85、§91） |
| 7 | **每个公式可测试** | `pytest`，黄金案例期望值手工独立推算（§117） |

规范 §8 定义的分层顺序（本文档按此组织）：

```text
Presentation（展示） → Application（应用服务） → Calculation（计算） → Domain（数据模型）
```

---

## 2. 分层架构图

```text
╔══════════════════════════════════════════════════════════════════════════════╗
║  ① Presentation 层 —— 只负责"把结果画出来"                                    ║
║                                                                              ║
║   src/cenep/reports/                      状态：已实现（V2：24 表 / 16 部分）  ║
║     ├── excel_exporter.py  ExcelExporter / excel_exporter / SHEET_NAMES      ║
║     │                      **24 张工作表**（V1 的 13 张全部保留且顺序不变，   ║
║     │                      V2 §66；未启用时序时新表输出占位说明）              ║
║     └── pdf_exporter.py    PdfExporter / pdf_exporter / REPORT_SECTIONS      ║
║                            DISCLAIMER；**16 部分报告**（V1 的 15 章重新归并，  ║
║                            V2 §67；含 3 张 reportlab.graphics 图表）          ║
║                                                                              ║
║   src/cenep/ui/                           状态：已实现                       ║
║     app.py          run / create_application / create_window                 ║
║     main_window.py  MainWindow：新建/打开/保存/另存为/计算/导出 Excel+PDF     ║
║     pages.py        ProjectPage / ParametersPage / CalculatePage /           ║
║                     ResultPage / SensitivityPage / ReportPage / SettingsPage ║
║                     + **V2「时序仿真」页**（概要/时序数据/能源平衡/储能/图表）║
║     sections.py     SectionSpec 定义 + TARIFF/INVESTMENT/OPEX/... 各分区     ║
║     field_spec.py   FieldSpec / FieldRow / SectionForm（字段↔控件双向绑定、   ║
║                     按 SourceType 着色）                                      ║
║     charts.py       ★ V2 新增：PyQtGraph 交互图表（滚轮缩放/拖拽平移/         ║
║                     右键复位/十字光标读数/时间筛选/导出 PNG）                 ║
║                                                                              ║
║   src/cenep/__main__.py   `python -m cenep`（默认 GUI；--selftest 自检）      ║
║   src/cenep/selftest.py   run_selftest：三类项目各算一次 + 导出 Excel/PDF     ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │ 只允许调用 ↓，禁止算术运算
╔═══════════════════════════════▼══════════════════════════════════════════════╗
║  ② Application 层 —— 编排：新建/打开/保存 → 校验 → 计算 → 结果               ║
║                                                                              ║
║   src/cenep/application/                  状态：已实现（§151）                ║
║     ├── calculation_service.py  CalculationService / CalculationOutcome      ║
║     │     计算前自动保存(§134)、日志(§133)、耗时统计、异常翻译                ║
║     └── project_service.py      ProjectService                               ║
║           新建 / 打开 / 保存 / 另存为 / 自动保存 / 最近项目                   ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │ engine.calculate(project, ...)
╔═══════════════════════════════▼══════════════════════════════════════════════╗
║  ③ Calculation 层 —— 唯一公式所在地（§82、§145、§146）                        ║
║                                                                              ║
║   src/cenep/calculation/                  状态：已实现                       ║
║     engine.py             CalculationEngine / calculation_engine / calculate ║
║     validator.py          输入校验 + 能量守恒校验（中文报错，§112、§113、§132）║
║     errors.py             CalculationError / ValidationError /               ║
║                           EnergyBalanceError / ScenarioError                 ║
║     pv.py                 resolve_pv_capacity / first_year_generation /      ║
║                           generation_for_year / allocate_pv_energy           ║
║     storage.py            storage_duration_hours / resolve_efficiencies /    ║
║                           annual_discharge_energy / annual_charge_energy /   ║
║                           available_energy_for_year / storage_year_result    ║
║     revenue.py            average_tou_price / resolve_tariff /               ║
║                           self_use_revenue / export_revenue /                ║
║                           storage_arbitrage_revenue / storage_total_revenue  ║
║     investment.py         compute_capex / unit_investment / CapexBreakdown   ║
║     opex.py               resolve_opex_item / roof_rent / opex_for_year      ║
║     tax.py                depreciable_base / annual_depreciation /           ║
║                           depreciation_for_year / residual_value /           ║
║                           revenue_net / tax_year_result / 利润链函数          ║
║     financing.py          loan_amount_of / equity_amount_of /                ║
║                           principal_schedule / build_loan_schedule           ║
║     cashflow.py           project_cashflow / equity_cashflow /               ║
║                           year0_project_cashflow / cumulative                ║
║     financial_metrics.py  npv / irr / payback_period /                       ║
║                           discounted_payback_period / lcoe / lcos / roi /    ║
║                           dscr_series / minimum_dscr / cfads_of              ║
║     scenario.py           apply_delta / delta_for               (§92–§94)    ║
║     sensitivity.py        applicable_variables / apply_variable (§95、§96)   ║
║                                                                              ║
║   ★ V2 新增模块（状态：已实现；详见 3.4 节）                                  ║
║     timeseries_engine.py  TimeAxis / Resolution / points_per_year /          ║
║                           build_time_axis          （V2 §3 P0.1、§7）        ║
║     load_profile.py       resolve_load_series / peak_load(_of)  （V2 §8–§11）║
║     pv_profile.py         resolve_pv_series / 出力系数           （V2 §5–§7）║
║     tariff_series.py      resolve_tariff_series（购电+上网两列）（V2 §12）   ║
║     storage_soc.py        SOC 递推与四上限约束                   （V2 §12–§20）║
║     dispatch_engine.py    PEAK_VALLEY / PV_SELF_CONSUMPTION /                ║
║                           ECONOMIC_OPTIMIZATION 三策略 + 逐日窗口调度（§12）║
║     energy_balance.py     EnergyBalance 年度残差 + 最差逐时残差   （V2 §73）  ║
║     economic_v2.py        YearSimulation（NumPy 列式）/ 逐时经济评价（§20+） ║
║     scenario_engine.py    方案比较、三级寻优编排、ProjectType 归一化（§45+） ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │ 输入 Project，输出 CalculationResult
╔═══════════════════════════════▼══════════════════════════════════════════════╗
║  ③′ V2 辅助层 —— 寻优与数据导入（只被 Calculation / Application 调用）       ║
║                                                                              ║
║   src/cenep/optimization/                 状态：已实现（V2 §45–§48）          ║
║     rule_based.py       规则型推荐（默认策略，零依赖）                        ║
║     greedy_optimizer.py 贪心逐维坐标下降（网格/坐标搜索，max_evaluations 硬上限）║
║     lp_optimizer.py     线性规划（scipy.optimize.linprog，method="highs"；    ║
║                         scipy 不可用时**优雅降级**到贪心并留痕）              ║
║                                                                              ║
║   src/cenep/data/                         状态：已实现（V2 §22–§30）          ║
║     importer.py         负荷/光伏/电价三类模板导入（.xlsx / .csv）            ║
║     validator.py        18 条校验规则（V01–V18）+ 14 条异常告警                ║
║     quality.py          DataQualityScore 0~100 四维度评分（只评分不阻断）     ║
╚═══════════════════════════════╤══════════════════════════════════════════════╝
                                │ 输入 Project，输出 CalculationResult
╔═══════════════════════════════▼══════════════════════════════════════════════╗
║  ④ Domain 层 —— 只描述数据，不含计算（§11）                                   ║
║                                                                              ║
║   src/cenep/domain/                       状态：已实现                       ║
║     models.py      Project / BasicInfo / LoadConfig / PVConfig /             ║
║                    StorageConfig / TariffConfig / InvestmentConfig /         ║
║                    OpexConfig / TaxConfig / FinancingConfig /                ║
║                    PolicyProfile / ScenarioConfig / ScenarioDelta /          ║
║                    SensitivityConfig                                          ║
║     results.py     CalculationResult / AnnualResult /                        ║
║                    SensitivityRow / ScenarioSummary    (§80、§81)            ║
║     timeseries.py  ★ V2：TimeSeriesPoint / TimeSeriesProfile / LoadProfile /  ║
║                    PVProfile / TariffProfile / StorageDispatchConfig /        ║
║                    TimeSeriesConfig 等输入时序模型                            ║
║     timeseries_results.py ★ V2：TimeSeriesResultSet（列式）/ HourlyResult /   ║
║                    DispatchDecision / TimeSeriesReport / EnergyBalance /      ║
║                    DataQualityScore / ScenarioResult / OptimizationResult    ║
║     enums.py       V1 的 11 个 + **V2 的 10 个** StrEnum（§13、V2 §13…）      ║
║     provenance.py  ParameterMeta / ParameterRegistry / SOURCE_PRIORITY /     ║
║                    UNITS / unit_of                    (§83–§85、§91、§144)  ║
╚══════════════════════════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════════════════════════╗
║  ⑤ Infrastructure 层 —— 文件、数据库、日志（被 Application 依赖）            ║
║                                                                              ║
║   src/cenep/infrastructure/               状态：已实现                       ║
║     project_file.py  .nep 读写：save_project / load_project /                ║
║                      read_file_info / autosave / recover_autosave（§9、§134）║
║                      ★ V2：信封写入 schema_version="2.0" 与四个版本号        ║
║     migration.py     ★ V2 新增：migrate_project_payload / migrate_v1_to_v2 /  ║
║                      CURRENT_SCHEMA_VERSION="2.0" / MigrationError（V2 §64） ║
║     db.py            SQLite：政策版本 / 政策模板 / 项目模板 /                 ║
║                      参数字典 / 历史索引（§10）                               ║
║     logging_setup.py setup_logging / get_logger（§133）                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════════════════════════╗
║  ⑥ Policy 层 —— 政策模板与版本（§34–§36、§89、§90）                          ║
║                                                                              ║
║   src/cenep/policy/                       状态：已实现                       ║
║     template.py  PolicyTemplate / PolicyTemplateError / 字段中文标签          ║
║                  （允许"未填写"= None，与 0.0 严格区分，§159）                ║
║     hubei.py     HUBEI_TEMPLATE / describe_startup_notice                    ║
║                  （占位模板：所有数值字段均为 None，不预填具体数值）           ║
║     store.py     PolicyStore：模板增删查、publish 版本、latest、attach        ║
╚══════════════════════════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════════════════════════╗
║   src/cenep/utils/                        状态：尚未实现（目录不存在）       ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 2.1 依赖方向总览

```text
        ┌──────────────┐    ┌──────────────┐
        │      ui      │    │   reports    │
        └──────┬───────┘    └──────┬───────┘
               │                   │
               └────────┬──────────┘
                        ▼
                 ┌──────────────┐
                 │ application  │
                 └──────┬───────┘
             ┌──────────┼───────────┐
             ▼          ▼           ▼
      ┌────────────┐ ┌──────┐ ┌────────────────┐
      │calculation │ │policy│ │ infrastructure │
      └─────┬──────┘ └───┬──┘ └───────┬────────┘
            │            │            │
            └────────────┴────────────┘
                         ▼
                    ┌─────────┐
                    │ domain  │   ← 被所有层依赖，自己不依赖任何层
                    └─────────┘
```

---

## 3. 目录结构树与职责

### 3.1 顶层

```text
cenep/
├── pyproject.toml            打包与依赖声明（Python ≥ 3.12）
├── requirements.txt          运行依赖清单
├── README.md                 项目简介、铁律、文档索引
├── CORE_PARAMETERS_AND_FORMULAS.md   ★ 最高优先级规范（本次交付）
├── CALCULATION_ENGINE.md            计算引擎规范（本次交付）
├── DATA_MODEL.md                    数据模型与字段字典（本次交付）
├── ARCHITECTURE.md                  分层架构与依赖规则（本次交付）
├── PROJECT_SPEC.md           产品范围与验收标准（已存在）
├── UI_SPEC.md                界面规范（已存在）
├── REPORT_SPEC.md            Excel / PDF 报告规范（已存在）
├── REGULATIONS_AND_POLICY.md 法规、标准与政策依据（已存在）
├── HUBEI_POLICY_MODEL.md     湖北政策模型与版本机制（已存在）
├── AI_EXECUTION_GUIDE.md     给 AI Agent 的执行指引（已存在）
├── ROADMAP.md                阶段计划与 V2/V3 候选（已存在）
├── TIMESERIES_MODEL.md       ★ V2 时序数据模型与结果字典
├── STORAGE_DISPATCH.md       ★ V2 储能 SOC 模型与调度策略
├── OPTIMIZATION.md           ★ V2 优化与方案／参数扫描
├── DATA_IMPORT_SPEC.md       ★ V2 时序数据导入规范
├── TEST_PLAN.md              测试计划（含 V2 测试清单，第 8 节）
├── CHANGELOG.md              变更日志（[Unreleased] — V2.0.0）
├── V2_ACCEPTANCE.md          ★ V2 验收清单
├── src/cenep/                源码包（见 3.2）
├── tests/                    pytest 测试体系（见 3.3）
├── tools/                    辅助脚本
├── examples/                 示例项目与导出产物
├── build/                    PyInstaller 打包配置与入口
│     ├── CENEP.spec          打包规格（--windowed，产物 dist\CENEP\CENEP.exe；
│     │                       V2 追加 scipy/pyqtgraph hiddenimports 与版本资源）
│     ├── entry.py            打包入口脚本
│     └── version_info.txt    ★ V2 新增：Win32 版本资源（FileVersion 2.0.0.0）
├── logs/                     运行日志（运行时生成）
└── .pylibs/                  离线依赖目录（本机环境，非项目代码）
```

> **不存在**：`templates/` 目录（`README.md` 曾把它列为规划目录）；
> `docs/` 目录也不存在，规范文档直接放在项目根目录。

### 3.2 `src/cenep/` 逐目录职责

| 目录 | 模块数 | 职责 | 状态 | 规范 |
|---|---|---|---|---|
| `domain/` | 7 个 `.py` | 数据模型、结果对象、枚举、参数来源、**V2 时序输入/结果模型**；**不含计算** | 已实现 | §11、§80、§81、§83；V2 §4–§6 |
| `calculation/` | 24 个 `.py` | **唯一公式所在地**；V1 的 31 步编排 + **V2 的 9 个时序模块** | 已实现 | §8、§82、§145、§146；V2 §7–§21 |
| `optimization/` | 4 个 `.py` | ★ **V2 新增包**：规则型 / 贪心 / 线性规划三级寻优与回退链 | 已实现 | V2 §45–§48 |
| `data/` | 4 个 `.py` | ★ **V2 新增包**：时序数据导入、校验与质量评分 | 已实现 | V2 §22–§30 |
| `application/` | 3 个 `.py` | 编排、自动保存、日志、异常翻译、**项目类型按容量归一化** | 已实现 | §8、§134、§151；V2 §105 |
| `infrastructure/` | 5 个 `.py` | `.nep` 文件、SQLite、日志、**V2 `.nep` 版本迁移** | 已实现 | §9、§10、§133；V2 §64、§65 |
| `policy/` | 4 个 `.py` | 政策模板/版本/发布，刻意不预填数值 | 已实现 | §34–§36、§89、§90、§159 |
| `reports/` | 3 个 `.py` | Excel（**24 表**）与 PDF（**16 部分**）导出，**只渲染不计算** | 已实现 | §108–§111；V2 §66、§67 |
| `ui/` | 7 个 `.py` | PySide6 界面：字段绑定 + 结果展示 + **V2 时序页与 PyQtGraph 图表**，**无任何公式** | 已实现 | §5、§6、§130、§131、§135；V2 §68–§71 |
| `utils/` | — | 通用工具 | **尚未实现** | — |

`ui/` 内部模块：

| 模块 | 主要对象 | 职责 |
|---|---|---|
| `app.py` | `run` / `create_application` / `create_window` / `APP_NAME` | 建 QApplication、建主窗口、进入事件循环 |
| `main_window.py` | `MainWindow` | 菜单与流程：新建/打开/保存/另存为/计算/导出 Excel/导出 PDF；`collect_project()`、`calculate_now()` |
| `pages.py` | `ProjectPage`、`ParametersPage`、`CalculatePage`、`ResultPage`、`SensitivityPage`、`ReportPage`、`SettingsPage`，以及 **V2 的 `TimeSeriesPage`**（5 个子页） | 各页面的 `load()` / `apply()` / `show_result()` |
| `sections.py` | `SectionSpec` 常量（`GENERAL_SECTION`、`LOAD_SECTION`、`PV_SECTION`、`STORAGE_SECTION`、`TARIFF_SECTION`、`INVESTMENT_SECTION`、`OPEX_SECTION`、`TAX_SECTION`、`FINANCING_SECTION`）与 **V2 的 5 个时序分区**（时序总开关/负荷/光伏/分时电价/储能调度）与 `SOURCE_LEGEND` | 参数分区与字段清单 |
| `field_spec.py` | `Kind`、`FieldSpec`、`SectionSpec`、`FieldRow`、`SectionForm`、`get_path` / `set_path` | 字段↔控件双向绑定、按 `SourceType` 着色（§144）、`value()` / `set_value()` / `colorize()` |
| `charts.py` ★ V2 | `TimeSeriesChart` 等（`CHART_TYPICAL_DAY` 等图表常量） | **PyQtGraph 交互图表**：全年负荷/PV/储能 SOC/典型日四线/电价/方案比较；滚轮缩放、拖拽平移、右键复位、十字光标读数、时间筛选、导出 PNG。数据只从 `TimeSeriesResultSet.column()` 取列，**界面不做任何计算**（§61） |

`ui/__init__.py` 导出的 `__all__` 为 `["FieldSpec", "FieldRow", "SectionForm"]`。

`src/cenep/__main__.py`：`main(argv)`；带 `--selftest [输出文件]` 时走
`selftest.run_selftest`（三类黄金项目各算一次并导出 Excel + PDF，成功返回 0）。

### 3.3 `tests/` 与 `tools/`、`examples/`

```text
tests/                                共 32 个 test_*.py（885 个用例）
├── conftest.py                     黄金案例夹具（§117）
│     GOLDEN_* 常量 + golden_pv / golden_storage / golden_pv_storage / hubei_policy
│     + V2 时序夹具
├── ── V1 既有（16 个文件）────────────────────────────────────────────
├── test_engine_golden.py           端到端黄金案例（手工推算期望值）
├── test_pv.py / test_storage.py / test_revenue.py
├── test_finance_modules.py / test_metrics.py / test_lcoe_caliber.py
├── test_scenario_sensitivity.py    情景与敏感性
├── test_validation.py              校验与中文报错
├── test_project_file.py            .nep 与 SQLite
├── test_policy.py                  政策模板完整性与版本机制
├── test_application.py             应用服务、自动保存、日志、性能
├── test_excel_export.py            Excel 与数值一致性
├── test_pdf_export.py              PDF 与免责声明
├── test_gui.py                     界面绑定、结果展示、校验反馈（需 PySide6）
├── test_selftest.py                python -m cenep --selftest 自检流程
├── ── V2 新增（16 个文件）────────────────────────────────────────────
├── test_timeseries_models.py       时序输入/结果模型、JSON 序列化（J1–J8）
├── test_timeseries_engine.py       时间轴、分辨率、平/闰年点数
├── test_profile_engines.py         负荷/PV/分时电价三个曲线引擎
├── test_storage_soc.py             SOC 递推与四上限约束
├── test_dispatch_engine.py         三策略调度 + 逐日窗口调度缺陷回归
├── test_dispatch_semantics.py      调度原因与实际动作一致
├── test_v2_integration.py          时序端到端 + 能量守恒
├── test_result_wiring.py           CalculationResult 字段贯通（两处同源写入）
├── test_project_type_alignment.py  ProjectType 按容量归一化（20.03% → 15.00%）
├── test_data_layer.py              导入 / 18 条校验 / 14 条告警 / 质量评分
├── test_optimization.py            规则型 / 贪心 / 线性规划三级寻优
├── test_scenario_engine.py         方案比较与参数扫描
├── test_report_v2.py               Excel 24 表 / PDF 16 部分
├── test_gui_v2.py                  时序页与 PyQtGraph 图表
├── test_v2_performance.py          V2 性能预算
└── test_migration.py               .nep 1.0/1.1 → 2.0 迁移

tools/
└── make_examples.py                生成 examples/ 下的示例 .nep/.xlsx/.pdf

examples/
├── 示例1_工商业光伏.nep / .xlsx / .pdf
├── 示例2_工商业储能.nep / .xlsx / .pdf
├── 示例3_工商业光储.nep / .xlsx / .pdf
└── checks.json                     示例校验结果
```

**黄金案例固定参数**（`tests/conftest.py`，§117）：

| 常量 | 值 | 说明 |
|---|---|---|
| `GOLDEN_PV_CAPACITY_KWP` | `1000.0` | 光伏容量 |
| `GOLDEN_EQUIVALENT_HOURS` | `1100.0` | 年等效小时 |
| `GOLDEN_PERFORMANCE_RATIO` | `1.0` | 性能比 |
| `GOLDEN_SELF_CONSUMPTION` | `0.8` | 自用比例 |
| `GOLDEN_PV_DEGRADATION` | `0.005` | 光伏年衰减 |
| `GOLDEN_PV_CAPEX_PER_KW` | `3000.0` | 光伏单位投资 |
| `GOLDEN_ANALYSIS_PERIOD` | `25` | 计算期 |
| `GOLDEN_STORAGE_POWER_KW` | `500.0` | 储能功率 |
| `GOLDEN_STORAGE_ENERGY_KWH` | `1000.0` | 储能容量 |
| `GOLDEN_LOAD_KWH` | `1_200_000.0` | 年用电量 |
| `GOLDEN_EXPORT_PRICE` | `0.35` | 上网电价 |
| `GOLDEN_DEBT_RATIO` | `0.5` | 贷款比例 |
| `GOLDEN_INTEREST_RATE` | `0.04` | 贷款利率 |
| `GOLDEN_LOAN_TERM` | `10` | 贷款期限 |

### 3.4 ★ V2 模块布局（新增，均已实现）

V2 **只新增模块、不改变 V1 既有模块的公式与口径**（兼容性承诺 C4）。
下表是 V2 新增/扩展的全部落点；每条都可在磁盘上找到对应文件。

**① `calculation/` 下新增 9 个时序模块**

| 模块 | 主要对象 / 函数 | 职责 | 规范 |
|---|---|---|---|
| `timeseries_engine.py` | `TimeAxis`（frozen dataclass）、`Resolution` 消费、`is_leap_year`、`points_per_year`、`build_time_axis` | 建时间轴：`timestamp` 为**唯一主时间索引**，一次向量化算出年/月/日/时/星期/周末/节假日/日类型；平年 8760、闰年 8784，并为 15 分钟（35040/35044）预留 | V2 §3 P0.1、§7 |
| `load_profile.py` | `resolve_load_series`、`peak_load_of`、`peak_load`、`_delta_hours_of` | 负荷曲线引擎：`HOURLY` / `TYPICAL_DAY`（典型日形状 × 年电量定水平）/ `ANNUAL_SIMPLE` 三模式 | V2 §8–§11 |
| `pv_profile.py` | `resolve_pv_series` | 光伏出力曲线引擎：`EQUIVALENT_HOURS` / `TYPICAL_DAY` / `MONTHLY_HOUR_FACTOR` / `HOURLY` 四模式，含夜间零出力语义 | V2 §5–§7 |
| `tariff_series.py` | `resolve_tariff_series`（返回 `(price, export_price)` 两列） | 分时电价曲线引擎：尖峰/高峰/平/谷/深谷 + 上网电价；上网电价**独立**于购电电价增长 | V2 §12 |
| `storage_soc.py` | SOC 递推、充放电能力四上限取小、`soc_min`/`soc_max` 硬约束 | 储能 SOC 模型：`E_inc = P_charge × η_charge × Δt`、`E_dec = P_discharge / η_discharge × Δt`；单向效率取 `√0.88` 以严格保持 V1 的往返 0.88 | V2 §12–§20 |
| `dispatch_engine.py` | `DispatchDecision`、三策略入口、逐日价格排序窗口调度 | 三策略：`PEAK_VALLEY`（逐日窗口，**非**逐时贪心）/ `PV_SELF_CONSUMPTION`（PV→负荷→储能→上网）/ `ECONOMIC_OPTIMIZATION`（规则型逐时净收益） | V2 §12、§14 |
| `energy_balance.py` | `EnergyBalance` | 逐时/年度能量守恒：`error` + `max_hourly_error` + `tolerance = 1e-6` + `is_balanced` | V2 §73 |
| `economic_v2.py` | `YearSimulation`（**NumPy 列式**）、`to_result_set`、逐时经济评价 | 8760 时序仿真与逐时经济评价：自用率/自给率/电费节省/储能收益分解/需量电费与削峰/逐时套利/LCOS | V2 §20+ |
| `scenario_engine.py` | `normalize_project_type`、`effective_pv_capacity`、方案扫描与寻优编排 | 方案比较与三级寻优编排；**`ProjectType` 按容量自动归一化**（缺陷 ⑬） | V2 §45–§48、§105 |

**② 新增 `optimization/` 包** —— 三级寻优与回退链（`rule_based` → `greedy_optimizer` → `lp_optimizer`），
目标函数 `min(购电成本 − 上网收入 + 储能运行成本)`，`scipy` 不可用时**降级但留痕**，
结果按五段式输出且禁止黑盒（`explanation` 必须含数值比较）。

**③ 新增 `data/` 包** —— 时序数据导入（`importer.py`）、校验（`validator.py`：18 条规则 V01–V18
+ 14 条告警）、质量评分（`quality.py`：0~100 四维度，**只评分不阻断**）。

**④ 新增 `ui/charts.py`** —— PyQtGraph 交互图表；**只从 `TimeSeriesResultSet.column()` 取列，
界面不做任何计算**（§61）。

**⑤ 新增 `infrastructure/migration.py`** —— V1（`1.0`/`1.1`）→ V2（`2.0`）`.nep` 迁移器。

**⑥ `domain/` 新增 `timeseries.py` 与 `timeseries_results.py`**，`CalculationResult` 新增 8 个字段
（`time_series_results`、`baseline_results`、`annual_results`、`dispatch_results`、`energy_balance`、
`data_quality`、`scenario_results`、`optimization_results`），V1 字段语义不变。

**⑦ `reports/`** —— Excel 13 → **24 表**、PDF 15 章 → **16 部分**（V1 内容全部保留）。

### 3.5 ★ V2 数据流（§61 单一计算源）

V2 的数据流**只有一条主干**，且**只在最末端**接上 V1 的年度引擎 —— 这是"单一计算源"的
结构性保证，而不是事后比对：

```text
                   ┌─────────────────────────────────────────┐
                   │  Project.timeseries（TimeSeriesConfig）  │
                   └────────────────────┬────────────────────┘
                                        ▼
 ① TimeAxis           TimeAxis（时间轴）── timestamp 唯一主索引，向量化年/月/日/时/星期/日类型
                                        │
                                        ▼
 ② 三条曲线          负荷曲线 ┃ 光伏出力曲线 ┃ 分时电价曲线（含上网电价）
    （三个引擎各产出一列 NumPy 数组，长度 = axis.point_count）
                                        │
                                        ▼
 ③ dispatch          dispatch_engine：三策略择一 ── 逐日价格排序窗口 / 自用优先 / 规则型经济
                    → 逐时购电、上网、充放电量 + DispatchDecision（动作 + 中文原因）
                                        │
                                        ▼
 ④ energy_balance    energy_balance：九条能量流 + 年度残差 error + 最差逐时残差 max_hourly_error
                    → 超出 tolerance(1e-6) 即判定计算失败（不裁剪后继续）
                                        │
                                        ▼
 ⑤ metrics           economic_v2：YearSimulation（NumPy 列式）
                    自用率 / 自给率 / 电费节省 / 储能收益分解 / 需量电费与削峰 / 逐时套利 / LCOS
                                        │
                                        ▼
 ⑥ YearOverride     把逐时仿真的首年口径（发电量、自用/上网电量、储能充放电与收益、
                    需量削减……）作为**年度覆盖量**传给 V1 年度引擎
                                        │
                                        ▼
 ⑦ V1 年度引擎       calculation/engine.py 的 31 步固定顺序（**唯一公式源**）
                    → CalculationResult（唯一结果对象，V2 新增 8 个字段）
                                        │
                     ┌──────────────────┼──────────────────┐
                     ▼                  ▼                  ▼
                  ui/charts.py      excel_exporter      pdf_exporter
                  （PyQtGraph）     （24 表）           （16 部分）
                     └──────── 一律只渲染、不计算（§61、§148–§150）────────┘
```

**关键约束（V2 未破）**

1. `time_series_results` 等 8 个字段只是 `CalculationResult` 的**扩展**，
   GUI / Excel / PDF 仍然只消费这一个对象；
2. 时序路径与 V1 路径**共用同一套**投资、折旧、税务、融资与指标函数，
   时序部分只提供"年度覆盖量"，**不另起一套年度公式**；
3. 未启用时序（`timeseries.enabled = False`）时，**第 ①②③④⑤⑥ 步整体跳过**，
   直接从 `Project` 进 V1 年度引擎 —— 这就是 V1 结果可复现（C2）的实现方式。

---

## 4. 依赖规则

### 4.1 允许的依赖

| 层 | 允许依赖 | 说明 |
|---|---|---|
| `domain` | 仅标准库 + Pydantic | **不依赖任何本项目其它层** |
| `calculation` | `domain` | 可依赖数据模型与结果对象；不得依赖 application / reports / infrastructure / ui / optimization / data |
| `optimization` ★ V2 | `domain` + `calculation` | 寻优只能"改参数 → 调 `calculate()` → 读结果"；**不得**自己写财务公式 |
| `data` ★ V2 | `domain` | 时序导入与校验：只产出/检查时序模型，不含任何经济公式 |
| `policy` | `domain` + `infrastructure` | 模板与版本持久化 |
| `infrastructure` | `domain` | `.nep`/SQLite/日志/迁移 |
| `application` | `calculation` + `domain` + `infrastructure`（+ `optimization` 编排寻优） | 编排 |
| `reports` | `domain`（结果对象 + 输入快照）+ `infrastructure.logging_setup` | 只渲染 |
| `ui` | `application`（`CalculationService` / `ProjectService`）+ `domain` | 只绑定与展示；`ui/charts.py` 额外依赖 `pyqtgraph`，但**仍不得计算** |

> **V2 的分层不变式**：`optimization/` 与 `data/` 是**旁挂包**，位于 `calculation/` 的
> 两侧（`data/` 在输入侧、`optimization/` 在编排侧），二者都**不得**被 `calculation/`
> 反向依赖，也**不得**绕过 `calculation_engine.calculate()` 自行出经济结果（§61 单一计算源）。

已核实的真实依赖示例：

```python
# calculation/engine.py
from ..domain.enums import ProjectType, ScenarioType, SensitivityVariable, SourceType
from ..domain.models import Project
from ..domain.provenance import ParameterRegistry
from ..domain.results import AnnualResult, CalculationResult, ScenarioSummary, SensitivityRow
from . import cashflow as cf
from . import financial_metrics as fm
...

# application/calculation_service.py
from ..calculation.engine import CalculationEngine, calculation_engine
from ..calculation.errors import CalculationError
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger
from .project_service import ProjectService

# reports/excel_exporter.py
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger
```

### 4.2 禁止的依赖

| 编号 | 禁止 | 原因 |
|---|---|---|
| D1 | `domain/` 依赖任何其它层 | 领域层必须可独立测试、可序列化 |
| D2 | `calculation/` 依赖 `application/`、`reports/`、`ui/`、`infrastructure/` | 保证计算层是纯函数式的、与 I/O 无关 |
| D3 | `calculation/` 内任何模块导入 GUI 或文件系统 | 纯计算，便于单元测试 |
| D4 | `ui/` 或 `reports/` 导入 `calculation/` 的子模块（`pv`、`tax`、`financing`…） | 绕过 31 步编排与守恒校验 |
| D5 | `reports/` 中的算术运算 | §109：报表数值必须来自 `CalculationResult` |
| D6 | 任何层硬编码电价/税率/补贴等业务常量 | §33、§34 |
| D7 | `infrastructure/` 依赖 `application/` 或 `ui/` | 基础设施是被依赖方，方向不可反转 |
| D8 | `policy/` 直接参与计算 | 政策只提供参数，计算仍由 `calculation/` 完成 |
| D9 ★ V2 | `calculation/` 依赖 `optimization/` 或 `data/` | 旁挂包不得反向依赖；时序模块只接受**已解析好的数组**（`TimeAxis` + 三条曲线） |
| D10 ★ V2 | `optimization/` 或 `data/` 内出现财务公式（`npv` / `irr` / `lcoe` / `lcos`） | 寻优只能调 `calculate()` 取结果；违反即破坏"只有一套公式" |
| D11 ★ V2 | `ui/charts.py` 从原始时序数据自行聚合/计算指标 | 图表数据**只**来自 `TimeSeriesResultSet.column()`（§61、§69） |

### 4.3 依赖规则的自检方法

```powershell
# ① 计算层是否被"越层"导入（应为空）
Select-String -Path src\cenep\reports\*.py,src\cenep\application\*.py `
  -Pattern 'from \.\.calculation\.(pv|storage|tax|financing|investment|opex|cashflow|financial_metrics|revenue)'

# ② 计算层是否依赖 I/O 或界面（应为空）
Select-String -Path src\cenep\calculation\*.py `
  -Pattern 'open\(|Path\(|sqlite3|reportlab|openpyxl|PySide6'

# ③ 报表层是否引入算术重算（人工审查每处 + - * / **）
Select-String -Path src\cenep\reports\*.py -Pattern '(\+|\-|\*|/|\*\*)'

# ④ 全量测试
python -m pytest -q
```

---

## 5. 一次计算的完整调用时序

### 5.1 时序图（从"点击计算"到"结果渲染"）

```text
用户                 UI(GUI)        Application          Calculation       Domain
 │                      │                │                    │              │
 │ 点击"计算"           │                │                    │              │
 ├─────────────────────>│                │                    │              │
 │                      │ 收集界面输入    │                    │              │
 │                      │ 组装 Project   │                    │              │
 │                      ├───────────────>│ CalculationService │              │
 │                      │                │  .calculate_with_outcome(project, path)
 │                      │                │                    │              │
 │                      │                │ ① ProjectService.autosave(project, path)
 │                      │                ├───────────────────────────────────>│ .nep 写盘
 │                      │                │    （§134，失败不影响计算）          │
 │                      │                │                    │              │
 │                      │                │ ② logger.info("计算开始：…")
 │                      │                │ ③ engine.calculate(project, include_scenario, include_sensitivity)
 │                      │                ├───────────────────>│              │
 │                      │                │                    │ _calculate_core(project)
 │                      │                │                    │  ├─ 1. validator.validate_project
 │                      │                │                    │  │     └── 失败 → ValidationError(中文, field)
 │                      │                │                    │  ├─ ParameterRegistry 登记（§83）
 │                      │                │                    │  ├─ 2..21 逐年循环（§82）
 │                      │                │                    │  ├─ 30. validate_energy_balance
 │                      │                │                    │  │     失败 → EnergyBalanceError
 │                      │                │                    │  ├─ 22..27 IRR/NPV/回收期/LCOE/LCOS/DSCR
 │                      │                │                    │  └─ 31. 组装 CalculationResult
 │                      │                │                    │<─────────────┤
 │                      │                │                    │ ④ 28. _run_scenarios → 3 × _calculate_core
 │                      │                │                    │ ⑤ 29. _run_sensitivity → V×S × _calculate_core
 │                      │                │<───────────────────┤              │
 │                      │                │ ⑥ elapsed = perf_counter 差
 │                      │                │ ⑦ logger.info("计算结束：…IRR=…NPV=…")
 │                      │<───────────────┤ CalculationOutcome(result, elapsed, autosaved_to)
 │                      │                │                    │              │
 │                      │ ⑧ 只读 CalculationResult 渲染       │              │
 │<─────────────────────┤   （表格 / 曲线 / 指标卡片 / 配色）  │              │
 │                      │                │                    │              │
 │ 点击"导出 Excel"     │                │                    │              │
 ├─────────────────────>│ ExcelExporter.export(project, result, path)        │
 │                      │  逐表写入数值（§108、§109），不写公式                │
 │ 点击"导出 PDF"       │                │                    │              │
 ├─────────────────────>│ PdfExporter.export(project, result, path)          │
 │                      │  16 部分报告 + 免责声明（§110、§111；V2 §67）        │
```

### 5.2 分步说明

| 步 | 动作 | 代码位置 | 失败处理 |
|---|---|---|---|
| ① | 计算前自动保存 | `ProjectService.autosave` | 禁用时返回 `None`；不影响计算 |
| ② | 记录"计算开始" | `CalculationService.calculate_with_outcome` | — |
| ③ | 调用引擎 | `CalculationEngine.calculate` | — |
| ④ | 输入校验 | `validator.validate_project` | 抛 `ValidationError`（中文 + `field`） |
| ⑤ | 参数来源登记 | `CalculationEngine._register_parameters` | — |
| ⑥ | 31 步核心计算 | `CalculationEngine._calculate_core` | 各模块抛 `ValidationError` |
| ⑦ | 守恒校验 | `validator.validate_energy_balance` / `validate_storage_balance` | 抛 `EnergyBalanceError` |
| ⑧ | 指标计算 | `financial_metrics` | 不可解时返回 `None`（不抛异常） |
| ⑨ | 情景分析 | `_run_scenarios` → `scenario.apply_delta` | `ScenarioError` |
| ⑩ | 敏感性分析 | `_run_sensitivity` → `sensitivity.apply_variable` | — |
| ⑪ | 耗时与日志 | `CalculationService` | — |
| ⑫ | 结果渲染 | `ui`（表格/曲线/指标卡片/配色）/ `reports` | — |

### 5.3 应用服务的关键代码

```python
def calculate_with_outcome(self, project, current_path=None, *,
                          include_scenario=True, include_sensitivity=True) -> CalculationOutcome:
    autosaved: Path | None = None
    if current_path is not None:
        autosaved = self.project_service.autosave(project, current_path)   # §134

    logger.info("计算开始：%s", project.basic_info.project_name)
    started = time.perf_counter()
    try:
        result = self.engine.calculate(project,
                                       include_scenario=include_scenario,
                                       include_sensitivity=include_sensitivity)
    except CalculationError as exc:
        logger.warning("计算失败：%s", exc)
        raise
    except Exception as exc:
        logger.exception("计算出现未预期异常")
        raise CalculationError(f"计算出现未预期错误：{exc}") from exc

    elapsed = time.perf_counter() - started
    logger.info("计算结束：%s，耗时 %.3f 秒，项目IRR=%s，NPV=%.2f 元", ...)
    return CalculationOutcome(result=result, elapsed_seconds=elapsed, autosaved_to=autosaved)
```

### 5.4 项目生命周期（新建 → 打开 → 保存）

```text
新建：ProjectService.new_project(project_type, name, province, city)
        · 按类型清空无关容量（避免"储能项目带光伏容量"脏数据）
        · 含光伏且未给容量 → 系统默认起点 1000 kWp（SYSTEM_DEFAULT，用户可改）
        · 含储能且容量 ≤ 0 → 系统默认起点 500 kW / 1000 kWh
打开：ProjectService.open_project(path)
        · project_file.load_project（信封校验 + schema 校验 + Pydantic 校验）
        · 若注入了 Database → db.touch_history(...)
保存：ProjectService.save_project(project, path)
        · project_file.save_project（临时文件 + os.replace 原子替换）
        · 若注入了 Database → 记历史
另存为：save_project_as → ensure_suffix
自动保存：autosave → "<stem>.autosave.nep"
最近项目：recent_projects(limit) → db.recent_projects（按 MAX(id) 排序）
```

---

## 6. 扩展点

### 6.1 新增省份政策

**目标**：新增一个省份的政策模板（例如广东），不预填任何具体数值，带版本与出处。

| 步 | 改哪里 | 具体内容 |
|---|---|---|
| 1 | `src/cenep/policy/` 新增 `guangdong.py` | 仿 `hubei.py`：定义 `GUANGDONG_TEMPLATE = PolicyTemplate(...)`，**所有数值字段留 `None`**，`source_url` 只给已核实的官方发布页 |
| 2 | `src/cenep/policy/__init__.py` | 导出 `GUANGDONG_TEMPLATE` 与（如需）`describe_startup_notice` |
| 3 | `src/cenep/policy/store.py` | `seed_builtin_templates()` 的元组中加入新模板 |
| 4 | `HUBEI_POLICY_MODEL.md`（或新建省份文档） | 记录模板 ID、版本策略、出处 |
| 5 | 测试 | 在 `tests/` 增加模板完整性用例（`is_complete()` 应为 `False`，`to_profile()` 应拒绝） |

**禁止**：

- 不得在 `calculation/revenue.py` 里为新省份加 `if province == "广东"` 分支（§33、§34）；
- 不得给模板预填任何电价/比例数值（§159）；
- 不得让 `PolicyTemplate` 的 `None` 直接进入 `PolicyProfile`（`to_profile()` 会拒绝，
  除非显式 `allow_unfilled=True`，此时 `notes` 会自动加"未填写警示"）。

**政策数据流**：

```text
PolicyTemplate（可未填写）
   │ 用户填写 → is_complete() == True
   ▼
PolicyTemplate.to_profile()  →  PolicyProfile（数值为 float，不可为 None）
   │ PolicyStore.publish(template) → db.save_policy(profile)（版本只新增不覆盖）
   ▼
PolicyStore.attach(project, profile) → project.policy
   │ engine._register_parameters → SourceType.POLICY 登记
   ▼
CalculationResult.parameter_sources / notes / Excel「政策依据」表 / PDF「政策依据」章
```

### 6.2 新增收益类型

**目标**：例如新增"需求响应收益"。

| 步 | 改哪里 | 具体内容 |
|---|---|---|
| 1 | `domain/models.py` | 在对应 Config 中新增金额字段（`ge=0`，给默认值 `0.0`，否则旧 `.nep` 会加载失败） |
| 2 | `calculation/revenue.py` | 若需要新公式，新增**纯函数**并写 docstring 标注规范条款 |
| 3 | `domain/results.py:AnnualResult` | 新增结果字段（默认 `0.0`） |
| 4 | `calculation/engine.py` | 在逐年循环第 13 步区间取值、第 14 步计入 `total_revenue`、填入 `AnnualResult` |
| 5 | `calculation/scenario.py` | 若该收益随电价/运维乘数联动，加入 `apply_delta` 的显式施加范围 |
| 6 | `calculation/sensitivity.py` | 若需要敏感性变量，`_scale_*` 中加入该字段 |
| 7 | `reports/excel_exporter.py` | 在「年度现金流」或新增列中展示（**只取结果值**） |
| 8 | `reports/pdf_exporter.py` | 在「收益测算」章展示 |
| 9 | `CORE_PARAMETERS_AND_FORMULAS.md` | 参数表 + 公式 + 对照表三处同步 |
| 10 | 测试 | 单元测试 + 黄金案例期望值（手工推算，§117） |

**禁止**：不得在 `reports/` 里用"总收入 − 已知项"反推新收益（那是计算，不是渲染）。
§44 已经明确储能四类收益**必须分开记录**，新增收益应沿用这一模式。

### 6.3 新增报表

**目标**：例如新增"投委会简报"（Word 或 Markdown）。

| 步 | 改哪里 | 具体内容 |
|---|---|---|
| 1 | `src/cenep/reports/` 新增模块（如 `brief_exporter.py`） | 定义 `BriefExporter` 类与模块级单例；方法签名统一为 `export(project, result, path) -> Path` |
| 2 | `src/cenep/reports/__init__.py` | 导出新类与单例（仿现有 `ExcelExporter` / `PdfExporter` 写法） |
| 3 | `REPORT_SPEC.md` | 定义章节/工作表规范（已存在，须与新报表保持同步） |
| 4 | 测试 | 断言"报表数值与 `CalculationResult` 完全一致"且"不写公式/不重算" |
| 5 | `ARCHITECTURE.md` 本文档 3.2 节 | 更新模块与职责 |

**报表扩展的硬约束**：

1. 数据只能来自 `CalculationResult`（以及 `Project` 的**参数原值**）；
2. `project` 与 `result` 必须作为**两个独立入参**传入（与现有实现一致），
   以便报表同时展示"输入参数"与"计算结果"；
3. 不得在报表中写公式（§109）；Excel 已有测试断言不存在以 `=` 开头的单元格；
4. 不可计算项必须显示"无法计算"/"未回收"/"—"，**不得**显示 0；
5. 免责声明必须逐字出现（§111，见 `pdf_exporter.DISCLAIMER` / `DISCLAIMER_2`）。

**现有报表接口速查**：

```python
# Excel：V2 为 24 张工作表（V2 §67 对 V1 §108 的正当超集；V1 的 13 张全部保留、相对顺序不变）
SHEET_NAMES = ["项目概况", "基础参数", "技术参数", "储能与调度", "负荷曲线", "光伏曲线",
               "电价参数", "分时电价", "投资参数", "运维参数", "融资参数",
               "8760时序仿真", "能量平衡", "年度汇总", "收益分解", "年度现金流",
               "财务指标", "敏感性分析", "情景分析", "方案比较", "方案寻优",
               "数据质量", "政策依据", "参数来源"]
# ★ 为 V2 新增的 11 张：储能与调度 / 负荷曲线 / 光伏曲线 / 分时电价 / 8760时序仿真 /
#   能量平衡 / 年度汇总 / 收益分解 / 方案比较 / 方案寻优 / 数据质量
#   未启用时序时这些表输出占位说明，V1 项目仍可导出（不缺表、不报错）

ExcelExporter().export(project, result, path) -> Path       # 自动补 .xlsx
ExcelExporter().export_sheets(project, result) -> list[str]  # 只返回表名，不做计算

# PDF：V2 为 16 部分（V1 的 15 章重新归并 + 免责声明）
REPORT_SECTIONS = ["项目概况", "输入参数", "负荷分析", "PV时序分析", "储能SOC分析",
                   "能源流", "电费分析", "储能收益", "投资", "现金流", "经济指标",
                   "方案比较", "敏感性", "风险", "参数来源", "免责声明"]

PdfExporter().export(project, result, path) -> Path          # 自动补 .pdf
PdfExporter().build_story(project, result, styles) -> list   # 拆出便于测试
register_cjk_font() -> str                                   # 中文字体注册（带回退链）
```

### 6.4 其它扩展点速查

| 需求 | 首选位置 | 关键约束 |
|---|---|---|
| 新增项目类型 | `domain/enums.py:ProjectType` + `has_pv` / `has_storage` | V1 只允许三类工商业项目（§13） |
| 新增税率/折旧方法 | `domain/enums.py` + `calculation/tax.py` | 必须新增 `TODO(V2)` 说明（§158） |
| 新增校验规则 | `calculation/validator.py` | 中文 + `field`（§132） |
| 新增指标 | `calculation/financial_metrics.py` + `domain/results.py` | 不可计算返回 `None`（§114） |
| 新增情景乘数 | `domain/models.py:ScenarioDelta` + `scenario.py:apply_delta` | 显式施加，无隐藏逻辑（§158） |
| 新增敏感性变量 | `domain/enums.py:SensitivityVariable` + `sensitivity.py` | 一次只改一个参数（§95） |
| 新增政策字段 | `domain/models.py:PolicyProfile` + `policy/template.py` | 模板字段必须可为 `None` |
| 新增 `.nep` 字段 | `domain/models.py`（给默认值） | 版本不匹配当前直接拒载（无迁移器） |

---

## 7. 关键设计决策

### 7.1 为什么情景/敏感性用"深拷贝 + 重算"而不是写第二套公式

- **正确性**：只有一条代码路径能产生数值，不存在"两套公式不一致"的风险；
- **可审计**：`apply_delta` / `apply_variable` 只改输入，改了什么一目了然（§158 禁止隐藏逻辑）；
- **代价**：计算量上升（默认光储项目 44 次核心计算），但纯 Python 秒级即可完成（§155）。

### 7.2 为什么 `.nep` 用"信封 + JSON"而不是裸 `model_dump_json()`

- 信封的 `format` 字段让"这不是本软件的文件"能被识别并给出中文提示；
- `schema_version` 让未来版本迁移有抓手（当前策略是**不匹配即拒载**）；
- `app_version` + `saved_at` 便于问题复现；
- 原子写入（临时文件 + `os.replace`）避免崩溃导致文件损坏。

### 7.3 为什么政策要分"模板"与"实例"

`PolicyProfile` 的数值字段是 `float`（不可为 `None`）。若模板把"未填写"写成 `0.0`，
报告中就会出现"机制电价 0 元/kWh"这种**看起来像事实的假数据**，违反 §91。
因此 `PolicyTemplate` 用 `None` 表达"未填写"，`to_profile()` 在未填完时**拒绝转换**（§159）。

### 7.4 为什么报表把 `project` 和 `result` 都作为入参

`CalculationResult` 只包含**计算结果与派生指标**；报表还需要展示**输入参数原值**
（如折现率、贷款期限、各项运维模式）。让报表同时接收 `project` 与 `result`，
可以做到"参数区显示输入、结果区显示结果"，且**不需要在结果对象里冗余存一份输入**。

### 7.5 为什么守恒校验放在指标计算之前

电量不守恒时，IRR / NPV / LCOE 全都没有意义。引擎在逐年循环结束后**立即**执行
`validate_energy_balance` 与 `validate_storage_balance`（§82 第 30 步），
不通过就抛 `EnergyBalanceError`，从而**不可能**拿到基于错误电量的指标（§113）。

---

## 8. 测试架构

### 8.1 测试分层

| 层次 | 测试文件 | 关注点 |
|---|---|---|
| 单元 | `test_pv.py`、`test_storage.py`、`test_revenue.py`、`test_finance_modules.py`、`test_metrics.py` | 纯函数正确性、边界与异常 |
| 集成 | `test_engine_golden.py` | 31 步端到端、守恒、确定性、参数来源、政策披露 |
| 分析 | `test_scenario_sensitivity.py` | 情景从 BASE 复制、非链式、单变量、系数符号、变量过滤 |
| 校验 | `test_validation.py` | 中文报错 + 字段定位、Pydantic 兜底、守恒阈值边界 |
| 持久化 | `test_project_file.py` | `.nep` 往返、信封、原子写、版本校验、SQLite |
| 应用 | `test_application.py` | 自动保存、异常翻译、性能、日志 |
| 报表 | `test_excel_export.py`、`test_pdf_export.py` | 章节/表数、数值一致性、无公式重算、免责声明 |
| ★ V2 时序模型 | `test_timeseries_models.py`、`test_timeseries_engine.py` | 输入/结果模型字段、`model_dump(mode="json")`（J1–J8）、时间轴与分辨率、平/闰年点数 |
| ★ V2 曲线引擎 | `test_profile_engines.py` | 三个曲线引擎的模式与归一化、Δt 口径（15 分钟不低估功率）、边界 |
| ★ V2 储能与调度 | `test_storage_soc.py`、`test_dispatch_engine.py`、`test_dispatch_semantics.py` | SOC 递推与四上限、三策略语义、原因与实际动作一致、逐日窗口调度（非逐时贪心） |
| ★ V2 数据层 | `test_data_layer.py` | 三类模板导入、18 条校验、14 条告警、质量评分、空值不静默变 0 |
| ★ V2 寻优 | `test_optimization.py`、`test_scenario_engine.py` | 三级寻优与回退链、`max_evaluations` 硬上限、方案比较、五段式解释 |
| ★ V2 集成与一致性 | `test_v2_integration.py`、`test_result_wiring.py`、`test_project_type_alignment.py` | 守恒、口径归一化（缺陷 ⑬）、`CalculationResult` 字段贯通 |
| ★ V2 界面 | `test_gui_v2.py` | 时序页、PyQtGraph 图表、界面值与结果一致、界面不计算 |
| ★ V2 报表 | `test_report_v2.py` | Excel 24 表 / PDF 16 部分与 `CalculationResult` 逐位一致 |
| ★ V2 性能 | `test_v2_performance.py` | 性能预算（多次取样取最小值） |
| ★ V2 迁移 | `test_migration.py` | `1.0`/`1.1` → `2.0` 迁移、留痕、未知版本拒载 |

### 8.2 测试规模（实测）

| 项 | 数量 |
|---|---|
| 测试文件 | **32 个**（`tests/test_*.py`；另有 `conftest.py`） |
| 展开后的用例总数 | **885** |
| 执行结果 | **885 passed, 0 failed**（Python 3.12 + pytest 9.1.1，`QT_QPA_PLATFORM=offscreen`） |
| 被测源码规模 | `src/cenep/`（11 个包：V1 的 8 个 + V2 新增 `optimization/`、`data/`，以及 `ui/charts.py` 等扩展） |
| 测试代码规模 | `tests/`（32 个文件） |

> V1 交付时为 **263 个用例 / 16 个文件**；V2 交付为 **885 个用例 / 32 个文件**，
> 净增 **622 个用例**，其中 **16 个全新文件**（V2 专用），其余增量分布在原有文件
> （如 `test_engine_golden.py` 29→31、`test_project_file.py` 22→25、
> `test_excel_export.py` 14→19、`test_pdf_export.py` 15→19）。
> **V1 用例无删除、无放宽**。

V2 新增用例的分布（逐文件，与 `pytest --collect-only` 实测一致）：

| 文件 | 用例 | 文件 | 用例 |
|---|---|---|---|
| `test_data_layer.py` | 107 | `test_gui_v2.py` | 45 |
| `test_profile_engines.py` | 66 | `test_scenario_engine.py` | 45 |
| `test_dispatch_engine.py` | 43 | `test_timeseries_models.py` | 43 |
| `test_report_v2.py` | 41 | `test_timeseries_engine.py` | 40 |
| `test_storage_soc.py` | 32 | `test_optimization.py` | 30 |
| `test_v2_integration.py` | 28 | `test_project_file.py` | 25 |
| `test_migration.py` | 20 | `test_dispatch_semantics.py` | 19 |
| `test_project_type_alignment.py` | 17 | `test_result_wiring.py` | 14 |
| `test_v2_performance.py` | 12 | `test_engine_golden.py` | 31 |

> 完整逐文件清单（32 个文件全部列出）见 [TEST_PLAN.md](TEST_PLAN.md) 第 8 节。

> **环境说明（重要）**：本机依赖不是装在虚拟环境，而是装在项目内 `.pylibs`（走清华镜像，
> 因为 `python -m venv` 的 `ensurepip` 被环境策略拦截）。因此运行 pytest 时必须让
> `.pylibs` 与 runtime 的解释器**同时**可用（用 `PYTHONPATH=".pylibs;src"` 调用
> runtime 的 `python.exe`）。若误用系统默认的 Python 3.14，会因 `pydantic_core`
> 是 cp312 而无法导入。另外 GUI 测试需要 `QT_QPA_PLATFORM=offscreen`。
>
> 完整复现命令见 `TEST_PLAN.md` 第 1 节。**以实测 32 个测试文件 / 885 个用例 /
> 885 passed（0 failed）为准。**

### 8.3 关键测试断言（架构相关）

| 断言 | 位置 | 保障的设计约束 |
|---|---|---|
| 同一输入重复 100 次结果完全一致 | `test_engine_golden.py` | 引擎无状态、可重入（§118） |
| `CalculationResult` 数值 == Excel 单元格数值 | `test_excel_export.py` | 报表不重算（§109） |
| Excel 中不存在 `=` 开头单元格 | `test_excel_export.py` | 报表不写公式 |
| `apply_delta` 不修改基准项目 | `test_scenario_sensitivity.py` | 深拷贝隔离（§93） |
| 情景顺序 `["BASE","CONSERVATIVE","OPTIMISTIC"]` | `test_scenario_sensitivity.py` | 全部从 BASE 复制 |
| 0% 敏感性行等于基准值 | `test_scenario_sensitivity.py` | 单变量施加正确（§95） |
| 计算耗时 < 5 秒 | `test_application.py` | 性能要求（§155） |
| 报错含中文且 `field` 正确 | `test_validation.py`、`test_application.py` | 友好报错（§132） |
| `.nep` 往返后 `model_dump()` 完全相等 | `test_project_file.py` | 序列化无损 |
| 旧政策版本不被覆盖 | `test_project_file.py` | 版本只新增（§35） |

---

## 9. 当前实现状态总表

| 能力 | 状态 | 位置 |
|---|---|---|
| 数据模型（Pydantic） | ✅ 已实现 | `domain/` |
| 统一计算引擎（31 步） | ✅ 已实现 | `calculation/engine.py` |
| 校验与中文报错 | ✅ 已实现 | `calculation/validator.py` |
| 情景 / 敏感性分析 | ✅ 已实现 | `calculation/scenario.py`、`sensitivity.py` |
| 参数来源与可追溯 | ✅ 已实现 | `domain/provenance.py` |
| 应用服务（编排/自动保存/日志） | ✅ 已实现 | `application/` |
| `.nep` 项目文件 | ✅ 已实现 | `infrastructure/project_file.py` |
| SQLite（政策版本/模板/参数字典/历史） | ✅ 已实现 | `infrastructure/db.py` |
| 日志 | ✅ 已实现 | `infrastructure/logging_setup.py` |
| 政策模板与版本机制 | ✅ 已实现（不预填数值） | `policy/` |
| Excel 导出（**V2：24 表**） | ✅ 已实现 | `reports/excel_exporter.py` |
| PDF 导出（**V2：16 部分** + 免责声明） | ✅ 已实现 | `reports/pdf_exporter.py` |
| GUI（PySide6） | ✅ 已实现 | `ui/`（app / main_window / pages / sections / field_spec / **charts**） |
| `python -m cenep` 与 `--selftest` 自检 | ✅ 已实现 | `src/cenep/__main__.py`、`selftest.py` |
| PyInstaller 打包 | ✅ **已构建并启动验证** | `build/CENEP.spec`、`build/entry.py`、`build/version_info.txt`（产物 `dist\CENEP\CENEP.exe`，版本资源 2.0.0） |
| 示例项目与导出产物 | ✅ 已实现 | `examples/`、`tools/make_examples.py` |
| ★ V2 8760 时序仿真 | ✅ 已实现 | `calculation/timeseries_engine.py`、`economic_v2.py` |
| ★ V2 三条曲线引擎（负荷/PV/分时电价） | ✅ 已实现 | `calculation/load_profile.py`、`pv_profile.py`、`tariff_series.py` |
| ★ V2 储能 SOC 与三策略调度 | ✅ 已实现 | `calculation/storage_soc.py`、`dispatch_engine.py` |
| ★ V2 能量守恒校验 | ✅ 已实现 | `calculation/energy_balance.py` |
| ★ V2 方案比较与三级寻优 | ✅ 已实现 | `calculation/scenario_engine.py`、`optimization/` |
| ★ V2 数据导入校验与质量评分 | ✅ 已实现 | `data/importer.py`、`validator.py`、`quality.py` |
| ★ V2 PyQtGraph 交互图表 | ✅ 已实现 | `ui/charts.py` |
| ★ V2 `.nep` 迁移器 | ✅ **已实现** | `infrastructure/migration.py`（`1.0`/`1.1` → `2.0`） |
| ★ V2 `ProjectType` 按容量归一化 | ✅ 已实现 | `application/calculation_service.py`、`calculation/scenario_engine.py` |
| `TEST_PLAN.md`（黄金案例手工推导过程） | ✅ **已存在** | 项目根目录（第 8 节为 V2 测试清单） |
| `utils/` 通用工具包 | ❌ **尚未实现** | 目录不存在 |
| 政策数值自动折算为电价 | ❌ 尚未实现 | 政策当前只参与登记与披露 |
| 多省份政策模板数据集 | ⚠️ 仅湖北占位模板 | `policy/hubei.py`（数值全为 `None`） |
| `templates/` 报表模板目录 | ❌ 不存在 | 报表在代码中直接构造 |

> **与早期项目概要的差异说明**：概要称"GUI、Excel/PDF 导出、.nep 项目文件、政策模板、
> 打包、迁移器尚未实现"。经逐文件核实，这些能力**均已实现**（含 `build/CENEP.spec`
> 打包配置与已构建的 `dist\CENEP\CENEP.exe`、以及 `.nep` 迁移器）。
> 真正尚未实现的是 **`utils/`** 与 **政策数值自动折算**。
> 本文档以磁盘上的真实代码为准（§157）。

---

## 10. 变更纪律

1. **改公式 → 改三处**：`calculation/` 代码、`CORE_PARAMETERS_AND_FORMULAS.md`、
   对应测试；并同步 `engine.py:_CALIBER_NOTES`。
2. **改分层 → 改本文档**：新增目录、新增层、改变依赖方向，必须更新第 2、3 节。
3. **新增报表 → 更新 6.3 节**与 `reports/__init__.py` 的 `__all__`。
4. **新增政策 → 更新 6.1 节**与政策文档；模板**永不预填数值**。
5. **不得为了赶进度把计算塞进展示层**：这是本项目唯一不可妥协的架构约束
   （README 铁律 4、§148–§150）。
6. 任何"文档与代码不一致"都必须停下并消解；优先级按 §157 判定。
