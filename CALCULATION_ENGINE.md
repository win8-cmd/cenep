# CALCULATION_ENGINE.md —— 计算引擎规范

> **优先级**：本文档优先级仅次于 [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md)，
> 高于 `DATA_MODEL.md`、`PROJECT_SPEC.md`、`UI_SPEC.md`、`REPORT_SPEC.md`、`README.md` 与代码注释（规范 §157）。
> 公式与参数的**唯一权威定义**在 `CORE_PARAMETERS_AND_FORMULAS.md`；本文档只规定
> **执行顺序、调用关系、异常约定与复用规则**，不重复定义公式。
>
> 本文档描述的对象是 `src/cenep/calculation/engine.py` 中的
> `CalculationEngine`、模块级单例 `calculation_engine`、快捷函数 `calculate`。

---

## 1. 引擎在系统中的位置

### 1.1 分层调用关系（规范 §8、§151）

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  Presentation 层（已实现）                                                │
│  GUI(PySide6)  ── src/cenep/ui/（app / main_window / pages / sections /   │
│                   field_spec；只绑定字段与展示结果，无任何公式）           │
│  Excel 报表     ── src/cenep/reports/excel_exporter.py（13 张工作表）      │
│  PDF 报表       ── src/cenep/reports/pdf_exporter.py（15 章 + 免责声明）   │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ ① 只调用应用服务，绝不自己算
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Application 层（已实现）                                                 │
│  src/cenep/application/calculation_service.py : CalculationService        │
│  src/cenep/application/project_service.py     : ProjectService            │
│  职责：自动保存、日志、耗时统计、异常翻译、生命周期编排                    │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ ② engine.calculate(project, ...)
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Calculation 层（唯一公式所在地，已实现）                                  │
│  src/cenep/calculation/engine.py : CalculationEngine                      │
│    └─ _calculate_core()  ← §82 第 1–27、30、31 步                         │
│    └─ _run_scenarios()   ← §82 第 28 步（复用 _calculate_core）           │
│    └─ _run_sensitivity() ← §82 第 29 步（复用 _calculate_core）           │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ ③ 返回唯一结果对象
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Domain 层（已实现）                                                      │
│  CalculationResult（唯一结果对象，§81）→ GUI / Excel / PDF 都只展示它     │
└──────────────────────────────────────────────────────────────────────────┘
```

调用链的**唯一合法路径**：

```text
GUI / Excel / PDF  →  Application Service  →  CalculationEngine  →  CalculationResult
```

规范 §148–§150 的三条禁令：

1. GUI **不得**直接调用 `calculation/` 下的子模块（如 `pv.py`、`tax.py`）；
2. GUI / 报表 **不得**复制任何公式；
3. 报表数值 **不得**与 `CalculationResult` 不一致（Excel 导出已通过测试断言，
   详见 `tests/test_excel_export.py`）。

### 1.2 模块清单与职责

| 模块 | 路径 | 职责 |
|---|---|---|
| `engine` | `calculation/engine.py` | 统一入口、31 步编排、参数来源登记、口径说明 |
| `validator` | `calculation/validator.py` | 输入校验、能量守恒校验、中文报错 |
| `errors` | `calculation/errors.py` | 计算层异常层级 |
| `pv` | `calculation/pv.py` | 容量解析、发电量、衰减、电量分配 |
| `storage` | `calculation/storage.py` | 时长、效率、充放电量、可用容量衰减 |
| `revenue` | `calculation/revenue.py` | 电价解析、光伏收益、储能四类收益 |
| `investment` | `calculation/investment.py` | CAPEX 九项、双模式、单位投资 |
| `opex` | `calculation/opex.py` | 单项 OPEX、屋顶租金、逐年增长 |
| `tax` | `calculation/tax.py` | 折旧、利润链、所得税、附加税费 |
| `financing` | `calculation/financing.py` | 贷款额、还本计划、利息、债务余额 |
| `cashflow` | `calculation/cashflow.py` | 项目现金流、资本金现金流、累计 |
| `financial_metrics` | `calculation/financial_metrics.py` | IRR / NPV / 回收期 / LCOE / LCOS / ROI / DSCR |
| `scenario` | `calculation/scenario.py` | 情景乘数的深拷贝施加（§93、§94） |
| `sensitivity` | `calculation/sensitivity.py` | 单变量变化的深拷贝施加（§95） |

---

## 2. 公共入口

### 2.1 类与单例

```python
# calculation/engine.py

class CalculationEngine:
    """统一计算引擎。无状态、可重入、结果完全确定（规范 §118）。"""

calculation_engine = CalculationEngine()          # 全局唯一实例（规范 §147）

def calculate(project: Project, **kwargs) -> CalculationResult:
    """模块级快捷函数，等价于 calculation_engine.calculate(project)。"""
```

导入方式（模块 docstring 给出的**唯一**推荐写法）：

```python
from cenep.calculation.engine import calculation_engine

result = calculation_engine.calculate(project)
```

### 2.2 `calculate` 签名与返回值

```python
def calculate(
    self,
    project: Project,
    *,
    include_scenario: bool = True,
    include_sensitivity: bool = True,
) -> CalculationResult:
    """按规范 §82 计算并返回唯一的 CalculationResult。"""
