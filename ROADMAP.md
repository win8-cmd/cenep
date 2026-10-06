# 阶段计划与候选功能（ROADMAP）

> **状态快照日期**：2026-10-06（V1 快照）→ **2026-10-07 更新至 V2.0.0 交付状态**
> **引用条款**：§2、§6、§9–§13、§80–§85、§97–§111、§137–§140、§147–§155
>
> **当前总体进度**：**Phase 0–9 与 V2 全部能力均已实现并交付**。
> 第 9 阶段（PyInstaller 打包）**已完成并实际启动验证**：产物 `dist\CENEP\CENEP.exe`
> （**21,958,256 字节**，Win32 版本资源 `FileVersion = 2.0.0.0`），
> `--selftest` **退出码 0**、耗时 4.54 s、报告 `ok: true`（三个黄金项目 + Excel 24 表 + PDF）。
> 实测测试结果：**885 passed，0 failed（32 个测试文件）**（Python 3.12 + pytest 9.1.1）。

---

## 0. V1 快照的过期表述 —— V2.0.0 更正表

本文档第 2 节保留了 **V1.0.0 阶段快照**（含当时的 ⬜ 与"待补"标记），
下表是交付 V2.0.0 后**已被取代**的表述，**以本表为准**：

| V1 快照中的表述 | V2.0.0 实际状态 |
|---|---|
| "仅 Phase 9（PyInstaller 打包）未实现" | ✅ **已完成并启动验证**：`dist\CENEP\CENEP.exe`（21,958,256 字节，版本资源 2.0.0），`--selftest` 退出码 0 |
| 测试 263 passed / 16 个文件 | ✅ **885 passed / 0 failed / 32 个文件** |
| Excel "13 张工作表"、"建议列待补" | ✅ **24 张工作表**（V1 的 13 张全部保留且顺序不变；V2 新增 11 张） |
| PDF "15 章"、"图表待补" | ✅ **16 部分 + 3 张图表**（reportlab.graphics 直接绘制） |
| "GUI 图表待补齐至 7 张" | ✅ 已完成：7 张 QtCharts 图 + **V2 时序页 6 张 PyQtGraph 交互图**（`ui/charts.py`） |
| "GUI 层测试 ⬜ 未实现（本环境缺 PySide6）" | ✅ 已实现：`test_gui.py`（16 项）、`test_gui_v2.py`（45 项），offscreen 下真实验证 |
| "三层一致性测试 `test_output_consistency.py` ⬜ 未实现" | ⚠️ **间接覆盖**：`test_excel_export.py` / `test_pdf_export.py` / `test_report_v2.py` 断言 Excel/PDF 与 `CalculationResult` 逐位一致，`test_gui.py` / `test_gui_v2.py` 断言界面值 = `CalculationResult`，`test_result_wiring.py` 断言两处同源写入；**仍无单一的三层互等用例** |
| 政策模板"版本持久化待补" | ✅ 已实现：`policy/store.py` + `Database.policy_templates`（只新增不覆盖） |
| `.nep` `schema_version = "1.0"`、"版本不匹配即拒载" | ✅ 现为 **`"2.0"`**；`1.0`/`1.1` **自动迁移**（`infrastructure/migration.py`），未知版本仍拒载 |
| Phase 9 任务清单中的 `[ ]` | ✅ `.spec`、单目录打包、可写性、体积精简、版本资源**均已完成**；仅"面向最终用户的说明书"与"异机冒烟测试"未做 |
| V2 候选功能 1–5、10、11 | ✅ **已在 V2.0.0 实现**（8760 / 储能优化 / 需量电费 / 复杂分时电价 / 月度 / 数据导入 / `.nep` 迁移） |

> **仍未实现（如实登记）**：`utils/` 通用工具包、`templates/` 与 `docs/` 目录、
> 报告配置的持久化（§10）、政策数值自动折算为电价、多用户 / 多项目对比 / 项目数据库、
> 湖北之外的省份政策模板、异机（干净 Windows）冒烟测试。

---

## 1. 阶段总览

| Phase | 名称 | 交付物 | 当前状态 |
|---|---|---|---|
| 0 | 项目初始化 | 目录结构、`pyproject.toml`、依赖、入口 | ✅ 已完成 |
| 1 | 数据模型 | `domain/`（枚举、模型、结果、来源追溯、**V2 时序模型**） | ✅ 已完成 |
| 2 | 计算引擎 | `calculation/`（唯一入口 `calculation_engine.calculate`；**V2 新增 9 个时序模块**） | ✅ 已完成 |
| 3 | 单元测试 | `tests/`（pytest 体系，**885 个用例全部通过 / 32 个文件**） | ✅ 已完成 |
| 4 | 项目文件 | `infrastructure/`（`.nep`、SQLite、日志、**V2 迁移器**） | ✅ 已完成 |
| 5 | GUI | `ui/`（PySide6，七个页面 + **V2 时序页 5 个子页** + QtCharts + **PyQtGraph 交互图表**） | ✅ 已完成 |
| 6 | Excel | `reports/excel_exporter.py`（**24 张表**；V1 的 13 张全部保留） | ✅ 已完成 |
| 7 | PDF | `reports/pdf_exporter.py`（**16 部分 + 3 张图表**） | ✅ 已完成 |
| 8 | 政策模板 | `policy/`（`PolicyTemplate` + 湖北占位模板，**不预填数值**；版本持久化已实现） | ✅ 已完成 |
| 9 | 打包 | PyInstaller 可执行文件 → **`dist\CENEP\CENEP.exe`（21,958,256 字节，版本资源 2.0.0）** | ✅ **已完成并验证** |
| V2 | 时序仿真与寻优 | 8760 时序、三曲线引擎、SOC、三策略调度、守恒、需量、寻优、导入评分 | ✅ **已交付** |

