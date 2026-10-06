# 测试计划（TEST_PLAN）

> 规范 §115–§119、§152–§155；V2 补充 §73、§85、§86。**任何公式改动后必须 `pytest` 全绿，
> 否则不得进入下一阶段。**

---

## 1. 当前状态（实测，V2.0.0）

| 项目 | V2.0.0（当前） | V1.0.0（历史） |
|---|---|---|
| 测试文件 | **32 个**（`tests/test_*.py`；另有 `conftest.py`） | 16 个 |
| 展开用例 | **885 个** | 263 个 |
| 结果 | **885 passed, 0 failed**（Python 3.12 + pytest 9.1.1） | 263 passed, 0 failed |
| 被测源码 | `src/cenep/`（11 个包：V1 的 8 个 + V2 的 `optimization/`、`data/`，以及 `ui/charts.py` 等扩展） | 6,087 行 / 8 个包 |
| 测试代码 | `tests/`（32 个文件） | 1,994 行 / 15 个文件 |

> **用例数下限**：V2 交付要求为**用例数不得少于 884**、且必须 **0 failed**；
> 实测为 **885 项 / 0 failed**（达到并超过下限）。
> V1 的 263 个用例**全部保留**（其中 5 个文件仅追加用例，无删除、无放宽）。

复现命令：

```powershell
$PY = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$WS = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\cenep"
$env:PYTHONPATH = "$WS\.pylibs;$WS\src"
$env:QT_QPA_PLATFORM = "offscreen"     # GUI 测试在无显示器环境运行
Set-Location $WS
& $PY -m pytest tests -q
```

---

## 2. 测试分层（规范 §115）

| 层 | 说明 | 对应文件 |
|---|---|---|
| **Unit Test** | 单个公式/函数，输入标量、断言语义 | `test_pv.py`、`test_storage.py`、`test_revenue.py`、`test_finance_modules.py`、`test_metrics.py` |
| **Calculation Test** | 黄金案例端到端计算，逐指标手工核对 | `test_engine_golden.py` |
| **Integration Test** | 项目文件 + 应用服务 + SQLite 串联 | `test_project_file.py`、`test_application.py`、`test_policy.py` |
| **Report Test** | Excel / PDF 与 `CalculationResult` 一致性 | `test_excel_export.py`、`test_pdf_export.py`、**`test_report_v2.py`** |
| **UI Test** | offscreen 下真实建窗，界面值与结果一致 | `test_gui.py`、**`test_gui_v2.py`** |
| **Regression Test** | 确定性（100 次一致）、自检、边界与校验 | `test_engine_golden.py::TestDeterminismAndStructure`、`test_selftest.py`、`test_validation.py` |
| **Caliber Test** | LCOE/LCOS 口径开关与设备更换，含按招标公式的端到端复算 | `test_lcoe_caliber.py` |
| **Calculation Test（扩展）** | 情景与敏感性 | `test_scenario_sensitivity.py` |
| **★ V2 时序模型** | 输入/结果模型字段与 JSON 序列化（J1–J8）、时间轴与分辨率 | `test_timeseries_models.py`、`test_timeseries_engine.py` |
| **★ V2 曲线引擎** | 负荷 / PV / 分时电价三个曲线引擎的模式、归一化与 Δt 口径 | `test_profile_engines.py` |
| **★ V2 储能与调度** | SOC 递推与四上限、三策略语义、原因一致、逐日窗口调度 | `test_storage_soc.py`、`test_dispatch_engine.py`、`test_dispatch_semantics.py` |
| **★ V2 数据层** | 三类模板导入、18 条校验、14 条告警、质量评分 | `test_data_layer.py` |
| **★ V2 寻优** | 规则型/贪心/线性规划三级寻优与回退链、方案比较 | `test_optimization.py`、`test_scenario_engine.py` |
| **★ V2 集成与一致性** | 时序端到端、守恒、口径归一化、结果字段贯通 | `test_v2_integration.py`、`test_result_wiring.py`、`test_project_type_alignment.py` |
| **★ V2 性能预算** | §86 性能预算（多次取样取最小值） | `test_v2_performance.py` |
| **★ V2 迁移** | `.nep` `1.0`/`1.1` → `2.0` 迁移与留痕 | `test_migration.py` |