```

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `project` | `domain.models.Project` | 必填 | 完整项目输入（位置参数） |
| `include_scenario` | `bool` | `True` | 是否执行情景分析；实际还会与 `project.scenario.enabled` 取"与" |
| `include_sensitivity` | `bool` | `True` | 是否执行敏感性分析；实际还会与 `project.sensitivity.enabled` 取"与" |
| **返回** | `domain.results.CalculationResult` | — | 唯一结果对象（§81） |

实现体（逐字）：

```python
result = self._calculate_core(project)

if include_scenario and project.scenario.enabled:
    result.scenarios = self._run_scenarios(project)
if include_sensitivity and project.sensitivity.enabled:
    result.sensitivity = self._run_sensitivity(project, result)
return result
```

要点：

1. **核心计算只跑一次**。情景与敏感性各自再跑 `_calculate_core`，但核心结果不会因此被覆盖。
2. 情景/敏感性关闭时，对应字段保持空列表（`[]`），而不是 `None`。
3. `calculate()` 不修改传入的 `project` 的业务字段；唯一副作用是把参数来源登记表
   回写到 `project.parameter_registry`（见 5.3）。

### 2.3 应用层封装

`application/calculation_service.py:CalculationService` 是 GUI / 报表应当调用的入口：

```python
service = CalculationService()
result = service.calculate(project)                       # 只返回 CalculationResult
outcome = service.calculate_with_outcome(project, path)    # 返回 CalculationOutcome

@dataclass(frozen=True)
class CalculationOutcome:
    result: CalculationResult
    elapsed_seconds: float
    autosaved_to: Path | None
```

`CalculationService` 的职责边界（模块 docstring）：

| 做 | 不做 |
|---|---|
| 计算前自动保存（§134） | **不做任何计算** |
| 日志：计算开始 / 结束 / 异常（§133） | 不复制公式 |
| 耗时统计 | 不修改业务口径 |
| 异常翻译（把未预期异常包成 `CalculationError`） | 不吞掉 `CalculationError` 的 `field` |

兜底异常翻译：

```python
except CalculationError as exc:
    logger.warning("计算失败：%s", exc)
    raise
except Exception as exc:                      # 兜底：避免 GUI 崩在未预期异常上
    logger.exception("计算出现未预期异常")
    raise CalculationError(f"计算出现未预期错误：{exc}") from exc
```

---

## 3. §82 的 31 步执行顺序

### 3.1 编号来源说明

规范 §82 定义了计算引擎的 31 步执行顺序。源码在 `engine.py` 中以注释形式标注了每一步
的编号；本节严格按**源码注释中的编号**整理，不臆造未出现的步骤：

- `_calculate_core` 覆盖 **第 1–27、30、31 步**（`engine.py` 第 75 行注释原文：
  "核心计算（§82 第 1–27、30、31 步）"）；
- 第 **28 / 29** 步（情景分析 / 敏感性分析）位于 `calculate()` 中，通过复用
  `_calculate_core` 实现；
- 步骤名称在源码中出现的即为源码注释原文（如 `# --- 15. Calculate CAPEX ---`），
  其余（第 5、6、11–14、19–21 步）取逐年循环内的编号注释
  （如 `# 4/5. 光伏发电量与衰减`、`# 6. 光伏电量分配`）。

> **诚实声明**：规范 §82 的条款正文本身**不在本仓库中**（仓库根目录只有
> `README.md`、`PROJECT_SPEC.md`、`UI_SPEC.md`、`REPORT_SPEC.md`、
> `REGULATIONS_AND_POLICY.md`、`HUBEI_POLICY_MODEL.md`、`ROADMAP.md`、
> `AI_EXECUTION_GUIDE.md` 与本次交付的 4 份文档，`TEST_PLAN.md` 尚未创建）。
> 因此下表中"步骤名"来自代码注释，属于对 §82 的实现映射，
> 而非对 §82 原文的逐字复述。

### 3.2 31 步总表