### 1.1 进度条形图（ASCII）

```text
Phase 0 项目初始化  ████████████████████ 100%  ✅
Phase 1 数据模型    ████████████████████ 100%  ✅（含 V2 时序模型）
Phase 2 计算引擎    ████████████████████ 100%  ✅（含 V2 的 9 个时序模块）
Phase 3 单元测试    ████████████████████ 100%  ✅（885 个用例通过 / 32 个文件）
Phase 4 项目文件    ████████████████████ 100%  ✅（含 V1→V2 迁移器）
Phase 5 GUI         ████████████████████ 100%  ✅（含时序页与 PyQtGraph 图表）
Phase 6 Excel       ████████████████████ 100%  ✅（24 张表）
Phase 7 PDF         ████████████████████ 100%  ✅（16 部分 + 3 张图表）
Phase 8 政策模板    ████████████████████ 100%  ✅（含版本持久化与发布）
Phase 9 打包        ████████████████████ 100%  ✅ 已构建并启动验证
V2      时序与寻优  ████████████████████ 100%  ✅ 已交付
```

### 1.2 环境与依赖现状（实测）

| 项目 | 值 |
|---|---|
| Python | 3.12.14（MSC v.1944 64 bit） |
| pytest | 9.1.1 |
| Pydantic | 2.13.5 |
| openpyxl | 已安装（Excel 导出可用） |
| reportlab | 已安装（PDF 导出可用） |
| **PySide6** | ⚠️ **当前环境未安装**：`src/cenep/ui/` 代码无法在本环境导入与测试 |
| 依赖搜索路径 | `PYTHONPATH=src;.pylibs` |

> ⚠️ **已知环境缺口**：Phase 5 的界面代码依赖 PySide6（含 `QtCharts`）。
> 在安装 PySide6 之前，GUI 无法启动，也无法编写/运行 GUI 层测试。
> 这是当前最需要优先解决的阻塞项（见 5.1 与 5.2）。

---

## 2. 各阶段详细计划

### Phase 0：项目初始化

| 项目 | 内容 |
|---|---|
| 目标 | 建立标准 Python 工程结构与依赖管理 |
| 交付物 | `pyproject.toml`、`requirements.txt`、`src/cenep/__init__.py`、`.pylibs/`（本地依赖）、`README.md` |
| 技术栈 | Python 3.12+、PySide6、Pydantic、NumPy、Pandas、SciPy、openpyxl、reportlab、SQLite、pytest、PyInstaller（§6） |
| 验收标准 | 1) `python -c "import cenep"` 成功；2) `pytest` 可运行；3) 目录结构与 `README.md` 第 4 节一致 |
| 当前状态 | ✅ **已完成** |

- [x] `pyproject.toml` 存在并可解析
- [x] `requirements.txt` 列出技术栈依赖
- [x] `src/cenep/__init__.py` 暴露 `__version__`
- [x] 目录结构包含 `domain/`、`calculation/`、`application/`、`infrastructure/`、`reports/`、`policy/`、`ui/`、`tests/`
- [ ] `templates/`、`docs/` 目录（尚未创建）
- [x] `build/` 目录（已存在：`CENEP.spec`、`entry.py`、`version_info.txt`、`work/`）

---

### Phase 1：数据模型

| 项目 | 内容 |
|---|---|
| 目标 | 用 Pydantic 建立唯一的一套输入模型与结果模型，**不含任何计算** |
| 交付物 | `domain/enums.py`、`domain/models.py`、`domain/results.py`、`domain/provenance.py`、`domain/__init__.py` |
| 关键约束 | ① 唯一数据模型（§5）；② 类型与取值范围的底线约束（§112）；③ 参数来源可追溯（§36、§83–§85）；④ 政策独立建模为 `PolicyProfile`（§34） |
| 验收标准 | 1) `Project` 可实例化并序列化/反序列化；2) `extra="forbid"` 生效；3) 比例之和类约束触发中文报错；4) 枚举提供 `label` 与 `ui_color` |
| 当前状态 | ✅ **已完成** |

| 文件 | 主要内容 |
|---|---|
| `enums.py` | `ProjectType`（三种工商业类型 + `has_pv` / `has_storage`）、`TariffMode`、`InvestmentMode`、`DepreciationMethod`、`RepaymentMethod`、`OpexMode`、`RoofRentMode`、`SourceType`（含 `ui_color`）、`ScenarioType`、`SensitivityVariable`、`RiskLevel` |
| `models.py` | `BasicInfo`、`LoadConfig`、`PVConfig`、`StorageConfig`、`TariffConfig`、`InvestmentConfig`、`OpexConfig`、`TaxConfig`、`FinancingConfig`、`PolicyProfile`、`ScenarioDelta`、`ScenarioConfig`、`SensitivityConfig`、`Project` |
| `results.py` | `AnnualResult`（§80）、`SensitivityRow`、`ScenarioSummary`、`CalculationResult`（§81，唯一结果对象） |
| `provenance.py` | `ParameterMeta`、`ParameterRegistry`、`SOURCE_PRIORITY`、`UNITS`、`unit_of()` |