---

## 3. 黄金测试案例（规范 §117、§118）

### 3.1 固定参数（`tests/conftest.py`）

| 参数 | 取值 |
|---|---|
| 项目 | 湖北某工商业厂房（武汉市） |
| 光伏容量 | 1000 kWp |
| 年等效利用小时 | 1100 h |
| 性能比 | 1.0（不得重复扣减，§21） |
| 自用比例 | 80% |
| 光伏年衰减 | 0.5% |
| 光伏单位投资 | 3000 元/kWp |
| 项目寿命 | 25 年 |
| 储能 | 500 kW / 1000 kWh，往返效率 88%，DoD 90%，330 次/年，年衰减 2% |
| 负荷 | 1,200,000 kWh/年（年增长 0） |
| 电价 | 峰 1.0 / 平 0.7 / 谷 0.4，比例 3:4:3（综合 0.70），余电上网 0.35 |
| 运维 | 光伏 2%、储能 2%（按投资比例）、保险 0.5%、管理费 1 万、其他 0.5 万 |
| 折旧/税 | 20 年直线、残值 5%、所得税 25% |
| 融资 | 贷款 50%、利率 4%、期限 10 年、等额本金、无宽限期 |
| 折现率 | 8% |

> 规范 §117 未规定负荷、电价、运维与融资，上述取值是为保证"同一输入重复计算 100 次结果一致"而**显式固定**的，已在此登记。

### 3.2 手工推导的关键期望值（写入断言，防止公式被误改）

光伏：`G1 = 1000 × 1100 × 1.0 × (1−0) = 1,100,000 kWh`

| 指标（第 1 年，光储案例） | 推导 | 期望值 |
|---|---|---|
| 自用电量 | `min(1,100,000 × 0.8, 1,200,000)` | 880,000 kWh |
| 进储能电量 | 余量 220,000，储能可充 316,603.06 | 220,000 kWh |
| 余电上网 | 220,000 − 220,000 | 0 kWh |
| 储能放电量 | `1000 × 0.9 × 330 × √0.88` | 278,610.696 kWh |
| 储能充电量 | `278,610.696 / 0.88` | 316,603.064 kWh |
| 自用收益 | `880,000 × 0.70` | 616,000 元 |
| 套利收益 | `278,610.696×0.70 − 316,603.064×0.40` | 68,386.26 元 |
| 总投资 | `1000×3000 + 1000×1000` | 4,000,000 元 |
| 首年运维费 | `60,000 + 20,000 + 20,000 + 10,000 + 5,000` | 115,000 元 |
| 年折旧 | `4,000,000 × 0.95 / 20` | 190,000 元 |
| 第 1 年利息 | 平均余额 `(2,000,000+1,800,000)/2 × 4%` | 76,000 元 |
| EBITDA | `684,386.26 − 115,000` | 569,386.26 元 |
| 所得税 | `(569,386.26 − 190,000 − 76,000) × 25%` | 75,846.565 元 |
| 项目现金流 | `569,386.26 − 75,846.565` | 493,539.696 元 |
| 资本金现金流 | `493,539.696 − 76,000 − 200,000` | 217,539.696 元 |

三个案例：`golden_pv`（纯光伏）、`golden_storage`（纯储能）、`golden_pv_storage`（光储）。

### 3.3 确定性（规范 §118）

`test_engine_golden.py::TestDeterminismAndStructure::test_same_input_100_times_identical`
连续计算 100 次，比对 **9 个指标 + 全部 25 年项目现金流元组**，要求集合大小为 1（无随机性）。
IRR 采用固定区间二分法（`_IRR_LOW=-0.9999`、`_IRR_HIGH=10.0`、固定 300 次迭代），不使用随机初值。