| 步 | 名称（源码注释） | 完成者 | 关键产物 |
|---|---|---|---|
| 1 | Validate Inputs | `validator.validate_project` | 校验通过或抛 `ValidationError` |
| 2 | Calculate Load | `engine._calculate_core` | `Load_1 = load.annual_load_kwh`、`g = annual_load_growth_rate` |
| 3 | Calculate PV Capacity | `pv.resolve_pv_capacity` | `pv_capacity`、`capacity_basis` |
| 4 | Calculate PV Generation（首年） | `pv.first_year_generation` | `first_year_generation` |
| 5 | Apply PV Degradation（逐年） | `pv.generation_for_year` | 第 n 年 `generation` |
| 6 | Allocate PV Energy | `pv.allocate_pv_energy` | `PvAllocation(direct_use, to_storage, export, loss)` |
| 7 | Calculate Storage Capacity / Duration | `storage.storage_duration_hours` | `storage_hours` |
| 8 | Resolve Storage Efficiency | `storage.resolve_efficiencies` | `charge_eff`、`discharge_eff`、`round_trip` |
| 9 | Calculate Storage Cycles（充放电量） | `storage.storage_year_result` | `StorageYear(...)` |
| 10 | Calculate Electricity Price | `revenue.resolve_tariff` | `TariffResolution` |
| 11 | Calculate PV Self-use Revenue | `revenue.self_use_revenue` | `pv_self_use_revenue` |
| 12 | Calculate PV Export Revenue | `revenue.export_revenue` | `pv_export_revenue` |
| 13 | Calculate Storage Revenue（四类） | `revenue.storage_arbitrage_revenue` 等 | 套利 / 容量 / 辅助 / 其他 |
| 14 | Calculate Total Revenue | `engine._calculate_core` | `total_revenue` |
| 15 | Calculate CAPEX | `investment.compute_capex` | `CapexBreakdown`、`total_capex` |
| 16 | Calculate OPEX（首年） | `opex.resolve_opex_item`、`opex.roof_rent` | `OpexBreakdown`、`first_year_opex` |
| 17 | Calculate Depreciation | `tax.depreciation_for_year` | `depreciation` |
| 18 | Calculate Financing | `financing.loan_amount_of`、`build_loan_schedule` | `loan_schedule`、`loan`、`equity` |
| 19 | Calculate Taxes | `tax.tax_year_result` | `TaxYear`（EBITDA/EBIT/EBT/所得税/附加） |
| 20 | Calculate Project Cashflow | `cashflow.project_cashflow` | `project_cf` |
| 21 | Calculate Equity Cashflow | `cashflow.equity_cashflow` | `equity_cf` |
| 22 | Calculate IRR | `financial_metrics.irr` | `project_irr`、`equity_irr` |
| 23 | Calculate NPV | `financial_metrics.npv` | `project_npv`、`equity_npv` |
| 24 | Calculate Payback | `financial_metrics.payback_period`、`discounted_payback_period` | 静态 / 动态回收期 |
| 25 | Calculate LCOE | `financial_metrics.lcoe` | `lcoe`（成本基数：总投资 − 储能投资；可选增值税/残值抵减） |
| 26 | Calculate LCOS | `financial_metrics.lcos` | `lcos` |
| 27 | Calculate DSCR | `financial_metrics.minimum_dscr`、`cfads_of` | `min_dscr`、逐年 `dscr` |
| 28 | Scenario Analysis | `engine._run_scenarios`（`scenario.apply_delta`） | `result.scenarios` |
| 29 | Sensitivity Analysis | `engine._run_sensitivity`（`sensitivity.apply_variable`） | `result.sensitivity` |
| 30 | Validate Energy Balance | `validator.validate_energy_balance`、`validate_storage_balance` | 守恒通过或抛 `EnergyBalanceError` |
| 31 | Generate CalculationResult | `engine._calculate_core` | `CalculationResult` |

### 3.3 实际代码中的执行次序（重要）

代码**并非严格按 1→31 的线性顺序**书写，而是做了两类重排，**结果不变**：

**（a）先解析、后循环。** 第 3、4、7、8、9、10、15、16、18 步中与年份无关的部分
在逐年循环**之前**计算一次；随年份变化的部分（第 5、6、9、16、17、18-利息、19、20、21 步）
在 `for year in range(1, n + 1)` 循环内逐次计算。

**（b）守恒校验（第 30 步）在指标（第 22–27 步）之前执行。** 源码顺序为：

```text
逐年循环结束
  → 30. validate_energy_balance / validate_storage_balance
  → 22/23/24. irr / 累计现金流
  → 25/26. lcoe / lcos
  → 27. minimum_dscr
  → 31. 组装 CalculationResult
```

即：**守恒不通过就不可能拿到任何指标**，这是有意的防御性设计 ——
电量不守恒时指标没有意义，必须先报错。

### 3.4 逐年循环内部的顺序

`for year in range(1, n + 1)` 内部严格执行下列顺序（源码注释编号）：

```text
# 2. 负荷                    load = load_1 × (1 + g)^(year-1)
# 4/5. 光伏发电量与衰减       generation = G1 × (1-d)^(year-1)   （无光伏则 0）
# 7/8/9. 储能年度等效循环     storage_year_result(...)          （无储能则全 0）
# 6. 光伏电量分配             allocate_pv_energy(generation, load, ratio, Echg, loss_ratio)
#    → grid_charge = max(Echg - to_storage, 0)
# 11/12/13. 收益              自用 / 上网 / 储能四类
# 14. 总收益
# 16. OPEX                    opex_for_year(...)
# 17. 折旧                    depreciation_for_year(...)
# 18. 利息                    loan_schedule[year-1].interest
# 19. 税                      revenue_net(...) → tax_year_result(...)
# 投资与残值                   replacement_capex（更换年）/ residual（末年）
# 20. 项目现金流              project_cashflow(...)
# 21. 资本金现金流            equity_cashflow(...)
# 附加：CFADS / DebtService / DSCR、LCOE-LCOS 序列追加、累计净收益累加
```

**顺序约束（不可交换）**：

1. 第 9 步的 `Echg` 必须在第 6 步之前算好 —— 它是光伏分配的
   `storage_charge_headroom`；
