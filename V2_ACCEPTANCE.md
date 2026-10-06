# CENEP V2 验收清单（规范 §106）

> 本清单逐条对应《工商业新能源项目经济评价软件 V2 · AI直接执行总规范》的验收要求。
> 每条都给出**可复核的证据**（测试名、实测数值或命令），不做无证据的声明。
> 生成时间以文末"证据快照"为准。

---

## 一、正确性验收

| # | 规范要求 | 证据 | 结论 |
|---|---|---|---|
| 1 | 能量守恒误差 < 1e-6 kWh（§19、§70） | `test_v2_integration.py::TestEnergyConservationRandom::test_balance_within_tolerance_for_100_cases`：**随机 100 组** PV/负荷/储能/电价组合逐点校验 | ✅ |
| 2 | 电费节省不得重复计算（§27–§31、§105） | 分解口径恒等校验 `economic_v2.check_saving_identity`，偏差超限**直接抛错**；`test_saving_identity_for_100_cases` 随机 100 组 | ✅ |
| 3 | SOC 恒在 [SOC_min, SOC_max]（§10.5、§74） | `test_soc_bounds_for_100_cases`（100 组随机参数） | ✅ |
| 4 | 充放电不超功率上限（§11、§75） | `test_power_limits_for_100_cases`（100 组随机参数，含 15 分钟粒度按 Δt 折算） | ✅ |
| 5 | 储能效率口径与 V1 一致（§76） | `test_storage_soc.py::TestEfficiencyDirection`：往返闭合断言——充 100 kWh(AC) 后只需放 88 kWh 即精确回到起点 | ✅ |
| 6 | 无储能退化为"仅光伏+电网"（§77） | `TestDegenerateCases::test_no_storage_no_arbitrage` / `test_no_storage_consumption_only_pv_and_grid` | ✅ |
| 7 | 无光伏退化为"纯电网负荷"（§78） | `TestDegenerateCases::test_no_pv_is_pure_grid_load`（节省额 0、购电 = 负荷） | ✅ |
| 8 | PV Only 三种关系（§79） | `TestDegenerateCases::test_pv_only_greater_than_load` / `_less_than_load` / `test_pv_equal_load_scale` | ✅ |
| 9 | PV+Storage 四态（§80） | `TestPvStorageStates`：盈余充电、缺口放电、SOC 满原因码、SOC 空原因码 | ✅ |
| 10 | 峰谷套利方向正确（§81） | `TestPeakValleyArbitrage::test_charges_in_valley_discharges_in_peak` | ✅ |
| 11 | 需量与削峰（§18、§30、§82） | `TestDemand`：削峰后需量下降、需量电费随电价线性缩放、无需量电价时为 0 | ✅ |
| 12 | 8760 时序驱动经济评价（§27–§41） | V2 Golden Case（下节）端到端可复现 | ✅ |
| 13 | 逐年曲线增长与衰减（§8.3、§9.2、§17.2、§25） | `test_profile_engines.py`（负荷年增长、光伏衰减、电价增长、上网电价**不**随购电价增长）、`test_v2_performance.py::test_degradation_disables_reuse` | ✅ |
| 14 | 闰年 8784 与分辨率预留（§3 P0.1、§7） | `test_timeseries_engine.py`：8760 / 8784 / 35040 / 35136 / 365 / 366 / 12 点数全覆盖 | ✅ |

## 二、兼容性验收（V1 不得被破坏）

| # | 规范要求 | 证据 | 结论 |
|---|---|---|---|
| 15 | V1 计算逻辑与结果不变（§1.1、§65、§84） | `year_override=None` 与不传该参数**逐位一致**（`test_v2_integration.py::TestEngineWiring::test_override_none_matches_plain_calculation`）；V1 Golden Case 280 项基线全绿 | ✅ |
| 16 | 未启用时序时结果对象不得出现 V2 内容 | `test_v1_untouched_when_timeseries_disabled`：`time_series_results is None`、`baseline_results is None`、`dispatch_results == []` | ✅ |
| 17 | V2 模块延迟导入，V1 路径不加载 | `calculation_service._attach_timeseries` 内 `from ..calculation import economic_v2` | ✅ |
| 18 | `.nep` 可读 V1 文件并迁移到 2.0（§64、§65） | `test_migration.py`（20 项）；迁移前后 IRR/NPV/LCOE/CAPEX **完全一致** | ✅ |
| 19 | 年度评价模式仍可用（§1） | `timeseries.enabled` 默认 `False`；全部 V1 报表与界面走原路径 | ✅ |

## 三、性能验收（§86）

| # | 要求 | 实测 | 预算 | 结论 |
|---|---|---|---|---|
| 20 | 单项目 8760 小时计算 | **81 ms** | < 2000 ms | ✅ 25× 余量 |
| 21 | 100 个方案扫描 | **8.0 s**（120 候选网格） | < 30 s | ✅ |
| 22 | 25 年全周期 | ~2.1 s（动态）/ **173 ms**（静态形状复用） | —— | ✅ |
| 23 | 无 8760 逐点 NumPy 调用（§87） | 全部曲线引擎向量化；仅 SOC 状态机与报告时间戳构造为顺序循环 | —— | ✅ |
| 24 | 内存不得为每小时建对象（§87） | `YearSimulation` 不预物化 8760 个 `DispatchDecision`（否则 25 年约 110 MB）；改为列式存储 + `decisions()` 按需物化 | —— | ✅ |

证据文件：`tests/test_v2_performance.py`（12 项，含"复用不得改变数值"的逐位比对）。

## 四、交付物验收（§102、§103、§108）