---

## 4. 覆盖矩阵（规范条款 → 测试）

| 规范条款 | 内容 | 测试位置 |
|---|---|---|
| §19 §20 §21 §22 | 光伏容量、发电量、性能比、衰减 | `test_pv.py` |
| §23 §24 §25 §47 §113 | 电量分配、自用上限、守恒、禁止重复计算 | `test_pv.py`、`test_engine_golden.py` |
| §39 §40 §41 §42 §45 | 储能时长、效率换算、充放电量、衰减与更换电芯 | `test_storage.py` |
| §27 §28 §31 §32 §43 §44 | 电价与四类收益 | `test_revenue.py` |
| §49 §50 §51 §52 | CAPEX 九项与两种模式 | `test_finance_modules.py` |
| §54 §55 §56 | OPEX 两种模式、增长、屋顶租金三种模式 | `test_finance_modules.py` |
| §57 §58 §60 §61 | 折旧、利润链、所得税 | `test_finance_modules.py` |
| §63 §64 §65 | 贷款、余额非负、平均余额计息 | `test_finance_modules.py` |
| §70–§79 §114 | IRR/NPV/回收期/LCOE/LCOS/ROI/DSCR | `test_metrics.py` |
| §67 §68 §69 | 项目/资本金现金流与 Year 0 | `test_engine_golden.py` |
| §82 §147 | 31 步顺序、唯一入口 | `test_engine_golden.py`、`test_selftest.py` |
| §92 §93 §94 | 情景分析（从 BASE 复制、不链式） | `test_scenario_sensitivity.py` |
| §95 §96 | 敏感性（单变量、输出 IRR/NPV/回收期） | `test_scenario_sensitivity.py` |
| §83 §84 §85 §91 | 参数来源与假设值标记 | `test_engine_golden.py`、`test_policy.py` |
| §34 §35 §36 §89 §90 §159 | 政策版本化、不预填数值 | `test_policy.py` |
| §9 §10 §134 | `.nep` 文件、SQLite、自动保存 | `test_project_file.py` |
| §132 §133 | 中文报错、日志 | `test_validation.py`、`test_application.py` |
| §108 §109 §154 | Excel **24 表**（V1 的 13 表全部保留）且与结果一致 | `test_excel_export.py`、`test_report_v2.py` |
| §110 §111 §154 | PDF **16 部分**、免责声明 | `test_pdf_export.py`、`test_report_v2.py` |
| §97 §105 §106 §107 §144 §152 §155 | 界面结构、指标卡、图表、配色、性能 | `test_gui.py`、`test_gui_v2.py` |
| §8 §148 | **界面层不得计算** | `test_gui.py::TestNoCalculationInUi`（静态 import 检查 + 禁止函数名扫描） |
| §152 §153 §166 | 打包产物自检 | `test_selftest.py`、`dist\CENEP\CENEP.exe --selftest` |
| **§3 §7（V2）** | 时间轴、分辨率、平/闰年点数、`timestamp` 唯一主索引 | `test_timeseries_engine.py` |
| **§4–§6（V2）** | 时序输入/结果模型字段、JSON 序列化（J1–J8） | `test_timeseries_models.py` |
| **§5–§14（V2）** | 负荷/PV/分时电价三曲线引擎、模式与归一化、真实 Δt 口径 | `test_profile_engines.py` |
| **§12–§20（V2）** | SOC 递推、四上限取小、SOC 硬约束、往返效率严格 0.88 | `test_storage_soc.py` |
| **§12 §14（V2）** | 三策略调度、逐日窗口调度（非逐时贪心）、原因与实际动作一致 | `test_dispatch_engine.py`、`test_dispatch_semantics.py` |
| **§73（V2）** | **能量守恒**：年度残差 + 最差逐时残差 + 容差判定；**随机 100 组守恒** | `test_v2_integration.py`、`test_v2_performance.py` |
| **§45–§48（V2）** | 三级寻优与回退链、`max_evaluations` 硬上限、五段式解释 | `test_optimization.py`、`test_scenario_engine.py` |
| **§22–§30（V2）** | 三类模板导入、18 条校验（V01–V18）、14 条告警、质量评分 | `test_data_layer.py` |
| **§64 §65（V2）** | `.nep` `1.0`/`1.1` → `2.0` 迁移与 `migration_notes` 留痕 | `test_migration.py` |
| **§85（V2）** | **V2 Golden Case**（时序黄金案例端到端） | `test_v2_integration.py`、`test_result_wiring.py` |
| **§86（V2）** | **性能预算**（8760 仿真 / 25 年 / 内存预算） | `test_v2_performance.py` |
| **§105（V2）** | 结果一致性 + `ProjectType` 按容量归一化（20.03% → 15.00%） | `test_project_type_alignment.py`、`test_report_v2.py` |
| **§61（V2）** | 单一计算源：图表只取 `TimeSeriesResultSet.column()`，界面不计算 | `test_gui_v2.py` |