2. 第 18 步的利息必须在第 19 步之前 —— 利息进 `EBT`；
3. 第 17 步的折旧必须在第 19 步之前 —— 折旧进 `EBIT`；
4. 第 19 步的 `cash_tax` 必须在第 20、21 步之前 —— 现金税费直接扣减两套现金流。

---

## 4. 核心计算的复用：情景与敏感性

### 4.1 复用的铁律

**情景与敏感性不复制任何公式。** 它们只做两件事：

1. `project.model_copy(deep=True)` —— 深拷贝基准项目；
2. 在拷贝体上施加**显式乘数/变化率**；
3. 把拷贝体重新交给 `self._calculate_core(p2)` 完整重算。

因此：`_calculate_core` 是**唯一**实现公式的代码路径；情景与敏感性只是"换一组输入再算一遍"。

### 4.2 情景分析（§92–§94）

```python
def _run_scenarios(self, project: Project) -> list[ScenarioSummary]:
    """情景分析：BASE / CONSERVATIVE / OPTIMISTIC，全部从 BASE 复制（§93）。"""
    out: list[ScenarioSummary] = []
    for scenario in (ScenarioType.BASE, ScenarioType.CONSERVATIVE, ScenarioType.OPTIMISTIC):
        delta = delta_for(project, scenario)                       # scenario.py
        p2 = apply_delta(project, delta)                           # 深拷贝 + 施加乘数
        r2 = self._calculate_core(p2)                              # 复用核心计算
        out.append(ScenarioSummary(...))
    return out
```

规范 §93 的落实：**所有情景必须从 BASE 复制**，绝不"保守 → 乐观"链式推导。
证据：

- `delta_for(project, ScenarioType.BASE)` 返回全新的 `ScenarioDelta()`（全 1.0）；
- `apply_delta(project, delta)` 的第一行就是 `p = project.model_copy(deep=True)`，
  **不会**污染基准项目（`tests/test_scenario_sensitivity.py::test_apply_delta_does_not_mutate_base`）；
- 情景顺序固定为 `BASE → CONSERVATIVE → OPTIMISTIC`（测试断言
  `labels == ["BASE", "CONSERVATIVE", "OPTIMISTIC"]`）。

`apply_delta` 的施加范围（全部显式，无隐藏逻辑）：

| 乘数 | 作用对象 |
|---|---|
| `capex_multiplier` | 光伏单位投资、DETAILED 光伏投资、并网、屋顶、开发、工程、施工、其他、预备费 |
| `storage_capex_multiplier` | **在 capex 乘数基础上再乘一次**储能相关投资：`investment.storage_capex_per_kwh`、`investment.storage_capex`、`storage.replacement_capex` |
| `electricity_price_multiplier` | 固定/峰/平/谷/市场/上网/绿电/绿色环境价值、自定义替代价与充电价、两个 override、储能的放电替代价与充电价 |
| `generation_multiplier` | `pv.equivalent_hours` |
| `opex_multiplier` | 六项运维数值 + `rent_per_m2`、`rent_per_kw`、`annual_fixed_rent` |
| `self_consumption_ratio_multiplier` | `pv.self_consumption_ratio`，结果**裁剪到 0–1** |
| `storage_cycles_multiplier` | `storage.annual_cycles` |
| `interest_rate_multiplier` | `financing.interest_rate`（仅 `financing.enabled` 为真时） |

情景摘要 `ScenarioSummary` 的字段由 `r2`（重算结果）与 `delta.describe()` 填充。

### 4.3 敏感性分析（§95、§96）

```python
def _run_sensitivity(self, project: Project, base: CalculationResult) -> list[SensitivityRow]:
    rows: list[SensitivityRow] = []
    for variable in applicable_variables(project):
        for change in project.sensitivity.steps:
            p2 = apply_variable(project, variable, float(change))
            r2 = self._calculate_core(p2)
            # 敏感度系数 = IRR 变化率 / 参数变化率
            rows.append(SensitivityRow(...))
    return rows
```

规范 §95：**一次只改变一个参数**，其他参数保持基准值。
`apply_variable` 同样以 `project.model_copy(deep=True)` 为起点。

**变量过滤**（`sensitivity.py:applicable_variables`）：

```text
基础变量（一定包含）：CAPEX、ELECTRICITY_PRICE、OPEX
若项目含光伏：插入 GENERATION，追加 SELF_CONSUMPTION_RATIO
若项目含储能：追加 STORAGE_CYCLES、STORAGE_CAPEX
若 financing.enabled 且 debt_ratio > 0：追加 INTEREST_RATE
```

行数关系：`len(result.sensitivity) == len(applicable_variables) × len(steps)`
（由 `tests/test_scenario_sensitivity.py` 断言）。

**单变量作用范围**（`sensitivity.py`）：

