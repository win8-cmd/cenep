"""界面字段定义（规范 §99–§104）。

这里只描述"页面上有哪些输入项、对应 Project 的哪个字段、什么类型、什么单位"，
不含任何计算。新增参数只需在本文件加一行。
"""

from __future__ import annotations

from ..domain.enums import (
    InvestmentMode,
    OpexMode,
    RepaymentMethod,
    RoofRentMode,
    SourceType,
    TariffMode,
)
from .field_spec import FieldSpec, Kind, SectionSpec

#: 枚举选项（值, 中文标签）
TARIFF_MODE_CHOICES = (
    (TariffMode.FIXED.value, "固定电价"),
    (TariffMode.TOU.value, "峰平谷电价"),
    (TariffMode.MARKET.value, "市场电价"),
    (TariffMode.CUSTOM.value, "自定义电价"),
)
INVESTMENT_MODE_CHOICES = (
    (InvestmentMode.UNIT_PRICE.value, "按单位造价（容量 × 单价）"),
    (InvestmentMode.DETAILED.value, "按明细填列"),
)
OPEX_MODE_CHOICES = ((OpexMode.FIXED.value, "固定金额"), (OpexMode.RATIO_OF_CAPEX.value, "按投资比例"))
ROOF_RENT_MODE_CHOICES = (
    (RoofRentMode.AREA.value, "按屋顶面积"),
    (RoofRentMode.CAPACITY.value, "按光伏容量"),
    (RoofRentMode.FIXED.value, "固定租金"),
)
REPAYMENT_CHOICES = (
    (RepaymentMethod.EQUAL_PRINCIPAL.value, "等额本金"),
    (RepaymentMethod.EQUAL_INSTALLMENT.value, "等额本息"),
)

PCT = 100.0

# --------------------------------------------------------------------------- #
# 通用（项目级）
# --------------------------------------------------------------------------- #
GENERAL_SECTION = SectionSpec(
    "计算设置",
    [
        FieldSpec("analysis_period", "项目生命周期", Kind.INT, "年", 1, 40, step=1, tooltip="规范 §15：Year 0 为建设期，Year 1 起为运营期"),
        FieldSpec("discount_rate", "折现率", Kind.PERCENT, "%", 0, 50),
    ],
)