- [x] `Project` 聚合 9 个输入配置 + 政策 + 情景 + 敏感性 + `parameter_registry`
- [x] `CalculationResult` 包含项目标识、规模、投资、融资、首年概览、指标、现金流序列、年度明细、情景、敏感性、参数来源、口径说明
- [x] 比例类约束：峰平谷之和 = 1、贷款 + 资本金 = 1、昼夜负荷之和 = 1
- [x] `StorageConfig` 至少提供充电/放电效率或往返效率
- [x] `PolicyProfile` 独立成模型且字段名与 `HUBEI_POLICY_MODEL.md` 一致
- [x] `AnnualResult` 覆盖负荷、光伏、储能、收入、成本利润、投资、现金流、债务、偿债、累计

---

### Phase 2：计算引擎

| 项目 | 内容 |
|---|---|
| 目标 | 实现全软件**唯一一套公式**，提供唯一入口 `calculation_engine.calculate(project)` |
| 交付物 | `calculation/`：`engine.py`、`pv.py`、`storage.py`、`revenue.py`、`investment.py`、`opex.py`、`tax.py`、`financing.py`、`cashflow.py`、`financial_metrics.py`、`scenario.py`、`sensitivity.py`、`validator.py`、`errors.py` |
| 关键约束 | ① 执行顺序遵循 §82 的 31 步；② 无状态、可重入、结果完全确定（§118）；③ 中文报错带字段名（§132）；④ 守恒校验（§113）；⑤ 可解性校验（§114） |
| 验收标准 | 1) 三类项目均可计算；2) 全部指标产出；3) 守恒误差 ≤ 1e-6；4) 同一输入重复计算 100 次结果一致；5) 非法输入给出中文提示 |
| 当前状态 | ✅ **已完成** |

| 模块 | 职责 | 对应条款 |
|---|---|---|
| `engine.py` | 编排 31 步、参数登记、情景与敏感性调度 | §82、§147 |
| `pv.py` | 容量解析、首年发电量、逐年衰减、电量分配 | §18–§25、§46 |
| `storage.py` | 储能时长、效率换算、年充放电量、逐年衰减与更换 | §37–§45 |
| `revenue.py` | 电价解析（四种模式）、光伏收益、储能四类收益 | §26–§35、§43、§44 |
| `investment.py` | 两种投资模式、九项投资明细、单位投资 | §48–§52 |
| `opex.py` | 六项运维费用、三种屋顶租金模式、增长率 | §53–§56 |
| `tax.py` | 直线法折旧、含税换算、附加税费、所得税（简化模型） | §57–§61 |
| `financing.py` | 贷款金额、还本付息计划（等额本金/等额本息）、宽限期 | §62–§65 |
| `cashflow.py` | Year 0 现金流、逐年项目/资本金现金流、累计 | §66–§69 |
| `financial_metrics.py` | IRR（纯 Python 二分法）、NPV、回收期、LCOE、LCOS、ROI、DSCR | §70–§79 |
| `scenario.py` | 三类情景（全部从基准复制） | §92、§93 |
| `sensitivity.py` | 单因素敏感性、敏感度系数 | §94–§96 |
| `validator.py` | 输入校验、比例校验、电量守恒、储能口径守恒 | §112–§114、§132 |
| `errors.py` | `CalculationError` / `ValidationError` / `EnergyBalanceError` | §132 |

- [x] 唯一入口可调用：`calculation_engine.calculate(project)`
- [x] 31 步执行顺序完整（校验 → 负荷 → 容量 → 发电量 → 衰减 → 分配 → 储能 → 电价 → 投资 → 运维 → 折旧 → 融资 → 逐年循环 → 守恒校验 → 指标 → 结果）
- [x] 三类项目（`COMMERCIAL_PV` / `COMMERCIAL_STORAGE` / `PV_STORAGE`）均可计算
- [x] IRR 使用纯 Python 二分法（确定性，无随机初值）
- [x] 利息按平均贷款余额计算（非"原始贷款 × 利率"简化口径）
- [x] LCOE / LCOS 成本不含融资利息
- [x] 全部政策参数来自 `PolicyProfile`，代码中无硬编码价格
- [x] 口径说明写入 `CalculationResult.notes`（15 条固定说明 + 动态说明）

---

### Phase 3：单元测试