| 变量 | 施加方式 | 内部函数 |
|---|---|---|
| `CAPEX` | 投资各项 ×(1+change)，含 `replacement_capex` | `_scale_capex` |
| `ELECTRICITY_PRICE` | 全部电价 ×(1+change) | `_scale_prices` |
| `GENERATION` | `pv.equivalent_hours` ×(1+change) | 内联 |
| `OPEX` | 运维各项与租金单价 ×(1+change) | `_scale_opex` |
| `SELF_CONSUMPTION_RATIO` | 比例 ×(1+change) 后裁剪到 0–1 | 内联 |
| `STORAGE_CYCLES` | `storage.annual_cycles` ×(1+change) | 内联 |
| `STORAGE_CAPEX` | 储能四项投资 ×(1+change) | `_scale_storage_capex` |
| `INTEREST_RATE` | `financing.interest_rate` ×(1+change) | 内联 |

**敏感度系数**（`engine.py:_run_sensitivity`）：

```text
若 base.project_irr 不为 None 且不为 0，且 r2.project_irr 不为 None：
    irr_change  = (r2.project_irr - base.project_irr) / base.project_irr
    若 |change| > 1e-12:
        coefficient = irr_change / change
否则：irr_change = None，coefficient = None
```

**基准行自洽**：`change == 0.0` 的行必须与基准结果完全相等
（`tests/test_scenario_sensitivity.py::test_zero_change_row_matches_base`）。

### 4.4 计算量估算

单次 `calculate()` 的核心计算次数：

```text
1（核心） + 3（BASE/CONSERVATIVE/OPTIMISTIC） + V × S（敏感性）
其中 V = 适用变量个数（3–8），S = steps 个数（默认 5）
默认光储项目：V = 8，S = 5 → 1 + 3 + 40 = 44 次 _calculate_core
```

每次核心计算都是纯 Python、无随机数、无迭代收敛（IRR 固定 300 次二分），
因此在普通电脑上应在秒级完成（`tests/test_application.py` 断言 < 5 秒，§155）。

---

## 5. 异常约定

### 5.1 异常层级（`calculation/errors.py`）

```text
Exception
└── CalculationError(message: str, field: str | None = None)
    ├── ValidationError      # 用户输入校验失败（§112、§114）
    ├── EnergyBalanceError   # 能量守恒校验失败（§113），允许误差 1e-6
    └── ScenarioError        # 情景分析配置错误（§93）
```

`CalculationError.__str__` 的拼接规则：

```python
def __str__(self) -> str:
    return f"[{self.field}] {self.message}" if self.field else self.message
```

`field` 的取值是**数据模型字段路径**，便于 GUI 定位到具体控件：

```text
pv.pv_capacity_kwp          storage.storage_energy_kwh
storage.storage_power_kw    storage.depth_of_discharge
storage.annual_cycles       storage.annual_degradation_rate
storage.efficiency          storage.replacement_year
load.annual_load_kwh        tariff.average_price / peak_price / flat_price / valley_price
tariff.market_price         tariff.export_price
tariff.custom_avoided_price tariff.custom_charge_price
tax.vat_rate                tax.income_tax_rate
tax.residual_value_ratio    financing.debt_ratio
financing.interest_rate     financing.loan_term
financing.grace_period      financing.repayment_method
investment.pv_capex_per_kw  investment.storage_capex_per_kwh
investment_mode             capex
discount_rate               analysis_period
tou_ratios                  tariff_mode
roof_rent_mode              opex.*
pv.energy_balance           storage.charge_energy
```

### 5.2 中文报错约定（§132）

`calculation/validator.py` 是**面向用户报错的唯一来源**（模块 docstring 原文：
"面向用户的报错统一从这里产生，全部为中文，并携带出错字段名"）。

报错必须满足三条：

1. **中文**，可直接展示给用户；
2. 说明**「哪个参数有问题」**，而不是抛裸 `ValueError`；
3. 带 `field`，可定位控件。

典型中文报错（源码原文，节选）：

```text
光伏装机容量必须大于 0（可直接输入容量，或输入可利用屋顶面积与单位面积容量）
储能容量必须大于 0
储能功率必须大于 0
放电深度 DoD 必须大于 0 且不超过 100%
年循环次数不能为负数
储能充电效率不能大于 100% 或小于等于 0
更换电芯年份不能超过项目生命周期
年用电量不能为负数
峰、平、谷电量比例之和必须等于 1（当前为 1.200000）
自定义电价模式必须填写替代电价
增值税率必须在 0~100% 之间
贷款比例与资本金比例之和必须等于 1（当前为 1.200000）
宽限期必须小于贷款期限，否则无法还本
第 3 年光伏电量不守恒：发电量 1000000.000000 kWh，分配合计 800000.000000 kWh，误差 200000.000000 kWh
第 1 年储能充电量小于光伏转入电量，请检查储能参数
不支持的投资模式：MAGIC（只能是 UNIT_PRICE 或 DETAILED）
不支持的运维费用模式：XXX
不支持的屋顶租金模式：SOMETHING
不支持的还款方式：SOMETHING（只能是等额本金或等额本息）
不支持的情景类型：XXX
贷款余额出现负数，请检查贷款期限与还款方式
```

### 5.3 校验的两道防线

| 防线 | 位置 | 负责 | 报错形态 |
|---|---|---|---|
| 第一道 | `domain/models.py`（Pydantic） | 类型、取值范围、交叉一致性 | `pydantic.ValidationError`（英文，面向开发者） |
| 第二道 | `calculation/validator.py` | 业务规则、友好中文、字段定位 | `cenep.calculation.errors.ValidationError`（中文） |