---

## 5. 关键测试设计说明

### 5.1 "只有一套公式"的机器化验证（规范 §8、§109）
* `test_excel_export.py::test_no_formulas_written`：遍历工作簿所有单元格，断言**公式单元格数为 0**；
* `test_gui.py::TestNoCalculationInUi`：读取 `src/cenep/ui/*.py` 源码，断言没有 `from ..calculation import ...`（仅允许 `errors`），且不出现 `npv(`、`irr(`、`lcoe(` 等计算函数名。

### 5.2 校验分层（规范 §112、§132）
* **第一道**：Pydantic 字段约束（类型/范围），赋值即拦截；
* **第二道**：`calculation/validator.py` 产出**中文、带字段名**的错误。
  测试用 `model_copy(update=...)`（不经 Pydantic 校验）构造非法项目，专门验证第二道防线。

### 5.3 导出层一致性（规范 §154）
Excel 测试逐行比对 `年度现金流` 工作表与 `result.annual_results` 的每个字段；PDF 测试解析文档流（含表格单元格）核对 IRR、总投资等数值与章节齐全性。

### 5.4 政策"不预填数值"（规范 §89、§159）
断言内置湖北模板的全部 5 个数值字段均为 `None`，且 `to_profile()` 在未填完整时抛出中文错误；同时验证 `0.0`（用户确实填 0）与 `None`（未填写）可区分。

### 5.5 ★ §73 随机 100 组能量守恒

`test_v2_integration.py` / `test_v2_performance.py` 中的守恒用例：

* **随机 100 组**参数组合（负荷曲线模式、光伏模式、电价时段规则、策略、SOC 上下限、
  充放电效率、电网充电开关等）**用固定随机种子**生成，逐组跑完整时序仿真；
* 每组断言 `EnergyBalance.error ≤ tolerance`（年度残差）**且**
  `max_hourly_error ≤ tolerance`（最差逐时残差），`is_balanced is True`；
* 九条能量流（PV→负荷 / PV→储能 / PV→上网 / 电网→负荷 / 电网→储能 /
  储能→负荷 / 储能→上网 / 负荷合计 / 储能 SOC 变化）逐条闭合；
* 任何一组不守恒即判定计算失败（`EnergyBalanceError`），**不裁剪后继续**。

### 5.6 ★ §85 V2 Golden Case

V2 的黄金案例（时序路径端到端）与 V1 黄金案例**同一把尺子**：

* 三类项目（`COMMERCIAL_PV` / `COMMERCIAL_STORAGE` / `PV_STORAGE`）各跑一次启用
  `timeseries.enabled = True` 的完整仿真；
* 关键指标**手工独立推算**（不反抄程序输出）：首年发电量、自用率、自给率、
  电费节省、储能套利、需量削减、项目 IRR / NPV、LCOS；
