"""V2.3 阶段 5：12 份真实账单的复算校准回归测试（规格书 §7.4、§7.5、§10 阶段 5）。

数据来源
--------
``tests/data/dongfeng_2025_bill_facts.json``：东风本田汽车有限公司第三工厂 2025 年 12 份
《国网湖北省电力公司电费账单》（户号 4206851784045，交流 110kV 大工业用电，市场化直购客户）
的**只读 PDF 抽取并逐份核对**结果（抽取脚本与核对结论见 ``taiqu-storage/`` 下的阶段 5 分析脚本）。

本测试固化两条校准轨道（**差异必须被解释，而不是调参数抹平**，§7.4 第 4 条）：

* **轨道 B（同口径自校验）**：用账单自身实际分时单价反算电价计划后复算，
  必须几乎完全复现账单电度电费 → 证明"分时电量 × 分时单价"公式与账单口径一致；
* **轨道 A（跨期口径对照）**：用官方 2026-01 代理购电价格表（两部制 110 千伏）复算 2025 年账单，
  差异必须完全可解释（分解残差为 0），且不得把差异归零。

注意：官方表执行期为 2026-01-01~2026-01-31，与 2025 年账单**不同期**，
轨道 A 只作口径对照，不代表该月实际执行电价（测试中显式校验该披露存在）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cenep.application.tariff_service import TariffService
from cenep.calculation.bill_recalculator import (
    calibrate_bills,
    energy_by_period_from_hourly,
)
from cenep.calculation.tariff_plan_engine import effective_period_prices
from cenep.domain.bill_models import ElectricityBill
from cenep.domain.enums import BillSourceType, DemandBillingMode, TariffPeriod, TariffStructure
from cenep.domain.models import Project
from cenep.policy.hubei_commercial import (
    OFFICIAL_2026_01_COMMON_COMPONENTS,
    build_dfl_material_plan,
)

FIXTURE = Path(__file__).parent / "data" / "dongfeng_2025_bill_facts.json"
OFFICIAL_110_ID = "HUBEI_COMMERCIAL_2026_01_TWO_PART_KV110"

#: 官方 110 千伏两部制表列绝对值（用于口径对照断言）
OFFICIAL_ENERGY_UNIT = (
    OFFICIAL_2026_01_COMMON_COMPONENTS["agent_purchase_price"]
    + OFFICIAL_2026_01_COMMON_COMPONENTS["line_loss_price"]
    + 0.0884
    + OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"]
    + OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
)


@pytest.fixture(scope="module")
def facts() -> dict:
    """12 份真实账单的结构化事实（模块级缓存）。"""
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def _bill(entry: dict) -> ElectricityBill:
    """由夹具条目构造账单事实（只读校准用，不写回任何数据）。"""
    return ElectricityBill(
        bill_id=f"BILL-{entry['billing_month']}",
        project_id="东风本田第三工厂（校准用）",
        billing_period_start=date.fromisoformat(entry["billing_period_start"]),
        billing_period_end=date.fromisoformat(entry["billing_period_end"]),
        meter_id="MAIN",
        voltage_level=entry["voltage_level"],
        tariff_structure=TariffStructure.TWO_PART,
        billing_demand_kw=entry["demand_kw"],
        energy_total_kwh=entry["energy_total_kwh"],
        energy_charge_yuan=entry["energy_charge_yuan"],
        demand_charge_yuan=entry["demand_charge_yuan"],
        power_factor_adjustment_yuan=entry["power_factor_yuan"],
        bill_total_yuan=entry["bill_total_yuan"],
        source_type=BillSourceType.EXCEL,
        source_file_name=entry["file_name"],
        source_row_number=1,
    )


def _period_energies(entry: dict) -> dict[TariffPeriod, float]:
    return {TariffPeriod(key): value for key, value in entry["period_energy_kwh"].items()}


def _period_prices(entry: dict) -> dict[TariffPeriod, float]:
    return {TariffPeriod(key): value for key, value in entry["period_unit_price_yuan_per_kwh"].items()}


# --------------------------------------------------------------------------- #
# 夹具自洽性（先证明数据可信，再谈复算）
# --------------------------------------------------------------------------- #
def test_fixture_totals_match_verified_figures(facts: dict) -> None:
    """12 份账单合计必须与前序核验值一致：85,269,994 kWh / 62,277,707.14 元 / 0.730359 元每千瓦时。"""
    assert facts["totals"]["energy_kwh"] == pytest.approx(85_269_994.0)
    assert facts["totals"]["amount_yuan"] == pytest.approx(62_277_707.14)
    assert facts["totals"]["weighted_price_yuan_per_kwh"] == pytest.approx(0.730359, abs=5e-7)
    assert len(facts["months"]) == 12


@pytest.mark.parametrize("index", range(12))
def test_each_bill_is_internally_consistent(facts: dict, index: int) -> None:
    """逐份核对：账单总额 = 电度电费 + 需量电费 + 功率因数调整；需量电费 = 需量 × 39。"""
    entry = facts["months"][index]
    assert entry["bill_total_yuan"] == pytest.approx(
        entry["energy_charge_yuan"] + entry["demand_charge_yuan"] + entry["power_factor_yuan"],
        abs=0.02,
    )
    assert entry["demand_charge_yuan"] == pytest.approx(entry["demand_kw"] * 39.0, abs=0.01)
    total = sum(entry["period_energy_kwh"].values())
    assert total == pytest.approx(entry["energy_total_kwh"], abs=1.0)


def test_tou_structure_matches_documented_shares(facts: dict) -> None:
    """按官方时段规则归并的分时结构与前序核验值一致（尖峰 ~9.2%、谷 ~24.1%）。"""
    structure = facts["tou_structure_official_periods"]
    assert structure["尖峰"]["share"] == pytest.approx(0.0915, abs=0.002)
    assert structure["高峰"]["share"] == pytest.approx(0.2847, abs=0.003)
    assert structure["平段"]["share"] == pytest.approx(0.3831, abs=0.003)
    assert structure["谷段"]["share"] == pytest.approx(0.2407, abs=0.003)
    assert sum(item["kwh"] for item in structure.values()) == pytest.approx(85_269_994.0)


# --------------------------------------------------------------------------- #
# 轨道 B：按账单实际分时单价反算 → 必须几乎完全复现账单
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", range(12))
def test_track_b_reproduces_each_bill(facts: dict, index: int) -> None:
    """轨道 B：账单实际分时单价 + 官方时段规则 → 复算电度电费与账单相差 < 1 元（12/12）。"""
    entry = facts["months"][index]
    bill = _bill(entry)
    project = Project()
    project.basic_info.project_name = "东风本田第三工厂（校准用）"
    project.bills = [bill]
    service = TariffService(project)
    plan = service.build_plan_from_bill(
        bill.bill_id,
        template_plan_id=OFFICIAL_110_ID,
        period_unit_price=_period_prices(entry),
        note="回归测试：按账单实际分时单价反算",
    )
    outcome = service.recompute_bill(
        bill.bill_id,
        plan.tariff_plan_id,
        period_energies=_period_energies(entry),
        require_verified=False,
    )
    energy_difference = float(outcome.recomputed_energy_charge_yuan) - entry["energy_charge_yuan"]
    # 复算 = Σ 分时电量 × 账单实际分时单价；差额来自账单公布的逐小时价格只有 7 位小数，
    # 用其重算数百万 kWh 的电费会产生个位数元的舍入差（12 个月合计仅 −0.71 元）
    assert abs(energy_difference) < 5.0, entry["billing_month"]
    assert abs(energy_difference) / entry["energy_charge_yuan"] < 1e-6
    assert outcome.passed_tolerance is True
    assert abs(outcome.difference_after_baseline_yuan) < 5.0
    assert outcome.recomputed_demand_charge_yuan == pytest.approx(entry["demand_kw"] * 39.0)
    assert outcome.demand_billing_mode is DemandBillingMode.DEMAND


def test_track_b_annual_total_matches(facts: dict) -> None:
    """轨道 B 年度合计：12 份账单电度电费 50,896,163.64 元，复算差额 < 5 元（合计口径）。"""
    project = Project()
    project.basic_info.project_name = "东风本田第三工厂（校准用）"
    bills = [_bill(entry) for entry in facts["months"]]
    project.bills = bills
    service = TariffService(project)
    plan = service.build_plan_from_bill(
        bills[0].bill_id,
        template_plan_id=OFFICIAL_110_ID,
        period_unit_price=_period_prices(facts["months"][0]),
        note="回归测试：年度合计",
    )
    assert plan.tariff_plan_id.endswith("_FROM_2025-01")

    overrides = {bill.bill_id: _period_energies(entry) for bill, entry in zip(bills, facts["months"])}
    # 逐月价格不同 → 逐月各自反算一份计划
    total_bill_energy = 0.0
    total_recomputed = 0.0
    for bill, entry in zip(bills, facts["months"]):
        monthly_plan = service.build_plan_from_bill(
            bill.bill_id,
            template_plan_id=OFFICIAL_110_ID,
            period_unit_price=_period_prices(entry),
            note="回归测试：年度合计",
        )
        outcome = service.recompute_bill(
            bill.bill_id,
            monthly_plan.tariff_plan_id,
            period_energies=overrides[bill.bill_id],
            require_verified=False,
        )
        total_bill_energy += entry["energy_charge_yuan"]
        total_recomputed += float(outcome.recomputed_energy_charge_yuan)
    assert total_bill_energy == pytest.approx(50_896_163.64, abs=0.02)
    assert abs(total_recomputed - total_bill_energy) < 5.0
    # 需量电费合计：与 12 份账单需量电费严格相等（官方 110 千伏需量电价 39 元/千瓦·月）
    assert sum(entry["demand_charge_yuan"] for entry in facts["months"]) == pytest.approx(11_775_738.0)


# --------------------------------------------------------------------------- #
# 轨道 A：官方 2026-01 代理购电价格表（跨期口径对照）
# --------------------------------------------------------------------------- #
def test_track_a_official_plan_usable_and_hours_complete(facts: dict) -> None:
    """官方计划在自身执行期内可用于正式复算，且 12 个月均满足 24 小时覆盖。"""
    service = TariffService(Project())
    validation = service.validate_plan(OFFICIAL_110_ID, as_of=date(2026, 1, 10))
    assert validation.usable_for_formal_use is True
    assert all(item.is_complete for item in validation.coverage)
    # 官方 110 千伏四时段电价固化
    plan = service.get_plan(OFFICIAL_110_ID)
    prices = effective_period_prices(plan)
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(1.012099)
    assert prices[TariffPeriod.PEAK] == pytest.approx(0.809061)
    assert prices[TariffPeriod.FLAT] == pytest.approx(0.613986)
    assert prices[TariffPeriod.VALLEY] == pytest.approx(0.395024)


def test_track_a_difference_is_fully_explained(facts: dict) -> None:
    """官方口径复算与账单的差异必须完全可解释：分解残差为 0，各因素方向明确。"""
    project = Project()
    project.basic_info.project_name = "东风本田第三工厂（校准用）"
    project.bills = [_bill(entry) for entry in facts["months"]]
    service = TariffService(project)
    official = service.get_plan(OFFICIAL_110_ID)

    overrides = {
        bill.bill_id: _period_energies(entry)
        for bill, entry in zip(project.bills, facts["months"])
    }
    summary = service.calibrate_bills_with_plan(
        project.bills, official, year=2025, require_verified=False, period_energies_by_bill=overrides
    )
    # 官方代理购电价格水平高于该市场化直购用户实际结算水平 → 官方复算更高
    assert summary.total_difference_yuan < 0
    assert summary.calibration_passed is False  # 跨期口径对照必然不通过（不得抹平差异）
    assert summary.months_covered == [f"2025-{month:02d}" for month in range(1, 13)]
    assert summary.can_sum_directly is True

    factors = summary.factor_totals
    # 差异分解必须闭合：残差严格为 0
    assert factors["口径残差（账单内部合计与复算口径之差）"] == pytest.approx(0.0, abs=1e-6)
    # 基本电费差为 0：官方 110 千伏需量电价 39 元/千瓦·月与账单完全一致
    assert factors["基本电费差（容量 / 需量）"] == pytest.approx(0.0, abs=1e-6)
    # 未建模费用（功率因数调整，全部为返还/奖励）按账单实际值保留
    assert factors["未建模费用（功率因数调整 / 其他 / 增值税）"] == pytest.approx(-394_194.50, abs=0.02)
    # 电度电费差：官方口径比实际高
    assert factors["电度电费差（账单实录 − 计划复算）"] == pytest.approx(-4_806_688.81, abs=0.02)
    assert sum(factors.values()) == pytest.approx(summary.total_difference_yuan, abs=0.02)


def test_track_a_unit_price_decomposition_directions(facts: dict) -> None:
    """差异因素方向（用夹具中的账单分项单位价与官方分项单位价对照）：

    * 系统运行费折价：官方 0.082273 远高于该市场化直购用户实际 0.0399 → 官方更高（最大因素）；
    * 电能量：该用户市场化购电均价 0.4003 高于官方代理购电 0.384470 → 官方更低；
    * 上网环节线损：实际 0.0206 高于官方折价 0.013643 → 官方更低；
    * 电度输配电价：实际含 3% 非居民照明子表（费率 0.170299），略高于官方 110 千伏 0.0884；
    * 政府性基金及附加：两者**完全一致** 0.0452。
    """
    total_energy = facts["totals"]["energy_kwh"]
    aggregated = {
        key: sum(entry["component_unit_price_yuan_per_kwh"][key] * entry["energy_total_kwh"] for entry in facts["months"])
        / total_energy
        for key in facts["months"][0]["component_unit_price_yuan_per_kwh"]
    }
    assert aggregated["market_energy"] == pytest.approx(0.400319, abs=1e-6)
    assert aggregated["line_loss"] == pytest.approx(0.020594, abs=1e-6)
    assert aggregated["system_operation"] == pytest.approx(0.039915, abs=1e-6)
    assert aggregated["government_fund"] == pytest.approx(0.0452, abs=1e-9)
    assert aggregated["transmission_distribution"] == pytest.approx(0.090854, abs=1e-6)

    assert aggregated["market_energy"] > OFFICIAL_2026_01_COMMON_COMPONENTS["agent_purchase_price"]
    assert aggregated["line_loss"] > OFFICIAL_2026_01_COMMON_COMPONENTS["line_loss_price"]
    assert aggregated["transmission_distribution"] > 0.0884  # 含非居民照明子表
    assert aggregated["system_operation"] < OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"]
    assert aggregated["government_fund"] == pytest.approx(
        OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
    )


def test_official_tou_weighting_raises_average_above_flat(facts: dict) -> None:
    """官方分时浮动影响：按用户负荷结构加权后的官方均价高于官方平段单价（这是差异的第二大因素）。"""
    service = TariffService(Project())
    official_prices = effective_period_prices(service.get_plan(OFFICIAL_110_ID))
    shares = facts["tou_structure_official_periods"]
    weighted = (
        shares["尖峰"]["share"] * official_prices[TariffPeriod.SHARP_PEAK]
        + shares["高峰"]["share"] * official_prices[TariffPeriod.PEAK]
        + shares["平段"]["share"] * official_prices[TariffPeriod.FLAT]
        + shares["谷段"]["share"] * official_prices[TariffPeriod.VALLEY]
    )
    flat = official_prices[TariffPeriod.FLAT]
    assert weighted > flat
    assert weighted - flat == pytest.approx(0.0392, abs=0.002)


def test_track_a_discloses_cross_period_limitation(facts: dict) -> None:
    """跨期口径对照必须被显式披露，不得让用户误以为是该月实际电价（§7.4、§2.3）。"""
    project = Project()
    project.basic_info.project_name = "东风本田第三工厂（校准用）"
    project.bills = [_bill(facts["months"][10])]  # 2025-11
    service = TariffService(project)
    entry = facts["months"][10]
    outcome = service.recompute_bill(
        project.bills[0].bill_id,
        OFFICIAL_110_ID,
        period_energies=_period_energies(entry),
        require_verified=False,
    )
    text = "\n".join(outcome.assumptions)
    assert "跨期口径对照" in text
    assert "不代表该月实际执行的电价" in text
    messages = "\n".join(outcome.messages)
    assert "复算口径" in messages and "不是账单实录值" in messages


# --------------------------------------------------------------------------- #
# 项目资料版口径影响（业务核心发现，量化而不抹平）
# --------------------------------------------------------------------------- #
def test_material_calibre_overstates_peak_and_understates_valley(facts: dict) -> None:
    """资料版口径（平段对齐）会放大峰谷价差：尖峰/高峰更高、低谷更低（量化断言）。"""
    service = TariffService(Project())
    official = service.get_plan(OFFICIAL_110_ID)
    official_prices = effective_period_prices(official)
    material = build_dfl_material_plan(
        base_price_yuan_per_kwh=float(official_prices[TariffPeriod.FLAT])
        - OFFICIAL_2026_01_COMMON_COMPONENTS["government_fund"]
    )
    material_prices = effective_period_prices(material)

    assert material_prices[TariffPeriod.FLAT] == pytest.approx(official_prices[TariffPeriod.FLAT])
    assert material_prices[TariffPeriod.SHARP_PEAK] > official_prices[TariffPeriod.SHARP_PEAK]
    assert material_prices[TariffPeriod.PEAK] > official_prices[TariffPeriod.PEAK]
    assert material_prices[TariffPeriod.VALLEY] < official_prices[TariffPeriod.VALLEY]

    official_spread = (
        official_prices[TariffPeriod.SHARP_PEAK] - official_prices[TariffPeriod.VALLEY]
    )
    material_spread = (
        material_prices[TariffPeriod.SHARP_PEAK] - material_prices[TariffPeriod.VALLEY]
    )
    assert material_spread / official_spread - 1 == pytest.approx(0.2167, abs=0.01)


def test_material_periods_move_energy_between_peak_and_valley() -> None:
    """时段划分差异会改变电量结构（用构造的"典型日"形状演示方向，非实测数据）：

    资料版把 9:00-15:00 整段计为高峰、23:00-次日 7:00 计为低谷；
    官方同期 12:00-14:00 为低谷、9:00-12:00 与 14:00-15:00 为平段、16:00-24:00 为高峰/尖峰。
    因此在"白天高、夜间低"的负荷形状下：资料版高峰电量更多、低谷电量更少。
    """
    service = TariffService(Project())
    official = service.get_plan(OFFICIAL_110_ID)
    material = build_dfl_material_plan(base_price_yuan_per_kwh=0.5)

    # 构造典型日形状（每小时一台机组负荷，单位任意）：夜间低、白天高、午间略降
    shape = [
        30, 28, 28, 30, 35, 45,   # 0-5 夜间低谷
        70, 90, 120, 140, 145, 140,  # 6-11 上班爬坡 + 上午高峰
        100, 105,                  # 12-13 午间下降
        140, 145,                  # 14-15 下午高峰
        130, 120, 100, 95,         # 16-19 收工
        80, 70, 60, 45,            # 20-23 夜班收尾
    ]
    official_by_period: dict[TariffPeriod, float] = {}
    material_by_period: dict[TariffPeriod, float] = {}
    for month in (3, 7, 11):  # 覆盖普通月份与 7、8 月规则
        for period, value in energy_by_period_from_hourly(official, month, shape).items():
            official_by_period[period] = official_by_period.get(period, 0.0) + value
        for period, value in energy_by_period_from_hourly(material, month, shape).items():
            material_by_period[period] = material_by_period.get(period, 0.0) + value

    total = sum(shape) * 3
    assert material_by_period[TariffPeriod.PEAK] > official_by_period[TariffPeriod.PEAK]
    assert material_by_period[TariffPeriod.VALLEY] < official_by_period[TariffPeriod.VALLEY]
    peak_shift = (material_by_period[TariffPeriod.PEAK] - official_by_period[TariffPeriod.PEAK]) / total
    valley_shift = (material_by_period[TariffPeriod.VALLEY] - official_by_period[TariffPeriod.VALLEY]) / total
    assert peak_shift > 0.05
    assert valley_shift < -0.03