`CalculationError` 不会被 `CalculationService` 改写；只有**未预期异常**才会被包成
`CalculationError(f"计算出现未预期错误：{exc}")`（无 `field`）。

能量守恒校验（第 30 步，§113）：

```text
validate_energy_balance(annual_results, tolerance=1e-6)
    lhs = pv_generation_kwh
    rhs = pv_self_use_kwh + pv_to_storage_kwh + pv_export_kwh + pv_loss_kwh
    |lhs - rhs| > tolerance  →  EnergyBalanceError(field="pv.energy_balance")

validate_storage_balance(annual_results, tolerance=1e-6)
    pv_to_storage_kwh - storage_charge_kwh > tolerance
        →  EnergyBalanceError(field="storage.charge_energy")
```

---

## 6. 参数来源登记（§83、§84）

### 6.1 登记时机

`_calculate_core` 在**校验之后、计算之前**建立登记表：

```python
registry = ParameterRegistry()
self._register_parameters(project, registry)
```

`_register_parameters` 是 `@staticmethod`，末尾把登记表**回写**到项目对象：

```python
project.parameter_registry = registry.items()      # 便于 .nep 保存与报告"参数来源"表
```

### 6.2 登记的键与来源类型

**用户输入**（`SourceType.USER_INPUT`，24 个键）：

```text
analysis_period                       year            小数→年
discount_rate                         小数
load.annual_load_kwh                  kWh
load.annual_load_growth_rate          小数
pv.pv_capacity_kwp                    kWp
pv.equivalent_hours                   h
pv.performance_ratio                  小数
pv.annual_degradation_rate            小数
pv.curtailment_rate                   小数
pv.self_consumption_ratio             小数
storage.storage_power_kw              kW
storage.storage_energy_kwh            kWh
storage.annual_cycles                 次/年
storage.depth_of_discharge            小数
storage.annual_degradation_rate       小数
investment.pv_capex_per_kw            元/kWp
investment.storage_capex_per_kwh      元/kWh
tax.vat_rate                          小数
tax.income_tax_rate                   小数
tax.depreciation_years                年
tax.residual_value_ratio              小数
financing.debt_ratio                  小数
financing.interest_rate               小数
financing.loan_term                   年
```

**政策参数**（`SourceType.POLICY`，仅当 `project.policy is not None`）：

```text
policy.market_price                   元/kWh
policy.mechanism_price                元/kWh
policy.mechanism_volume_ratio         小数
policy.green_energy_price             元/kWh
policy.green_environmental_value      元/kWh
policy.version                        值为 policy.display_version
```

以上每条都带 `source_name = policy.source`、`source_url = policy.source_url`、
`note = policy.display_version`。

**假设值**（`SourceType.ASSUMPTION`、`is_assumption=True`）：

```text
scenario.conservative    值为 "、".join(delta.describe()) 或 "全 1"
scenario.optimistic      同上
note = "情景乘数为系统默认假设值，可在情景设置中修改"
```

**系统默认**（`SourceType.SYSTEM_DEFAULT`）：

```text
sensitivity.steps        值为 "、".join(f"{s:+.0%}")，例如 "-20%、-10%、+0%、+10%、+20%"
note = "敏感性分析步长"
```

> **未登记的字段**：`PolicyProfile` 之外的政策字段已在上面覆盖；其余多数参数
> （如 `roof_area_m2`、`loss_ratio`、`insurance`、`annuity` 相关字段等）当前**不登记**。
> 若报表需要更完整的来源表，应在 `_register_parameters` 中补登（见
> `CORE_PARAMETERS_AND_FORMULAS.md` 第 3 节各表的"来源登记"列）。

### 6.3 结果中的呈现

```text
CalculationResult.parameter_sources : dict[str, dict]
```

由 `ParameterRegistry.to_dict()`（`domain/provenance.py`）生成，每个键为：

```text
value, unit, source_type, source_type_label, source_name,
source_date, source_url, is_assumption, note, override_reason
```

Excel 导出的「参数来源」表直接消费该字典，并按 `source_type` 着色（§144，见第 8 节）。

---

## 7. 「单一公式来源」铁律

### 7.1 铁律原文（README §5）

1. **只有一套公式**，全部位于 `src/cenep/calculation/`；
2. **只有一套数据模型**，位于 `src/cenep/domain/`；
3. **只有一个结果对象** `CalculationResult`：GUI、Excel、PDF 都是它的展示方式；
4. GUI / Excel / PDF 一律**不得自行计算**，只能调用计算引擎；
5. 政策不硬编码，任何电价、补贴、机制电量等政策参数都来自 `PolicyProfile`，且带版本与出处（§34、§33）；
6. **每个数字可追溯**：参数带来源类型（§83、§91）；
7. **每个公式可测试**：修改任何公式后 `pytest` 必须全绿。

### 7.2 禁止事项清单