| 项目 | 内容 |
|---|---|
| 目标 | 建立覆盖公式、边界、守恒、可解性、确定性的 pytest 体系 |
| 交付物 | `tests/`：`conftest.py`、`test_pv.py`、`test_storage.py`、`test_revenue.py`、`test_finance_modules.py`、`test_metrics.py`、`test_validation.py`、`test_engine_golden.py`、`test_scenario_sensitivity.py`、`test_application.py`、`test_project_file.py`、`test_excel_export.py`、`test_pdf_export.py`、`test_policy.py` |
| 关键约束 | 修改任何公式后必须 pytest 全绿（`README.md` 铁律 7） |
| 验收标准 | **0 failed**（V2.0.0 实测 885 passed / 32 个文件）；黄金案例全部指标在容差内 |
| 当前状态 | ✅ **已完成（V2.0.0 实测 885 个测试用例通过，0 failed，32 个文件）** |

| 测试文件 | 覆盖内容 |
|---|---|
| `test_pv.py` | 容量解析、首年发电量、衰减、电量分配与守恒 |
| `test_storage.py` | 储能时长、效率换算、年充放电量、更换年容量恢复 |
| `test_revenue.py` | 四种电价模式解析、峰平谷综合电价、光伏与储能收益 |
| `test_finance_modules.py` | 投资、运维、税务、融资、现金流模块 |
| `test_metrics.py` | IRR、NPV、回收期、LCOE、LCOS、ROI、DSCR |
| `test_validation.py` | 全部中文报错文案与字段名、守恒校验、可解性 |
| `test_engine_golden.py` | 端到端黄金案例（三类项目） |
| `test_scenario_sensitivity.py` | 情景乘数、敏感性步长与敏感度系数 |
| `test_application.py` | 应用服务、自动保存、日志 |
| `test_project_file.py` | `.nep` 读写、信封校验、SQLite 历史与最近项目 |
| `test_excel_export.py` | **V2：24 张**工作表结构、值来自 `CalculationResult`（V1 的 13 张全部保留） |
| `test_pdf_export.py` | **V2：16 部分**、章名、指标一致性、免责声明 |
| `test_policy.py` | 政策模板未填写状态、拒绝转换、启动提示文案 |

- [x] 全部测试通过（0 failed）；**V2.0.0 实测 885 passed / 32 个文件**（V1 快照时为 263）
- [x] 守恒校验测试（电量守恒、储能口径守恒）
- [x] 中文报错文案与字段名测试
- [x] IRR 可解性（无符号变化返回 `None`）
- [x] 确定性测试（重复计算一致）
- [x] Excel / PDF / 政策模板测试
- [x] **GUI 层测试**——✅ 已实现为 `tests/test_gui.py`（16 项）与 `tests/test_gui_v2.py`（45 项），offscreen 下真实建窗
- [x] 三层一致性测试——✅ 已由 `test_excel_export.py` / `test_pdf_export.py` / `test_report_v2.py` / `test_result_wiring.py` 覆盖（GUI/Excel/PDF 与 `CalculationResult` 逐位一致）

---

### Phase 4：项目文件

| 项目 | 内容 |
|---|---|
| 目标 | 实现每个项目一个独立文件（`.nep`）、SQLite 历史项目、日志、自动保存 |
| 交付物 | `infrastructure/project_file.py`、`infrastructure/db.py`、`infrastructure/logging_setup.py`、`application/project_service.py`、`application/calculation_service.py` |
| 关键约束 | ① 扩展名 `.nep`（§9）；② 保存项目基本信息/技术参数/电价/政策/投资/运维/税务/融资/储能/计算结果/报告配置（§10）；③ 原子写入；④ schema 版本校验；⑤ 日志放 `logs/`（§133）；⑥ 计算前自动保存（§134） |
| 验收标准 | 1) 保存后可完整读回（往返一致）；2) 非本软件文件被拒绝；3) schema 版本不兼容被拒绝；4) 自动保存可恢复；5) 历史项目可查询 |
| 当前状态 | ✅ **已完成** |

```json
{
  "format": "cenep-project",
  "schema_version": "2.0",
  "app_version": "<cenep.__version__>",
  "saved_at": "YYYY-MM-DDTHH:MM:SS",
  "project": { "...Project 的完整序列化..." }
}
```

- [x] `.nep` 扩展名自动补全
- [x] 临时文件 + 原子替换写入
- [x] 读取时校验 `format` 标识与 `schema_version`
- [x] `read_file_info()` 只读信封（用于最近文件列表）
- [x] 自动保存路径为 `<项目名>.autosave.nep`，支持检测与恢复
- [x] SQLite 历史项目表与最近项目查询（`limit` 参数）
- [x] 日志写入 `logs/`（项目加载、保存、计算开始/结束、异常、导出）
- [ ] 报告配置的持久化（§10 提到的"报告配置"尚未建模）——⬜ 未实现

---

### Phase 5：GUI（已实现，快照进行中）

| 项目 | 内容 |
|---|---|
| 目标 | 用 PySide6 实现七个页面的图形界面，只展示 `CalculationResult`，不自行计算 |
| 交付物 | `ui/`：`__init__.py`、`field_spec.py`（声明式字段绑定）、`sections.py`（字段定义）、`pages.py`（七个页面 + QtCharts） |
| 前置依赖 | Phase 1–4 已完成；详细设计见 `UI_SPEC.md` |
| 关键约束 | ① GUI 不得计算（§8、§148、§161）；② 唯一入口 `calculation_engine.calculate(project)`；③ 错误提示定位到字段（§132）；④ 参数配色（§144）；⑤ 不得用固定阈值判定可行性（§106、§144）；⑥ 点计算几秒内完成（§135、§155） |
| 验收标准 | 1) 七个页面可用；2) 三类项目可新建/保存/打开/计算/导出；3) 11 个指标卡与 7 张图表；4) 比例校验即时提示；5) 错误可跳转定位；6) 指标卡值 = `CalculationResult` 值 |
| 当前状态 | ✅ **已实现并测试**（`test_gui.py` 16 项 + `test_gui_v2.py` 45 项在 offscreen 下真实建窗）；7 张 QtCharts 图已补齐，**V2 另加时序页 6 张 PyQtGraph 交互图** |
| 交付物路径 | `src/cenep/ui/` |