* **`ProjectType` 按容量归一化**的一致性断言：缺陷 ⑬ 的回归用例明确断言
  项目 IRR 为 **15.00%** 而非被高估的 **20.03%**（差 5 个百分点）；
* `CalculationResult` 的 8 个 V2 字段必须被写入且与 `TimeSeriesReport` **同源**
  （`test_result_wiring.py`）。

### 5.7 ★ §86 性能预算

`test_v2_performance.py` 断言 V2 的性能**预算**而不是单次墙钟时间（避免并行负载导致假失败）：

| 预算项 | 断言方式 |
|---|---|
| 8760 时序仿真耗时 | **多次取样取最小值**，与预算比较（并行负载下的单次计时不作为判定依据） |
| 25 年仿真（`YearSimulation` 列式） | 内存与耗时预算；防止退回"预物化 25×8760 个对象（约 110 MB）"的旧实现（缺陷 ⑧） |
| `TimeAxis` 构造 | 仅约 10 个长度 8760 的 NumPy 数组，**不产生逐小时 Python 对象** |

---

## 6. 已知缺口（如实登记）

| 缺口 | 说明 | 计划 |
|---|---|---|
| 无真实显示器下的视觉效果验证 | GUI 测试在 `offscreen` 平台断言**数据与结构**，不验证像素级外观 | 人工验收；或在有显示器环境执行 |
| 未覆盖 `RiskLevel` | 该枚举已定义但尚未参与计算与报表（§106 禁止用固定阈值判定可行性） | 待定 |
| 未覆盖政策数值折算为电价 | 政策参数目前只做记录与披露，不自动折算进电价（V2 §92 已登记） | 后续版本 |
| ~~未覆盖 `.nep` 跨版本迁移~~ | ✅ **V2 已解决** | `test_migration.py`（`1.0`/`1.1` → `2.0`） |
| ~~无性能基准回归~~ | ✅ **V2 已解决**：改为性能预算用例 | `test_v2_performance.py` |
| 打包 EXE 的 GUI 交互 | 仅验证 `--selftest`（无界面路径）与**启动期无异常退出** | 人工双击验收 |

---

## 7. 运行命令速查

```powershell
& $PY -m pytest tests                               # 全部（必须 0 failed，≥ 885 项）
& $PY -m pytest tests/test_engine_golden.py -v      # 黄金案例（公式回归第一防线）
& $PY -m pytest tests/test_excel_export.py tests/test_pdf_export.py -v   # 导出层
& $PY -m pytest tests/test_gui.py tests/test_gui_v2.py -v   # 界面（需 QT_QPA_PLATFORM=offscreen）
& $PY -m pytest tests/test_v2_integration.py tests/test_v2_performance.py -v   # V2 守恒与性能预算
& $PY -m pytest tests/test_project_type_alignment.py -v   # 缺陷 ⑬ 回归（15.00% 而非 20.03%）
& $PY -m cenep --selftest out.json                  # 端到端自检（打包前后的同一把尺子）
& "$WS\dist\CENEP\CENEP.exe" --selftest "$WS\build\packaged_selftest.json"   # 验证打包产物
```

---

## 8. ★ V2 测试清单与用例数（885 项 / 32 个文件）

实测方式（2026-10，本机 runtime Python 3.12 + pytest 9.1.1）：

```powershell
$env:PYTHONPATH = "$WS\.pylibs;$WS\src"; $env:QT_QPA_PLATFORM = "offscreen"
& $PY -m pytest tests --collect-only -q     # 逐文件用例数
```

