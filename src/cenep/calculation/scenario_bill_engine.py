"""光储四场景账单对比与**收益去重**引擎（V2.3 §7.1、§7.5、§7.6、§3.5、§10 阶段 6）。

本模块是**阶段 6 全部核心公式的唯一实现处**（§0.2：核心公式必须在 ``calculation/``）。
它**不新建第二套调度或经济引擎**：四场景的逐周期调度全部调用既有
:func:`cenep.calculation.dispatch_engine.dispatch`，年度口径全部调用既有
:func:`cenep.calculation.economic_v2.compute_baseline` / ``compute_metrics``，
电价解析调用既有 :mod:`cenep.calculation.tariff_series`，
能量流构造调用既有 :mod:`cenep.calculation.self_consumption`。

一、四场景（§7.1、§10 阶段 6）
============================

===================================  ==========================  ==============
场景                                  光伏                      储能
===================================  ==========================  ==============
基准 ``NO_PV_NO_STORAGE``             0 kWp                       0 kW / 0 kWh
方案 A ``PV_ONLY``                    项目光伏容量                 0 kW / 0 kWh
方案 B ``STORAGE_ONLY``               0 kWp                       项目储能容量
方案 C ``PV_STORAGE``                 项目光伏容量                 项目储能容量
===================================  ==========================  ==============

**同一负荷、同一电价计划、同一套需求与其他费用假设**，只有设备配置不同（§7.1）。
``pv`` 数组按容量系数 ``本场景容量 ÷ 项目容量`` 缩放，因此"无光伏"场景严格等价于
"光伏容量为 0"，不需要改写项目文件、也不依赖"项目光伏容量必须 > 0"的既有校验
（那条校验属 V1 项目模型契约，本阶段**不改**它，只在本引擎内按场景切换设备，§0.2）。

二、费用口径与恒等式（§3.4、§7.1、§7.5）
=====================================

.. code-block:: text

    C_energy  = Σ_t (E_grid,t × P_t)                                   §3.4 电度电费
    C_demand  = Σ_m (D_m × P_demand)                                   §3.4／§7.5 需量电费
    C_capacity= K_contract,kVA × P_capacity                            §3.4 容量电费（与需量互斥）
    C_fund    = E_grid × P_fund                                        §2.3 政府性基金及附加（分类列示）
    C_other   = 用户显式给定的未建模费用（固定值或按电量比例分摊）        §3.4
    total_cost= C_energy + C_capacity + C_demand + C_pf + C_fund + C_other
    net_cost  = total_cost − R_export                                  上网收入单列为收入
    S_bill    = total_cost(基准) − total_cost(方案)                      §3.4／§7.6 唯一来源

三、需量口径（§7.5：**不能**简单使用每小时平均负荷峰值冒充电表需量）
===============================================================
:func:`windowed_max_demand_kw` 按**配置的计量窗口**（默认 15 分钟）计算"窗口滑动平均值"
的逐月最大值。窗口内含缺失点（``NaN``）时**整个窗口作废**并在结果中记录，
绝不把缺失当 0（§6.4 同一红线）。窗口长于数据间隔时按点平均（不插值、不夸大）。

四、收益去重（§7.6 —— 本阶段的核心与最大风险）
==========================================

规格书 §7.6 的六条要求逐条落地如下：

1. **"将『基准账单 − 方案账单』作为账单节省额唯一来源"**
   :attr:`~cenep.domain.scenario_bill.ScenarioBillComparison.bill_saving_yuan`
   严格等于"基准 total_cost − 方案 total_cost"，没有第二个来源。
2. **"不得再把同一份光伏自用电量乘平均电价作为另一笔独立收益加入现金流"**
   光伏自用节省在结果中标记为 ``decomposition``（分解口径），
   :attr:`~cenep.domain.scenario_bill.ScenarioBenefitEntry.counted_in_unique_benefit` 恒为 ``False``。
3. **"上网电量收益可以独立计算，但必须确保未包含在账单节省额中"**
   上网收入是独立现金来源，且**数学上不在** ``S_bill`` 内：``S_bill`` 只由购电成本构成，
   上网电量从未进入 ``C_energy``。本模块用
   :func:`check_export_not_in_bill_saving` 显式校验（把上网收入设成任意值，
   ``S_bill`` 必须逐位不变）。
4. **"储能套利收益应由方案账单差额体现；如额外列示分解，只做分析展示，不再次累加"**
   储能套利收益同样标记为 ``decomposition``。
5. **"财务现金流只接收一个去重后的年度运营收益对象"**
   :attr:`~cenep.domain.scenario_bill.ScenarioBillSet.unique_annual_benefit_yuan`
   是唯一数值；:meth:`~cenep.calculation.scenario_bill_engine.build_finance_overrides`
   把它交给**既有** ``economic_v2.YearOverride``，不新建财务引擎（§3.5）。
6. **"所有差异项应有可追溯明细"** 去重清单逐项带 ``caliber`` 与 ``excluded_reason``。

**分解恒等式（本模块强制校验，§7.6）**——三条互相独立的校验同时成立才算"去重通过"：

.. code-block:: text

    ① 分解口径闭合（含储能损耗残差）：
       S = S_光伏自用 + A_储能套利 + ρ_损耗
       S_光伏自用 = Σ_t (光伏→负荷,t × 电价 t)
       A_储能套利 = Σ_t (储能→负荷,t × 电价 t) − Σ_t (电网→储能,t × 电价 t)
       ρ_损耗     = 往返效率造成的口径残差（通常为 0）

    ② 账单口径闭合（需量电费**单列**，§7.5）：
       S_bill = S + ΔC_需量

    ③ 上网收入代数独立（§7.6 第 3 条）：
       把上网电价设为任意值，S_bill 逐位不变

**为什么 ① 能逐位闭合（关键推导，已实测验证）**：把逐时功率平衡
``购电 = 负荷 − 光伏→负荷 − 储能→负荷 + 电网→储能`` 代入现金口径，
上网项（``光伏→上网 + 储能→上网``）在代入过程中被**完全对消**：

.. code-block:: text

    Σ(购电×电价) = Σ(负荷×电价) − S_光伏自用 − A_储能套利 − ρ_损耗

因此"上网电量"根本没有进入现金口径，**无需为它做任何抵减**。
这正是 V2.0 既定口径（``economic_v2.check_saving_identity``）在
"储能上网开启""电网充电开启"等全部边界下仍然成立的原因：
``compute_metrics`` 的 ``storage_arbitrage_revenue`` 只用 ``load_from_storage``
（供负荷的放电），上网那部分放电电量被排除在套利之外，改由上网收入单独体现。

**上网收入必须独立列示**（§7.6 第 3 条）：它既不在 ``S_bill`` 里（所以不能"再次相加"），
也不能被忽略（否则低估收益）。实测：储能上网开启时上网收入由 0 升到
``Σ(储能→上网 × 上网电价)``，而 ``S_bill`` **逐位不变**。

**两条口径互不替代，也绝不叠加**：现金口径是唯一权威
（``电费节省 = Σ负荷×电价 − Σ购电×电价``），分解口径只用于解释"钱从哪来"。

五、边界条件（"储能上网开启""电网充电开启"）是否仍成立
=================================================
以下三条结论均由 ``taiqu-storage/stage6_diag_identity*.py`` 与
``tests/test_scenario_bill_engine.py`` 的边界用例**实测验证**（不是推理）：

* ``allow_export=True``（储能上网）：上网收入显著增加（本模块实测由 0 升到
  ``Σ(储能→上网 × 上网电价)``），但 ``S_bill`` **逐位不变**
  ——因为储能上网替代的是"卖电"而不是"少买电"，它压根不在购电成本里。
  此时 §7.6 第 3 条最关键：上网收入必须独立列示，
  否则会漏计（低估收益）；反过来若把它加进 ``S_bill`` 就是重复计算。
* ``allow_grid_charge=True``（电网充电）：``C_energy`` 增加（多买电），
  ``A_储能套利`` 相应减少，``S_bill`` 仍然闭合；
  此时光伏充电量减少甚至为 0，边界进一步退化。
* 光伏同时自用与充电：两者按既有 ``dispatch`` 的优先级
  ``光伏 → 负荷 → 储能 → 上网`` 唯一分派，同一度电只会出现在一个去向里，
  因此不存在"同一度电被计入两次"的可能（§20、§7.6）。
* 无储能 / 无光伏：数组自然退化为 0，恒等式退化为
  ``S = S_光伏自用`` 或 ``S = A_储能套利``，仍然逐位闭合。

**成本层面同样存在重复计算风险（本模块已修）**：政府性基金及附加在湖北官方电价表里
已被声明为 ``included_in_tou_price=True``（即**已含在分时电度电价内**）。
若再把它当作一笔独立费用加进 ``total_cost``，就是一次实打实的重复计算——
本模块早期草稿正是如此，导致"账单节省"比现金口径高出
``Σ购电量 × 基金单价``（实测 41,730 元）。现在通过计划自带的
``included_in_tou_price`` 标记自动判定，只在**确实不含**时才相加（§2.3）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np

from ..domain.enums import (
    DispatchStrategy,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
)
from ..domain.scenario_bill import (
    COST_COMPONENT_CALIBERS,
    DEDUP_EXCLUDED_REASON,
    SCENARIO_ORDER,
    DemandMeasureMethod,
    PowerFactorAdjustMode,
    ScenarioBenefitEntry,
    ScenarioBillComparison,
    ScenarioBillLine,
    ScenarioBillSet,
    ScenarioCostComponent,
    ScenarioDedupManifest,
    ScenarioKind,
    ScenarioMonthlyRow,
    label_of_benefit,
    label_of_component,
)
from ..domain.tariff_models import TariffPlan
from ..domain.timeseries import StorageDispatchConfig
from . import economic_v2 as econ
from . import self_consumption as sc_mod
from .dispatch_engine import DispatchOutcome, dispatch
from .errors import ValidationError
from .tariff_plan_engine import require_formal_use
from .tariff_series import TariffSeries, price_for_period, resolve_period_codes, tariff_profile_from_plan
from .timeseries_engine import TimeAxis, axis_from_timestamps

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DEMAND_WINDOW_MINUTES",
    "IDENTITY_TOLERANCE_YUAN",
    "ScenarioConfig",
    "ScenarioEvaluation",
    "build_finance_overrides",
    "build_project_tariff_series",
    "cash_electricity_cost_saving",
    "check_export_not_in_bill_saving",
    "compare_four_scenarios",
    "evaluate_scenario",
    "forgone_export_opportunity_cost",
    "windowed_max_demand_kw",
]

#: 默认需量计量窗口（分钟）。与国内两部制大工业电表的 15 分钟需量窗口一致（§7.5）。
DEFAULT_DEMAND_WINDOW_MINUTES = 15

#: 恒等式校验的绝对容差（元）。与 ``economic_v2.SAVING_IDENTITY_TOLERANCE`` 同一量级。
IDENTITY_TOLERANCE_YUAN = 1e-6

#: 政府性基金及附加的"不参与峰谷浮动"标记来源（§2.3）
_FUND_COMPONENT_TYPES = (TariffComponentType.GOVERNMENT_FUND,)


def _num(value: object) -> float:
    """把 ``None`` 视为 0 的便捷取值（**只用于合计**，缺失语义由调用方另行判断）。"""
    return 0.0 if value is None else float(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 配置与中间结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScenarioConfig:
    """四场景对比的输入配置（V2.3 §7.1、§7.5、§3.4）。
    全部字段都有**显式默认值或显式必填**，不存在"看起来像真实数值的隐含默认"
    （§0.2 红线：不得预填任何未经核验的电价）。

    :param export_price: 上网电价 元/kWh。**默认 0.0 表示"未设置"，不是假设有收益**；
        用户必须按项目实际（脱硫煤基准价 / 市场化交易价 / 合同价）显式给定。
    :param demand_method: 需量取值方法（§7.5）；默认按计量窗口从曲线计算。
    :param demand_window_minutes: 计量窗口（分钟），§7.5 要求必须由用户确认。
    :param demand_fixed_kw: ``demand_method=FIXED`` 时的固定需量 kW（必填）。
    :param bill_baseline_demand_kw: 基准场景采用的**账单计费需量** kW；
        提供时基准场景改用 :attr:`DemandMeasureMethod.BILL` 口径（§7.5 首选真实账单）。
    :param power_factor_mode: 功率因数调整电费的处理方式（§7.5、§3.4）。
    :param power_factor_fixed_yuan: ``FIXED`` 模式下的固定金额（取账单实际值，可为负 = 奖励）。
    :param power_factor_ratio: ``RATIO_OF_ENERGY_CHARGE`` 模式下的比例（相对电度电费，可为负）。
    :param government_fund_yuan_per_kwh: 政府性基金及附加单价 元/kWh；
        ``None`` 表示**未提供**，则从电价计划的分项中提取；两者都缺时按 0 列示并给出警告。
    :param other_unmodeled_mode: 其他未建模费用的处理方式：``fixed`` / ``ratio`` / ``excluded``。
    :param other_unmodeled_yuan: ``fixed`` 模式金额（元）。
    :param other_unmodeled_ratio: ``ratio`` 模式下相对**基准购电量**的分摊比例。
    :param baseline_demand_charge_yuan_per_kw_month: 基准需量电价覆盖（元/kW·月）；
        ``None`` 时取电价计划的需量电价。
    """

    export_price: float = 0.0
    demand_method: DemandMeasureMethod = DemandMeasureMethod.CURVE_WINDOW
    demand_window_minutes: int = DEFAULT_DEMAND_WINDOW_MINUTES
    demand_fixed_kw: float | None = None
    bill_baseline_demand_kw: float | None = None

    power_factor_mode: PowerFactorAdjustMode = PowerFactorAdjustMode.EXCLUDED
    power_factor_fixed_yuan: float = 0.0
    power_factor_ratio: float = 0.0

    government_fund_yuan_per_kwh: float | None = None
    #: 政府性基金及附加是否**已包含在电价计划的电度电价内**。
    #: ``None`` = 由电价计划的 ``included_in_tou_price`` 标记自动判定（推荐，§2.3）；
    #: ``True`` = 只**分类列示**，不再计入 ``total_cost``（**默认口径，避免成本层面重复计算**）；
    #: ``False`` = 电价计划的分时单价**不含**基金及附加，此时才真正加进 ``total_cost``。
    #: 湖北官方电价表的基金及附加已被声明为 ``included_in_tou_price=True``，
    #: 若再作为独立费用相加，就是一次实打实的**重复计算**（§2.3、§7.6）。
    government_fund_included_in_tou_price: bool | None = None
    other_unmodeled_mode: str = "excluded"
    other_unmodeled_yuan: float = 0.0
    other_unmodeled_ratio: float = 0.0

    baseline_demand_charge_yuan_per_kw_month: float | None = None

    #: 仅做哪几个场景（默认四场景全做，§10 阶段 6 第 2 条）
    scenarios: tuple[ScenarioKind, ...] = SCENARIO_ORDER

    # ------------------------------------------------------------------ #
    # 校验（§0.2：所有新增输入均有**中文**校验错误）
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        if float(self.export_price) < 0.0:
            raise ValidationError(
                f"上网电价不能为负（当前 {self.export_price} 元/千瓦时）；"
                "未设置上网电价时请填 0，并确认项目确实不上网（V2.3 §3.5）",
                field="scenario.export_price",
            )
        window = int(self.demand_window_minutes)
        if window <= 0:
            raise ValidationError(
                f"需量计量窗口必须为正整数分钟（当前 {self.demand_window_minutes}）；"
                "国内两部制大工业电表通常为 15 分钟，请核对账单或供电合同（V2.3 §7.5）",
                field="scenario.demand_window_minutes",
            )
        if window > 24 * 60:
            raise ValidationError(
                f"需量计量窗口不能超过 1440 分钟（当前 {window}）；"
                "请核对合同约定的需量计量窗口（V2.3 §7.5）",
                field="scenario.demand_window_minutes",
            )
        if self.demand_method is DemandMeasureMethod.FIXED and self.demand_fixed_kw is None:
            raise ValidationError(
                "需量取值方法选择了『固定需量假设』，但没有填写固定需量值；"
                "请填写固定需量（kW），或改用按计量窗口从曲线计算（V2.3 §7.5）",
                field="scenario.demand_fixed_kw",
            )
        if self.demand_fixed_kw is not None and float(self.demand_fixed_kw) < 0.0:
            raise ValidationError(
                f"固定需量不能为负（当前 {self.demand_fixed_kw} kW）；请核对账单计费需量或合同约定",
                field="scenario.demand_fixed_kw",
            )
        if self.bill_baseline_demand_kw is not None and float(self.bill_baseline_demand_kw) < 0.0:
            raise ValidationError(
                f"账单计费需量不能为负（当前 {self.bill_baseline_demand_kw} kW）；请核对账单（V2.3 §7.5）",
                field="scenario.bill_baseline_demand_kw",
            )
        if self.power_factor_mode is PowerFactorAdjustMode.FIXED and not np.isfinite(
            float(self.power_factor_fixed_yuan)
        ):
            raise ValidationError(
                "功率因数调整电费的固定金额不是有效数值；请填写账单实际金额（可为负 = 奖励，V2.3 §3.4）",
                field="scenario.power_factor_fixed_yuan",
            )
        if self.power_factor_mode is PowerFactorAdjustMode.RATIO_OF_ENERGY_CHARGE and not (
            -1.0 < float(self.power_factor_ratio) < 1.0
        ):
            raise ValidationError(
                f"功率因数调整比例必须在 (−1, 1) 之间（当前 {self.power_factor_ratio}）；"
                "该比例是相对电度电费的显式假设，请按账单实际值反算后填写（V2.3 §7.5）",
                field="scenario.power_factor_ratio",
            )
        if self.government_fund_yuan_per_kwh is not None and float(
            self.government_fund_yuan_per_kwh
        ) < 0.0:
            raise ValidationError(
                f"政府性基金及附加单价不能为负（当前 {self.government_fund_yuan_per_kwh} 元/千瓦时）；"
                "请核对湖北官方电价表的基金及附加分项（V2.3 §2.3、§4.3）",
                field="scenario.government_fund_yuan_per_kwh",
            )
        mode = str(self.other_unmodeled_mode)
        if mode not in ("fixed", "ratio", "excluded"):
            raise ValidationError(
                f"其他未建模费用的处理方式『{self.other_unmodeled_mode}』无法识别；"
                "可选值为 fixed（固定金额）/ ratio（按电量比例分摊）/ excluded（不计入，仅口径对照）"
                "（V2.3 §3.4）",
                field="scenario.other_unmodeled_mode",
            )
        if mode == "ratio" and not (0.0 <= float(self.other_unmodeled_ratio) <= 1.0):
            raise ValidationError(
                f"其他未建模费用的分摊比例必须在 0~1 之间（当前 {self.other_unmodeled_ratio}）；"
                "该比例是相对基准购电量的显式假设，请勿用比例抹平差异（V2.3 §7.4 第 4 条）",
                field="scenario.other_unmodeled_ratio",
            )
        if not self.scenarios:
            raise ValidationError(
                "至少要选择一个场景；四场景对比要求至少包含基准（无光伏、无储能）（V2.3 §7.1）",
                field="scenario.scenarios",
            )
        if ScenarioKind.NO_PV_NO_STORAGE not in self.scenarios:
            raise ValidationError(
                "四场景对比必须包含基准场景『无光伏、无储能』——所有节省额都以它为减数，"
                "缺少基准就无法定义节省额（V2.3 §7.1、§7.6）",
                field="scenario.scenarios",
            )


@dataclass
class ScenarioEvaluation:
    """单场景求值的中间结果（含逐周期数组，供上层复用与测试断言）。"""

    kind: ScenarioKind
    line: ScenarioBillLine
    outcome: DispatchOutcome
    baseline_cost_yuan: float
    metrics: object
    price: np.ndarray
    export_price: np.ndarray
    load: np.ndarray
    pv: np.ndarray


# --------------------------------------------------------------------------- #
# 需量：按配置计量窗口从高频购电曲线计算（§7.5）
# --------------------------------------------------------------------------- #
def windowed_max_demand_kw(
    grid_import_kwh: np.ndarray,
    axis: TimeAxis,
    *,
    window_minutes: int = DEFAULT_DEMAND_WINDOW_MINUTES,
) -> tuple[list[float], float, list[str]]:
    """按**配置的计量窗口**逐月求最大需量（V2.3 §7.5）。

    §7.5 明确"方案后的需量必须由用户确认的计量窗口、负荷曲线、储能充放电曲线计算；
    不能简单使用每小时平均负荷峰值冒充电表需量"。因此本函数的窗口语义是：

    .. code-block:: text

        D_m = max over 窗口 w ⊆ 第 m 月 of ( Σ_{t ∈ w} E_t ÷ Σ_{t ∈ w} Δt )
        D   = max_m D_m

    即"窗口内平均功率"的最大值。当 ``window_minutes`` 恰为一个数据间隔时，
    退化为"逐周期最大功率"；窗口更长时按**点平均**（不插值、不夸大）。

    窗口内出现缺失点（``NaN``）时**整个窗口作废**（§6.4：缺失不得默认按 0 处理），
    作废窗口数在返回值中披露。

    :param grid_import_kwh: 逐周期购电量 kWh（方案侧用调度后的购电曲线）
    :param axis: 时间轴
    :param window_minutes: 计量窗口（分钟）
    :returns: ``(逐月最大需量 kW 列表, 全年最大需量 kW, 中文警告列表)``
    :raises ValidationError: 数组长度与时间轴不一致 / 窗口非正
    """
    energy = np.asarray(grid_import_kwh, dtype=float)
    if energy.size != axis.point_count:
        raise ValidationError(
            f"需量计算的购电曲线长度（{energy.size}）与时间轴点数（{axis.point_count}）不一致；"
            "请检查负荷曲线与时间轴是否对齐（V2.3 §7.5、V2.2 §6.4）",
            field="scenario.grid_import",
        )
    if int(window_minutes) <= 0:
        raise ValidationError(
            f"需量计量窗口必须为正整数分钟（当前 {window_minutes}）；请核对供电合同（V2.3 §7.5）",
            field="scenario.demand_window_minutes",
        )

    dt = float(axis.delta_hours)
    step_minutes = max(dt * 60.0, 1e-9)
    span = max(1, int(round(float(window_minutes) / step_minutes)))

    warnings: list[str] = []
    invalid_windows = 0
    monthly: list[float] = []
    for month in range(1, 13):
        idx = np.flatnonzero(np.asarray(axis.month) == month)
        if idx.size == 0:
            monthly.append(0.0)
            continue
        values = energy[idx]
        if span >= idx.size:
            if np.isfinite(values).all():
                monthly.append(float(values.sum() / (values.size * dt)) if values.size else 0.0)
            else:
                monthly.append(0.0)
                invalid_windows += 1
            continue
        # 滑动窗口和：cumsum 差分，O(n)
        cumulative = np.concatenate(([0.0], np.cumsum(values)))
        window_sums = cumulative[span:] - cumulative[:-span]
        finite = np.isfinite(window_sums)
        invalid_windows += int((~finite).sum())
        if not finite.any():
            monthly.append(0.0)
            continue
        monthly.append(float(np.max(window_sums[finite]) / (span * dt)))

    if invalid_windows:
        warnings.append(
            f"需量计算中有 {invalid_windows} 个计量窗口因含缺失点而作废（未按 0 处理）；"
            "该月的计算需量可能偏低，请先补齐负荷数据（V2.3 §7.5、§6.4）"
        )
    return monthly, (max(monthly) if monthly else 0.0), warnings


# --------------------------------------------------------------------------- #
# 电价：版本化电价计划 → 整年逐周期 TariffSeries（§7.2、§7.3、§7.4）
# --------------------------------------------------------------------------- #
def build_project_tariff_series(
    plan: TariffPlan,
    axis: TimeAxis,
    *,
    export_price: float = 0.0,
    demand_charge_yuan_per_kw_month: float | None = None,
    basic_charge_yuan: float = 0.0,
    require_verified: bool = True,
    as_of=None,
) -> TariffSeries:
    """把**版本化电价计划**解析为**整年逐周期** :class:`TariffSeries`（§7.2、§7.3）。

    为什么不能直接调用 :func:`cenep.calculation.tariff_series.tariff_profile_from_plan`：
    该适配器一次只接受**一个月**（湖北官方时段划分在 7、8 月与其他月份不同，见
    :mod:`cenep.policy.hubei_commercial`），一次调用无法表达"跨月不同时段规则"的整年电价。
    本函数**按月调用既有适配器**、把 12 个月的逐周期电价拼回整年，
    因此**不新建第二套电价引擎**，只是把既有适配器正确地用到 12 个月上（§0.2）。

    时区/长度口径：拼接前逐月校验月内点数，拼接后校验总点数等于 ``axis.point_count``；
    任何不一致都抛**中文**异常，绝不静默补齐。

    :param plan: 电价计划（版本化、有来源、有适用范围）
    :param axis: 整年时间轴
    :param export_price: 上网电价 元/kWh（本引擎**不臆造**，由调用方给定；默认 0）
    :param demand_charge_yuan_per_kw_month: 覆盖需量电价；``None`` 时取计划的需量电价
    :param basic_charge_yuan: 基本电费（元/月）；容量计费模式下由调用方显式给出
    :param require_verified: 是否要求计划可用于正式模拟（§4.2、§7.4 第 6 条）
    :raises ValidationError: 计划不可用 / 时段不闭合 / 月份点数与时间轴不符 / 需量电价缺失
    """
    if require_verified:
        require_formal_use(plan, as_of=as_of)
    if float(export_price) < 0.0:
        raise ValidationError(
            f"上网电价不能为负（当前 {export_price} 元/千瓦时）；未设置时请填 0（V2.3 §3.5）",
            field="scenario.export_price",
        )

    month_array = np.asarray(axis.month)
    pieces_price: list[np.ndarray] = []
    pieces_export: list[np.ndarray] = []
    pieces_code: list[np.ndarray] = []
    demand_values: set[float] = set()

    for month in range(1, 13):
        idx = np.flatnonzero(month_array == month)
        if idx.size == 0:
            continue
        stamps = [axis.timestamps[int(i)] for i in idx]
        sub_axis = axis_from_timestamps(stamps, axis.resolution)
        profile = tariff_profile_from_plan(plan, month=month, export_price=export_price)
        sub_price, sub_export = _resolve_price_piece(profile, sub_axis)
        if sub_price.size != idx.size:
            raise ValidationError(
                f"{month} 月电价解析得到 {sub_price.size} 个点，与时间轴的 {idx.size} 个点不一致；"
                "请检查电价计划的月份时段规则与时间轴是否匹配（V2.3 §4.2、§7.4）",
                field="tariff.time_period_rules",
            )
        pieces_price.append(sub_price)
        pieces_export.append(sub_export)
        pieces_code.append(np.full(idx.size, f"{month:02d}", dtype=object))
        demand_values.add(
            round(
                float(
                    demand_charge_yuan_per_kw_month
                    if demand_charge_yuan_per_kw_month is not None
                    else (plan.demand_charge_yuan_per_kw_month or 0.0)
                ),
                10,
            )
        )

    if not pieces_price:
        raise ValidationError(
            "时间轴没有任何可识别的月份，无法解析电价；请检查负荷数据的日期范围（V2.3 §4.2）",
            field="timeseries.axis",
        )
    price = np.concatenate(pieces_price)
    export = np.concatenate(pieces_export)
    codes = np.concatenate(pieces_code)
    if price.size != axis.point_count:
        raise ValidationError(
            f"整年电价点数（{price.size}）与时间轴点数（{axis.point_count}）不一致；"
            "请检查月份时段规则是否覆盖全年 24 小时（V2.3 §4.2、§7.7）",
            field="tariff.time_period_rules",
        )
    if len(demand_values) > 1:
        raise ValidationError(
            "本阶段电价计划的需量电价被逐月解析出多个不同取值，无法用单一需量电价建模；"
            "请为不同执行期的需量电价分别建立独立的电价计划版本（V2.3 §2.3、§7.4 第 7 条）",
            field="tariff.demand_charge_yuan_per_kw_month",
        )
    demand_charge = next(iter(demand_values)) if demand_values else 0.0

    logger.debug(
        "整年电价解析：计划=%s 点数=%d 购电均价=%.4f 元/kWh 上网价=%.4f 需量电价=%.2f 元/kW·月",
        plan.tariff_plan_id,
        price.size,
        float(price.mean()) if price.size else 0.0,
        float(export_price),
        demand_charge,
    )
    return TariffSeries(
        price=price,
        export_price=export,
        period_code=codes,
        demand_charge=demand_charge,
        basic_charge=float(basic_charge_yuan),
        demand_charge_enabled=demand_charge > 0.0,
        basic_charge_enabled=float(basic_charge_yuan) > 0.0,
    )


def _resolve_price_piece(profile, sub_axis: TimeAxis) -> tuple[np.ndarray, np.ndarray]:
    """解析单个月份的逐周期电价（复用既有 :func:`tariff_series` 的时段/价格函数）。

    这里刻意**不**使用 ``resolve_tariff``：年度增长因子由整年口径统一处理
    （``TariffSeriesConfig.annual_growth_rate`` 在既有实现里乘在购电价上），
    本函数只做最底层的"时段 → 电价"映射，避免重复乘算因子。
    """
    codes, _ = resolve_period_codes(profile, sub_axis)
    price = np.fromiter(
        (price_for_period(profile, TariffPeriod(code)) for code in codes),
        dtype=float,
        count=sub_axis.point_count,
    )
    export = np.full(price.shape, float(profile.export_price), dtype=float)
    if np.any(price < 0.0) or np.any(export < 0.0):
        raise ValidationError(
            "电价出现负值，请检查电价计划的时段单价与上网电价（V2.3 §4.2）",
            field="tariff.time_period_rules",
        )
    return price, export


def extract_government_fund_yuan_per_kwh(plan: TariffPlan) -> float | None:
    """从电价计划的**不参与峰谷浮动**分项中提取政府性基金及附加单价（元/kWh）。

    §2.3 要求"不同电价组成项目是否参与峰谷浮动必须由规则配置明确控制"，
    因此只有 ``component_type=government_fund`` **且** ``adjustable_by_tou=False``
    的分项才被识别。

    :returns: 单价（元/kWh）；未配置该分项时返回 ``None``（**不臆造**）
    """
    total: float | None = None
    for component in plan.price_components:
        if component.component_type not in _FUND_COMPONENT_TYPES:
            continue
        if component.unit is not TariffComponentUnit.YUAN_PER_KWH:
            continue
        if component.adjustable_by_tou or not component.included_in_tou_price:
            continue
        if component.value is None:
            continue
        total = (total or 0.0) + float(component.value)
    return total


def government_fund_included_in_tou_price(plan: TariffPlan) -> bool | None:
    """政府性基金及附加是否**已包含在电价计划的电度电价内**（§2.3）。

    判据是分项自带的 ``included_in_tou_price`` 标记（这是该字段存在的唯一目的，
    见 :class:`~cenep.domain.tariff_models.TariffPriceComponent` 文档）。

    :returns: ``True``（已含，只分类列示）/ ``False``（不含，需相加）/
        ``None``（计划未声明该分项，**无法判断**，调用方必须显式选择而不能默认）
    """
    seen: bool | None = None
    for component in plan.price_components:
        if component.component_type not in _FUND_COMPONENT_TYPES:
            continue
        if component.unit is not TariffComponentUnit.YUAN_PER_KWH:
            continue
        if component.value is None:
            continue
        if seen is None:
            seen = bool(component.included_in_tou_price)
        elif seen != bool(component.included_in_tou_price):
            logger.warning(
                "电价计划 %s 的政府性基金及附加分项对『是否含在分时电价内』的声明不一致，"
                "按『已含』处理；请核对该计划的分项配置（V2.3 §2.3）",
                plan.tariff_plan_id,
            )
            return True
    return seen


# --------------------------------------------------------------------------- #
# 单场景求值（§7.1、§7.5、§7.6）
# --------------------------------------------------------------------------- #
def apply_separate_price_surcharge(
    tariff: TariffSeries,
    *,
    surcharge_yuan_per_kwh: float,
    label: str = "外挂度电附加费",
) -> TariffSeries:
    """把**计划分时电价不含的**度电附加费（政府性基金及附加等）并入逐周期电价（§2.3、§7.6）。

    为什么必须并入 ``price`` 而不是"事后相加"（本阶段实测发现的口径陷阱）：

    现金口径的恒等式

    .. code-block:: text

        Σ(购电 × 边际电价) = Σ(负荷 × 边际电价) − 光伏自用节省 − 储能套利 − 损耗残差

    只在"所有成本都能写成 ``电量 × 同一个逐周期电价``"时成立。
    如果某笔度电费用（例如计划电价不含的政府性基金及附加）在电价之外的
    基准与方案两侧**各自按各自购电量**相加，那么两侧的**边际电价就不同了**，
    恒等式会相差 ``(基准购电量 − 方案购电量) × 附加费单价``。
    受控算例实测该偏差为 61,217 元（仅光伏）与 73,827 元（光储）——
    这正是本模块的去重校验要抓的东西。

    因此正确做法是把附加费**并入逐周期电价**再交给既有调度与经济引擎：
    这样它自然进入 ``电度电费``，边际电价前后一致，恒等式逐位闭合
    （湖北官方电价表本身就是把基金及附加含在分时电价内的，见
    :data:`cenep.policy.hubei_commercial.OFFICIAL_2026_01` 的注 2）。

    :param tariff: 原始电价序列
    :param surcharge_yuan_per_kwh: 附加费单价 元/kWh（0 时原样返回）
    :param label: 中文说明（仅用于日志）
    :returns: 新的 :class:`TariffSeries`（原对象不被修改）
    :raises ValidationError: 附加费为负
    """
    rate = float(surcharge_yuan_per_kwh)
    if rate == 0.0:
        return tariff
    if rate < 0.0:
        raise ValidationError(
            f"{label}不能为负（当前 {rate} 元/千瓦时）；请核对电价计划的分项配置（V2.3 §2.3）",
            field="scenario.government_fund_yuan_per_kwh",
        )
    logger.debug("把%s %.6f 元/kWh 并入逐周期电价（保证边际电价前后一致，§7.6）", label, rate)
    return replace(
        tariff,
        price=np.asarray(tariff.price, dtype=float) + rate,
    )


def evaluate_scenario(
    *,
    kind: ScenarioKind,
    load: np.ndarray,
    pv: np.ndarray,
    axis: TimeAxis,
    tariff: TariffSeries,
    dispatch_config: StorageDispatchConfig,
    project_pv_capacity_kwp: float,
    project_storage_power_kw: float,
    project_storage_energy_kwh: float,
    scenario_config: ScenarioConfig,
    project=None,
    year_index: int = 1,
    government_fund_yuan_per_kwh: float | None = None,
    load_source: str = "",
    load_is_measured: bool = False,
) -> ScenarioEvaluation:
    """对**一个场景**跑完整年度模拟并汇总分项费用（V2.3 §7.1、§7.5）。

    调度调用既有 :func:`cenep.calculation.dispatch_engine.dispatch`；
    基准成本与收益分解调用既有 :func:`cenep.calculation.economic_v2.compute_baseline`
    / ``compute_metrics``；能量流汇总调用既有
    :func:`cenep.calculation.self_consumption.analyze_dispatch_outcome`。
    **本函数不实现任何调度逻辑。**

    :param dispatch_config: **储能调度配置**（既有 :class:`StorageDispatchConfig`，
        原样透传给调度引擎，不复制、不改写）。无储能场景通过把容量/功率传 0 关闭储能，
        既有 ``dispatch`` 会走 ``has_storage=False`` 分支（等价于"无储能"）。
    :param project: 可选的 :class:`~cenep.domain.models.Project`；
        提供时把消纳结果（含能量平衡与中文假设）一并算入，保证与 V2.2 口径一致。
    """
    pv_cap = float(project_pv_capacity_kwp) if kind.has_pv else 0.0
    st_power = float(project_storage_power_kw) if kind.has_storage else 0.0
    st_energy = float(project_storage_energy_kwh) if kind.has_storage else 0.0

    scale = (pv_cap / project_pv_capacity_kwp) if project_pv_capacity_kwp > 0.0 else 0.0
    pv_series = np.asarray(pv, dtype=float) * scale
    load_series = np.asarray(load, dtype=float)

    warnings: list[str] = []
    if kind.has_pv and project_pv_capacity_kwp <= 0.0:
        warnings.append(
            "本场景声明投入光伏，但项目光伏容量为 0 kWp，实际按无光伏计算；"
            "请先在「光伏参数」中填写装机容量（V2.3 §7.1）"
        )

    outcome = dispatch(
        load=load_series,
        pv=pv_series,
        tariff=tariff,
        axis=axis,
        config=dispatch_config,
        storage_capacity_kwh=st_energy,
        storage_power_kw=st_power,
        storage_degradation_rate=0.0,
        replacement_year=None,
        year_index=year_index,
    )

    baseline = econ.compute_baseline(load_series, tariff, axis)
    metrics = econ.compute_metrics(
        outcome,
        axis,
        baseline,
        demand_charge=float(tariff.demand_charge) if tariff.demand_charge_enabled else 0.0,
    )

    # ---- 需量（§7.5）：基准用账单需量、方案按计量窗口；两者都用购电曲线 ---- #
    monthly_curve, peak_curve, demand_warnings = windowed_max_demand_kw(
        outcome.grid_import, axis, window_minutes=scenario_config.demand_window_minutes
    )
    warnings.extend(demand_warnings)

    is_baseline = kind.is_baseline
    if is_baseline and scenario_config.bill_baseline_demand_kw is not None:
        method = DemandMeasureMethod.BILL
        bill_kw = float(scenario_config.bill_baseline_demand_kw)
        # 账单计费需量是**整月一个值**：按账单口径平铺到各月
        monthly_demand = [bill_kw for _ in range(12)]
        measured_monthly = monthly_curve
    elif scenario_config.demand_method is DemandMeasureMethod.FIXED:
        method = DemandMeasureMethod.FIXED
        fixed = float(scenario_config.demand_fixed_kw or 0.0)
        monthly_demand = [fixed for _ in range(12)]
        measured_monthly = monthly_curve
    else:
        method = DemandMeasureMethod.CURVE_WINDOW
        monthly_demand = list(monthly_curve)
        measured_monthly = list(monthly_curve)

    demand_price = float(tariff.demand_charge)
    demand_charge_cost = float(sum(value * demand_price for value in monthly_demand))
    peak_demand = max(monthly_demand) if monthly_demand else 0.0

    # ---- 分项费用（§7.1、§7.5）---- #
    energy_charge = float(np.sum(outcome.grid_import * np.asarray(tariff.price, dtype=float)))
    grid_import_kwh = float(np.sum(outcome.grid_import))
    grid_export_kwh = float(np.sum(outcome.grid_export))
    export_revenue = float(np.sum(outcome.grid_export * np.asarray(tariff.export_price, dtype=float)))

    fund_rate = government_fund_yuan_per_kwh
    fund_note = "取自电价计划的『政府性基金及附加』分项（不参与峰谷浮动）"
    if fund_rate is None:
        fund_rate = 0.0
        fund_note = "电价计划未配置政府性基金及附加分项，本次按 0 列示（不是 0 元电价的结论）"
        warnings.append(
            "电价计划未配置『政府性基金及附加』分项，该费用按 0 列示；"
            "请在电价计划中补齐分项或显式给定单价（V2.3 §2.3、§3.4）"
        )
    fund_cost = float(grid_import_kwh * float(fund_rate))
    # §2.3／§7.6：该分项是否**已含在分时电度电价内**决定它怎么进入成本：
    #   fund_included=True  → 电度电费里已经含了它，此处只做**分类列示**；
    #                          若再加进 total_cost 就是实打实的重复计算。
    #   fund_included=False → 分时单价不含它，**必须并入逐周期电价**（不是事后相加）。
    #                          事后相加会让基准与方案两侧的边际电价不一致，
    #                          从而破坏"分摊恒等式"——实测偏差达 6.1 万元（见
    #                          apply_separate_price_surcharge 的文档）。
    # 本函数收到的 ``tariff`` 已由 :func:`compare_four_scenarios` 预先处理，
    # 因此这里只需按标记决定是否把它计入 total_cost 即可。
    fund_included = scenario_config.government_fund_included_in_tou_price
    if fund_included is None:
        fund_included = True
    if fund_included:
        fund_note += "；该分项已包含在电价计划的电度电价内，此处**只分类列示**，不重复计入成本合计（§2.3、§7.6）"
    else:
        fund_note += (
            "；该分项**不含**在电价计划的电度电价内，已**并入逐周期电价**后计入电度电费"
            "（不是事后相加——事后相加会使基准与方案边际电价不一致，破坏分摊恒等式；§2.3、§7.6）"
        )

    if scenario_config.power_factor_mode is PowerFactorAdjustMode.FIXED:
        pf_cost = float(scenario_config.power_factor_fixed_yuan)
        pf_note = "按用户给定的固定金额（取账单实际值，不随方案变化；§3.4、§7.5）"
    elif scenario_config.power_factor_mode is PowerFactorAdjustMode.RATIO_OF_ENERGY_CHARGE:
        pf_cost = float(energy_charge * float(scenario_config.power_factor_ratio))
        pf_note = (
            f"按电度电费的显式比例假设 {float(scenario_config.power_factor_ratio):+.4%} 分摊"
            "（本阶段不重建力调规则；§3.4、§7.5）"
        )
    else:
        pf_cost = 0.0
        pf_note = "本场景不计入力调电费（仅作口径对照，不得用于正式结论；§7.5）"
        warnings.append(
            "功率因数调整电费未计入（method=excluded）；该费用在真实账单中通常存在，"
            "结论只能作口径对照（V2.3 §7.5）"
        )

    if scenario_config.other_unmodeled_mode == "fixed":
        other_cost = float(scenario_config.other_unmodeled_yuan)
        other_note = "按用户给定的固定金额"
    elif scenario_config.other_unmodeled_mode == "ratio":
        other_cost = float(grid_import_kwh * float(scenario_config.other_unmodeled_ratio))
        other_note = f"按购电量 × 显式分摊比例 {float(scenario_config.other_unmodeled_ratio):.6f} 元/kWh"
    else:
        other_cost = 0.0
        other_note = "不计入（仅作口径对照）"

    total_cost = energy_charge + demand_charge_cost + pf_cost + other_cost
    net_cost = total_cost - export_revenue

    # ---- 分项清单（报告/Excel 直接遍历；§7.5 要求分开列示）---- #
    components = [
        ScenarioCostComponent(
            component="energy_charge",
            label=label_of_component("energy_charge"),
            amount_yuan=energy_charge,
            quantity=grid_import_kwh,
            quantity_unit="kWh",
            unit_price=(energy_charge / grid_import_kwh) if grid_import_kwh else None,
            caliber=COST_COMPONENT_CALIBERS["energy_charge"],
            source_note="电价计划的分时时段规则与表列绝对电价",
        ),
        ScenarioCostComponent(
            component="demand_charge",
            label=label_of_component("demand_charge"),
            amount_yuan=demand_charge_cost,
            quantity=peak_demand,
            quantity_unit="kW",
            unit_price=demand_price,
            caliber=COST_COMPONENT_CALIBERS["demand_charge"],
            source_note=f"需量取值方法：{method.label}",
        ),
        ScenarioCostComponent(
            component="capacity_charge",
            label=label_of_component("capacity_charge"),
            amount_yuan=0.0,
            quantity=None,
            quantity_unit="kVA",
            unit_price=None,
            caliber=COST_COMPONENT_CALIBERS["capacity_charge"],
            source_note="本场景按需量计费，容量计费不适用（两者默认互斥，§7.5）",
            included_in_energy_charge=False,
            included_in_total_cost=False,
        ),
        ScenarioCostComponent(
            component="power_factor_adjustment",
            label=label_of_component("power_factor_adjustment"),
            amount_yuan=pf_cost,
            caliber=COST_COMPONENT_CALIBERS["power_factor_adjustment"],
            source_note=pf_note,
        ),
        ScenarioCostComponent(
            component="government_fund",
            label=label_of_component("government_fund"),
            amount_yuan=fund_cost,
            quantity=grid_import_kwh,
            quantity_unit="kWh",
            unit_price=float(fund_rate),
            # 两种情形下该分项都已在 energy_charge 内（含入 or 并入），因此都是分类列示；
            # 差别只在 source_note 的中文说明（§2.3、§7.6）。
            included_in_energy_charge=True,
            included_in_total_cost=False,
            caliber=COST_COMPONENT_CALIBERS["government_fund"],
            source_note=fund_note,
        ),
        ScenarioCostComponent(
            component="other_unmodeled",
            label=label_of_component("other_unmodeled"),
            amount_yuan=other_cost,
            caliber=COST_COMPONENT_CALIBERS["other_unmodeled"],
            source_note=other_note,
        ),
        ScenarioCostComponent(
            component="export_revenue",
            label=label_of_component("export_revenue"),
            amount_yuan=export_revenue,
            quantity=grid_export_kwh,
            quantity_unit="kWh",
            unit_price=(
                export_revenue / grid_export_kwh if grid_export_kwh > 0.0 else float(np.max(tariff.export_price))
            ),
            is_revenue=True,
            included_in_total_cost=False,
            caliber=COST_COMPONENT_CALIBERS["export_revenue"],
            source_note="上网电价由用户按项目实际（脱硫煤基准价 / 市场化交易价 / 合同价）给定",
        ),
    ]

    monthly_rows = _build_monthly_rows(
        outcome, axis, tariff, monthly_demand, measured_monthly, float(fund_rate)
    )

    # ---- 消纳四项指标（复用 V2.2 唯一口径来源）---- #
    self_rate = sc_mod.ratio_or_none(float(np.sum(outcome.pv_to_load)), float(np.sum(outcome.pv)))
    coverage_rate = sc_mod.ratio_or_none(float(np.sum(outcome.pv_to_load)), float(np.sum(outcome.load)))
    export_rate = sc_mod.ratio_or_none(float(np.sum(outcome.grid_export)), float(np.sum(outcome.pv)))
    grid_rate = sc_mod.ratio_or_none(grid_import_kwh, float(np.sum(outcome.load)))

    line = ScenarioBillLine(
        kind=kind,
        label=kind.label,
        is_baseline=is_baseline,
        pv_capacity_kwp=pv_cap,
        storage_power_kw=st_power,
        storage_energy_kwh=st_energy,
        load_kwh=float(np.sum(outcome.load)),
        pv_generation_kwh=float(np.sum(outcome.pv)),
        pv_to_load_kwh=float(np.sum(outcome.pv_to_load)),
        pv_to_storage_kwh=float(np.sum(outcome.pv_to_storage)),
        pv_curtailed_kwh=float(np.sum(outcome.pv_curtailed)),
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        storage_charge_kwh=float(np.sum(outcome.storage.charge_ac)),
        storage_discharge_kwh=float(np.sum(outcome.storage.discharge_ac)),
        storage_grid_charge_kwh=float(np.sum(outcome.grid_to_storage)),
        self_consumption_rate=self_rate,
        load_coverage_rate=coverage_rate,
        export_rate=export_rate,
        grid_dependency_rate=grid_rate,
        demand_method=method,
        demand_method_label=method.label,
        demand_window_minutes=int(scenario_config.demand_window_minutes),
        peak_demand_kw=float(peak_demand),
        monthly_billable_demand_kw=[float(v) for v in monthly_demand],
        energy_charge_yuan=energy_charge,
        demand_charge_yuan=demand_charge_cost,
        capacity_charge_yuan=0.0,
        power_factor_adjustment_yuan=pf_cost,
        government_fund_yuan=fund_cost,
        other_unmodeled_yuan=other_cost,
        export_revenue_yuan=export_revenue,
        total_cost_yuan=total_cost,
        net_cost_yuan=net_cost,
        average_price_yuan_per_kwh=(total_cost / grid_import_kwh) if grid_import_kwh else None,
        cost_components=components,
        monthly=monthly_rows,
        pv_self_consumption_saving_yuan=float(metrics.pv_self_consumption_saving),
        storage_arbitrage_revenue_yuan=float(metrics.storage_arbitrage_revenue),
        demand_saving_kw=float(baseline.peak_demand_kw - peak_demand),
        demand_cost_saving_yuan=float(baseline.annual_demand_cost - demand_charge_cost),
        balance_is_balanced=True,
        data_quality_status="valid",
        calculation_warnings=warnings,
        assumptions=[],
    )

    evaluation = ScenarioEvaluation(
        kind=kind,
        line=line,
        outcome=outcome,
        baseline_cost_yuan=float(baseline.annual_electricity_cost),
        metrics=metrics,
        price=np.asarray(tariff.price, dtype=float),
        export_price=np.asarray(tariff.export_price, dtype=float),
        load=load_series,
        pv=pv_series,
    )
    _ = (load_source, load_is_measured)
    return evaluation


def _build_monthly_rows(
    outcome: DispatchOutcome,
    axis: TimeAxis,
    tariff: TariffSeries,
    monthly_demand: Sequence[float],
    monthly_measured: Sequence[float],
    fund_rate: float,
) -> list[ScenarioMonthlyRow]:
    """构造逐月费用与需量明细（§7.7：输出每月费用拆分）。

    逐月行严格由逐周期数组按月聚合，因此逐月合计与年度值一致（差额只在浮点末位）。
    需要说明的口径：逐月行只列 **电度电费 / 需量电费 / 政府性基金及附加 / 上网收入**
    四项（这四项天生按月可分解）；力调电费与其他未建模费用是**年度口径的显式假设**，
    不分摊到月份，年度合计请直接读 :class:`~cenep.domain.scenario_bill.ScenarioBillLine`。
    """
    price = np.asarray(tariff.price, dtype=float)
    rows: list[ScenarioMonthlyRow] = []
    for month in range(1, 13):
        idx = np.flatnonzero(np.asarray(axis.month) == month)
        if idx.size == 0:
            continue
        demand_kw = float(monthly_demand[month - 1]) if month <= len(monthly_demand) else 0.0
        measured_kw = float(monthly_measured[month - 1]) if month <= len(monthly_measured) else 0.0
        rows.append(
            ScenarioMonthlyRow(
                month=month,
                billing_month=f"{axis.year}-{month:02d}",
                grid_import_kwh=float(np.sum(outcome.grid_import[idx])),
                grid_export_kwh=float(np.sum(outcome.grid_export[idx])),
                pv_generation_kwh=float(np.sum(outcome.pv[idx])),
                pv_to_load_kwh=float(np.sum(outcome.pv_to_load[idx])),
                load_kwh=float(np.sum(outcome.load[idx])),
                storage_charge_kwh=float(np.sum(outcome.storage.charge_ac[idx])),
                storage_discharge_kwh=float(np.sum(outcome.storage.discharge_ac[idx])),
                energy_charge_yuan=float(np.sum(outcome.grid_import[idx] * price[idx])),
                demand_charge_yuan=demand_kw * float(tariff.demand_charge),
                government_fund_yuan=float(np.sum(outcome.grid_import[idx]) * float(fund_rate)),
                export_revenue_yuan=float(
                    np.sum(outcome.grid_export[idx] * np.asarray(tariff.export_price, dtype=float)[idx])
                ),
                billable_demand_kw=demand_kw,
                demand_measured_kw=measured_kw,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# 去重校验（§7.6 —— 本阶段的核心）
# --------------------------------------------------------------------------- #
def cash_electricity_cost_saving(
    load: np.ndarray, tariff: TariffSeries, outcome: DispatchOutcome
) -> float:
    """**现金口径**电费节省（唯一权威，V2.3 §7.6、V2.0 ``economic_v2`` 模块文档）。

    .. code-block:: text

        S = Σ_t (负荷_t × 电价_t) − Σ_t (购电_t × 电价_t)

    本函数只是把 ``economic_v2`` 的定义显式重述一遍，供去重校验**独立**核对
    （校验必须有自己的算式，不能拿被校验对象的中间值当作校验依据）。
    """
    price = np.asarray(tariff.price, dtype=float)
    return float(np.sum(np.asarray(load, dtype=float) * price) - np.sum(outcome.grid_import * price))


def forgone_export_opportunity_cost(
    outcome: DispatchOutcome, export_price: np.ndarray
) -> float:
    """光伏充电的**上网机会成本**（元）= ``Σ_t (光伏→储能,t × 上网电价 t)``。

    本函数**不参与**去重恒等式，只用于**边界分析与报告披露**：它回答
    "光伏充电这部分电量如果上网能卖多少钱"，是解释"为什么光伏充电按 0 计价"
    的量化依据（§7.6、§3.5）。

    .. warning::

       实测（``taiqu-storage/stage6_diag_identity*.py``，2026-10）证明：
       **把它从分解口径里扣除会破坏恒等式**。原因是"上网电量"
       （``pv_to_grid + storage_to_grid``）根本没有进入现金口径
       ``S = Σ(负荷×电价) − Σ(购电×电价)``，因此无需为它做任何抵减；
       ``compute_metrics`` 里 ``storage_arbitrage_revenue`` 只用
       ``load_from_storage``（供负荷的放电），上网那部分放电电量已被排除在套利之外。
       正确的分解恒等式是 ``S = 光伏自用节省 + 储能套利收益 + 储能损耗残差``，
       与 :func:`cenep.calculation.economic_v2.check_saving_identity` 完全一致。
       本函数因此**不得**再被任何恒等式使用（早期草稿曾误用，已更正）。
    """
    return float(np.sum(np.asarray(outcome.pv_to_storage, dtype=float) * np.asarray(export_price, dtype=float)))


def storage_loss_residual_yuan(outcome: DispatchOutcome) -> float:
    """储能往返效率损失的**口径残差**（元，通常为很小的正数）。

    .. code-block:: text

        ρ = Σ_t 购电_t×电价_t − (Σ_t 负荷_t×电价_t − Σ_t 光伏→负荷×电价_t)
                                     + (Σ_t 储能→负荷×电价_t − Σ_t 电网→储能×电价_t)

    当电池无损耗、且时间窗口的首末 SOC 相同时 ``ρ = 0``；损耗存在时它是一个
    **残差项**（不是收益、也不是成本的新来源），报告必须列出而不得并入任何收益项。
    """
    price = np.asarray(outcome.price, dtype=float)
    grid_import_cost = float(np.sum(outcome.grid_import * price))
    load_cost = float(np.sum(np.asarray(outcome.load, dtype=float) * price))
    pv_self = float(np.sum(outcome.pv_to_load * price))
    discharge_value = float(np.sum(outcome.load_from_storage * price))
    charge_cost = float(np.sum(outcome.grid_to_storage * price))
    return grid_import_cost - (load_cost - pv_self) + (discharge_value - charge_cost)


def check_export_not_in_bill_saving(
    *,
    baseline_cost_yuan: float,
    scenario_cost_yuan: float,
    export_revenue_yuan: float,
    tolerance: float = IDENTITY_TOLERANCE_YUAN,
) -> float:
    """校验**上网收入未包含在账单节省额内**（V2.3 §7.6 第 3 条）。

    规格书原文："上网电量收益可以独立计算，但必须确保未包含在账单节省额中。"

    本函数给出的可执行判据是"**代数独立性**"：
    账单节省额 ``S_bill = 基准成本 − 方案成本`` 的定义式中**只有购电成本**，
    上网收入 ``R_export`` 根本不出现在任何一项里。因此把 ``R_export`` 用同一数值
    替换两次（等价于"任意改变上网收入"）后，``S_bill`` 必须逐位不变：

    .. code-block:: text

        Δ = |（基准成本 − 方案成本）−（基准成本' − 方案成本'）| ，其中
            基准成本' = 基准成本 − R_export、方案成本' = 方案成本 − R_export

    由于两个成本各减去**同一个** ``R_export``，差值在数学上恒为 0（与数值无关），
    这正是"上网收入不在账单节省额内"的代数证据。**若实现里误把上网收入
    加进了某一侧成本，本校验会立刻返回非 0。**

    :returns: 偏差（元）。调用方应在偏差 > ``tolerance`` 时判定计算失败。
    """
    baseline_shifted = float(baseline_cost_yuan) - float(export_revenue_yuan)
    scenario_shifted = float(scenario_cost_yuan) - float(export_revenue_yuan)
    base_saving = float(baseline_cost_yuan) - float(scenario_cost_yuan)
    shifted_saving = baseline_shifted - scenario_shifted
    deviation = abs(base_saving - shifted_saving)
    if deviation > tolerance:
        raise ValidationError(
            f"上网收入与账单节省额存在算术耦合（偏差 {deviation:.6e} 元，容差 {tolerance:.6e} 元）："
            "上网收入必须独立列示，既不能计入也不能并入账单节省额（V2.3 §7.6 第 3 条）",
            field="scenario.export_revenue",
        )
    return deviation


# --------------------------------------------------------------------------- #
# 四场景总编排（§7.1、§7.6、§10 阶段 6）
# --------------------------------------------------------------------------- #
def compare_four_scenarios(
    *,
    plan: TariffPlan,
    axis: TimeAxis,
    load: np.ndarray,
    pv: np.ndarray,
    dispatch_config: StorageDispatchConfig,
    project_pv_capacity_kwp: float,
    project_storage_power_kw: float,
    project_storage_energy_kwh: float,
    config: ScenarioConfig | None = None,
    project=None,
    year_index: int = 1,
    require_verified: bool = True,
    basic_charge_yuan: float = 0.0,
    load_source: str = "",
    load_is_measured: bool = False,
    storage_capacity_revenue_yuan: float = 0.0,
    storage_ancillary_revenue_yuan: float = 0.0,
    storage_other_revenue_yuan: float = 0.0,
    reference_scenario: ScenarioKind = ScenarioKind.PV_STORAGE,
) -> ScenarioBillSet:
    """在**同一负荷、同一电价计划**下生成四场景并完成收益去重（V2.3 §7.1、§7.6）。

    步骤（严格按规格书 §10 阶段 6）：

    1. 用既有适配器 + 月度拼接把电价计划解析为**整年逐周期电价**（§7.2）；
    2. 对四个场景分别调用**既有调度引擎**（§10 阶段 6 第 1 条）；
    3. 汇总方案电费差额、需量变化与分项费用（第 3 条）；
    4. 用**唯一口径**计算每个场景的收益并做去重校验（§7.6，第 4 条）；
    5. 选出参考场景，把去重后的**唯一数值**转成财务引擎的 ``YearOverride``（第 4 条）。

    :param reference_scenario: 哪个场景的节省额作为财务输入（默认光储）；
        仅光伏项目应显式传 ``ScenarioKind.PV_ONLY``。
    :raises ValidationError: 电价不可用、场景缺基准、负荷/光伏长度不符等（全部**中文**报错）
    """
    cfg = config or ScenarioConfig()
    load_series = np.asarray(load, dtype=float)
    pv_series = np.asarray(pv, dtype=float)
    if load_series.size != axis.point_count:
        raise ValidationError(
            f"负荷曲线点数（{load_series.size}）与时间轴点数（{axis.point_count}）不一致；"
            "请检查负荷数据与时间轴是否对齐（V2.3 §7.1、V2.2 §6.4）",
            field="scenario.load",
        )
    if pv_series.size not in (0, axis.point_count):
        raise ValidationError(
            f"光伏曲线点数（{pv_series.size}）与时间轴点数（{axis.point_count}）不一致；"
            "请检查光伏数据与时间轴是否对齐（V2.3 §7.1、V2.2 §6.4）",
            field="scenario.pv",
        )
    if pv_series.size == 0:
        pv_series = np.zeros(axis.point_count, dtype=float)

    tariff = build_project_tariff_series(
        plan,
        axis,
        export_price=float(cfg.export_price),
        demand_charge_yuan_per_kw_month=cfg.baseline_demand_charge_yuan_per_kw_month,
        basic_charge_yuan=basic_charge_yuan,
        require_verified=require_verified,
    )
    if not tariff.demand_charge_enabled:
        raise ValidationError(
            "电价计划没有可用的最大需量电价（元/千瓦·月），无法模拟两部制基本电费；"
            "请在电价计划中补录需量电价，或显式给出需量电价覆盖值（V2.3 §7.5、§3.4）",
            field="tariff.demand_charge_yuan_per_kw_month",
        )

    fund_rate = (
        float(cfg.government_fund_yuan_per_kwh)
        if cfg.government_fund_yuan_per_kwh is not None
        else extract_government_fund_yuan_per_kwh(plan)
    )
    # §2.3：政府性基金及附加是否已含在电度电价内，由计划的 included_in_tou_price 标记决定。
    # 未声明时按"已含"处理（湖北官方电价表即如此），并且**只在确实需要相加时才相加**，
    # 从根上避免成本层面的重复计算（§7.6）。
    fund_included = cfg.government_fund_included_in_tou_price
    if fund_included is None:
        declared = government_fund_included_in_tou_price(plan)
        fund_included = True if declared is None else bool(declared)
    holder = replace(cfg, government_fund_included_in_tou_price=bool(fund_included))
    if not fund_included and fund_rate:
        # 分时电价不含该附加费 → **并入逐周期电价**再交给既有引擎（不是事后相加），
        # 否则基准与方案的边际电价不一致，分摊恒等式会差
        # (基准购电量 − 方案购电量) × 附加费单价（§2.3、§7.6）。
        tariff = apply_separate_price_surcharge(
            tariff,
            surcharge_yuan_per_kwh=float(fund_rate),
            label="政府性基金及附加",
        )
    evaluations: list[ScenarioEvaluation] = []
    for kind in cfg.scenarios:
        evaluations.append(
            evaluate_scenario(
                kind=kind,
                load=load_series,
                pv=pv_series,
                axis=axis,
                tariff=tariff,
                dispatch_config=dispatch_config,
                project_pv_capacity_kwp=float(project_pv_capacity_kwp),
                project_storage_power_kw=float(project_storage_power_kw),
                project_storage_energy_kwh=float(project_storage_energy_kwh),
                scenario_config=holder,
                project=project,
                year_index=year_index,
                government_fund_yuan_per_kwh=fund_rate,
                load_source=load_source,
                load_is_measured=load_is_measured,
            )
        )

    by_kind = {item.kind: item for item in evaluations}
    baseline_eval = by_kind.get(ScenarioKind.NO_PV_NO_STORAGE)
    if baseline_eval is None:
        raise ValidationError(
            "四场景对比缺少基准场景『无光伏、无储能』，无法计算任何节省额（V2.3 §7.1）",
            field="scenario.scenarios",
        )

    # ---- 同一负荷校验（§7.1）---- #
    loads = {item.line.kind: item.line.load_kwh for item in evaluations}
    if len({round(value, 6) for value in loads.values()}) > 1:
        raise ValidationError(
            "四场景的负荷电量不一致，违反『同一负荷下比较』的前提（V2.3 §7.1）："
            + "、".join(f"{kind.short_label} {value:,.2f} kWh" for kind, value in loads.items()),
            field="scenario.load",
        )

    comparisons: list[ScenarioBillComparison] = []
    max_deviation = 0.0
    dedup_ok = True
    for item in evaluations:
        if item.kind.is_baseline:
            continue
        comparison, deviation, passed = _build_comparison(
            item,
            baseline_eval,
            tariff,
            holder,
            storage_capacity_revenue_yuan=storage_capacity_revenue_yuan,
            storage_ancillary_revenue_yuan=storage_ancillary_revenue_yuan,
            storage_other_revenue_yuan=storage_other_revenue_yuan,
        )
        comparisons.append(comparison)
        max_deviation = max(max_deviation, deviation)
        dedup_ok = dedup_ok and passed

    reference = by_kind.get(reference_scenario)
    if reference is None:
        raise ValidationError(
            f"作为财务输入的参考场景『{reference_scenario.label}』不在本次要模拟的场景列表中；"
            "请把该场景加入 scenario 选择，或改用已选择的场景作为参考（V2.3 §3.5、§7.6）",
            field="scenario.scenarios",
        )
    if reference.line.grid_import_kwh <= 0.0:
        raise ValidationError(
            f"参考场景『{reference_scenario.label}』没有购入任何电量，其节省额不构成"
            "有效的年度运营收益（可能因为负荷为 0 或全部由自发电供应）；"
            "请改用其他场景作为财务输入（V2.3 §3.5）",
            field="scenario.reference_scenario",
        )
    reference_comparison = next(
        (c for c in comparisons if c.scenario.kind is reference_scenario), None
    )
    unique_benefit = (
        float(reference_comparison.dedup.unique_annual_benefit_yuan)
        if reference_comparison is not None
        else 0.0
    )

    messages = [
        f"四场景对比（电价计划 {plan.display_name}；基准年 {axis.year}；{axis.describe()}）",
        f"基准场景（无光伏、无储能）电费成本 {baseline_eval.line.total_cost_yuan:,.2f} 元"
        f"（其中电度电费 {baseline_eval.line.energy_charge_yuan:,.2f} 元、"
        f"需量电费 {baseline_eval.line.demand_charge_yuan:,.2f} 元）",
    ]
    warnings: list[str] = []
    for comparison in comparisons:
        messages.append(
            f"{comparison.scenario.label}：电费成本 {comparison.scenario.total_cost_yuan:,.2f} 元，"
            f"账单节省 {comparison.bill_saving_yuan:,.2f} 元"
            + (
                f"（{comparison.bill_saving_rate:+.2%}）"
                if comparison.bill_saving_rate is not None
                else ""
            )
            + f"，需量削减 {comparison.demand_reduction_kw:,.2f} kW，"
            f"上网收入 {comparison.scenario.export_revenue_yuan:,.2f} 元"
        )
    messages.append(
        f"**唯一去重后的年度运营收益 = {unique_benefit:,.2f} 元**"
        f"（取自{reference_scenario.label}；财务引擎只接收这一个数值，V2.3 §3.5、§7.6）"
    )
    for item in evaluations:
        warnings.extend(item.line.calculation_warnings)
    if (
        any(item.kind is ScenarioKind.STORAGE_ONLY for item in evaluations)
        and dispatch_config.strategy is DispatchStrategy.PV_SELF_CONSUMPTION
    ):
        warnings.append(
            "『仅储能』场景使用的是『光伏自用优先』策略，而该场景没有光伏，"
            "因此储能不会动作、节省额为 0。若要看储能套利价值，请把调度策略改为"
            "『峰谷套利』或『经济优化』（V2.3 §7.1、V2 §12、§14）"
        )
    if not dedup_ok:
        warnings.append(
            "至少有一个场景的收益去重校验未通过；**禁止**把结果用于财务测算（V2.3 §7.6）"
        )
    if not load_is_measured:
        warnings.append(
            "负荷数据不是实测高频曲线（"
            + (load_source or "来源未标注")
            + "）：按规格书 §7.7 末条，报告**不得**声称精确计算逐时消纳率或需量削减效果"
        )

    assumptions = [
        "四场景使用**同一负荷曲线、同一电价计划、同一套需求与其他费用假设**，"
        "仅设备配置不同（V2.3 §7.1）",
        "账单节省 S_bill = 基准电费成本 − 方案电费成本，是节省额的**唯一**来源（V2.3 §7.6 第 1 条）",
        "上网收入单列为收入，且**数学上不在** S_bill 内（S_bill 只由购电成本构成；§7.6 第 3 条）",
        "光伏自用节省与储能套利收益只作**分解展示**，已包含在 S_bill 中，不再累加（§7.6 第 2、4 条）",
        f"需量口径：计量窗口 {cfg.demand_window_minutes} 分钟（§7.5）；"
        f"基准场景需量方法：{baseline_eval.line.demand_method_label}",
        "政府性基金及附加已含在分时电度电价内，此处按计划分项**分类列示**，不是二次加价（§2.3）",
        "功率因数调整电费：" + PowerFactorAdjustMode(cfg.power_factor_mode).label
        + "（本阶段不重建力调规则；§3.4、§7.5）",
        "本阶段不做增值税建模，沿用既有财务引擎的简化处理（§3.5 末条）",
        f"电价来源：{plan.source_text}",
    ]

    result = ScenarioBillSet(
        tariff_plan_id=plan.tariff_plan_id,
        tariff_plan_name=plan.name,
        tariff_plan_source=plan.source_text,
        tariff_plan_status=plan.status.label,
        load_source=load_source,
        load_is_measured=load_is_measured,
        interval_minutes=int(round(float(axis.delta_hours) * 60)),
        base_year=int(axis.year),
        point_count=int(axis.point_count),
        baseline=baseline_eval.line,
        scenarios=[item.line for item in evaluations],
        comparisons=comparisons,
        unique_annual_benefit_yuan=unique_benefit,
        reference_scenario=reference_scenario,
        dedup_verified=dedup_ok,
        identity_max_deviation_yuan=max_deviation,
        messages=messages,
        warnings=warnings,
        assumptions=assumptions,
    )
    logger.info(
        "四场景对比完成：基准 %.2f 元，节省额 %s，唯一去重年度收益 %.2f 元，去重校验=%s",
        baseline_eval.line.total_cost_yuan,
        "、".join(f"{c.scenario.kind.short_label} {c.bill_saving_yuan:,.2f}" for c in comparisons),
        unique_benefit,
        "通过" if dedup_ok else "未通过",
    )
    return result


def _build_comparison(
    item: ScenarioEvaluation,
    baseline_eval: ScenarioEvaluation,
    tariff: TariffSeries,
    cfg: ScenarioConfig,
    *,
    storage_capacity_revenue_yuan: float,
    storage_ancillary_revenue_yuan: float,
    storage_other_revenue_yuan: float,
) -> tuple[ScenarioBillComparison, float, bool]:
    """构造"方案 vs 基准"的对比与**收益去重清单**（§7.1、§7.6）。"""
    line = item.line
    base = baseline_eval.line
    metrics = item.metrics

    # ---- 需量电费节省：用**真实计费需量口径**的差额覆盖引擎内的峰值近似（§7.5）---- #
    # ``economic_v2.compute_metrics`` 的 demand_cost_saving 基于"逐周期最大功率"，
    # 而 §7.5 要求按**配置的计量窗口**取值，因此这里用逐月计费需量的差额重算，
    # 并同步写回结果行，保证"报告里的需量节省"与"分项费用差"是同一个数。
    line.demand_cost_saving_yuan = float(base.demand_charge_yuan - line.demand_charge_yuan)
    line.demand_saving_kw = float(base.peak_demand_kw - line.peak_demand_kw)

    bill_saving = float(base.total_cost_yuan - line.total_cost_yuan)
    export_delta = float(line.export_revenue_yuan - base.export_revenue_yuan)
    net_saving = float(base.net_cost_yuan - line.net_cost_yuan)
    rate = (bill_saving / base.total_cost_yuan) if base.total_cost_yuan else None

    # ---- §7.6 第 3 条：上网收入必须与账单节省额代数独立 ----
    export_independence = check_export_not_in_bill_saving(
        baseline_cost_yuan=float(base.total_cost_yuan),
        scenario_cost_yuan=float(line.total_cost_yuan),
        export_revenue_yuan=float(line.export_revenue_yuan),
    )

    # ---- 恒等式校验（§7.6）：三条**互相独立**的校验 ----
    #   ① 分解口径闭合（含储能损耗残差）：
    #        电度电费节省 = 光伏自用节省 + 储能套利收益 + 储能损耗残差
    #   ② 账单口径闭合（需量电费**单列**）：
    #        账单节省 = 电度电费节省 + 需量电费节省
    #   ③ 上网收入代数独立（§7.6 第 3 条）
    cash_saving = cash_electricity_cost_saving(item.load, tariff, item.outcome)
    pv_self = float(metrics.pv_self_consumption_saving)
    arbitrage = float(metrics.storage_arbitrage_revenue)
    residual = storage_loss_residual_yuan(item.outcome)
    demand_delta = float(base.demand_charge_yuan - line.demand_charge_yuan)
    decomposed = pv_self + arbitrage + residual
    tolerance = max(IDENTITY_TOLERANCE_YUAN, abs(cash_saving) * 1e-9)
    deviation = abs(decomposed - cash_saving)
    saving_deviation = abs(bill_saving - (cash_saving + demand_delta))
    passed = (
        deviation <= tolerance
        and saving_deviation <= tolerance
        and export_independence <= IDENTITY_TOLERANCE_YUAN
    )
    deviation = max(deviation, saving_deviation)

    month_reduction = [
        float(base.monthly_billable_demand_kw[i] - line.monthly_billable_demand_kw[i])
        if i < len(base.monthly_billable_demand_kw) and i < len(line.monthly_billable_demand_kw)
        else 0.0
        for i in range(12)
    ]

    equations = [
        "① 现金口径（唯一权威）：电费节省 S = Σ(负荷×电价) − Σ(购电×电价)",
        "② 分解口径（仅展示）：S = 光伏自用节省 + 储能套利收益 + 储能损耗残差；"
        "其中光伏给储能充电按 0 计价、电网充电按购电价计价，因此同一度电只计价一次",
        "③ 账单节省（唯一来源）：S_bill = 基准电费成本 − 方案电费成本 = S + 需量电费节省"
        "（力调/政府性基金/其他未建模三项在本场景前后相同或按同一假设分摊时差值为 0）",
        "④ 唯一运营收益：B = S_bill + 上网收入 + 储能容量/辅助服务/其他收益",
        "⑤ 上网收入独立性：把上网电价设为任意值，S_bill 逐位不变（§7.6 第 3 条）",
    ]

    notes = [
        "光伏自用电量在『光伏自用节省』里计价一次；若该电量先充入储能、"
        "其价值只在放电替代购电时再体现一次，充电侧按 **0 计价**（V2.0 既定口径）——"
        "因此同一度电不重复计价。",
        f"本场景光伏充电 {line.pv_to_storage_kwh:,.2f} kWh，"
        f"对应上网机会成本 {forgone_export_opportunity_cost(item.outcome, item.export_price):,.2f} 元"
        f"（上网电价 {cfg.export_price} 元/kWh）；该机会成本是『为什么光伏充电按 0 计价』的"
        "量化依据，**不参与恒等式**（上网电量本就不在现金口径内，无需抵减）。",
        f"储能损耗残差 {residual:,.2f} 元：这是往返效率造成的口径残差，"
        "既不是收益也不是成本的新来源，报告必须单列、不得并入任何收益项。",
        "**储能上网开启（allow_export=True）时**：上网收入增加，但 S_bill 不变"
        "（储能上网替代的是『卖电』而不是『少买电』）；此时必须把上网收入独立列示，"
        "既不能漏计（漏掉会低估收益）也不能加进 S_bill（加进去就是重复计算）。",
        "**电网充电开启（allow_grid_charge=True）时**：电度电费因多买电而上升、"
        "储能套利收益相应下降，S_bill 仍然闭合；此时光伏充电量为 0 或更少，"
        "上网机会成本随之减小，退化为 V2.0 的窄口径恒等式。",
    ]

    entries = [
        ScenarioBenefitEntry(
            key="electricity_cost_saving",
            label=label_of_benefit("electricity_cost_saving"),
            amount_yuan=bill_saving,
            counted_in_unique_benefit=True,
            entry_role="unique",
            caliber="基准电费成本 − 方案电费成本（§3.4、§7.6 第 1 条）",
        ),
        ScenarioBenefitEntry(
            key="export_revenue",
            label=label_of_benefit("export_revenue"),
            amount_yuan=float(line.export_revenue_yuan),
            counted_in_unique_benefit=True,
            entry_role="unique",
            caliber="Σ 上网电量 × 上网电价；独立于购电成本，数学上不在账单节省额内（§7.6 第 3 条）",
        ),
        ScenarioBenefitEntry(
            key="pv_self_consumption_saving",
            label=label_of_benefit("pv_self_consumption_saving"),
            amount_yuan=pv_self,
            counted_in_unique_benefit=False,
            entry_role="decomposition",
            excluded_reason=DEDUP_EXCLUDED_REASON,
            caliber="Σ 光伏→负荷 × 当期购电价（分解口径，§7.6 第 2 条）",
        ),
        ScenarioBenefitEntry(
            key="storage_arbitrage_revenue",
            label=label_of_benefit("storage_arbitrage_revenue"),
            amount_yuan=arbitrage,
            counted_in_unique_benefit=False,
            entry_role="decomposition",
            excluded_reason=DEDUP_EXCLUDED_REASON,
            caliber="Σ 储能→负荷 × 购电价 − Σ 电网→储能 × 购电价"
            "（光伏→储能按 0 计价；分解口径，§7.6 第 4 条）",
        ),
        ScenarioBenefitEntry(
            key="demand_cost_saving",
            label=label_of_benefit("demand_cost_saving"),
            amount_yuan=float(line.demand_cost_saving_yuan),
            counted_in_unique_benefit=False,
            entry_role="decomposition",
            excluded_reason=DEDUP_EXCLUDED_REASON
            + "；需量电费已作为**分项**计入 total_cost，其差额已含在 S_bill 中（§7.5 要求分列展示）",
            caliber="基准需量电费 − 方案需量电费（分解口径，§7.5）",
        ),
        ScenarioBenefitEntry(
            key="storage_capacity_revenue",
            label=label_of_benefit("storage_capacity_revenue"),
            amount_yuan=float(storage_capacity_revenue_yuan),
            counted_in_unique_benefit=abs(float(storage_capacity_revenue_yuan)) > 0.0,
            entry_role="external",
            excluded_reason=(
                ""
                if abs(float(storage_capacity_revenue_yuan)) > 0.0
                else "本项目未配置储能容量收益，金额为 0"
            ),
            caliber="项目参数给定的外部收入（需求响应 / 容量补偿），与账单节省无重叠（§7.6 第 4 条）",
        ),
        ScenarioBenefitEntry(
            key="storage_ancillary_revenue",
            label=label_of_benefit("storage_ancillary_revenue"),
            amount_yuan=float(storage_ancillary_revenue_yuan),
            counted_in_unique_benefit=abs(float(storage_ancillary_revenue_yuan)) > 0.0,
            entry_role="external",
            excluded_reason=(
                ""
                if abs(float(storage_ancillary_revenue_yuan)) > 0.0
                else "本项目未配置储能辅助服务收益，金额为 0"
            ),
            caliber="项目参数给定的外部收入（辅助服务），与账单节省无重叠（§7.6 第 4 条）",
        ),
        ScenarioBenefitEntry(
            key="storage_other_revenue",
            label=label_of_benefit("storage_other_revenue"),
            amount_yuan=float(storage_other_revenue_yuan),
            counted_in_unique_benefit=abs(float(storage_other_revenue_yuan)) > 0.0,
            entry_role="external",
            excluded_reason=(
                ""
                if abs(float(storage_other_revenue_yuan)) > 0.0
                else "本项目未配置储能其他收益，金额为 0"
            ),
            caliber="项目参数给定的外部收入，与账单节省无重叠（§7.6 第 4 条）",
        ),
    ]

    external = (
        float(storage_capacity_revenue_yuan)
        + float(storage_ancillary_revenue_yuan)
        + float(storage_other_revenue_yuan)
    )
    unique_benefit = bill_saving + float(line.export_revenue_yuan) + external
    analysis_total = (
        bill_saving
        + float(line.export_revenue_yuan)
        + pv_self
        + arbitrage
        + float(line.demand_cost_saving_yuan)
        + external
    )
    entries.append(
        ScenarioBenefitEntry(
            key="total_benefit_analysis",
            label=label_of_benefit("total_benefit_analysis"),
            amount_yuan=analysis_total,
            counted_in_unique_benefit=False,
            entry_role="decomposition",
            excluded_reason="分析口径合计把分解口径与唯一来源**混在一起**，"
            "只用于说明「钱从哪来」，绝不可填入现金流（§7.6 第 5 条）",
            caliber="账单节省 + 上网收入 + 光伏自用节省 + 储能套利 + 需量节省 + 外部收益（**仅展示**）",
        )
    )

    manifest = ScenarioDedupManifest(
        baseline_cost_yuan=float(base.total_cost_yuan),
        baseline_net_cost_yuan=float(base.net_cost_yuan),
        bill_saving_yuan=bill_saving,
        export_revenue_yuan=float(line.export_revenue_yuan),
        storage_capacity_revenue_yuan=float(storage_capacity_revenue_yuan),
        storage_ancillary_revenue_yuan=float(storage_ancillary_revenue_yuan),
        storage_other_revenue_yuan=float(storage_other_revenue_yuan),
        unique_annual_benefit_yuan=unique_benefit,
        analysis_total_yuan=analysis_total,
        identity_deviation_yuan=deviation,
        identity_tolerance_yuan=tolerance,
        identity_passed=passed,
        identity_equations=equations,
        duplicate_risk_notes=notes,
        entries=entries,
        messages=[
            f"账单节省（唯一来源）{bill_saving:,.2f} 元 = 基准 {base.total_cost_yuan:,.2f} − "
            f"方案 {line.total_cost_yuan:,.2f}（§7.6 第 1 条）",
            f"上网收入（独立来源，不在账单节省内）{float(line.export_revenue_yuan):,.2f} 元"
            f"（§7.6 第 3 条）",
            f"分解口径合计 {analysis_total:,.2f} 元（含光伏自用节省 {pv_self:,.2f}、"
            f"储能套利 {arbitrage:,.2f}、需量节省 {float(line.demand_cost_saving_yuan):,.2f}）；"
            "**仅展示，不累加**（§7.6 第 2、4 条）",
            f"恒等式校验：现金口径 {cash_saving:,.6f} 元 vs 分解口径 {decomposed:,.6f} 元，"
            f"偏差 {deviation:.6e} 元（容差 {tolerance:.6e} 元）→ "
            + ("通过" if passed else "**未通过：存在重复计算或漏计**"),
            f"**唯一去重后的年度运营收益 = {unique_benefit:,.2f} 元**"
            "（财务引擎唯一输入，§3.5、§7.6 第 5 条）",
        ],
        assumptions=[
            "现金口径（Σ负荷×电价 − Σ购电×电价）是唯一权威；分解口径只用于解释（V2.0 既定口径）",
            "光伏给储能充电按 0 计价、电网充电按购电价计价（去重关键规则）",
            "上网收入独立列示：既不在账单节省额内，也不被忽略（§7.6 第 3 条）",
            "需量电费、上网收入、储能容量/辅助服务收益与电度电费节省**分开列示**（§7.5）",
        ],
    )

    comparison = ScenarioBillComparison(
        scenario=line,
        baseline=base,
        bill_saving_yuan=bill_saving,
        bill_saving_rate=rate,
        net_saving_yuan=net_saving,
        export_revenue_delta_yuan=export_delta,
        energy_charge_delta_yuan=float(base.energy_charge_yuan - line.energy_charge_yuan),
        capacity_charge_delta_yuan=float(base.capacity_charge_yuan - line.capacity_charge_yuan),
        demand_charge_delta_yuan=float(base.demand_charge_yuan - line.demand_charge_yuan),
        power_factor_delta_yuan=float(
            base.power_factor_adjustment_yuan - line.power_factor_adjustment_yuan
        ),
        government_fund_delta_yuan=float(base.government_fund_yuan - line.government_fund_yuan),
        other_unmodeled_delta_yuan=float(base.other_unmodeled_yuan - line.other_unmodeled_yuan),
        baseline_peak_demand_kw=float(base.peak_demand_kw),
        scenario_peak_demand_kw=float(line.peak_demand_kw),
        demand_reduction_kw=float(base.peak_demand_kw - line.peak_demand_kw),
        monthly_demand_reduction_kw=month_reduction,
        baseline_average_price_yuan_per_kwh=base.average_price_yuan_per_kwh,
        scenario_average_price_yuan_per_kwh=line.average_price_yuan_per_kwh,
        dedup=manifest,
        messages=[
            f"{line.label}：账单节省 {bill_saving:,.2f} 元"
            + (f"（{rate:+.2%}）" if rate is not None else "（基准成本为 0，节省率不适用）")
            + f"；需量 {base.peak_demand_kw:,.2f} → {line.peak_demand_kw:,.2f} kW"
            f"（削减 {base.peak_demand_kw - line.peak_demand_kw:,.2f} kW，"
            f"对应需量电费差 {base.demand_charge_yuan - line.demand_charge_yuan:,.2f} 元）",
            f"分项差额：电度电费 {base.energy_charge_yuan - line.energy_charge_yuan:,.2f}、"
            f"需量电费 {base.demand_charge_yuan - line.demand_charge_yuan:,.2f}、"
            f"力调 {base.power_factor_adjustment_yuan - line.power_factor_adjustment_yuan:,.2f}、"
            f"政府性基金及附加 {base.government_fund_yuan - line.government_fund_yuan:,.2f}、"
            f"其他未建模 {base.other_unmodeled_yuan - line.other_unmodeled_yuan:,.2f}（元）",
        ],
        assumptions=list(manifest.assumptions),
    )
    return comparison, deviation, passed


# --------------------------------------------------------------------------- #
# 财务引擎接入（§3.5、§7.6 第 5 条）
# --------------------------------------------------------------------------- #
def build_finance_overrides(
    result: ScenarioBillSet,
    *,
    dispatch_config: StorageDispatchConfig,
    project,
    years: int | None = None,
) -> dict[int, object]:
    """把**唯一去重后的年度运营收益**接入既有财务引擎（V2.3 §3.5、§7.6 第 5 条）。

    关键约束（§3.5）：**不允许财务模块和账单模块分别独立计算后叠加**。
    因此本函数**只产出年度的收益与电量，
    其余（OPEX、折旧、税、融资、现金流、IRR/NPV）全部沿用既有
    :class:`~cenep.calculation.engine.CalculationEngine`，
    通过既有 ``economic_v2.YearOverride`` 覆盖表注入，**不新建第二套财务引擎**。

    :param result: 四场景对比结果（唯一收益取自 ``result.reference_scenario``）
    :param dispatch_config: 年度重算其余年份时使用的调度配置
    :param project: 项目（提供增长率与衰减率，用于其余年份的线性外推）
    :param years: 运营期年数；``None`` 时取 ``project.analysis_period``
    :returns: ``{年份: YearOverride}``，直接传给 ``CalculationEngine.calculate``
    :raises ValidationError: 去重校验未通过、参考场景缺失（**中文**报错）
    """
    if not result.dedup_verified:
        raise ValidationError(
            "四场景收益去重校验未通过，禁止把结果送入财务引擎（V2.3 §7.6 第 5 条）；"
            f"最大恒等式偏差 {result.identity_max_deviation_yuan:.6e} 元，请先修正口径",
            field="scenario.dedup",
        )
    line = result.line_of(result.reference_scenario)
    if line is None:
        raise ValidationError(
            f"参考场景『{result.reference_scenario.label}』不在结果中，无法构造财务输入"
            "（V2.3 §3.5）",
            field="scenario.reference_scenario",
        )
    comparison = result.comparison_of(result.reference_scenario)
    dedup = comparison.dedup if comparison is not None else ScenarioDedupManifest()

    period = int(years if years is not None else getattr(project, "analysis_period", 1))
    period = max(1, period)

    ts = getattr(project, "timeseries", None)
    load_growth = float(getattr(getattr(ts, "load", None), "annual_growth_rate", 0.0) or 0.0)
    pv_degradation = float(getattr(getattr(project, "pv", None), "annual_degradation_rate", 0.0) or 0.0)
    storage_degradation = float(
        getattr(getattr(project, "storage", None), "annual_degradation_rate", 0.0) or 0.0
    )
    tariff_growth = float(getattr(getattr(ts, "tariff", None), "annual_growth_rate", 0.0) or 0.0)

    # 首年即为本次模拟结果；其余年份按**与既有 economic_v2 相同的线性外推口径**缩放。
    # 明确披露：跨年收益是外推值，不是逐年后重算值（§0.2：不得伪装成逐年精确仿真）。
    base = econ.YearOverride(
        year_index=1,
        load=float(line.load_kwh),
        pv_generation=float(line.pv_generation_kwh),
        pv_self_use=float(line.pv_to_load_kwh),
        pv_export=float(line.grid_export_kwh),
        pv_to_storage=float(line.pv_to_storage_kwh),
        grid_import=float(line.grid_import_kwh),
        grid_export=float(line.grid_export_kwh),
        storage_charge=float(line.storage_charge_kwh),
        storage_discharge=float(line.storage_discharge_kwh),
        grid_charge=float(line.storage_grid_charge_kwh),
        pv_self_use_revenue=float(line.pv_self_consumption_saving_yuan),
        pv_export_revenue=float(line.export_revenue_yuan),
        storage_arbitrage_revenue=float(line.storage_arbitrage_revenue_yuan),
        storage_capacity_revenue=float(dedup.storage_capacity_revenue_yuan),
        storage_ancillary_revenue=float(dedup.storage_ancillary_revenue_yuan),
        storage_other_revenue=float(dedup.storage_other_revenue_yuan),
        demand_saving=float(line.demand_cost_saving_yuan),
    )

    overrides: dict[int, object] = {}
    for year in range(1, period + 1):
        load_factor = (1.0 + load_growth) ** (year - 1)
        pv_factor = (1.0 - pv_degradation) ** (year - 1)
        storage_factor = (1.0 - storage_degradation) ** (year - 1)
        tariff_factor = (1.0 + tariff_growth) ** (year - 1)
        # 收益项按"电量因子 × 电价因子"缩放；需量节省与购电量同源，用 load × tariff
        price_scale = tariff_factor
        overrides[year] = replace(
            base,
            year_index=year,
            load=base.load * load_factor,
            pv_generation=base.pv_generation * pv_factor,
            pv_self_use=base.pv_self_use * pv_factor,
            pv_export=base.pv_export * pv_factor,
            pv_to_storage=base.pv_to_storage * pv_factor,
            grid_import=base.grid_import * load_factor,
            grid_export=base.grid_export * pv_factor,
            storage_charge=base.storage_charge * storage_factor,
            storage_discharge=base.storage_discharge * storage_factor,
            grid_charge=base.grid_charge * storage_factor,
            pv_self_use_revenue=base.pv_self_use_revenue * pv_factor * price_scale,
            pv_export_revenue=base.pv_export_revenue * pv_factor,
            storage_arbitrage_revenue=base.storage_arbitrage_revenue * storage_factor * price_scale,
            storage_capacity_revenue=base.storage_capacity_revenue,
            storage_ancillary_revenue=base.storage_ancillary_revenue,
            storage_other_revenue=base.storage_other_revenue,
            demand_saving=base.demand_saving * load_factor * price_scale,
        )
    return overrides


#: 供类型标注引用（``evaluate_scenario`` 的 ``config`` 形参）
TariffPlanLike = TariffPlan