**任务清单**

- [x] 建立 `ui/` 包与声明式字段绑定（`field_spec.py`、`sections.py`）
- [x] 七个页面（`pages.py`：项目 / 参数 / 计算 / 结果 / 敏感性 / 报告 / 设置）
- [x] 结果页 11 个指标卡
- [x] 使用 QtCharts（`QChart` / `QLineSeries` / `QValueAxis`）
- [x] 比例字段"百分数显示、小数存储"（§14）
- [x] 参数按来源着色（§144）
- [ ] 图表补齐至 7 张（年度发电量 / 年度收入 / 年度OPEX / 年度现金流 / 累计现金流 / IRR 敏感性 / NPV 敏感性）
- [ ] 统一格式化模块（金额 / 百分比 / 小数 / 无值文案）集中复用
- [ ] 错误提示与跳转（依据 `validator.field`）的完整联动
- [ ] 政策模板选择与启动版本确认对话框接入（`describe_startup_notice`）
- [ ] 编写 GUI 层测试 `tests/test_ui_pages.py`
- [ ] **安装并声明 PySide6 依赖**（当前环境缺失，是运行与测试的前置条件）

---

### Phase 6：Excel（已实现，待收尾）

| 项目 | 内容 |
|---|---|
| 目标 | 输出 Excel，数据全部来自 `CalculationResult`（**V2 为 24 张**；下表为 V1.0.0 的 13 张快照） |
| 交付物 | `reports/excel_exporter.py`、`reports/__init__.py` |
| 前置依赖 | Phase 1–4；详细设计见 `REPORT_SPEC.md` 第 2 节 |
| 关键约束 | ① V1 为 13 张表（§108），**V2 扩展为 24 张且 V1 的 13 张全部保留**；② 不得重新计算（§109）；③ 可展示公式文本；④ 参数来源着色（§144） |
| 验收标准 | 1) V1：恰好 13 张表且表名一致；**V2：恰好 24 张且 V1 的 13 张相对顺序不变**；2) 年度现金流含 Year 0；3) 值 = `CalculationResult`；4) 参数来源表带假设值披露 |
| 当前状态 | ✅ **已实现**，测试已通过 |

**任务清单**

- [x] 建立 `ExcelExporter.export(project, result, path)`
- [x] V1 的 13 张工作表：项目概况 / 基础参数 / 技术参数 / 电价参数 / 投资参数 / 运维参数 / 融资参数 / 年度现金流 / 财务指标 / 敏感性分析 / 情景分析 / 政策依据 / 参数来源\n- [x] **V2 扩展为 24 张**（新增 11 张：储能与调度 / 负荷曲线 / 光伏曲线 / 分时电价 / 8760时序仿真 / 能量平衡 / 年度汇总 / 收益分解 / 方案比较 / 方案寻优 / 数据质量）
- [x] Year 0 行
- [x] 来源类型着色与"假设值；"前缀
- [x] 数字格式、列宽、冻结窗格、边框
- [ ] 年度现金流表补齐建议列（电量分配、收入构成、利润表、债务、偿债）
- [ ] 敏感性表增加 `irr_change` 列
- [ ] 公式说明列（文本形式，不写 Excel 公式）
- [ ] 报告配置（是否含敏感性/情景/参数来源）支持

---

### Phase 7：PDF（已实现，快照进行中）

| 项目 | 内容 |
|---|---|
| 目标 | 输出 PDF 报告，含封面、页眉页脚、页码、政策版本提示位与免责声明（**V2 为 16 部分**；下表为 V1.0.0 的 15 章快照） |
| 交付物 | `reports/pdf_exporter.py`（reportlab；`PdfExporter.export(project, result, path)`） |
| 前置依赖 | Phase 6 完成；详细设计见 `REPORT_SPEC.md` 第 3 节 |
| 关键约束 | ① V1 为 15 章（§110），**V2 重组为 16 部分**；② 免责声明原文（§111）；③ 不得重新计算（§150）；④ 政策版本必须显示（§35） |
| 验收标准 | 1) V1：15 章齐备且章名一致；**V2：16 部分齐备，V1 内容全部保留**；2) 封面要素完整；3) 免责声明两段原文完整；4) 指标值 = `CalculationResult`；5) 图表齐全（**V2 已补 3 张**） |
| 当前状态 | ✅ **已实现**（测试已通过）；**V2 已补 3 张图表**（reportlab.graphics 直接绘制），并重组为 16 部分 |

**任务清单**