| 编号 | 禁止 | 违反后果 |
|---|---|---|
| F1 | 在 `ui/`、`reports/`、`application/` 中复制任何公式 | 数值与结果对象不一致（§109） |
| F2 | 在 `calculation/` 之外定义电价、税率、折旧率等业务常量 | 政策无法版本化（§34） |
| F3 | GUI 直接 `import` `calculation.pv` / `tax` / `financing` 等子模块 | 绕过 31 步编排，破坏守恒校验 |
| F4 | 用「原始贷款 × 利率」近似全部年份利息 | 违反 §65 |
| F5 | 把 `TaxableIncome` 写成负数，或漏掉 `max(EBT, 0)` | 违反 §61 |
| F6 | 让进入储能的电量同时计入光伏自用收益 | 重复计算，违反 §47 |
| F7 | 把储能四类收益合并成一个不可分的数 | 违反 §44 |
| F8 | 用 0 冒充"无法计算"的 IRR / 回收期 / LCOE / DSCR | 违反 §114，误导决策 |
| F9 | 用随机初值或依赖迭代初值的算法求 IRR | 违反 §118 的确定性要求 |
| F10 | 在情景分析中做"保守 → 乐观"链式推导 | 违反 §93 |
| F11 | 在敏感性分析中一次改变多个参数 | 违反 §95 |
| F12 | 公式算不出来时自创公式 | 违反 §158，必须记 TODO(V2) 并采用最简可替换模型 |
| F13 | 在 Excel 中写入公式（`=...`）让 Excel 重算 | 违反 §109；当前实现只写数值 |
| F14 | 在计算层做显示单位换算 | 违反 §14 的全局单位约定 |
| F15 | 修改公式后不更新黄金案例与本文档 | 违反 README 铁律 7 与 §157 |

### 7.3 唯一公式来源的自检方法

```text
① 在 reports/ 与 application/ 中搜索算术表达式（+ - * / 与 **）：
   合法用法仅限格式化、计数、比例展示；
② 校验 CalculationResult 与 Excel 数值一致：
   tests/test_excel_export.py::TestValuesComeFromResult；
③ 校验关键数值与手工推算一致：
   tests/test_engine_golden.py（期望值均为手工独立推算，非从程序输出反抄）；
④ 校验确定性：
   tests/test_engine_golden.py::test_same_input_100_times_identical（§118）。
```

### 7.4 报表层的既成事实（§109、§144）

Excel 导出（`reports/excel_exporter.py`）已经落实"不重算 + 可追溯"：

- 全部数值取自 `CalculationResult`（或 `project` 的输入快照）；
- **不写任何公式**，测试断言工作簿中不存在以 `=` 开头的单元格；
- 「参数来源」表按来源类型着色，配色与 §144 一致：

| `source_type` | 含义 | 填充色 |
|---|---|---|
| `USER_INPUT` | 用户输入 | `DDEBF7`（蓝） |
| `CALCULATED` | 系统计算 | `E2EFDA`（绿） |
| `ASSUMPTION` / `SYSTEM_DEFAULT` / `EXPERIENCE` | 假设 | `FFF2CC`（黄） |
| `POLICY` | 政策模板 | `EDEDED`（灰） |
| `CONTRACT` / `HISTORICAL` | 合同 / 历史 | `DDEBF7`（蓝） |

---

## 8. 引擎的输出契约

`_calculate_core` 组装 `CalculationResult` 时，下列字段的取值来源固定：

| 结果字段 | 取值来源 |
|---|---|
| `project_name` / `project_type` / `province` / `city` | `project.basic_info` |
| `pv_capacity_kwp` / `storage_power_kw` / `storage_energy_kwh` | 解析后的容量（无光伏/储能则为 0） |
| `storage_duration_hours` | `storage.storage_duration_hours` |
| `analysis_period` | `project.analysis_period` |
| `total_capex` | `CapexBreakdown.total` |
| `unit_investment` | `investment.unit_investment` |
| `capex_breakdown` | 九项中文名 → 金额（固定 9 个键） |
| `loan_amount` / `equity_amount` | `financing` 第 18 步 |
| `first_year_*` | `annual[0]` 的对应字段 / `project_flows[1]` |
| `annual_revenue` / `annual_opex` | 逐年算术平均（`sum(...) / n`） |
| `project_irr` / `equity_irr` | `financial_metrics.irr` |
| `project_npv` / `equity_npv` | `financial_metrics.npv` |
| `static_payback` / `discounted_payback` | `payback_period` / `discounted_payback_period` |
| `lcoe` / `lcos` | 仅当项目含光伏 / 含储能，否则 `None` |
| `roi` | `financial_metrics.roi(lifecycle_net_profit, total_capex)` |
| `min_dscr` | `financial_metrics.minimum_dscr` |
| `project_cashflows` / `equity_cashflows` | 长度 `N + 1`（含 Year 0） |
| `cumulative_cashflow` | `cashflow.cumulative(project_flows)` |
| `annual_results` | 长度 `N` 的 `AnnualResult` 列表 |
| `parameter_sources` | `registry.to_dict()` |
| `notes` | `_CALIBER_NOTES` + 4 条本次计算说明（+ 政策说明） |
| `scenarios` / `sensitivity` | `_run_scenarios` / `_run_sensitivity` |

`capex_breakdown` 的固定 9 个键（中文）：

