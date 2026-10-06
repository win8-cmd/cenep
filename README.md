# CENEP V2 — 工商业新能源项目经济评价软件

> **定位**：面向工商业新能源项目（分布式光伏 / 储能 / 光储）开发阶段的**快速经济评价计算器 + 投资分析工具**。
> **不是**设计院正式可研软件，**不替代**可行性研究、工程设计、造价咨询、审计、税务咨询与政府审批。

---

## 1. 一句话说明

用户输入少量参数 → 点击计算 → 得到 总投资 / 首年发电量 / 首年收入 / 项目IRR / 资本金IRR / NPV / 静态回收期 / 动态回收期 / LCOE / LCOS / 最低DSCR / 敏感性 / 情景分析 → 导出 Excel 与 PDF。

**V2 新增**：8760 小时时序仿真（负荷 / 光伏 / 分时电价曲线）、储能 SOC 模型与三策略调度、
能量守恒校验、需量电费与削峰、时序驱动的经济评价、方案与参数扫描及寻优、
PyQtGraph 交互式时序图表、Excel 24 表 / PDF 16 部分、V1→V2 项目文件迁移。

## 2. 范围（严格限定）

| 做 | 不做 |
|---|---|
| 工商业分布式光伏 `COMMERCIAL_PV` | 集中式光伏电站 |
| 工商业储能 `COMMERCIAL_STORAGE` | 集中式风电 / 海上风电 |
| 工商业光伏+储能 `PV_STORAGE` | 大型水电 / 独立电网项目 |

**不做**：GIS、CAD、组件排布、电气设计、短路/潮流计算、政府审批、正式可研。

> **年度评价模式仍然可用**：`timeseries.enabled` 默认为 `False`，此时完全不加载 V2 模块，
> 走 V1 的年度模型，计算结果与 V1.0.0 **逐位一致**。

## 3. 安装与运行

需要 **Python 3.12+**。

```bash
# 1) 安装（含界面与开发依赖）
pip install -e ".[gui,dev]"

# 2) 运行全部测试（当前 885 passed / 0 failed）
pytest

# 3) 启动图形界面
python -m cenep

# 4) 自检（可验证打包后的 EXE 是否完好）
python -m cenep --selftest            # 结果打印到控制台
python -m cenep --selftest out.json   # 结果写入文件
```

> **无显示器 / CI 环境**运行测试或界面时，需要设置 `QT_QPA_PLATFORM=offscreen`，
> 否则 Qt 会因找不到显示设备而退出。

### 打包为免安装 EXE

```bash
pip install pyinstaller
pyinstaller build/CENEP.spec --noconfirm
```

产物为 `dist/CENEP/CENEP.exe`，双击即可运行，**目标机器无需安装 Python**。

> 打包时 `openpyxl` 与 `reportlab` 的数据文件必须一并收集（`build/CENEP.spec` 已通过
> `collect_all` 处理），否则打包后的程序会在导出 Excel/PDF 时失败。

## 4. 目录

```text
cenep/
├── src/cenep/
│   ├── domain/           数据模型（Pydantic）
│   ├── calculation/      唯一计算引擎（所有公式集中于此）
│   │   ├── engine.py             年度模型（V1 口径，仍是唯一财务实现）
│   │   ├── timeseries_engine.py  时间轴（8760 / 闰年 8784 / 15 分钟预留）
│   │   ├── load_profile.py       负荷曲线（导入 / 典型日 / 年度简化）
│   │   ├── pv_profile.py         光伏曲线（四种来源模式）
│   │   ├── tariff_series.py      分时电价（月份×日类型×小时，含尖峰深谷）
│   │   ├── storage_soc.py        储能 SOC 模型（效率方向、功率与 SOC 约束、衰减更换）
│   │   ├── dispatch_engine.py    三策略调度（峰谷套利 / 自用优先 / 经济优化）
│   │   ├── energy_balance.py     能量守恒校验（容差 1e-6 kWh）
│   │   ├── economic_v2.py        时序→年度经济口径（含防重复计算校验）
│   │   └── scenario_engine.py    方案比较、容量/参数扫描、两阶段寻优
│   ├── optimization/     规则型 / 贪心 / LP 三个优化器
│   ├── data/             Excel/CSV 导入、校验规则、数据质量评分
│   ├── application/      应用服务（编排：校验 → 计算 → 结果）
│   ├── infrastructure/   .nep 项目文件、SQLite、日志、V1→V2 迁移
│   ├── policy/           政策 Profile（可版本化，不硬编码）
│   ├── reports/          Excel（24 表）/ PDF（16 部分）导出
│   └── ui/               PySide6 界面（只展示 CalculationResult）+ PyQtGraph 图表
├── tests/                pytest 测试体系（32 个测试文件 / 885 个用例）
├── tools/make_examples.py  生成示例项目与导出件（产物不入库）
├── build/                PyInstaller 打包配置（EXE 通过 Releases 分发）
└── *.md                  设计与规范文档（见第 6 节）
```