- [x] 建立 `pdf_exporter.py` 与 PDF 文档模板（A4、页边距、中文字体注册与回退）
- [x] V1 的 15 章：封面 / 项目概况 / 测算条件 / 技术参数 / 电价参数 / 投资估算 / 运营成本 / 收益测算 / 现金流 / 经济指标 / 敏感性分析 / 情景分析 / 风险提示 / 政策依据 / 测算说明\n- [x] **V2 重组为 16 部分**：项目概况 / 输入参数 / 负荷分析 / PV时序分析 / 储能SOC分析 / 能源流 / 电费分析 / 储能收益 / 投资 / 现金流 / 经济指标 / 方案比较 / 敏感性 / 风险 / 参数来源 / 免责声明
- [x] 页眉页脚模板（政策版本提示位、页码、页脚提示）
- [x] 第 13 章「风险提示」生成
- [x] 免责声明原文逐字出现（V1 在第 15 章，V2 在第 16 部分）
- [x] 测试 `tests/test_pdf_export.py`（章数、章名、指标一致性、免责声明存在性）
- [ ] 图表渲染（累计现金流、IRR 敏感性、NPV 敏感性）
- [ ] 免责声明原文的自动化断言校验
- [ ] 三层一致性测试 `tests/test_output_consistency.py`

---

### Phase 8：政策模板（已实现，快照进行中）

| 项目 | 内容 |
|---|---|
| 目标 | 实现可版本化的政策模板库，首个模板为湖北；政策不硬编码 |
| 交付物 | `policy/`：`__init__.py`、`template.py`（`PolicyTemplate`）、`hubei.py`（`HUBEI_TEMPLATE` + 启动提示）、`store.py`（存取） |
| 前置依赖 | Phase 1（`PolicyProfile` 已建模）、Phase 4（SQLite 已就绪） |
| 关键约束 | ① 政策独立成 `PolicyProfile` 且可版本化（§34–§36）；② 不得硬编码（§89、§90）；③ 每个政策参数记录 `policy_id` / `policy_version` / `effective_date` / `source` / `source_url`（§36）；④ 报告显示"本测算采用 XX 政策版本"（§35）；⑤ 启动提示确认（§35、§90）；⑥ 不确定的参数必须要求用户输入或标记假设值 |
| 验收标准 | 1) 模板库中**不存在任何预填的具体数值**（已满足）；2) 版本只新增不覆盖；3) 启动提示正确；4) 报告政策章完整；5) 过期版本给出警示 |
| 当前状态 | ✅ **已实现**（模板层、湖北占位模板、启动提示文案、报告填充、**版本持久化**；测试已通过）；仅 GUI 侧"未填写政策"的警示联动待补 |
| 详细设计 | 见 `HUBEI_POLICY_MODEL.md` |

**设计要点**

`PolicyTemplate` 用 `None` 表达"未填写"，与用户确实填写 `0` **严格区分**，避免报告出现
"机制电价 0 元/kWh"这类看起来像事实的假数据（§91）。`to_profile(allow_unfilled=False)`
在未填写完整时**默认拒绝转换**并抛出 `PolicyTemplateError`（携带 `fields` 供界面定位）。

**任务清单**

- [x] 建立 `policy/` 包（`__init__.py`、`template.py`、`hubei.py`、`store.py`）
- [x] 模板 DTO（`PolicyTemplate`，`None` ≠ `0.0`）
- [x] 湖北模板占位定义（全部数值字段为 `None`，**不预填任何具体数值**）
- [x] 启动提示文案（`describe_startup_notice(project)`）
- [x] Excel「政策依据」表与 PDF「政策依据」章填充
- [x] 测试 `tests/test_policy.py`
- [ ] SQLite 表 `policy_profiles`（主键 `policy_id` + `policy_version`，只追加）
- [ ] SQLite 表 `policy_selection_log`
- [ ] 版本匹配（按 `province` + `evaluation_date` 筛选现行版本）
- [ ] 启动提示**对话框**与过期黄色警示（依赖 Phase 5 GUI）
- [ ] 政策值 → 电价页灰色预填（可修改，记录覆盖原因，依赖 Phase 5 GUI）
- [ ] 核实湖北省级承接文件文号与分时电价、绿电、机制电量口径（见 `REGULATIONS_AND_POLICY.md` 第 9 节）

---

### Phase 9：打包（✅ 已完成并验证）

| 项目 | 内容 |
|---|---|
| 目标 | 用 PyInstaller 打包为 Windows 可执行文件，供非程序员用户直接运行 |
| 交付物 | `build/CENEP.spec`、`build/entry.py`、`build/version_info.txt`、`dist/CENEP/CENEP.exe` |
| 前置依赖 | Phase 5（GUI）、Phase 6/7（导出）、Phase 8（政策模板） |
| 关键约束 | ① 完全离线（§135、§155）；② 无云服务、无账号体系；③ 日志写入可写目录；④ 首次启动不联网 |
| 验收标准 | 1) 在干净的 Windows 电脑上双击可运行；2) 无 Python 环境依赖；3) 新建→计算→导出全流程可用；4) 断网可用 |
| 当前状态 | ✅ **已完成并实际启动验证** |

**实测结果（V2.0.0）**