| # | 测试文件 | 用例数 | V1/V2 | 覆盖的规范 |
|---|---|---|---|---|
| 1 | `test_application.py` | 10 | V1 | §133 §134 §151 §155 |
| 2 | `test_data_layer.py` | 107 | **V2** | V2 §22–§30（导入、校验、质量分） |
| 3 | `test_dispatch_engine.py` | 43 | **V2** | V2 §12 §14（三策略 + 逐日窗口调度） |
| 4 | `test_dispatch_semantics.py` | 19 | **V2** | V2 §12（原因与实际动作一致） |
| 5 | `test_engine_golden.py` | 31 | V1（+2） | §67–§69 §82 §83–§85 §118 §147 |
| 6 | `test_excel_export.py` | 19 | V1（+5） | §108 §109 §154（V2 §66 表数） |
| 7 | `test_finance_modules.py` | 32 | V1 | §49–§52 §54–§58 §60 §61 §63–§65 |
| 8 | `test_gui.py` | 16 | V1 | §8 §97 §105–§107 §144 §148 §152 |
| 9 | `test_gui_v2.py` | 45 | **V2** | V2 §49 §50 §61 §68–§71（时序页 + PyQtGraph） |
| 10 | `test_lcoe_caliber.py` | 10 | V1 | §79 §114（LCOE/LCOS 口径开关） |
| 11 | `test_metrics.py` | 20 | V1 | §70–§79 §114 |
| 12 | `test_migration.py` | 20 | **V2** | V2 §64 §65 §94 §95（`.nep` 迁移） |
| 13 | `test_optimization.py` | 30 | **V2** | V2 §45–§48（三级寻优与回退链） |
| 14 | `test_pdf_export.py` | 19 | V1（+4） | §110 §111 §154（V2 §67 章节） |
| 15 | `test_policy.py` | 17 | V1 | §34–§36 §89 §90 §159 |
| 16 | `test_profile_engines.py` | 66 | **V2** | V2 §5–§14（负荷/PV/分时电价曲线 + Δt 口径） |
| 17 | `test_project_file.py` | 25 | V1（+3） | §9 §134（V2 信封 2.0 与幂等） |
| 18 | `test_project_type_alignment.py` | 17 | **V2** | V2 §78 §105（按容量归一化，缺陷 ⑬） |
| 19 | `test_pv.py` | 20 | V1 | §19–§25 §47 §113 |
| 20 | `test_report_v2.py` | 41 | **V2** | V2 §66 §67 §105（24 表 / 16 部分一致性） |
| 21 | `test_result_wiring.py` | 14 | **V2** | V2 §25 §62（`CalculationResult` 字段贯通、同源写入） |
| 22 | `test_revenue.py` | 12 | V1 | §27 §28 §31 §32 §43 §44 |
| 23 | `test_scenario_engine.py` | 45 | **V2** | V2 §42 §45–§48（方案比较与扫描） |
| 24 | `test_scenario_sensitivity.py` | 18 | V1 | §92–§96 |
| 25 | `test_selftest.py` | 3 | V1 | §152 §153 §166 |
| 26 | `test_storage.py` | 15 | V1 | §39–§42 §45 |
| 27 | `test_storage_soc.py` | 32 | **V2** | V2 §12–§20（SOC 递推、四上限、往返 0.88） |
| 28 | `test_timeseries_engine.py` | 40 | **V2** | V2 §3 P0.1 §7（时间轴与分辨率） |
| 29 | `test_timeseries_models.py` | 43 | **V2** | V2 §4–§6（模型字段与 J1–J8 序列化） |
| 30 | `test_v2_integration.py` | 28 | **V2** | V2 §73 §85（守恒 + V2 Golden Case） |
| 31 | `test_v2_performance.py` | 12 | **V2** | V2 §86（性能预算） |
| 32 | `test_validation.py` | 16 | V1 | §112 §113 §132 |
| — | **合计** | **885** | 32 个文件 | — |

> **说明**
>
> 1. **"V1（+n）"** 表示该文件是 V1 既有文件，V2 在其中**追加**了 n 个用例
>    （无删除、无放宽）。
> 2. 上表的合计即 `pytest tests` 的实测用例数（**885 项，0 failed**）；
>    逐文件数字用 `pytest tests --collect-only -q` 复核。以实测为准，
>    新增用例后**只增不减**。
> 3. V2 的 16 个**全新**文件为第 2、3、4、9、12、13、16、18、20、21、23、27、28、29、30、31 项。
