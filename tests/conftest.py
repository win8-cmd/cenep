"""pytest 公共夹具：黄金测试案例（规范 §117）。

黄金测试项目固定为：

* 项目：湖北某工商业厂房
* 光伏：1000 kWp，年等效小时 1100 h，性能比 1.0，自用比例 80%，首年衰减 0.5%
* 光伏单位投资：3000 元/kWp
* 项目寿命：25 年
* 储能：500 kW / 1000 kWh

规范 §117 未规定的参数（用电量、电价、运维、融资）在下方**显式固定**，
以保证"同一输入重复计算 100 次结果一致"（规范 §118），详见 TEST_PLAN.md。
"""

from __future__ import annotations

import os

import pytest

# GUI 测试不需要真实显示器；必须在导入 PySide6 之前设置（规范 §152 的界面一致性验证）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cenep.domain.enums import (
    InvestmentMode,
    OpexMode,
    ProjectType,
    RepaymentMethod,
    RoofRentMode,
    TariffMode,
)
from cenep.domain.models import (
    BasicInfo,
    FinancingConfig,
    InvestmentConfig,
    LoadConfig,
    OpexConfig,
    PolicyProfile,
    Project,
    PVConfig,
    ScenarioConfig,
    SensitivityConfig,
    StorageConfig,
    TariffConfig,
    TaxConfig,
)

# ---- 黄金案例固定常量（供测试直接引用，避免魔法数字散落）----
GOLDEN_PV_CAPACITY_KWP = 1000.0
GOLDEN_EQUIVALENT_HOURS = 1100.0
GOLDEN_PERFORMANCE_RATIO = 1.0
GOLDEN_SELF_CONSUMPTION = 0.8
GOLDEN_PV_DEGRADATION = 0.005
GOLDEN_PV_CAPEX_PER_KW = 3000.0
GOLDEN_ANALYSIS_PERIOD = 25
GOLDEN_STORAGE_POWER_KW = 500.0
GOLDEN_STORAGE_ENERGY_KWH = 1000.0
GOLDEN_LOAD_KWH = 1_200_000.0
GOLDEN_EXPORT_PRICE = 0.35
GOLDEN_DEBT_RATIO = 0.5
GOLDEN_INTEREST_RATE = 0.04
GOLDEN_LOAN_TERM = 10

# LCOE / LCOS 锁定值（回归：LCOE 成本必须覆盖全部光伏相关运营成本：
# 光伏运维费 2% + 保险费 0.5% + 管理费 1 万 + 其他 0.5 万 = 首年 90,000 元）
GOLDEN_LCOE_PV = 0.35131245094782904
GOLDEN_LCOE_PV_STORAGE = 0.3560466564119183
GOLDEN_LCOS_STORAGE = 0.4776404621639379