## 5. 铁律（不可违反）

1. **只有一套公式**，全部位于 `src/cenep/calculation/`。
2. **只有一套数据模型**，位于 `src/cenep/domain/`。
3. **只有一个结果对象** `CalculationResult`：GUI、Excel、PDF 都是它的展示方式。
4. **GUI / Excel / PDF 一律不得自行计算**，只能调用 `calculation_engine.calculate(project)`。
5. **政策不硬编码**：任何电价、补贴、机制电量等政策参数都来自 `PolicyProfile`，且带版本与出处。
6. **每个数字可追溯**：参数带来源类型（用户输入/政策/合同/实测/经验/假设/系统默认/计算得出）。
7. **每个公式可测试**：修改任何公式后必须 `pytest` 全绿。

> 第 4 条由测试强制保证：`tests/test_gui.py` 会静态扫描 GUI/报表源码，
> 一旦出现 `npv(`、`irr(`、`lcoe(` 等计算调用即判定失败。

## 6. 设计文档

设计与规范文档**随本仓库分发**（规范 §108 要求交付物完整可追溯）：

| 文档 | 内容 |
|---|---|
| [PROJECT_SPEC.md](PROJECT_SPEC.md) | 产品范围与边界 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 分层架构、模块职责、数据流 |
| [DATA_MODEL.md](DATA_MODEL.md) | 数据模型（含 V2 `schema_version=2.0` 与 `timeseries`） |
| [CALCULATION_ENGINE.md](CALCULATION_ENGINE.md) | 计算引擎与年度模型 |
| [CORE_PARAMETERS_AND_FORMULAS.md](CORE_PARAMETERS_AND_FORMULAS.md) | 全部参数与公式 |
| [TIMESERIES_MODEL.md](TIMESERIES_MODEL.md) | **V2** 8760 时间模型、曲线、SOC、能量平衡 |
| [STORAGE_DISPATCH.md](STORAGE_DISPATCH.md) | **V2** 储能调度三策略 |
| [OPTIMIZATION.md](OPTIMIZATION.md) | **V2** 方案比较、扫描与寻优 |
| [DATA_IMPORT_SPEC.md](DATA_IMPORT_SPEC.md) | **V2** 数据导入、校验规则与质量评分 |
| [UI_SPEC.md](UI_SPEC.md) | 界面规范（含时序页与交互图表） |
| [REPORT_SPEC.md](REPORT_SPEC.md) | 报表规范（Excel 24 表 / PDF 16 部分） |
| [REGULATIONS_AND_POLICY.md](REGULATIONS_AND_POLICY.md) | 政策依据 |
| [HUBEI_POLICY_MODEL.md](HUBEI_POLICY_MODEL.md) | 湖北政策模型 |
| [TEST_PLAN.md](TEST_PLAN.md) | 测试计划 |
| [ROADMAP.md](ROADMAP.md) | 路线图 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录（含全部已修缺陷） |
| [AI_EXECUTION_GUIDE.md](AI_EXECUTION_GUIDE.md) | AI 执行指南 |
| [V2_ACCEPTANCE.md](V2_ACCEPTANCE.md) | **V2 验收清单**（逐条证据 + 已修缺陷 + 建模边界披露） |

## 7. 免责声明

本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，**不替代**项目正式可行性研究、工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。

电价、市场交易、税务、储能收益等政策参数具有时效性，应以项目实施时的最新正式政策及实际合同为准。