```text
光伏投资、储能投资、并网投资、屋顶费用、开发费用、
工程费用、施工费用、其他投资、预备费
```

`notes` 的口径说明（`_CALIBER_NOTES`，16 条）覆盖：时间模型、电量分配顺序与不重复计算、
储能套利口径、四类收益、LCOE/LCOS 口径（含公共费用全额计入 LCOE 与两个可选抵减开关）、
光伏/储能设备更换、成本不含利息、ROI 口径、两套 IRR 口径、
利息平均余额口径、折旧口径、税务简化、所得税口径、政策不硬编码。
每次计算额外追加：

```text
电价解析口径：{tariff.basis}；替代电价 ... 元/kWh，充电电价 ... 元/kWh，上网电价 ... 元/kWh。
光伏容量取值依据：用户直接输入 / 按屋顶面积换算 / 不适用。
储能往返效率：...（充电 ... × 放电 ...）。
折现率：...；计算期：N 年。
若挂了政策：本测算采用政策：{display_version}（来源：{source}）。
```

---

## 9. 测试与验收对应关系

| 测试文件 | 覆盖 |
|---|---|
| `tests/conftest.py` | 黄金案例固定参数（§117） |
| `tests/test_engine_golden.py` | 31 步端到端、手工推算期望值、确定性、守恒、参数来源、政策披露 |
| `tests/test_pv.py` | 容量解析、发电量、衰减、分配守恒、不重复计算 |
| `tests/test_storage.py` | 时长、效率换算、充放电量、容量复位 |
| `tests/test_revenue.py` | 综合电价、四种模式解析、覆盖优先级、套利、四类合计 |
| `tests/test_finance_modules.py` | CAPEX 九项与双模式、OPEX、折旧税、还本计划、平均余额利息 |
| `tests/test_metrics.py` | NPV、IRR（含无符号变化返回 None）、回收期、LCOE/LCOS、ROI、DSCR |
| `tests/test_scenario_sensitivity.py` | 情景从 BASE 复制、非链式、单变量、系数符号、变量过滤 |
| `tests/test_validation.py` | 中文报错 + 字段定位、Pydantic 兜底、守恒阈值边界 |
| `tests/test_project_file.py` | `.nep` 往返、信封、原子写、版本校验、SQLite |
| `tests/test_application.py` | 应用服务、自动保存、异常翻译、性能、日志 |
| `tests/test_excel_export.py` | 13 张表、数值一致性、无公式、着色、三种项目类型 |
| `tests/test_pdf_export.py` | 15 章结构、免责声明逐字出现、数值一致 |
| `tests/test_gui.py` | 界面只绑定字段、不出现公式、结果展示 |
| `tests/test_policy.py` | 政策模板完整性、拒绝未填完转换、版本只新增 |
| `tests/test_selftest.py` | `python -m cenep --selftest` 自检流程（计算 + Excel + PDF） |

> **环境提示**：本机 `.pylibs` 未包含 `openpyxl`，因此 `tests/test_excel_export.py`
> （16 个用例）在当前离线环境中无法导入；其余用例可正常收集执行。
> 完整数量与结果见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 8.2 节。

---

## 10. 扩展指南（与引擎相关）

| 需求 | 应改哪里 | 禁止改哪里 |
|---|---|---|
| 新增收益类型 | `domain/enums.py`（若有枚举）+ `calculation/revenue.py` 新增纯函数 + `domain/results.py:AnnualResult` 新字段 + `engine.py` 第 13/14 步 + `_CALIBER_NOTES` | 不得在 `reports/` 里补算 |
| 新增财务指标 | `calculation/financial_metrics.py` 新增纯函数 + `engine.py` 第 22–27 步区间 + `CalculationResult` 新字段 | 不得在 GUI 里算 |
| 新增政策参数 | `domain/models.py:PolicyProfile` + `engine.py:_register_parameters` 的 POLICY 段 + 报告「政策依据」表 | 不得写死在 `revenue.py` |
| 新增校验规则 | `calculation/validator.py`（中文 + `field`）+ 必要时 `models.py` 的 Pydantic 约束 | 不得在 GUI 里做业务校验 |
| 调整简化口径 | 先改 `CORE_PARAMETERS_AND_FORMULAS.md` 第 6 节，再改代码与 `_CALIBER_NOTES` | 不得先改代码后补文档（§157、§158） |
| 新增计算步骤 | 需先确认 §82 的 31 步是否需要扩展，并在本文档第 3 节登记编号 | 不得私自插入未编号步骤 |

---

## 11. 小结

1. **唯一入口**：`calculation_engine.calculate(project, include_scenario, include_sensitivity)`。
2. **唯一公式来源**：`calculation/` 下的纯函数；情景与敏感性通过**深拷贝 + 乘数 + 重算**复用。
3. **严格顺序**：§82 的 31 步；守恒校验（第 30 步）先于指标输出。
4. **友好报错**：`ValidationError` / `EnergyBalanceError` / `ScenarioError`，全中文且带 `field`。
5. **可追溯**：参数来源登记表随结果一起返回，报表按 §144 配色展示。
6. **确定性**：无随机数、无随机初值，同一输入 100 次结果完全一致（§118）。