def _base_project(project_type: ProjectType) -> Project:
    return Project(
        basic_info=BasicInfo(
            project_name="湖北某工商业厂房",
            province="湖北",
            city="武汉市",
            project_type=project_type,
            customer_name="某制造企业",
            industry="制造业",
        ),
        load=LoadConfig(
            annual_load_kwh=GOLDEN_LOAD_KWH,
            working_days=300,
            daytime_load_ratio=0.6,
            nighttime_load_ratio=0.4,
            annual_load_growth_rate=0.0,
        ),
        pv=PVConfig(
            pv_capacity_kwp=GOLDEN_PV_CAPACITY_KWP,
            roof_area_m2=8000.0,
            usable_roof_area_m2=6500.0,
            area_per_kwp=6.0,
            equivalent_hours=GOLDEN_EQUIVALENT_HOURS,
            performance_ratio=GOLDEN_PERFORMANCE_RATIO,
            annual_degradation_rate=GOLDEN_PV_DEGRADATION,
            curtailment_rate=0.0,
            self_consumption_ratio=GOLDEN_SELF_CONSUMPTION,
            loss_ratio=0.0,
        ),
        storage=StorageConfig(
            storage_power_kw=GOLDEN_STORAGE_POWER_KW,
            storage_energy_kwh=GOLDEN_STORAGE_ENERGY_KWH,
            charge_efficiency=None,
            discharge_efficiency=None,
            round_trip_efficiency=0.88,
            annual_cycles=330.0,
            depth_of_discharge=0.9,
            annual_degradation_rate=0.02,
            replacement_year=None,
            annual_capacity_revenue=0.0,
            annual_ancillary_revenue=0.0,
            annual_other_revenue=0.0,
        ),
        tariff=TariffConfig(
            tariff_mode=TariffMode.TOU,
            peak_price=1.0,
            flat_price=0.7,
            valley_price=0.4,
            peak_ratio=0.3,
            flat_ratio=0.4,
            valley_ratio=0.3,
            export_price=GOLDEN_EXPORT_PRICE,
        ),
        investment=InvestmentConfig(
            mode=InvestmentMode.UNIT_PRICE,
            pv_capex_per_kw=GOLDEN_PV_CAPEX_PER_KW,
            storage_capex_per_kwh=1000.0,
        ),
        opex=OpexConfig(
            pv_opex=0.02,
            pv_opex_mode=OpexMode.RATIO_OF_CAPEX,
            storage_opex=0.02,
            storage_opex_mode=OpexMode.RATIO_OF_CAPEX,
            insurance=0.005,
            insurance_mode=OpexMode.RATIO_OF_CAPEX,
            management_cost=10000.0,
            management_mode=OpexMode.FIXED,
            other_opex=5000.0,
            other_opex_mode=OpexMode.FIXED,
            roof_rent_mode=RoofRentMode.FIXED,
            annual_fixed_rent=0.0,
            annual_opex_growth_rate=0.0,
        ),
        tax=TaxConfig(
            depreciation_years=20,
            residual_value_ratio=0.05,
            depreciable_capex_ratio=1.0,
            vat_rate=0.13,
            income_tax_rate=0.25,
            surcharge_rate=0.0,
            other_tax_rate=0.0,
            revenue_is_vat_inclusive=False,
        ),
        financing=FinancingConfig(
            enabled=True,
            debt_ratio=GOLDEN_DEBT_RATIO,
            equity_ratio=1.0 - GOLDEN_DEBT_RATIO,
            loan_amount=None,
            interest_rate=GOLDEN_INTEREST_RATE,
            loan_term=GOLDEN_LOAN_TERM,
            grace_period=0,
            repayment_method=RepaymentMethod.EQUAL_PRINCIPAL,
        ),
        scenario=ScenarioConfig(enabled=True),
        sensitivity=SensitivityConfig(enabled=True),
        analysis_period=GOLDEN_ANALYSIS_PERIOD,
        discount_rate=0.08,
    )


@pytest.fixture
def golden_pv() -> Project:
    """黄金案例 1：工商业光伏（1000 kWp）。"""
    return _base_project(ProjectType.COMMERCIAL_PV)


@pytest.fixture
def golden_storage() -> Project:
    """黄金案例 2：工商业储能（500 kW / 1000 kWh，无光伏）。"""
    p = _base_project(ProjectType.COMMERCIAL_STORAGE)
    return p


@pytest.fixture
def golden_pv_storage() -> Project:
    """黄金案例 3：工商业光储（1000 kWp + 500 kW/1000 kWh）。"""
    return _base_project(ProjectType.PV_STORAGE)


@pytest.fixture
def hubei_policy() -> PolicyProfile:
    """湖北政策 Profile 样例（结构化占位，标注为假设值，见 HUBEI_POLICY_MODEL.md）。"""
    from datetime import date

    return PolicyProfile(
        policy_id="HUBEI-NEM-2025",
        policy_name="湖北新能源电价政策模板",
        policy_version="2025-10-01",
        effective_date=date(2025, 10, 1),
        expiry_date=None,
        province="湖北",
        pricing_mechanism="市场化交易 + 分时电价",
        market_price=0.42,
        mechanism_price=0.38,
        mechanism_volume_ratio=0.5,
        green_energy_price=0.02,
        green_environmental_value=0.01,
        source="系统内置模板（参数为假设值，需用户按现行政策核对）",
        source_url="",
        notes="模板仅提供结构，具体数值必须由用户按项目所在地现行政策填写。",
    )
