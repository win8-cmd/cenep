# AI 执行指南（AI_EXECUTION_GUIDE）

> 本文档面向**后续接手本项目的 AI Agent / 开发人员**，说明当前进度、运行方式、铁律与下一步。

---

## 1. 当前进度（截至最近一次提交）

| Phase | 内容 | 状态 | 证据 |
|---|---|---|---|
| **0** | 项目结构、依赖、README | ✅ 完成 | `pyproject.toml`、`requirements.txt` |
| **1** | Pydantic 数据模型 | ✅ 完成 | `src/cenep/domain/` |
| **2** | 统一 CalculationEngine | ✅ 完成 | `src/cenep/calculation/`（14 个模块） |
| **3** | pytest 测试体系 + 黄金案例 | ✅ 完成 | `tests/`，**263 passed, 0 failed** |
| **4** | `.nep` 项目文件 + SQLite | ✅ 完成 | `src/cenep/infrastructure/`、`application/` |
| **5** | PySide6 GUI（7 个页面 + 7 张图表） | ✅ 完成 | `src/cenep/ui/`，`tests/test_gui.py` 在 offscreen 下真实验证 |
| **6** | Excel 导出（13 张表） | ✅ 完成 | `src/cenep/reports/excel_exporter.py` |
| **7** | PDF 报告（15 章） | ✅ 完成 | `src/cenep/reports/pdf_exporter.py` |
| **8** | 湖北政策 Profile（模板不预填数值） | ✅ 完成 | `src/cenep/policy/` |
| **9** | PyInstaller 打包 EXE | ✅ 完成并验证 | `dist/CENEP/CENEP.exe`（16 MB），`--selftest` 返回 `ok: true` |

> **Phase 9 验证证据**：打包后的 `CENEP.exe --selftest report.json` 在 **3.8 秒**内完成
> 三个黄金项目（纯光伏 / 纯储能 / 光储）的全部计算 + Excel（13 表）+ PDF 导出，
> 报告 `{"ok": true, "stage": "done", "errors": []}`。
>
> **踩过的坑（务必保留 `--collect-all`）**：只用 `--windowed` 打包时，
> `openpyxl` 与 `reportlab` 的**数据文件不会被打进包**，运行时抛未捕获异常，
> 而 `--windowed` 的无控制台模式会弹出一个异常对话框并让进程**永久挂起**
> （表现为"双击后没反应"）。因此必须加 `--collect-all openpyxl --collect-all reportlab`，
> 并且自检模式已改为**每个阶段都把报告写回文件**（`stage` 字段），便于定位崩溃点。

**已验证的关键指标（黄金案例，光储 1000 kWp + 500 kW/1000 kWh）**

| 指标 | 数值 |
|---|---|
| 总投资 | 4,000,000 元 |
| 首年发电量 | 1,100,000 kWh |
| 首年收入 | 684,386.26 元 |
| 项目 IRR / 资本金 IRR | 10.29% / 10.29% |
| 项目 NPV（折现率 8%） | 777,688.13 元 |
| 静态 / 动态回收期 | 8.66 年 / 15.74 年 |
| LCOE / LCOS | 0.3513 / 0.4776 元/kWh |
| 单次计算耗时 | 约 0.2 秒 |

---

## 2. 环境与运行

本机环境有一个特殊之处：**pip 默认源（pypi.org）受网络策略限制会挂住**，必须走镜像，并且依赖被安装到项目内的 `.pylibs` 目录（而不是虚拟环境，因为 `python -m venv` 的 `ensurepip` 在本机被拦截）。

```powershell
# 依赖安装（如尚未安装）
$PY = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$WS = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\cenep"
& $PY -m pip install --target "$WS\.pylibs" -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn pydantic pytest reportlab PySide6 pyinstaller

# 运行全部测试（必须 0 failed）
$env:PYTHONPATH = "$WS\.pylibs;$WS\src"
Set-Location $WS
& $PY -m pytest tests

# 生成示例项目与报告
& $PY "$WS\tools\make_examples.py"

# 启动 GUI（Phase 5 完成后）
& $PY -m cenep
```

---

## 3. 不可违反的铁律（违反即视为严重缺陷）

1. **只有一套公式**：任何数学计算只能写在 `src/cenep/calculation/` 下。
2. **GUI / Excel / PDF 一律不得计算**：它们只能消费 `CalculationResult` 的字段。
   *验证方式*：`tests/test_excel_export.py::test_no_formulas_written` 断言导出的工作簿内没有任何公式单元格。
3. **只有一套数据模型**：所有结构在 `src/cenep/domain/`。
4. **政策不硬编码**：电价、补贴、机制电量等一律来自 `PolicyProfile`，且带版本与出处。
5. **每个数字可追溯**：新参数必须登记到 `ParameterRegistry`，标注来源类型。
6. **只有三种项目类型**：`COMMERCIAL_PV` / `COMMERCIAL_STORAGE` / `PV_STORAGE`；不得擅自加入集中式光伏、风电。
7. **不得自创公式**：遇到规范未定义的公式，记录 `TODO` 并采用最简单、透明、可替换的模型（规范 §158）。
8. **不得用固定阈值宣告项目"可行/不可行"**（规范 §106）；只做相对比较与敏感性排序。

---

## 4. 代码地图