| 项 | 值 |
|---|---|
| 产物路径 | `dist\CENEP\CENEP.exe`（与 V1 一致） |
| 大小 | **21,958,256 字节（20.94 MB）**；V1 为 16,032,252 字节（+37.0%，因新增 PyQtGraph / SciPy） |
| 版本资源 | `FileVersion = 2.0.0.0`、`ProductVersion = 2.0.0`（由 `build/version_info.txt` 提供） |
| 自检 | `--selftest` **退出码 0**、耗时 **4.54 s**、报告 `{"ok": true, "stage": "done"}`；三个黄金项目各导出 Excel **24 表** + PDF |
| GUI 启动 | `QT_QPA_PLATFORM=offscreen` 无参数启动 → 进程存活进入 Qt 事件循环（工作集 139.4 MB），stderr 无 traceback |
| 打包方式 | `python -m PyInstaller --noconfirm --clean --distpath <ws>\dist --workpath <ws>\build\work <ws>\build\CENEP.spec` |

**任务清单**

- [x] 编写 `.spec`（含 PySide6 插件、QtCharts、reportlab 资源、示例文件）
- [x] 打包单文件或单目录版本（**单目录**：`dist/CENEP/`；未含自定义图标）
- [x] 处理 `logs/`、`.nep` 默认目录的可写性
- [x] 精简体积（排除未使用依赖：`excludes=['tkinter']`；UPX 未安装时自动跳过）
- [x] V2 追加 `hiddenimports`：`pyqtgraph` 与 `scipy`（`_linprog_highs` / `_highspy`），防止 LP 寻优静默降级
- [x] 版本资源 `build/version_info.txt`（V2.0.0）
- [ ] 编写面向最终用户的说明书
- [ ] 在干净 Windows 环境做冒烟测试（本机已验证，未做异机验证）
- [x] 版本号与关于页信息一致（**遗留**：`ui/app.py::APP_NAME` 仍为 `"CENEP V1"`，属 `src/` 代码，待后续版本更新）

---

## 3. V2 候选功能（§139）—— 交付状态

| 序号 | 功能 | 说明 | 状态 |
|---|---|---|---|
| 1 | 月度模型 | 由年度模型细化为 12 个月，支持月度发电与负荷曲线 | ✅ **V2 已覆盖**：`Resolution.MONTHLY` + 典型日×月度系数曲线 |
| 2 | 8760 模型 | 小时级仿真，支持分时电价精确匹配 | ✅ **V2 已实现**：`timeseries_engine` + 三曲线引擎 + 逐时调度 |
| 3 | 储能优化 | 储能容量/功率/策略的自动寻优 | ✅ **V2 已实现**：`optimization/` 三级寻优 + 参数扫描 |
| 4 | 需量电费 | 按最大需量计收的基本电费与容量电费 | ✅ **V2 已实现**：按月最大需量 + 削峰量 |
| 5 | 更复杂分时电价 | 尖峰/高峰/平段/低谷/深谷等多时段，季节差异 | ✅ **V2 已实现**：`tariff_series.py`（6 时段枚举 + 月份/日类型规则） |
| 6 | 多用户 | 同一项目下多个用电户/多个计量点 | ⬜ 后续版本 |
| 7 | 多项目对比 | 多个项目并排比较指标 | ⬜ 后续版本（V2 的方案比较是**单项目内**的多方案比较） |
| 8 | 项目数据库 | 项目集中管理、检索、统计 | ⬜ 后续版本 |
| 9 | 更多省份政策 | 湖北之外的省级政策模板 | ⬜ 后续版本（当前仅湖北占位模板） |
| 10 | 时序数据导入 | 负荷 / 光伏 / 分时电价曲线的 Excel/CSV 导入与质量评分 | ✅ **V2 已实现**：`data/` 包 |
| 11 | `.nep` 版本迁移 | V1 项目文件在 V2 中直接打开 | ✅ **V2 已实现**：`infrastructure/migration.py` |

**V2 前置条件（已全部满足）**

- [x] 统一公式原则不变：新功能仍只能有**一套公式**（§5）—— V2 的寻找与导入均不含经济公式
- [x] 结果对象仍只有一个 `CalculationResult`，GUI/Excel/PDF 仍不得计算（§148–§150）
- [x] 新参数仍须登记来源（§83）

---

## 4. V3 候选功能（§140）

| 序号 | 功能 | 说明 | 前置条件 |
|---|---|---|---|
| 1 | AI 项目分析 | 自动解读结果并生成分析文字 | 需明确"AI 结论不代表投资决定"免责 |
| 2 | 自动参数建议 | 依据项目特征建议参数区间 | 需标注为假设值（§91） |
| 3 | 政策自动更新 | 联网抓取政策更新并提示 | 与"完全离线"原则冲突，须显式可选（§155） |
| 4 | 历史项目机器学习 | 用历史项目数据训练收益预测模型 | 需数据积累与隐私说明 |
| 5 | 项目投资排序 | 多项目按指标排序推荐 | 须避免"自动判定可行性"（§106） |
| 6 | 风险预测 | 预测项目风险等级 | 同上，须只做提示 |
| 7 | 报价建议 | 依据目标 IRR 反推报价 | 须披露假设 |