# --------------------------------------------------------------------------- #
# 负荷（规范 §16）
# --------------------------------------------------------------------------- #
LOAD_SECTION = SectionSpec(
    "负荷参数",
    [
        FieldSpec("load.annual_load_kwh", "用户全年实际用电量", Kind.FLOAT, "kWh", 0, 1e12, decimals=0, step=10000.0),
        FieldSpec("load.working_days", "年工作天数", Kind.INT, "天", 0, 366, step=1),
        FieldSpec("load.daytime_load_ratio", "白天负荷占比", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("load.nighttime_load_ratio", "夜间负荷占比", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("load.annual_load_growth_rate", "年用电量增长率", Kind.PERCENT, "%", 0, PCT),
    ],
)

# --------------------------------------------------------------------------- #
# 光伏（规范 §99）
# --------------------------------------------------------------------------- #
PV_SECTION = SectionSpec(
    "光伏参数",
    [
        FieldSpec(
            "pv.pv_capacity_kwp",
            "装机容量（留空则按面积换算）",
            Kind.OPTIONAL_FLOAT,
            "kWp",
            0,
            1e7,
            decimals=2,
            step=10.0,
            optional_label="直接指定容量",
        ),
        FieldSpec("pv.roof_area_m2", "屋顶总面积", Kind.FLOAT, "m²", 0, 1e7, decimals=2, step=100.0),
        FieldSpec("pv.usable_roof_area_m2", "可利用屋顶面积", Kind.FLOAT, "m²", 0, 1e7, decimals=2, step=100.0),
        FieldSpec("pv.area_per_kwp", "单位容量占用面积", Kind.FLOAT, "m²/kWp", 0.1, 100, decimals=2, step=0.5),
        FieldSpec("pv.equivalent_hours", "年等效利用小时", Kind.FLOAT, "h", 0, 8760, decimals=2, step=10.0),
        FieldSpec(
            "pv.performance_ratio",
            "性能比",
            Kind.PERCENT,
            "%",
            0,
            PCT,
            tooltip="规范 §21：若等效小时已是最终可利用小时，此处填 100%，不得重复扣减",
        ),
        FieldSpec("pv.annual_degradation_rate", "年衰减率", Kind.PERCENT, "%", 0, 50),
        FieldSpec("pv.curtailment_rate", "限电率", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("pv.self_consumption_ratio", "自发自用比例", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("pv.loss_ratio", "其他损失比例", Kind.PERCENT, "%", 0, PCT),
        FieldSpec(
            "pv.replacement_year",
            "设备更换年份（留空则不更换）",
            Kind.OPTIONAL_INT,
            "年",
            1,
            60,
            step=1,
            optional_label="指定更换年份",
            tooltip="规范 §25 延伸：逆变器等光伏设备的一次性更换，如第 12 年",
        ),
        FieldSpec(
            "pv.replacement_cost_per_kwp",
            "设备更换单价",
            Kind.FLOAT,
            "元/kWp",
            0,
            1e6,
            decimals=2,
            step=10.0,
            tooltip="仅在更换年份发生一次，计入项目现金流与 LCOE 成本",
        ),
    ],
)

# --------------------------------------------------------------------------- #
# 储能（规范 §100）
# --------------------------------------------------------------------------- #
STORAGE_SECTION = SectionSpec(
    "储能参数",
    [
        FieldSpec("storage.storage_power_kw", "储能功率", Kind.FLOAT, "kW", 0, 1e7, decimals=2, step=10.0),
        FieldSpec("storage.storage_energy_kwh", "储能容量", Kind.FLOAT, "kWh", 0, 1e8, decimals=2, step=10.0),
        FieldSpec("storage.round_trip_efficiency", "往返效率", Kind.PERCENT, "%", 0.01, PCT, tooltip="只填往返效率时，充放电效率各取其平方根（规范 §40）"),
        FieldSpec("storage.charge_efficiency", "充电效率（可选）", Kind.OPTIONAL_FLOAT, "小数", 0.01, 1.0, decimals=4, step=0.01),
        FieldSpec("storage.discharge_efficiency", "放电效率（可选）", Kind.OPTIONAL_FLOAT, "小数", 0.01, 1.0, decimals=4, step=0.01),
        FieldSpec("storage.annual_cycles", "年等效循环次数", Kind.FLOAT, "次/年", 0, 1000, decimals=1, step=10.0),
        FieldSpec("storage.depth_of_discharge", "放电深度 DoD", Kind.PERCENT, "%", 1, PCT),
        FieldSpec("storage.annual_degradation_rate", "年衰减率", Kind.PERCENT, "%", 0, 50),
        FieldSpec("storage.replacement_year", "更换电芯年份（可选）", Kind.OPTIONAL_INT, "年", 1, 60, step=1),
        FieldSpec(
            "investment.storage_capex_per_kwh",
            "储能单位投资",
            Kind.FLOAT,
            "元/kWh",
            0,
            100000,
            decimals=2,
            step=50.0,
            tooltip="与“投资参数”页为同一参数（唯一数据源），在此修改会同步生效",
        ),
        FieldSpec("storage.replacement_capex", "更换电芯投资", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("storage.discharge_avoided_price", "放电替代电价（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("storage.charge_price", "充电电价（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("storage.annual_capacity_revenue", "容量收益", Kind.FLOAT, "元/年", 0, 1e10, decimals=2, step=1000.0),
        FieldSpec("storage.annual_ancillary_revenue", "辅助服务收益", Kind.FLOAT, "元/年", 0, 1e10, decimals=2, step=1000.0),
        FieldSpec("storage.annual_other_revenue", "其他收益", Kind.FLOAT, "元/年", 0, 1e10, decimals=2, step=1000.0),
    ],
)

# --------------------------------------------------------------------------- #
# 电价（规范 §101）
# --------------------------------------------------------------------------- #
TARIFF_SECTION = SectionSpec(
    "电价参数",
    [
        FieldSpec("tariff.tariff_mode", "电价模式", Kind.CHOICE, choices=TARIFF_MODE_CHOICES),
        FieldSpec("tariff.average_price", "固定电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.peak_price", "峰电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.flat_price", "平电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.valley_price", "谷电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.peak_ratio", "峰电量比例", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("tariff.flat_ratio", "平电量比例", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("tariff.valley_ratio", "谷电量比例", Kind.PERCENT, "%", 0, PCT, tooltip="峰+平+谷必须等于 100%"),
        FieldSpec("tariff.market_price", "市场电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.export_price", "余电上网电价", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.green_energy_price", "绿电价格", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.green_environmental_value", "绿色环境价值", Kind.FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.custom_avoided_price", "自定义替代电价（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.custom_charge_price", "自定义充电电价（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.avoided_price_override", "替代电价覆盖（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
        FieldSpec("tariff.charge_price_override", "充电电价覆盖（可选）", Kind.OPTIONAL_FLOAT, "元/kWh", 0, 100, step=0.01),
    ],
)

# --------------------------------------------------------------------------- #
# 投资（规范 §102）
# --------------------------------------------------------------------------- #
INVESTMENT_SECTION = SectionSpec(
    "投资参数",
    [
        FieldSpec("investment.mode", "投资模式", Kind.CHOICE, choices=INVESTMENT_MODE_CHOICES),
        FieldSpec("investment.pv_capex_per_kw", "光伏单位投资", Kind.FLOAT, "元/kWp", 0, 100000, decimals=2, step=100.0),
        FieldSpec("investment.storage_capex_per_kwh", "储能单位投资", Kind.FLOAT, "元/kWh", 0, 100000, decimals=2, step=50.0),
        FieldSpec("investment.pv_capex", "光伏投资（明细模式）", Kind.FLOAT, "元", 0, 1e11, decimals=2, step=10000.0),
        FieldSpec("investment.storage_capex", "储能投资（明细模式）", Kind.FLOAT, "元", 0, 1e11, decimals=2, step=10000.0),
        FieldSpec("investment.grid_connection_cost", "并网投资", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.roof_cost", "屋顶费用", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.development_cost", "开发费用", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.engineering_cost", "工程费用", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.construction_cost", "施工费用", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.other_capex", "其他投资", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
        FieldSpec("investment.contingency", "预备费", Kind.FLOAT, "元", 0, 1e10, decimals=2, step=10000.0),
    ],
)

# --------------------------------------------------------------------------- #
# 运维（规范 §103）
# --------------------------------------------------------------------------- #
OPEX_SECTION = SectionSpec(
    "运维参数",
    [
        FieldSpec("opex.pv_opex", "光伏运维", Kind.FLOAT, "按模式", 0, 1e9, decimals=4, step=0.005),
        FieldSpec("opex.pv_opex_mode", "光伏运维模式", Kind.CHOICE, choices=OPEX_MODE_CHOICES),
        FieldSpec("opex.storage_opex", "储能运维", Kind.FLOAT, "按模式", 0, 1e9, decimals=4, step=0.005),
        FieldSpec("opex.storage_opex_mode", "储能运维模式", Kind.CHOICE, choices=OPEX_MODE_CHOICES),
        FieldSpec("opex.insurance", "保险费", Kind.FLOAT, "按模式", 0, 1e9, decimals=4, step=0.001),
        FieldSpec("opex.insurance_mode", "保险模式", Kind.CHOICE, choices=OPEX_MODE_CHOICES),
        FieldSpec("opex.management_cost", "管理费", Kind.FLOAT, "元/年", 0, 1e9, decimals=2, step=1000.0),
        FieldSpec("opex.management_mode", "管理模式", Kind.CHOICE, choices=OPEX_MODE_CHOICES),
        FieldSpec("opex.other_opex", "其他费用", Kind.FLOAT, "元/年", 0, 1e9, decimals=2, step=1000.0),
        FieldSpec("opex.other_opex_mode", "其他费用模式", Kind.CHOICE, choices=OPEX_MODE_CHOICES),
        FieldSpec("opex.roof_rent_mode", "屋顶租金模式", Kind.CHOICE, choices=ROOF_RENT_MODE_CHOICES),
        FieldSpec("opex.rent_per_m2", "面积租金单价", Kind.FLOAT, "元/m²", 0, 10000, decimals=2, step=1.0),
        FieldSpec("opex.rent_per_kw", "容量租金单价", Kind.FLOAT, "元/kWp", 0, 10000, decimals=2, step=10.0),
        FieldSpec("opex.annual_fixed_rent", "固定租金", Kind.FLOAT, "元/年", 0, 1e9, decimals=2, step=1000.0),
        FieldSpec("opex.annual_opex_growth_rate", "运维费用年增长率", Kind.PERCENT, "%", 0, PCT),
    ],
)

# --------------------------------------------------------------------------- #
# 税务与折旧
# --------------------------------------------------------------------------- #
TAX_SECTION = SectionSpec(
    "折旧与税务参数（V1 简化模型）",
    [
        FieldSpec("tax.depreciation_years", "折旧年限", Kind.INT, "年", 1, 50, step=1),
        FieldSpec("tax.residual_value_ratio", "残值率", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("tax.depreciable_capex_ratio", "可折旧投资占比", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("tax.vat_rate", "增值税率", Kind.PERCENT, "%", 0, 50),
        FieldSpec("tax.income_tax_rate", "所得税率", Kind.PERCENT, "%", 0, 50),
        FieldSpec("tax.surcharge_rate", "附加税费率（以不含税收入为基数）", Kind.PERCENT, "%", 0, 20),
        FieldSpec("tax.other_tax_rate", "其他税费率", Kind.PERCENT, "%", 0, 20),
        FieldSpec("tax.revenue_is_vat_inclusive", "收入为含税口径", Kind.BOOL),
        FieldSpec(
            "tax.lcoe_vat_deductible_ratio",
            "LCOE 增值税抵扣比例",
            Kind.PERCENT,
            "%",
            0,
            PCT,
            tooltip="0 = 不抵减（原始口径）。与招标/行业口径对齐时按 13% 填写",
        ),
        FieldSpec(
            "tax.lcoe_residual_credit",
            "LCOE 抵减残值现值",
            Kind.BOOL,
            tooltip="开启后残值现值作为 LCOE/LCOS 的成本抵减项",
        ),
    ],
)

# --------------------------------------------------------------------------- #
# 融资（规范 §104）
# --------------------------------------------------------------------------- #
FINANCING_SECTION = SectionSpec(
    "融资参数",
    [
        FieldSpec("financing.enabled", "使用债务资金", Kind.BOOL),
        FieldSpec("financing.debt_ratio", "贷款比例", Kind.PERCENT, "%", 0, PCT),
        FieldSpec("financing.equity_ratio", "资本金比例", Kind.PERCENT, "%", 0, PCT, tooltip="贷款比例 + 资本金比例必须等于 100%"),
        FieldSpec("financing.loan_amount", "贷款金额（可选，覆盖按比例计算）", Kind.OPTIONAL_FLOAT, "元", 0, 1e11, decimals=2, step=100000.0),
        FieldSpec("financing.interest_rate", "贷款利率", Kind.PERCENT, "%", 0, 30),
        FieldSpec("financing.loan_term", "贷款期限（含宽限期）", Kind.INT, "年", 1, 40, step=1),
        FieldSpec("financing.grace_period", "宽限期", Kind.INT, "年", 0, 39, step=1, tooltip="宽限期内只付息不还本，必须小于贷款期限"),
        FieldSpec("financing.repayment_method", "还款方式", Kind.CHOICE, choices=REPAYMENT_CHOICES),
    ],
)

#: 界面按顺序展示的全部分组（参数页用）
ALL_SECTIONS: tuple[SectionSpec, ...] = (
    GENERAL_SECTION,
    LOAD_SECTION,
    PV_SECTION,
    STORAGE_SECTION,
    TARIFF_SECTION,
    INVESTMENT_SECTION,
    OPEX_SECTION,
    TAX_SECTION,
    FINANCING_SECTION,
)

#: 参数来源说明（用于界面提示，规范 §144）
SOURCE_LEGEND = (
    ("#DDEBF7", "蓝色：用户输入"),
    ("#E2EFDA", "绿色：系统计算"),
    ("#FFF2CC", "黄色：假设值 / 系统默认"),
    ("#EDEDED", "灰色：政策模板"),
)

__all__ = [
    "ALL_SECTIONS",
    "GENERAL_SECTION",
    "LOAD_SECTION",
    "PV_SECTION",
    "STORAGE_SECTION",
    "TARIFF_SECTION",
    "INVESTMENT_SECTION",
    "OPEX_SECTION",
    "TAX_SECTION",
    "FINANCING_SECTION",
    "SOURCE_LEGEND",
    "SourceType",
]