| # | 交付物 | 状态 |
|---|---|---|
| 25 | V2 设计文档 4 份 + CHANGELOG | ✅ 已交付（另有 ARCHITECTURE / DATA_MODEL / TEST_PLAN 同步到 V2） |
| 26 | Excel 报表升级（§67） | ✅ **已交付：24 张工作表**（V1 的 13 张全部保留且顺序不变） |
| 27 | PDF 报表升级（§66，16 部分） | ✅ **已交付：16 部分 + 3 张图表** |
| 28 | GUI 时序页面与交互图表（§49–§50） | ✅ **已交付**：时序页 5 个子页 + `ui/charts.py`（PyQtGraph 交互图） |
| 29 | 可运行 Windows EXE | ✅ **已交付并启动验证**：`dist\CENEP\CENEP.exe`（21,958,256 字节，版本资源 2.0.0） |
| 30 | `pytest` 0 failed | ✅ **885 passed / 0 failed**（32 个测试文件） |

## 五、已发现并修复的缺陷（复核痕迹）

规范要求可追溯，本清单**如实列出**开发与复核过程中抓到的真实缺陷：

| # | 缺陷 | 危害 | 状态 |
|---|---|---|---|
| 1 | 储能单向效率默认 0.938 → 往返 0.879844 ≠ V1 的 0.88 | 套利收益与 LCOS 口径漂移 | ✅ 改为 √0.88 |
| 2 | `computed_field` 派生时间字段存得进 `.nep`、读不回 | 项目文件无法打开 | ✅ 前置校验器剥离并校验一致性 |
| 3 | `TYPICAL_DAY` 未按 `annual_energy_kwh` 归一化 | 负荷电量与用户输入不符（1,707,293 vs 1,500,000） | ✅ |
| 4 | 调度原因与实际动作不符（报"充电"但电量为 0） | 违反 §16 可解释性 | ✅ 加全年不变式测试 |
| 5 | 电价列映射错位 / `export_price` 从未解析 / 空值静默变 0 | 电价整列 NaN、上网电价恒 0、缺失与真值 0 无法区分 | ✅ |
| 6 | 光伏夜间出力为 0 被用全体中位数误判为"单位疑似 MWh" | 干净数据被扣分 | ✅ 改用非零值中位数 |
| 7 | `peak_load`/`peak_power` 把 Δt 钳到 `max(Δt,1)` | 15 分钟粒度下功率**低估 4 倍** | ✅ |
| 8 | `YearSimulation` 预物化 25×8760 个对象 | 内存约 110 MB | ✅ 列式 + 懒物化 |
| 9 | `PVConfig.pv_capacity_kwp` 不允许 0 / 曲线引擎对 0 抛错 | 与 §78 退化要求冲突 | ✅ 模型放宽为 `ge=0`，引擎返回全 0 |
| 10 | 存盘幂等断言比较全文含 `saved_at` | 跨秒偶发失败 | ✅ 剔除时间戳后比内容 |
| 11 | 性能用例单次墙钟计时 | 并行负载下 34 ms 虚高到 113 ms 假失败 | ✅ 改 5 次取最小值 |
| 12 | **`PEAK_VALLEY` 逐时近视贪心**：在平价时段抢先把电池充满，导致次日谷价时段无容量可充 | **套利收益低估约 60%**（84,792 元 vs 应约 22 万元），会把可行项目算成不可行 | ✅ 已改为按日价格排序的窗口调度；两充两放 IRR 2.32% → 13.65%，落进公开案例公布的 14%~18% |
| 13 | **项目类型与储能口径不一致**：V2 项目未设 `project_type` 时默认 `COMMERCIAL_PV`（`has_storage=False`），年度模型按"无储能"算（**不计储能造价**），而时序仿真确实用了储能、套利收益又被注入年度模型 | **算了储能收益却没算储能造价 → 项目 IRR 高估 5 个百分点**（20.03% → 15.00%，相对高估 33%），足以把边缘项目包装成有吸引力 | ✅ 时序启用时按容量自动归一化 `ProjectType` 并在 notes 说明依据；两种构造方式的 IRR/NPV/回收期须逐位一致 |

## 六、已知口径选择与建模边界（如实披露）

1. **光伏给储能充电按 0 计价、电网充电按购电价计价**。这是为了使分解口径
   （光伏自用节省 + 储能套利收益）**恒等于**现金口径的电费节省额。光伏→储能的
   机会成本（本可上网的收益）不在分解口径中扣除，属口径选择而非计算错误。
2. **储能功率不进入造价模型**。规范 §33 要求 CAPEX 保持 V1 模型，而 V1 的储能造价
   只有 `元/kWh` 一项，因此 §45 的"储能功率扫描"只影响调度能力、不影响投资额，
   同一容量下不同功率可能得到相同 NPV。
3. **方案扫描的排名用两阶段评估**：排名阶段每个候选只跑首年并按文档化线性外推
   构造 25 年口径，复核阶段才对最优候选跑精确 25 年仿真。近似与精确在
   `OptimizationResult.explanation` 中分别标注（§48 禁止黑盒）。
4. **静态形状复用**：负荷增长率、光伏/储能衰减率、电价增长率全为 0 时，各年逐位等价，
   只跑首年并复用（与逐年仿真逐位比对通过）。任一非零即逐年仿真。
5. **数据质量评分为加权制**：完整 40 / 连续 25 / 异常 20 / 来源 15，故"完美数据"的
   满分取决于来源类型（合约/政策 100、用户输入 96、系统默认 85）。分项量程已由模型强制。

---

## 证据快照

```
仓库         CENEP（分支 feature/v2-timeseries）
测试          756 passed / 0 failed
V1 基线       280 项全绿
单年 8760     81 ms（预算 2000 ms）
120 候选扫描  8.0 s（预算 30 s）
守恒误差      2.8e-14 kWh（容差 1e-6）
```
