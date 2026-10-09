"""湖北**用户侧工商业购电**电价计划数据（V2.3 §4、§7.3、§10 阶段 5）。

**与 `policy/hubei.py` 的概念分离（§2.3 硬性要求）**
--------------------------------------------------
* :mod:`cenep.policy.hubei` 的 ``HUBEI_TEMPLATE`` 是**新能源上网电价**政策模板（发电侧）；
* 本模块是**用户侧工商业购电**电价计划（用电侧）。
两者概念不同、字段不同、来源不同，**不得互相复用**；本模块不修改也不替代 ``hubei.py``。

本模块收录**两套并存**的电价计划（用户明确要求：不得只留一套）
-----------------------------------------------------------
1. :data:`OFFICIAL_2026_01` —— **官方 2026-01 版**：国网湖北省电力有限公司
   《代理购电工商业用户电价表》《代理购电价格表》（执行时间 2026-01-01 至 2026-01-31）。
   数值逐项来自官方表格图片证据，**已核验**（见 :data:`OFFICIAL_2026_01_SOURCE`）。
2. :data:`DFL_MATERIAL` —— **东风本田项目资料版**：来自项目初步设计文件
   ``发电量计算(N型组件).xlsx`` 的"时段划分 / 价差设定"表（尖峰 180%、高峰 149%、
   平段 100%、低谷 48%）。**资料只给出浮动系数与时段，未给出基础电价数值，也未定义基础电价口径**，
   因此该版本默认是**草稿**：可以作为口径对照与用户覆盖的起点，但**不能**直接用于正式复算
   （§4.1：预置规则只提供时间段框架与配置能力，具体价格由用户核验后录入）。

核实纪律（§0.2 红线、§4.3）
--------------------------
* 本模块**只收录有来源的数值**。任何没有来源的电价一律留 ``None`` 并标"待确认"，不得预填；
* 官方版的价格来自**国网湖北官方价格表**（政府网站转载 + 本地图片证据）；
* 官方版的**时段划分**来自同一张价格表的注 2，其文号为 **鄂发改价管〔2024〕77 号**；
* 本环境 ``web_fetch`` 对 ``*.gov.cn`` 域名一律解析为非公网地址而失败，
  因此**未能直接抓取 77 号文原文**：时段划分以官方价格表注 2 的表述为准，
  "低谷浮动比例由 0.48 调整为 0.45" 仅取得**搜索标题级线索**，已在
  :data:`DFL_MATERIAL_VS_OFFICIAL_NOTES` 与报告中标注证据等级。

单位：元/千瓦时（电度电价、分项电价）、元/千瓦·月（最大需量）、元/千伏安·月（变压器容量）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from ..calculation.tariff_plan_engine import implied_multiplier_from_prices
from ..domain.enums import (
    DemandBillingMode,
    MarketMode,
    PriceBasis,
    TariffComponentType,
    TariffPeriod,
    TariffPlanStatus,
)
from ..domain.tariff_models import TariffPlan, TariffPriceComponent, TariffTimePeriodRule

logger = logging.getLogger(__name__)

__all__ = [
    "DFL_MATERIAL",
    "DFL_MATERIAL_SOURCE",
    "DFL_MATERIAL_TIME_PERIOD_TEXT",
    "DFL_MATERIAL_VS_OFFICIAL_NOTES",
    "OFFICIAL_2026_01",
    "OFFICIAL_2026_01_COMMON_COMPONENTS",
    "OFFICIAL_2026_01_CONSISTENCY_NOTES",
    "OFFICIAL_2026_01_DECLARED_MULTIPLIERS",
    "OFFICIAL_2026_01_FUND_BREAKDOWN",
    "OFFICIAL_2026_01_PRICE_ROWS",
    "OFFICIAL_2026_01_SOURCE",
    "OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN",
    "OFFICIAL_2026_01_TABLE2",
    "OFFICIAL_2026_01_TIME_PERIOD_TEXT",
    "build_dfl_material_plan",
    "build_official_plan",
    "builtin_tariff_plans",
    "component_sum_of_row",
    "implied_multipliers_of_row",
    "official_voltage_levels",
]

# --------------------------------------------------------------------------- #
# 来源
# --------------------------------------------------------------------------- #
#: 官方 2026-01 版来源（文件名 + URL + 抓取日期 + 文号）
OFFICIAL_2026_01_SOURCE: dict[str, str] = {
    "name": (
        "国网湖北省电力有限公司《代理购电工商业用户电价表》《代理购电价格表》"
        "（执行时间 2026-01-01 至 2026-01-31）"
    ),
    "file_name": "hubei_price_08170209szw7.png",
    "url": "https://xgszsj.xiaogan.gov.cn/tzxg/tzcb/202601/t20260108_568308.shtml",
    "url_note": "孝感市招商局政府网站转载，原标注『来源：国家电网』；本地已留存表格图片证据",
    "document_number": "鄂发改价管〔2024〕77 号",
    "document_note": "分时时段划分与浮动机制的文号来自官方价格表注 2（《省发改委关于完善工商业分时电价机制有关事项的通知》）",
    "fetched_at": "2026-01-08",
    "verified_note": (
        "数值逐项取自本地图片证据（654×939 原始分辨率，逐行放大核对）："
        "表一 分时电度电价 / 容（需）量电价；表二 代理购电价格表；注 1 政府性基金及附加明细；注 2 时段划分与基础电价口径"
    ),
}

#: 项目资料版来源（东风本田第三工厂分布式光伏项目初步设计文件）
DFL_MATERIAL_SOURCE: dict[str, str] = {
    "name": "东风本田第三工厂分布式光伏项目——初步设计（2.26）《发电量计算(N型组件).xlsx》Sheet1『时段划分 / 价差设定』",
    "file_name": "发电量计算(N型组件).xlsx",
    "url": "",
    "url_note": "项目资料，无公开链接；文件位于东风本田项目初步设计资料目录（只读引用）",
    "document_number": "",
    "fetched_at": "2026-02-26",
    "verified_note": (
        "资料单元格原文：尖峰『基础电价×180%+政府性基金及附加』、高峰『基础电价×149%+政府性基金及附加』、"
        "平段『基础电价+政府性基金及附加』、低谷『基础电价×48%+政府性基金及附加』；"
        "资料**未给出基础电价数值，也未定义基础电价口径**（是否扣除输配电价、系统运行费未知）"
    ),
}

# --------------------------------------------------------------------------- #
# 官方时段划分（价格表注 2，文号 鄂发改价管〔2024〕77 号）
# --------------------------------------------------------------------------- #
OFFICIAL_2026_01_TIME_PERIOD_TEXT: str = (
    "尖峰时段：7 月、8 月 20:00-22:00，其他月份 18:00-20:00（共 2 小时）；"
    "高峰时段：7 月、8 月 16:00-20:00、22:00-24:00，其他月份 16:00-18:00、20:00-24:00（共 6 小时）；"
    "平段时段：6:00-12:00、14:00-16:00（共 8 小时）；"
    "低谷时段：0:00-6:00、12:00-14:00（共 8 小时）。"
)

#: 官方"基础电价"口径（价格表注 2 原文）
OFFICIAL_2026_01_BASE_PRICE_DEFINITION: str = (
    "基础电价：在电度电价基础上扣除政府性基金及附加、电度输配电价和系统运行费折价，"
    "作为峰谷分时电价计算的基础电价。"
    "（按该口径反算，本部制口径下基础电价 = 代理购电价 + 上网环节线损费用折价 = "
    "0.384470 + 0.013643 = 0.398113 元/千瓦时，与六项分项合计严格一致。）"
)

#: 官方分时**浮动系数**（相对基础电价的倍数）。
#:
#: **证据等级说明（必须随报告披露）**：官方价格表本身只公布各时段的**绝对电价**，不公布系数。
#: 这里记录的 2.00 / 1.50 / 1.00 / 0.45 是"政策声明值"，其证据为：
#:   ① 尖峰 2.00、平段 1.00、低谷 0.45 在全部 7 行上**逐行严格吻合**（差额 0.000000 元/千瓦时）；
#:   ② 公开检索结果标题显示湖北 2024 年将低谷浮动比例**由 0.48 调整为 0.45**（线索级，未取得原文）；
#:   ③ 高峰 1.50 与官方表列值存在 **−0.001982 ~ −0.003982 元/千瓦时** 的残差，属官方表格内部
#:      不一致（两部制 1-10(20)千伏反算为 1.49502，其余为 1.49000），由 T10 校验显式披露。
#: 因此：**计费一律以表列绝对电价为权威值**，本系数只作参考与交叉校验。
OFFICIAL_2026_01_DECLARED_MULTIPLIERS: dict[TariffPeriod, float] = {
    TariffPeriod.SHARP_PEAK: 2.00,
    TariffPeriod.PEAK: 1.50,
    TariffPeriod.FLAT: 1.00,
    TariffPeriod.VALLEY: 0.45,
}

#: 官方时段规则（月 -> 时段 -> 起止时刻），逐条对应价格表注 2
_SUMMER_MONTHS = [7, 8]
_OTHER_MONTHS = [1, 2, 3, 4, 5, 6, 9, 10, 11, 12]


def _official_time_period_rules() -> list[TariffTimePeriodRule]:
    """按价格表注 2 构造官方时段规则（24 小时严格闭合、无重叠）。

    构造顺序即优先级顺序：平段/低谷先声明，尖峰/高峰后声明；四条时段本身**互不重叠**，
    因此优先级只影响"万一用户额外追加规则"时的覆盖方式。
    """
    rules: list[TariffTimePeriodRule] = []
    # 平段（全年一致）6:00-12:00、14:00-16:00 = 8 小时
    for start, end in (("06:00", "12:00"), ("14:00", "16:00")):
        rules.append(
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT,
                months=list(range(1, 13)),
                start_time=start,
                end_time=end,
                priority=0,
                note="价格表注 2：平段时段 6:00-12:00、14:00-16:00（共 8 小时）",
            )
        )
    # 低谷（全年一致）0:00-6:00、12:00-14:00 = 8 小时
    for start, end in (("00:00", "06:00"), ("12:00", "14:00")):
        rules.append(
            TariffTimePeriodRule(
                period=TariffPeriod.VALLEY,
                months=list(range(1, 13)),
                start_time=start,
                end_time=end,
                priority=0,
                note="价格表注 2：低谷时段 0:00-6:00、12:00-14:00（共 8 小时）",
            )
        )
    # 尖峰：7、8 月 20:00-22:00；其他月份 18:00-20:00
    rules.append(
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            months=list(_SUMMER_MONTHS),
            start_time="20:00",
            end_time="22:00",
            priority=10,
            note="价格表注 2：尖峰时段 7 月、8 月 20:00-22:00（共 2 小时）",
        )
    )
    rules.append(
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            months=list(_OTHER_MONTHS),
            start_time="18:00",
            end_time="20:00",
            priority=10,
            note="价格表注 2：尖峰时段其他月份 18:00-20:00（共 2 小时）",
        )
    )
    # 高峰：7、8 月 16:00-20:00、22:00-24:00；其他月份 16:00-18:00、20:00-24:00
    for months, spans in (
        (_SUMMER_MONTHS, (("16:00", "20:00"), ("22:00", "24:00"))),
        (_OTHER_MONTHS, (("16:00", "18:00"), ("20:00", "24:00"))),
    ):
        for start, end in spans:
            rules.append(
                TariffTimePeriodRule(
                    period=TariffPeriod.PEAK,
                    months=list(months),
                    start_time=start,
                    end_time=end,
                    priority=5,
                    note="价格表注 2：高峰时段 7、8 月 16:00-20:00、22:00-24:00；其他月份 16:00-18:00、20:00-24:00（共 6 小时）",
                )
            )
    return rules


# --------------------------------------------------------------------------- #
# 官方价格行（表一）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HubeiPriceRow:
    """官方价格表一中的一行（某一电压等级 + 单一制/两部制）。"""

    tariff_structure: str
    voltage_level: str
    non_tou_yuan_per_kwh: float
    td_energy_yuan_per_kwh: float
    sharp_yuan_per_kwh: float
    peak_yuan_per_kwh: float
    flat_yuan_per_kwh: float
    valley_yuan_per_kwh: float
    demand_yuan_per_kw_month: float | None = None
    capacity_yuan_per_kva_month: float | None = None
    note: str = ""

    @property
    def structure_label(self) -> str:
        return "两部制" if self.tariff_structure == "two_part" else "单一制"


#: 官方表一全部价格行（逐项与图片证据一致）
OFFICIAL_2026_01_PRICE_ROWS: tuple[HubeiPriceRow, ...] = (
    # ---- 单一制 ----
    HubeiPriceRow("single_part", "不满1千伏", 0.735686, 0.2103, 1.133999, 0.930961, 0.735686, 0.516924),
    HubeiPriceRow("single_part", "1-10(20)千伏", 0.715686, 0.1903, 1.113999, 0.910961, 0.715686, 0.496924),
    HubeiPriceRow("single_part", "35千伏", 0.695686, 0.1703, 1.093999, 0.890961, 0.695686, 0.476924),
    # ---- 两部制 ----
    HubeiPriceRow(
        "two_part", "1-10(20)千伏", 0.651886, 0.1263, 1.049999, 0.848961, 0.651886, 0.432924, 42.0, 26.3
    ),
    HubeiPriceRow(
        "two_part", "35千伏", 0.632086, 0.1065, 1.030199, 0.827161, 0.632086, 0.413124, 42.0, 26.3
    ),
    HubeiPriceRow(
        "two_part", "110千伏", 0.613986, 0.0884, 1.012099, 0.809061, 0.613986, 0.395024, 39.0, 24.4
    ),
    HubeiPriceRow(
        "two_part", "220千伏及以上", 0.594986, 0.0694, 0.993099, 0.790061, 0.594986, 0.376024, 39.0, 24.4
    ),
)

#: 官方价格表中与电压等级无关的公共分项（表一列 2、3、5、6，及表二明细）
OFFICIAL_2026_01_COMMON_COMPONENTS: dict[str, float] = {
    # 表一列 2：代理购电价（表一仅在"两部制 1-10(20)千伏"行标注一次，为全省统一值）
    "agent_purchase_price": 0.384470,
    # 表一列 3：代理工商业上网环节线损费用折价
    "line_loss_price": 0.013643,
    # 表一列 5 / 注 1：政府性基金及附加
    "government_fund": 0.0452,
    # 表一列 6：系统运行费折价
    "system_operation": 0.082273,
}

#: 注 1：政府性基金及附加明细（三项之和 **严格等于** 0.0452）
OFFICIAL_2026_01_FUND_BREAKDOWN: dict[str, float] = {
    "农网还贷资金": 0.02,
    "大中型水库移民后期扶持资金": 0.0062,
    "可再生能源电价附加": 0.019,
}

#: 表二：系统运行费用折合度电水平明细（表二第 8~15 项）
OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN: dict[str, float] = {
    "抽水蓄能容量电费度电水平": 0.002316,
    "煤电容量电费度电水平": 0.036657,
    "新能源可持续发展价格结算机制差价结算费用折合度电水平": 0.019244,
    "辅助服务费用度电水平": 0.000000,
    "上网环节线损代理采购损益度电水平": 0.000000,
    "电价交叉补贴新增损益度电水平": 0.012994,
    "剩余优先发电价差损益度电水平": 0.006762,
}

#: 表二：代理购电价格表关键值（供报告引用与核对）
OFFICIAL_2026_01_TABLE2: dict[str, float] = {
    "代理工商业购电量规模_万千瓦时": 581200.0006,
    "采购优先发电电量_万千瓦时": 451111.0000,
    "采购市场化发电电量_万千瓦时": 130089.0000,
    "代理工商业购电价格_元每千瓦时": 0.384470,
    "当月平均购电价格_元每千瓦时": 0.388090,
    "历史偏差电费折价_元每千瓦时": -0.003620,
    "代理工商业上网环节线损费用折价_元每千瓦时": 0.013643,
    "系统运行费用折合度电水平_元每千瓦时": 0.082273,
}

#: 官方价格表内部一致性说明（**必须随报告披露**，不得为对上公式改动官方数值）
OFFICIAL_2026_01_CONSISTENCY_NOTES: tuple[str, ...] = (
    "两部制 4 个电压等级：代理购电价 0.384470 + 线损折价 0.013643 + 电度输配电价 + 政府性基金及附加 0.0452 "
    "+ 系统运行费折价 0.082273 = 表列『非分时电度电价』，**逐项严格相等**（已用测试固化）。",
    "单一制 3 个电压等级：同一套分项合计比表列非分时电度电价**高 0.0002 元/千瓦时**"
    "（例：1-10(20)千伏 合计 0.715886 vs 表列 0.715686）。该 0.0002 属官方表格内部不一致，"
    "本软件以表列**非分时电度电价**为准，差额作为『待确认』披露。",
    "分时电价反算：尖峰系数 2.00、低谷系数 0.45 **完全吻合**（全部 7 行）；"
    "高峰反算系数为 1.4900~1.4950（两部制 1-10(20)千伏 为 1.4950，其余为 1.4900），"
    "**不等于整 1.5**，最大差额 0.001982 元/千瓦时。软件以表列绝对电价为计费依据，"
    "反算系数只作参考值并由 T10 校验披露。",
    "政府性基金及附加明细三项之和 0.02 + 0.0062 + 0.019 = 0.0452，与表列 0.0452 **严格相等**。",
    "系统运行费用折合度电水平：表二第 8 项列示 0.082273 元/千瓦时，而其第 9~15 项明细之和为 "
    "0.077973 元/千瓦时（0.002316 + 0.036657 + 0.019244 + 0 + 0 + 0.012994 + 0.006762），"
    "**相差 0.0043 元/千瓦时**。本软件以表列**合计值 0.082273** 为计费依据（与表一列 6 一致），"
    "明细缺项作为『待确认』披露，不得据明细反推而改动合计值。",
)


def _official_plan_id(structure: str, voltage_level: str) -> str:
    slug = {
        "不满1千伏": "LT1KV",
        "1-10(20)千伏": "KV1_10",
        "35千伏": "KV35",
        "110千伏": "KV110",
        "220千伏及以上": "GE220KV",
    }[voltage_level]
    prefix = "TWO_PART" if structure == "two_part" else "SINGLE_PART"
    return f"HUBEI_COMMERCIAL_2026_01_{prefix}_{slug}"


def official_voltage_levels() -> list[str]:
    """官方价格表覆盖的全部电压等级（去重、保持表格顺序）。"""
    seen: list[str] = []
    for row in OFFICIAL_2026_01_PRICE_ROWS:
        if row.voltage_level not in seen:
            seen.append(row.voltage_level)
    return seen


def implied_multipliers_of_row(row: HubeiPriceRow) -> dict[TariffPeriod, float | None]:
    """按官方"基础电价"口径**反算**某行的隐含浮动系数（仅供报告与核对，不作计费依据）。

    基础电价 = 非分时电度电价 − 政府性基金及附加 − 电度输配电价 − 系统运行费折价；
    系数 = (某时段电度电价 − 固定分项) / 基础电价。

    实测结果（可由本函数复现）：尖峰 2.000000、平段 1.000000、低谷 0.450000 在全部 7 行上完全一致；
    高峰为 1.49502（两部制 1-10(20)千伏）或 1.49000（其余 6 行）。
    """
    fixed = (
        OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
        + OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"]
        + row.td_energy_yuan_per_kwh
    )
    return {
        period: implied_multiplier_from_prices(
            period_price=price, flat_price=row.flat_yuan_per_kwh, fixed_price=fixed
        )
        for period, price in (
            (TariffPeriod.SHARP_PEAK, row.sharp_yuan_per_kwh),
            (TariffPeriod.PEAK, row.peak_yuan_per_kwh),
            (TariffPeriod.FLAT, row.flat_yuan_per_kwh),
            (TariffPeriod.VALLEY, row.valley_yuan_per_kwh),
        )
    }


def component_sum_of_row(row: HubeiPriceRow) -> float:
    """按官方六项分项合计某行的非分时电度电价（用于核对官方表内部一致性）。"""
    return round(
        OFFICIAL_2026_01_COMMON_COMPONENTS["agent_purchase_price"]
        + OFFICIAL_2026_01_COMMON_COMPONENTS["line_loss_price"]
        + row.td_energy_yuan_per_kwh
        + OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
        + OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"],
        6,
    )


def build_official_plan(
    row: HubeiPriceRow,
    *,
    market_mode: MarketMode = MarketMode.UTILITY_AGENT,
    status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
) -> TariffPlan:
    """把官方价格表的一行构造为**已核验**的版本化电价计划（V2.3 §2.3、§4.1）。

    * 时段规则来自价格表注 2（文号 鄂发改价管〔2024〕77 号）；
    * 每个时段的 ``direct_price_yuan_per_kwh`` 取**表列绝对电价**（权威计费值）；
    * 同一条规则上的 ``price_multiplier`` 是**反算参考值**，用于 T10 交叉校验；
    * ``price_components`` 按表一列 2~6 逐项录入，并明确标注
      "代理购电价 + 线损折价参与浮动、其余不参与"（§2.3：不得默认对全部分项乘倍率）。

    :param market_mode: 官方表是**代理购电**产品；用户为市场化直购时应选择
        :class:`~cenep.domain.enums.MarketMode.RETAIL_MARKET` 并另行录入实际市场电价
        （§4.1 第 3、6 条：不允许仅凭"湖北省"自动判断用户执行哪种电价）。
    """
    rules: list[TariffTimePeriodRule] = []
    price_by_period = {
        TariffPeriod.SHARP_PEAK: row.sharp_yuan_per_kwh,
        TariffPeriod.PEAK: row.peak_yuan_per_kwh,
        TariffPeriod.FLAT: row.flat_yuan_per_kwh,
        TariffPeriod.VALLEY: row.valley_yuan_per_kwh,
    }
    for rule in _official_time_period_rules():
        price = price_by_period[rule.period]
        rules.append(
            rule.model_copy(
                update={
                    "direct_price_yuan_per_kwh": price,
                    "price_multiplier": OFFICIAL_2026_01_DECLARED_MULTIPLIERS[rule.period],
                    "price_basis": PriceBasis.DIRECT_PRICE,
                    "note": rule.note + "；计费以表列绝对电价为权威值，浮动系数为政策声明参考值",
                }
            )
        )

    components = [
        TariffPriceComponent(
            component_type=TariffComponentType.MARKET_ENERGY,
            name="代理购电价（工商业）",
            value=OFFICIAL_2026_01_COMMON_COMPONENTS["agent_purchase_price"],
            included_in_tou_price=True,
            adjustable_by_tou=True,
            source_note="官方表一列 2 / 表二第 4 项：代理工商业购电价格 0.384470 元/千瓦时",
        ),
        TariffPriceComponent(
            component_type=TariffComponentType.LINE_LOSS,
            name="代理工商业上网环节线损费用折价",
            value=OFFICIAL_2026_01_COMMON_COMPONENTS["line_loss_price"],
            included_in_tou_price=True,
            adjustable_by_tou=True,
            source_note="官方表一列 3 / 表二第 7 项：0.013643 元/千瓦时",
        ),
        TariffPriceComponent(
            component_type=TariffComponentType.TRANSMISSION_DISTRIBUTION,
            name="电度输配电价",
            value=row.td_energy_yuan_per_kwh,
            included_in_tou_price=True,
            adjustable_by_tou=False,
            source_note=f"官方表一列 4（{row.structure_label} {row.voltage_level}）：{row.td_energy_yuan_per_kwh} 元/千瓦时",
        ),
        TariffPriceComponent(
            component_type=TariffComponentType.SYSTEM_OPERATION,
            name="系统运行费折价",
            value=OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"],
            included_in_tou_price=True,
            adjustable_by_tou=False,
            source_note="官方表一列 6 / 表二第 8 项：0.082273 元/千瓦时（明细见系统运行费用折合度电水平）",
        ),
        TariffPriceComponent(
            component_type=TariffComponentType.GOVERNMENT_FUND,
            name="政府性基金及附加",
            value=OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"],
            included_in_tou_price=True,
            adjustable_by_tou=False,
            source_note="官方表一列 5 / 注 1：0.0452 元/千瓦时（农网还贷 0.02 + 大中型水库移民后期扶持 0.0062 + 可再生能源附加 0.019）",
        ),
    ]
    for name, value in OFFICIAL_2026_01_FUND_BREAKDOWN.items():
        components.append(
            TariffPriceComponent(
                component_type=TariffComponentType.OTHER,
                name=f"政府性基金明细：{name}",
                value=value,
                included_in_tou_price=False,
                adjustable_by_tou=False,
                source_note="官方表一注 1（明细，不重复计入电度电价合计）",
            )
        )
    for name, value in OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN.items():
        components.append(
            TariffPriceComponent(
                component_type=TariffComponentType.OTHER,
                name=f"系统运行费明细：{name}",
                value=value,
                included_in_tou_price=False,
                adjustable_by_tou=False,
                source_note="官方表二第 9~15 项（明细，不重复计入电度电价合计）",
            )
        )

    notes = [
        f"官方口径（{OFFICIAL_2026_01_SOURCE['document_number']}）：{OFFICIAL_2026_01_BASE_PRICE_DEFINITION}",
        "浮动基础：代理购电价 + 上网环节线损费用折价参与峰谷浮动；"
        "电度输配电价、系统运行费折价、政府性基金及附加**不参与**浮动，按固定分项加回。",
        "官方表的『非分时电度电价』= 代理购电价 + 线损折价 + 电度输配电价 + 政府性基金及附加 + 系统运行费折价。",
    ]
    notes.extend(OFFICIAL_2026_01_CONSISTENCY_NOTES)
    if market_mode is MarketMode.RETAIL_MARKET:
        notes.append(
            "注意：本表是**电网企业代理购电**产品的价格表。该用户为市场化直购客户时，"
            "电能量部分按市场形成的价格结算（逐时价格），本表仅可用于输配电价、政府性基金及附加、"
            "系统运行费与代理购电价格水平的口径对照（§4.1 第 3、6 条）。"
        )

    plan = TariffPlan(
        tariff_plan_id=_official_plan_id(row.tariff_structure, row.voltage_level),
        name=(
            f"湖北 2026-01 代理购电工商业电价（{row.structure_label} {row.voltage_level}）"
        ),
        province="湖北",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 1, 31),
        source_name=OFFICIAL_2026_01_SOURCE["name"],
        source_url=OFFICIAL_2026_01_SOURCE["url"],
        source_document_number=OFFICIAL_2026_01_SOURCE["document_number"],
        source_fetched_at=date(2026, 1, 8),
        verified_at=datetime(2026, 1, 8, 0, 0, 0),
        verified_by="阶段 5 执行核对：官方价格表图片证据逐行放大核对",
        status=status,
        applicable_voltage_levels=[row.voltage_level],
        applicable_tariff_structures=[row.tariff_structure],
        applicable_customer_types=["工商业用电", row.structure_label],
        market_mode=market_mode,
        time_period_rules=rules,
        price_components=components,
        capacity_charge_yuan_per_kva_month=row.capacity_yuan_per_kva_month,
        demand_charge_yuan_per_kw_month=row.demand_yuan_per_kw_month,
        demand_billing_mode=DemandBillingMode.DEMAND,
        power_factor_rule_config={
            "mode": "keep_bill_actual",
            "note": (
                "本阶段不重建功率因数调整规则：按账单实际金额作为固定基准保留并在结果中披露，"
                "不得凭空估算（§3.4）。官方表注 3 提到『按上表中的 1.5 倍执行』属代理购电价格适用规则，"
                "与功率因数调整无关，本阶段不做推断。"
            ),
        },
        base_price_definition=OFFICIAL_2026_01_BASE_PRICE_DEFINITION,
        notes="\n".join(notes),
        is_example=False,
    )
    logger.debug("构造官方电价计划：%s（%s）", plan.tariff_plan_id, plan.name)
    return plan


# --------------------------------------------------------------------------- #
# 官方 2026-01 版全部计划
# --------------------------------------------------------------------------- #
def _build_all_official(
    *, market_mode: MarketMode = MarketMode.UTILITY_AGENT, status: TariffPlanStatus = TariffPlanStatus.VERIFIED
) -> list[TariffPlan]:
    return [build_official_plan(row, market_mode=market_mode, status=status) for row in OFFICIAL_2026_01_PRICE_ROWS]


OFFICIAL_2026_01: tuple[TariffPlan, ...] = tuple(_build_all_official())
"""官方 2026-01 版 7 个电价计划（单一制 3 个 + 两部制 4 个电压等级）。"""


# --------------------------------------------------------------------------- #
# 项目资料版（东风本田）—— 与官方版**并存**
# --------------------------------------------------------------------------- #
DFL_MATERIAL_TIME_PERIOD_TEXT: str = (
    "尖峰时段：20:00-22:00（共 2 小时）；高峰时段：9:00-15:00（共 6 小时）；"
    "平段时段：7:00-9:00、15:00-20:00、22:00-23:00（共 8 小时）；"
    "低谷时段：23:00-次日 7:00（共 8 小时）。"
)

#: 24 小时严格闭合的资料版时段规则（2 + 6 + 8 + 8 = 24）
_DFL_MATERIAL_RULES: tuple[tuple[TariffPeriod, str, str, float], ...] = (
    (TariffPeriod.SHARP_PEAK, "20:00", "22:00", 1.80),
    (TariffPeriod.PEAK, "09:00", "15:00", 1.49),
    (TariffPeriod.FLAT, "07:00", "09:00", 1.00),
    (TariffPeriod.FLAT, "15:00", "20:00", 1.00),
    (TariffPeriod.FLAT, "22:00", "23:00", 1.00),
    (TariffPeriod.VALLEY, "23:00", "07:00", 0.48),
)

#: 资料版与官方版的口径差异结论（**证据等级逐条标注**，供报告直接引用）
DFL_MATERIAL_VS_OFFICIAL_NOTES: tuple[str, ...] = (
    "① 浮动系数不同：资料版 尖峰 180% / 高峰 149% / 平段 100% / 低谷 48%；"
    "官方 2026-01 表反算 尖峰 200% / 高峰 149.0%~149.5% / 平段 100% / 低谷 45%。"
    "其中尖峰差 20 个百分点（资料偏低）、低谷差 3 个百分点（资料偏高）、高峰基本一致。",
    "② 时段划分完全不同（这是影响更大的差异）：资料版把 9:00-15:00 整段计为**高峰**；"
    "官方同期 9:00-12:00、14:00-15:00 为**平段**、12:00-14:00 为**低谷**。"
    "资料版把 18:00-20:00 计为**平段**，官方为**尖峰**；资料版把 23:00-24:00 计为**低谷**，官方为**高峰**；"
    "资料版把 06:00-07:00 计为**低谷**，官方为**平段**；资料版把 16:00-18:00 计为**平段**，官方为**高峰**。"
    "两套时段各自都能闭合 24 小时，但**不可混用**。",
    "③ 浮动基础口径不同：官方明确『基础电价 = 电度电价 − 政府性基金及附加 − 电度输配电价 − 系统运行费折价』，"
    "反算后基础电价 = 代理购电价 + 线损折价 = 0.398113 元/千瓦时，且**要把输配电价与系统运行费加回**；"
    "资料版公式写作『基础电价×系数 + 政府性基金及附加』，**只加回政府性基金及附加**，"
    "若其『基础电价』按同样的扣除口径理解，则漏掉电度输配电价与系统运行费折价，会系统性低估各时段电价。",
    "④ 证据等级：资料版系数与时段有原始文件出处（项目初步设计《发电量计算(N型组件).xlsx》Sheet1 第 30~32 行），"
    "证据等级为**原始文件**；『资料版使用旧版政策』这一推断只有**线索级**证据——"
    "公开检索结果标题显示湖北 2024 年将低谷浮动比例由 0.48 调整为 0.45，与资料版 48% 吻合，"
    "但本环境无法抓取 77 号文原文，故**未能确证，仅列差异**。",
    "⑤ 软件处理：两套计划**并存**，由用户选择、核对、覆盖（用户明确要求不得只留一套）；"
    "资料版默认状态为**草稿**且不预填电价数值（资料未给出基础电价），只能作口径对照与用户覆盖起点。",
)


def build_dfl_material_plan(
    *,
    base_price_yuan_per_kwh: float | None = None,
    government_fund_yuan_per_kwh: float | None = None,
    status: TariffPlanStatus = TariffPlanStatus.DRAFT,
) -> TariffPlan:
    """构造**东风本田项目资料版**电价计划（与官方版并存，§4.1、§4.2）。

    :param base_price_yuan_per_kwh: 用户核验后的"基础电价"（元/kWh）。
        **默认 ``None``**：资料未给出该数值，因此计划默认是草稿、没有价格，
        由 :func:`cenep.calculation.tariff_plan_engine.validate_tariff_plan` 的 ``T04`` 阻断正式复算。
        传入数值后才会按"基础电价 × 浮动系数 + 政府性基金及附加"填入各时段直接单价。
    :param government_fund_yuan_per_kwh: 政府性基金及附加（元/kWh）；``None`` 时取官方 2026-01 值 0.0452，
        并在 ``notes`` 中披露该取值来自官方表而非项目资料。

    资料版**只声明浮动系数口径**（``price_basis=MULTIPLIER``），
    不把反算出的绝对电价伪装成官方公布值。
    """
    fund = (
        float(government_fund_yuan_per_kwh)
        if government_fund_yuan_per_kwh is not None
        else OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
    )
    rules: list[TariffTimePeriodRule] = []
    for period, start, end, multiplier in _DFL_MATERIAL_RULES:
        direct = None
        if base_price_yuan_per_kwh is not None:
            direct = float(base_price_yuan_per_kwh) * float(multiplier) + fund
        rules.append(
            TariffTimePeriodRule(
                period=period,
                months=list(range(1, 13)),
                start_time=start,
                end_time=end,
                price_multiplier=float(multiplier),
                direct_price_yuan_per_kwh=direct,
                price_basis=PriceBasis.MULTIPLIER if direct is None else PriceBasis.DIRECT_PRICE,
                priority=0,
                note="项目资料《发电量计算(N型组件).xlsx》Sheet1『时段划分/价差设定』",
            )
        )

    components = [
        TariffPriceComponent(
            component_type=TariffComponentType.OTHER,
            name="基础电价（资料版未定义口径，待用户核验）",
            value=base_price_yuan_per_kwh,
            included_in_tou_price=True,
            adjustable_by_tou=True,
            source_note="项目资料只写『基础电价』，未给出数值与扣除口径；该数值必须由用户核验后录入",
        ),
        TariffPriceComponent(
            component_type=TariffComponentType.GOVERNMENT_FUND,
            name="政府性基金及附加",
            value=fund,
            included_in_tou_price=True,
            adjustable_by_tou=False,
            source_note=(
                "项目资料公式明确『+ 政府性基金及附加』但未给数值；此处默认取官方表一注 1 的 0.0452，"
                "如需改为资料口径请用户显式覆盖"
            ),
        ),
    ]

    notes = [
        f"项目资料版时段划分：{DFL_MATERIAL_TIME_PERIOD_TEXT}",
        "项目资料版价差设定：尖峰 基础电价×180%+政府性基金及附加；高峰 基础电价×149%+政府性基金及附加；"
        "平段 基础电价+政府性基金及附加；低谷 基础电价×48%+政府性基金及附加。"
        "本计划只声明**浮动系数**口径，不把反算结果伪装成官方公布值；"
        "未录入基础电价前为草稿，正式复算会被 T04/T05 阻断（§4.2）。",
    ]
    notes.extend(DFL_MATERIAL_VS_OFFICIAL_NOTES)

    return TariffPlan(
        tariff_plan_id="HUBEI_DFL_MATERIAL_TOU_RATIO",
        name="东风本田项目资料版分时电价口径（尖峰180%/高峰149%/平段100%/低谷48%）",
        province="湖北",
        effective_from=date(2025, 1, 1),
        effective_to=None,
        source_name=DFL_MATERIAL_SOURCE["name"],
        source_url=None,
        source_document_number=DFL_MATERIAL_SOURCE["document_number"] or None,
        source_fetched_at=date(2026, 2, 26),
        verified_at=None,
        verified_by=None,
        status=status,
        applicable_voltage_levels=["110千伏"],
        applicable_tariff_structures=["two_part"],
        applicable_customer_types=["工商业用电", "两部制", "市场化直购"],
        market_mode=MarketMode.RETAIL_MARKET,
        time_period_rules=rules,
        price_components=components,
        capacity_charge_yuan_per_kva_month=None,
        demand_charge_yuan_per_kw_month=None,
        demand_billing_mode=DemandBillingMode.DEMAND,
        power_factor_rule_config=None,
        base_price_definition=(
            "项目资料未给出基础电价口径：其公式为『基础电价×浮动系数 + 政府性基金及附加』，"
            "但未说明基础电价是否已扣除电度输配电价与系统运行费折价。"
            "官方口径为『基础电价 = 电度电价 − 政府性基金及附加 − 电度输配电价 − 系统运行费折价』，"
            "两者不可混用（差异见 DFL_MATERIAL_VS_OFFICIAL_NOTES 第 ③ 条）。"
        ),
        notes="\n".join(notes),
        is_example=False,
    )


DFL_MATERIAL: TariffPlan = build_dfl_material_plan()
"""东风本田项目资料版电价口径（草稿、无预填电价）。"""


def builtin_tariff_plans() -> list[TariffPlan]:
    """内置电价计划库：官方 2026-01 版 7 个 + 项目资料版 1 个（**两套并存**）。

    :return: 新的列表（调用方可安全修改）；元素为 :class:`TariffPlan` 的副本。
    """
    return [plan.model_copy(deep=True) for plan in (*OFFICIAL_2026_01, DFL_MATERIAL)]