```text
src/cenep/
├── domain/           数据模型（enums / models / results / provenance）
├── calculation/      唯一计算引擎
│   ├── pv.py         §19 §20 §22 §24 §25
│   ├── storage.py    §39–§45
│   ├── revenue.py    §27 §28 §32 §43 §44
│   ├── investment.py §49–§52
│   ├── opex.py       §54–§56
│   ├── tax.py        §57 §58 §60 §61
│   ├── financing.py  §63–§65
│   ├── cashflow.py   §67–§69
│   ├── financial_metrics.py §70–§79
│   ├── scenario.py   §92–§94
│   ├── sensitivity.py §95 §96
│   ├── validator.py  §112 §113 §114
│   └── engine.py     §82 §147（唯一入口 calculation_engine.calculate）
├── application/      编排层（新建/打开/保存 → 计算 → 结果；自动保存、日志）
├── infrastructure/   .nep 项目文件、SQLite、日志
├── reports/          Excel（13 表）与 PDF（15 章）导出
├── policy/           政策 Profile（Phase 8，待实现）
├── ui/               PySide6 界面（Phase 5，待实现）
└── utils/
```

**调用关系（规范 §8、§151）**

```text
GUI / CLI
   ↓
CalculationService / ProjectService        （application）
   ↓
calculation_engine.calculate(project)      （calculation，唯一公式源）
   ↓
CalculationResult                          （domain.results）
   ↓            ↓
ExcelExporter   PdfExporter                （reports，只渲染不计算）
```

---

## 5. 下一步任务（按优先级）

### 任务 A：Phase 5 —— PySide6 GUI ✅ 已完成
7 个页面（项目 / 参数 / 计算 / 结果 / 敏感性 / 报告 / 设置）已实现：

- `ui/field_spec.py`：声明式字段绑定（控件自动生成、比例按百分数显示小数存储、按来源着色）；
- `ui/sections.py`：字段定义（新增参数只需加一行）；
- `ui/pages.py`：七个页面（含 11 个指标卡、年度明细表、7 张 QtCharts 图表）；
- `ui/main_window.py`：唯一编排入口，只调用 `ProjectService` / `CalculationService`；
- `tests/test_gui.py`：在 `QT_QPA_PLATFORM=offscreen` 下真实建窗计算，断言**界面显示值与 `CalculationResult` 完全一致**，并用静态检查确认界面层没有引入任何计算函数。

### 任务 B：Phase 8 —— 湖北政策 Profile ✅ 已完成
- `policy/template.py`：`PolicyTemplate` 用 `None` 表示"未填写"，与 `0.0` 严格区分；
  未填完整时 `to_profile()` 抛出中文错误（规范 §159）；
- `policy/hubei.py`：湖北占位模板，**所有数值字段均为 `None`**，并在 `notes` 中给出官方发布页作为填写指引；
- `policy/store.py`：模板与版本分离管理，同一 `policy_id` 多版本**只新增不覆盖**；
- `Database` 新增 `policy_templates` 表与版本查询方法。

### 任务 C：Phase 9 —— PyInstaller 打包 🔄 进行中
```powershell
& $PY -m PyInstaller --noconfirm --clean --name CENEP --paths "$WS\src" `
  --distpath "$WS\dist" --workpath "$WS\build\work" --specpath "$WS\build" `
  --windowed --hidden-import PySide6.QtCharts --exclude-module tkinter "$WS\build\entry.py"
```
打包后用自检模式验证（`--windowed` 无控制台，故写文件）：
```powershell
& "$WS\dist\CENEP\CENEP.exe" --selftest "$WS\build\selftest.json"
# selftest.json 中 "ok": true 即通过
```

### 任务 D：持续保持
- 每改一处公式 → 立即跑 `pytest`，必须 0 failed（§119、§165）；
- 新增报表/新参数时同步更新 `CORE_PARAMETERS_AND_FORMULAS.md` 与 `DATA_MODEL.md`。

---

## 6. 已知限制（V1 有意为之，不要"顺手补全"）

详见 `CORE_PARAMETERS_AND_FORMULAS.md` 的"V1 简化口径"一节。摘要：

1. 增值税进项抵扣与留抵退税不建模；附加税费以不含税收入为基数简化计算。
2. 不建模亏损跨年弥补。
3. 储能容量/辅助服务/其他收益按输入固定值计取，不随年份增长。
4. LCOE 采用光伏口径、LCOS 采用储能口径；**公共费用（保险/管理费/其他）全额计入 LCOE**，
   与现金流中的运维成本口径一致（纯光伏项目两者相等）。增值税进项抵扣与残值抵减为
   **可选开关**，默认关闭；需要与招标/行业 LCOE 口径对齐时在税务参数中开启。
5. 等额本息的还本额按期初余额分解，利息按 §65 的平均余额入账（差异在报告口径中披露）。
6. 不做 8760 小时仿真，储能采用年度等效循环模型（§37）。
7. 未建模建设期利息资本化（Year 0 一次性投资）。
8. 情景乘数为系统默认假设值（可在情景设置中修改），使用时会标记为"假设值"。

---

## 7. 常用校验命令

```powershell
# 全部测试
& $PY -m pytest tests

# 只跑黄金案例（公式回归的第一道防线）
& $PY -m pytest tests/test_engine_golden.py -v

# 只跑导出层（确认 GUI/Excel/PDF 与 CalculationResult 一致）
& $PY -m pytest tests/test_excel_export.py tests/test_pdf_export.py -v

# 结构化检查导出的 Excel
& $PY "C:\Users\Administrator\AppData\Local\Programs\DeepSeek Harness\resources\runtime\office-skills\scripts\check_office.py" examples\示例3_工商业光储.xlsx --out checks.json
```
