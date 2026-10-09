"""界面字段定义（规范 §99–§104）。

这里只描述"页面上有哪些输入项、对应 Project 的哪个字段、什么类型、什么单位"，
不含任何计算。新增参数只需在本文件加一行。
"""

from __future__ import annotations

from ..domain.bill_models import BILL_FIELD_LABELS
from ..domain.enums import (
    BillSourceType,
    DispatchStrategy,
    DuplicateStrategy,
    InvestmentMode,
    LoadProfileMode,
    MissingDataPolicy,
    OpexMode,
    PVProfileMode,
    RepaymentMethod,
    Resolution,
    RoofRentMode,
    SourceType,
    TariffMode,
    TariffStructure,
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

# ---- V2 §3 P0.1–P0.4、§12 时序与策略枚举选项（值取自枚举本身，防止手写字符串拼错）----
RESOLUTION_CHOICES = tuple((m.value, m.label) for m in Resolution)
LOAD_PROFILE_MODE_CHOICES = tuple((m.value, m.label) for m in LoadProfileMode)
PV_PROFILE_MODE_CHOICES = tuple((m.value, m.label) for m in PVProfileMode)
MISSING_DATA_POLICY_CHOICES = tuple((m.value, m.label) for m in MissingDataPolicy)
DISPATCH_STRATEGY_CHOICES = tuple((m.value, m.label) for m in DispatchStrategy)

PCT = 100.0

# --------------------------------------------------------------------------- #
# 通用（项目级）
# --------------------------------------------------------------------------- #
GENERAL_SECTION = SectionSpec(
    "计算设置",
    [
        FieldSpec("analysis_period", "项目生命周期", Kind.INT, "年", 1, 40, step=1, tooltip="规范 §15：Year 0 为建设期，Year 1 起为运营期"),
        FieldSpec("discount_rate", "折现率", Kind.PERCENT, "%", 0, 50),
        # V2 §19：能量平衡容差是逐时仿真的**数值校验**参数（全局计算选项），
        # 与「时序仿真总开关」段里的开关类字段不同类，放这里与总体计算口径同处一组。
        FieldSpec(
            "timeseries.balance_tolerance",
            "时序能量平衡容差（V2 §19）",
            Kind.FLOAT,
            "kWh",
            0,
            1000,
            decimals=8,
            step=1e-06,
            tooltip="V2 §19：任一小时偏差超过该值即判定计算失败，默认 1e-6",
        ),
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

# --------------------------------------------------------------------------- #
# V2 时序仿真（V2 §3、§6、§7、§19）
# --------------------------------------------------------------------------- #
TIMESERIES_SECTION = SectionSpec(
    "时序仿真总开关",
    [
        FieldSpec(
            "timeseries.enabled",
            "启用 8760 时序仿真",
            Kind.BOOL,
            tooltip="关闭时完全走 V1 年度模式，结果与 V1 一致（V2 §1.1）",
        ),
        FieldSpec(
            "timeseries.resolution",
            "计算粒度",
            Kind.CHOICE,
            choices=RESOLUTION_CHOICES,
            tooltip="V2 §3 P0.1：默认 1 小时；15 分钟为预留接口",
        ),
        FieldSpec(
            "timeseries.base_year",
            "仿真基准年",
            Kind.INT,
            "年",
            2000,
            2100,
            step=1,
            tooltip="V2 §3 P0.1：决定平年 8760 / 闰年 8784",
        ),
        FieldSpec(
            "timeseries.optimization_enabled",
            "启用方案寻优",
            Kind.BOOL,
            tooltip=(
                "V2 §45–§48：开启后按扫描候选做方案寻优（逐候选时序仿真 + 最优候选精确复核），"
                "耗时远高于常规计算，默认关闭；方案比较不受该开关影响"
            ),
        ),
        # 注：`timeseries.balance_tolerance`（能量平衡容差）原先在本段，现移至
        # 「计算设置」段 —— 它是逐时仿真的数值校验参数（V2 §19），属全局计算选项，
        # 而本段只放时序仿真的开关类字段。
    ],
)

TS_LOAD_SECTION = SectionSpec(
    "负荷曲线",
    [
        FieldSpec(
            "timeseries.load.mode",
            "负荷曲线来源",
            Kind.CHOICE,
            choices=LOAD_PROFILE_MODE_CHOICES,
            tooltip="V2 §8.1 优先级：用户 8760 > 典型日 > 模板 > 经验参数",
        ),
        FieldSpec(
            "timeseries.load.annual_energy_kwh",
            "年用电量",
            Kind.FLOAT,
            "kWh",
            0,
            1e12,
            decimals=0,
            step=10000.0,
            tooltip="V2 §8：典型日模式下用于把曲线缩放到该年电量",
        ),
        FieldSpec(
            "timeseries.load.annual_growth_rate",
            "年负荷增长率",
            Kind.PERCENT,
            "%",
            -50,
            PCT,
            tooltip="V2 §8.3：Load_n = Load_1 × (1+g)^(n-1)",
        ),
        FieldSpec(
            "timeseries.load.missing_data_policy",
            "缺失数据处理",
            Kind.CHOICE,
            choices=MISSING_DATA_POLICY_CHOICES,
            tooltip="V2 §53：默认禁止静默填充，必须报出缺失点数",
        ),
    ],
)

TS_PV_SECTION = SectionSpec(
    "光伏出力曲线",
    [
        FieldSpec(
            "timeseries.pv.mode",
            "光伏曲线来源",
            Kind.CHOICE,
            choices=PV_PROFILE_MODE_CHOICES,
            tooltip="V2 §9：导入曲线 / 典型日 / 月度小时系数 / 年等效小时",
        ),
        FieldSpec(
            "timeseries.pv.equivalent_hours",
            "年等效利用小时",
            Kind.FLOAT,
            "h",
            0,
            8760,
            decimals=2,
            step=10.0,
            tooltip="V2 §9：等效小时模式下全年发电量 = 容量 × 该小时数",
        ),
        FieldSpec(
            "timeseries.pv.performance_ratio",
            "性能比 PR",
            Kind.PERCENT,
            "%",
            0,
            PCT,
            tooltip="V2 §9.1：PV_t = Profile_t × 容量 × PR；等效小时已含损失时填 100%",
        ),
        FieldSpec(
            "timeseries.pv.capacity_kwp",
            "光伏容量（覆盖项）",
            Kind.OPTIONAL_FLOAT,
            "kWp",
            0,
            1e7,
            decimals=2,
            step=10.0,
            optional_label="指定时序容量",
            tooltip="留空则沿用「光伏参数」页的装机容量",
        ),
        FieldSpec(
            "timeseries.pv.missing_data_policy",
            "缺失数据处理",
            Kind.CHOICE,
            choices=MISSING_DATA_POLICY_CHOICES,
            tooltip="V2 §53",
        ),
    ],
)

TS_TARIFF_SECTION = SectionSpec(
    "分时电价",
    [
        FieldSpec("timeseries.tariff.profile.sharp_peak_price", "尖峰电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec("timeseries.tariff.profile.peak_price", "高峰电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec("timeseries.tariff.profile.flat_price", "平段电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec("timeseries.tariff.profile.valley_price", "谷段电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec("timeseries.tariff.profile.deep_valley_price", "深谷电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec("timeseries.tariff.profile.export_price", "上网电价", Kind.FLOAT, "元/kWh", 0, 100, decimals=4, step=0.01),
        FieldSpec(
            "timeseries.tariff.annual_growth_rate",
            "电价年增长率",
            Kind.PERCENT,
            "%",
            -50,
            PCT,
            tooltip="V2 §17.2：Price_n = Price_1 × (1+g)^(n-1)；上网电价不随该系数增长",
        ),
        FieldSpec(
            "timeseries.tariff.demand_charge_enabled",
            "计入需量电费",
            Kind.BOOL,
            tooltip="V2 §18：两部制电价用户建议开启",
        ),
        FieldSpec("timeseries.tariff.profile.demand_charge", "需量电价", Kind.FLOAT, "元/kW·月", 0, 10000, decimals=2, step=1.0),
        FieldSpec("timeseries.tariff.basic_charge_enabled", "计入基本电费", Kind.BOOL),
        FieldSpec("timeseries.tariff.profile.basic_charge", "基本电费", Kind.FLOAT, "元/月", 0, 1e7, decimals=2, step=100.0),
    ],
)

TS_DISPATCH_SECTION = SectionSpec(
    "储能调度策略",
    [
        FieldSpec(
            "timeseries.dispatch.strategy",
            "调度策略",
            Kind.CHOICE,
            choices=DISPATCH_STRATEGY_CHOICES,
            tooltip="V2 §12 峰谷套利 / §13 光伏自用优先 / §14 规则型经济优化",
        ),
        FieldSpec("timeseries.dispatch.soc_min", "SOC 下限", Kind.PERCENT, "%", 0, PCT, tooltip="V2 §10.1"),
        FieldSpec("timeseries.dispatch.soc_max", "SOC 上限", Kind.PERCENT, "%", 0, PCT, tooltip="V2 §10.1"),
        FieldSpec("timeseries.dispatch.initial_soc", "起始 SOC", Kind.PERCENT, "%", 0, PCT, tooltip="V2 §10.5"),
        FieldSpec(
            "timeseries.dispatch.charge_efficiency",
            "充电效率",
            Kind.PERCENT,
            "%",
            0,
            PCT,
            tooltip="V2 §10.3：内部增量 = AC 充电量 × η；默认 √0.88 与 V1 往返效率一致",
        ),
        FieldSpec(
            "timeseries.dispatch.discharge_efficiency",
            "放电效率",
            Kind.PERCENT,
            "%",
            0,
            PCT,
            tooltip="V2 §10.4：内部减少 = AC 放电量 ÷ η",
        ),
        FieldSpec(
            "timeseries.dispatch.max_charge_power",
            "最大充电功率",
            Kind.FLOAT,
            "kW",
            0,
            1e7,
            decimals=2,
            step=10.0,
            tooltip="V2 §11：填 0 表示按储能额定功率",
        ),
        FieldSpec(
            "timeseries.dispatch.max_discharge_power",
            "最大放电功率",
            Kind.FLOAT,
            "kW",
            0,
            1e7,
            decimals=2,
            step=10.0,
            tooltip="V2 §11：填 0 表示按储能额定功率",
        ),
        FieldSpec(
            "timeseries.dispatch.allow_grid_charge",
            "允许电网充电",
            Kind.BOOL,
            tooltip="V2 §21：总开关，默认关闭",
        ),
        FieldSpec(
            "timeseries.dispatch.charge_from_grid",
            "策略主动电网充电",
            Kind.BOOL,
            tooltip="V2 §21：需与总开关同时打开；电网充电量会单独统计",
        ),
        FieldSpec(
            "timeseries.dispatch.allow_export",
            "允许储能上网",
            Kind.BOOL,
            tooltip="V2 §20：默认关闭，禁止无意义上网",
        ),
        FieldSpec(
            "timeseries.dispatch.charge_from_pv",
            "允许光伏给储能充电",
            Kind.BOOL,
            tooltip="V2 §13：光伏盈余优先给储能充电",
        ),
        FieldSpec(
            "timeseries.dispatch.allow_arbitrage",
            "允许峰谷套利",
            Kind.BOOL,
            tooltip="V2 §12：低价充、高价放",
        ),
        FieldSpec(
            "timeseries.dispatch.charge_price_threshold",
            "充电价格阈值",
            Kind.FLOAT,
            "元/kWh",
            0,
            100,
            decimals=4,
            step=0.01,
            tooltip="V2 §12：电价不高于该值才充电；填 0 表示不启用阈值",
        ),
        FieldSpec(
            "timeseries.dispatch.discharge_price_threshold",
            "放电价格阈值",
            Kind.FLOAT,
            "元/kWh",
            0,
            100,
            decimals=4,
            step=0.01,
            tooltip="V2 §12：电价不低于该值才放电",
        ),
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
    TIMESERIES_SECTION,
    TS_LOAD_SECTION,
    TS_PV_SECTION,
    TS_TARIFF_SECTION,
    TS_DISPATCH_SECTION,
)

#: 参数来源说明（用于界面提示，规范 §144）
SOURCE_LEGEND = (
    ("#DDEBF7", "蓝色：用户输入"),
    ("#E2EFDA", "绿色：系统计算"),
    ("#FFF2CC", "黄色：假设值 / 系统默认"),
    ("#EDEDED", "灰色：政策模板"),
)

# --------------------------------------------------------------------------- #
# V2.1 阶段 2：账单页字段规格（规格书 §5.4「手动录入 UI」）
#
# 设计约束：
# * **只声明字段，不含任何计算**——求和、差异、平均电价全部由
#   ``cenep.calculation.bill_calculator`` 经 ``BillService`` 返回（§0.2 铁律 1）；
# * 中文名与单位**全部**取自 ``domain/bill_models.py::BILL_FIELD_LABELS``，
#   不在界面里写死文案与单位（交接文档 §7 第 3 条）。因此这里不再单列 ``unit``：
#   标签本身已带单位（如"总购电量（kWh）"），避免单位出现两处而漂移；
# * 三个分组**不进** ``ALL_SECTIONS``：参数页仍是既有 14 组，账单表单只在账单页使用。
# --------------------------------------------------------------------------- #
#: 可留空字段的勾选框提示：不勾选 = **账单未提供**（V2.1 §2.1：不得自动填 0）
BILL_MISSING_TOOLTIP = "不勾选 = 账单未提供该字段（按 None 处理，不会按 0 参与合计）"

#: 账单数据来源选项（§2.1：manual / excel / estimated，中文标签取自枚举）
BILL_SOURCE_CHOICES = tuple((m.value, m.label) for m in BillSourceType)
#: 计费方式选项（§2.1：single_part / two_part / unknown）
TARIFF_STRUCTURE_CHOICES = tuple((m.value, m.label) for m in TariffStructure)
#: 重复账单处理策略选项（§5.5：跳过 / 替换 / 保留）
DUPLICATE_STRATEGY_CHOICES = tuple((m.value, m.label) for m in DuplicateStrategy)


def _bill_field(
    name: str,
    kind: Kind = Kind.OPTIONAL_FLOAT,
    **kwargs,
) -> FieldSpec:
    """按字段名生成账单字段规格，中文名取 :data:`BILL_FIELD_LABELS`（V2.1 §5.4）。"""
    kwargs.setdefault("tooltip", BILL_MISSING_TOOLTIP)
    kwargs.setdefault("optional_label", "填写")
    kwargs.setdefault("optional_tooltip", BILL_MISSING_TOOLTIP)
    return FieldSpec(name, BILL_FIELD_LABELS[name], kind, **kwargs)


#: 账单基本信息（账期、计量点、计费方式、来源、备注）
BILL_BASIC_SECTION = SectionSpec(
    "账单基本信息",
    [
        _bill_field("billing_period_start", Kind.DATE, tooltip="账单账期起始日（含），格式 YYYY-MM-DD"),
        _bill_field("billing_period_end", Kind.DATE, tooltip="账单账期结束日（含），格式 YYYY-MM-DD"),
        _bill_field("meter_id", Kind.TEXT, tooltip="计量点编号；留空表示未提供（与账期共同决定重复判定）"),
        _bill_field("customer_name", Kind.TEXT, tooltip="账单上的客户名称；留空表示未提供"),
        _bill_field("voltage_level", Kind.TEXT, tooltip="如 10kV；留空表示未提供"),
        FieldSpec(
            "tariff_structure",
            BILL_FIELD_LABELS["tariff_structure"],
            Kind.CHOICE,
            choices=TARIFF_STRUCTURE_CHOICES,
            tooltip="单一制 / 两部制 / 未知（两部制才有基本电费与需量电费）",
        ),
        _bill_field("contract_capacity_kva", maximum=1e9, decimals=2, step=100.0),
        _bill_field(
            "billing_demand_kw",
            maximum=1e9,
            decimals=2,
            step=10.0,
            tooltip="账单上的计费需量（账单事实），**不是**负荷曲线最大值",
        ),
        FieldSpec(
            "source_type",
            BILL_FIELD_LABELS["source_type"],
            Kind.CHOICE,
            choices=BILL_SOURCE_CHOICES,
            tooltip="手动录入 / Excel 导入 / 估算；估算数据在界面与报告中都会带标签",
        ),
        _bill_field("notes", Kind.TEXT, tooltip="备注（如账单口径、补退费说明）"),
    ],
)

#: 账单电量（总购电量 + 五个分时时段；None = 未提供，不按 0 处理）
BILL_ENERGY_SECTION = SectionSpec(
    "电量（kWh）",
    [
        _bill_field("energy_total_kwh", decimals=2, step=1000.0),
        _bill_field("energy_sharp_kwh", decimals=2, step=100.0),
        _bill_field("energy_peak_kwh", decimals=2, step=100.0),
        _bill_field("energy_flat_kwh", decimals=2, step=100.0),
        _bill_field("energy_valley_kwh", decimals=2, step=100.0),
        _bill_field(
            "energy_offpeak_kwh",
            decimals=2,
            step=100.0,
            tooltip="深谷电量；**不是**低谷（低谷见「低谷电量」），两者不得混淆",
        ),
    ],
)

#: 账单费用分项（元；None = 未提供；仅"调整 / 返还"类允许负值）
BILL_CHARGE_SECTION = SectionSpec(
    "费用（元）",
    [
        _bill_field("energy_charge_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field("market_purchase_charge_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field("transmission_distribution_charge_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field("line_loss_charge_yuan", maximum=1e12, decimals=2, step=100.0),
        _bill_field("system_operation_charge_yuan", maximum=1e12, decimals=2, step=100.0),
        _bill_field("government_fund_charge_yuan", maximum=1e12, decimals=2, step=100.0),
        _bill_field("basic_capacity_charge_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field("demand_charge_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field(
            "power_factor_adjustment_yuan",
            minimum=-1e12,
            maximum=1e12,
            decimals=2,
            step=100.0,
            tooltip="功率因数调整电费；可为负（返还 / 奖励），这是允许负值的字段之一",
        ),
        _bill_field("other_charge_yuan", maximum=1e12, decimals=2, step=100.0),
        _bill_field("vat_yuan", maximum=1e12, decimals=2, step=1000.0),
        _bill_field(
            "adjustment_charge_yuan",
            minimum=-1e12,
            maximum=1e12,
            decimals=2,
            step=100.0,
            tooltip="调整 / 补退费；可为负，这是允许负值的字段之一",
        ),
        _bill_field("bill_total_yuan", maximum=1e12, decimals=2, step=1000.0),
    ],
)

#: 账单页使用的全部表单分组（顺序即界面顺序）
BILL_SECTIONS: tuple[SectionSpec, ...] = (
    BILL_BASIC_SECTION,
    BILL_ENERGY_SECTION,
    BILL_CHARGE_SECTION,
)

__all__ = [
    "ALL_SECTIONS",
    "BILL_BASIC_SECTION",
    "BILL_CHARGE_SECTION",
    "BILL_ENERGY_SECTION",
    "BILL_MISSING_TOOLTIP",
    "BILL_SECTIONS",
    "BILL_SOURCE_CHOICES",
    "DUPLICATE_STRATEGY_CHOICES",
    "GENERAL_SECTION",
    "LOAD_SECTION",
    "PV_SECTION",
    "STORAGE_SECTION",
    "TARIFF_SECTION",
    "TARIFF_STRUCTURE_CHOICES",
    "INVESTMENT_SECTION",
    "OPEX_SECTION",
    "TAX_SECTION",
    "FINANCING_SECTION",
    "TIMESERIES_SECTION",
    "TS_LOAD_SECTION",
    "TS_PV_SECTION",
    "TS_TARIFF_SECTION",
    "TS_DISPATCH_SECTION",
    "SOURCE_LEGEND",
    "SourceType",
]