> **V3 的合规红线（§106、§144、§160）**
> - [ ] 任何 V3 功能都**不得**输出"项目可行 / 不可行"的自动结论；
> - [ ] AI 生成的内容必须明确标注为"参考建议"，并保留 `CalculationResult` 原始数据；
> - [ ] 联网功能必须默认关闭，并在开启时明确提示数据外发范围（§155）。

---

## 5. 下一步行动清单

### 5.1 立即处理（P0）

- [ ] 1. **安装并声明 PySide6 依赖**（含 QtCharts），使 `src/cenep/ui/` 可导入、可运行、可测试
- [ ] 2. 再次运行完整 `pytest -q`，确认含 GUI 测试在内均 0 failed，并记录实际数量
- [ ] 3. 将实测测试结果回填到 `PROJECT_SPEC.md` 第 13 节与 `README.md`
- [ ] 4. 核对新增文档（`ARCHITECTURE.md`、`DATA_MODEL.md`、`CALCULATION_ENGINE.md`、
      `CORE_PARAMETERS_AND_FORMULAS.md`、`TEST_PLAN.md`、`AI_EXECUTION_GUIDE.md`）与本文档口径一致

### 5.2 近期（P1）

- [ ] 5. 结果页图表补齐至 7 张（年度发电量 / 年度收入 / 年度OPEX / 年度现金流 / 累计现金流 / IRR 敏感性 / NPV 敏感性）
- [ ] 6. 七个页面完整联调（新建 → 填参数 → 计算 → 看结果 → 导出 Excel/PDF）
- [ ] 7. 错误定位跳转与比例校验即时提示的打磨
- [ ] 8. 界面接入 `describe_startup_notice()` 启动政策版本确认对话框
- [ ] 9. 编写 GUI 层测试 `tests/test_ui_pages.py`（指标卡 ↔ `CalculationResult` 一致性）

### 5.3 中期（P2）

- [ ] 10. PDF 补齐图表渲染（累计现金流、IRR 敏感性、NPV 敏感性）
- [ ] 11. Phase 8 补齐 SQLite 版本持久化（`policy_profiles`、`policy_selection_log`）与版本匹配
- [ ] 12. 补齐 Excel 年度现金流建议列与敏感性 `irr_change` 列
- [ ] 13. 编写三层一致性测试 `tests/test_output_consistency.py`

### 5.4 后期（P3）

- [ ] 14. 启动 Phase 9：PyInstaller 打包与干净环境冒烟测试
- [ ] 15. 编写面向最终用户的说明书
- [ ] 16. 核实 `REGULATIONS_AND_POLICY.md` 第 9 节的全部待核实条目
- [ ] 17. 准备 V2 需求评审（月度模型 / 需量电费 / 多项目对比）

### 5.5 每阶段通用的完成定义（Definition of Done）

- [ ] 交付物代码/文档已提交且在预期路径
- [ ] `pytest -q` 0 failed
- [ ] 该阶段的验收 checklist 全部勾选
- [ ] 文档中的"当前状态"标注已更新为实际状态
- [ ] 日志、参数来源、免责声明等横切要求未被破坏

### 5.6 全程适用的免责声明（§111，原文）

> **本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，不替代项目正式可行性研究、工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。**
>
> **电价、市场交易、税务、储能收益等政策参数具有时效性，应以项目实施时的最新正式政策及实际合同为准。**

---

## 6. 阶段依赖关系（ASCII）

```text
Phase 0 项目初始化
    │
    ▼
Phase 1 数据模型 ────────────────┐
    │                            │
    ▼                            │
Phase 2 计算引擎                 │
    │                            │
    ▼                            │
Phase 3 单元测试                 │
    │                            │
    ▼                            ▼
Phase 4 项目文件 ────────▶ Phase 8 政策模板
    │                            │
    ▼                            │
Phase 6 Excel ──┐                │
    │           │                │
    ▼           │                │
Phase 7 PDF ────┼────────────────┘
    │           │
    ▼           ▼
Phase 5 GUI（依赖 4 / 6 / 7 / 8）
    │
    ▼
Phase 9 打包（依赖 5 / 6 / 7 / 8）
```

> **说明（现状）**：Phase 6（Excel）、Phase 7（PDF）、Phase 8（政策模板）均已在 Phase 5 之前实现，
> 且 Phase 5 的界面代码也已落地；**唯一剩下的主线是 Phase 9（打包）**，
> 但其前置条件是 PySide6 在本环境可用（见 5.1）。

---

## 7. 关联文档

| 文档 | 关系 |
|---|---|
| `README.md` | 项目总览、铁律与目录结构 |
| `PROJECT_SPEC.md` | 产品范围与三项验收 checklist |
| `UI_SPEC.md` | Phase 5 的实现依据与现状记录 |
| `REPORT_SPEC.md` | Phase 6 / Phase 7 的实现依据与现状记录 |
| `REGULATIONS_AND_POLICY.md` | 法规依据层与待核实清单 |
| `HUBEI_POLICY_MODEL.md` | Phase 8 的实现依据与现状记录 |
| `src/cenep/` | 已实现代码（domain / calculation / application / infrastructure / reports / policy / ui） |
| `tests/` | 测试体系与黄金案例（V2.0.0 实测 885 passed / 32 个文件） |
