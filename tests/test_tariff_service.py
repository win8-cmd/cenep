"""V2.3 阶段 5：电价计划版本库与应用服务测试（规格书 §4.1、§4.2、§7.1–§7.4、§8.2、§9.3）。

覆盖：
* 版本库内置计划播种（幂等、不覆盖用户修改）与 SQLite 持久化往返（§8.2）；
* 用户覆盖必须留痕（§4.1、§4.2），内置版本永不被改写；
* "按用户账单反算实际分时电价"两种口径（§4.1 第 3 条优先于未验证模板）；
* 市场化直购变体派生（§7.3）；
* 服务层编排的复算 / 年度校准 / 报告行（§7.4、§8.1）；
* 界面选择提示（"让用户选择、核对、覆盖"，不得替用户决定）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cenep.application.tariff_service import TariffService
from cenep.calculation.errors import ValidationError
from cenep.domain.bill_models import ElectricityBill
from cenep.domain.enums import (
    BillSourceType,
    MarketMode,
    TariffComponentType,
    TariffPeriod,
    TariffPlanStatus,
    TariffStructure,
)
from cenep.domain.models import Project
from cenep.domain.tariff_models import TariffPlan
from cenep.infrastructure.db import Database
from cenep.policy.hubei_commercial import OFFICIAL_2026_01, build_dfl_material_plan
from cenep.policy.tariff_plan_store import USER_PLAN_ID_SUFFIX, TariffPlanStore

OFFICIAL_110_ID = "HUBEI_COMMERCIAL_2026_01_TWO_PART_KV110"
MATERIAL_ID = "HUBEI_DFL_MATERIAL_TOU_RATIO"


def _bill(bill_id: str = "BILL-2026-03", **overrides: object) -> ElectricityBill:
    payload: dict[str, object] = {
        "bill_id": bill_id,
        "project_id": "测试项目",
        "billing_period_start": date(2025, 3, 1),
        "billing_period_end": date(2025, 3, 31),
        "voltage_level": "110千伏",
        "tariff_structure": TariffStructure.TWO_PART,
        "energy_total_kwh": 1_000_000.0,
        "energy_sharp_kwh": 100_000.0,
        "energy_peak_kwh": 200_000.0,
        "energy_flat_kwh": 300_000.0,
        "energy_valley_kwh": 400_000.0,
        "energy_charge_yuan": 600_000.0,
        "demand_charge_yuan": 1_000_000.0,
        "billing_demand_kw": 25_641.0,
        "power_factor_adjustment_yuan": -20_000.0,
        "bill_total_yuan": 1_580_000.0,
        "source_type": BillSourceType.MANUAL,
    }
    payload.update(overrides)
    return ElectricityBill(**payload)  # type: ignore[arg-type]


def _project(*bills: ElectricityBill) -> Project:
    project = Project()
    project.basic_info.project_name = "测试项目"
    project.bills = list(bills)
    return project


# --------------------------------------------------------------------------- #
# 版本库（§4.1、§8.2）
# --------------------------------------------------------------------------- #
def test_store_seeds_both_versions() -> None:
    """版本库必须同时播种官方 2026-01 版（7 个）与项目资料版（1 个）。"""
    store = TariffPlanStore()
    plans = store.list_plans()
    assert len(plans) == 8
    ids = {plan.tariff_plan_id for plan in plans}
    assert OFFICIAL_110_ID in ids and MATERIAL_ID in ids


def test_store_seeding_is_idempotent_and_keeps_user_edits() -> None:
    """重复播种不得覆盖用户已修改的计划（内置版本一次写入后保持用户版本）。"""
    store = TariffPlanStore()
    edited = store.get_plan(OFFICIAL_110_ID).model_copy(
        update={"notes": "用户补充说明", "user_overridden": True, "override_note": "补充口径"}
    )
    store.save_plan(edited)
    assert store.seed_builtin_plans() == 0
    assert "用户补充说明" in store.get_plan(OFFICIAL_110_ID).notes


def test_store_persistence_round_trip(tmp_path: Path) -> None:
    """SQLite 持久化往返：重开库后计价值、时段规则、来源与状态完全一致（§8.2）。"""
    db_path = tmp_path / "cenep.db"
    with Database(db_path) as db:
        store = TariffPlanStore(db)
        original = store.get_plan(OFFICIAL_110_ID)
    with Database(db_path) as db:
        reopened = TariffPlanStore(db).get_plan(OFFICIAL_110_ID)
    assert reopened.model_dump() == original.model_dump()


def test_store_missing_plan_raises_chinese() -> None:
    """取不存在的计划必须是**中文**报错并列出可用 ID。"""
    store = TariffPlanStore()
    with pytest.raises(ValidationError) as excinfo:
        store.get_plan("不存在")
    assert "未找到电价计划" in str(excinfo.value)


def test_user_override_requires_note_and_creates_new_id() -> None:
    """用户覆盖必须填写说明并生成新 ID（内置版本不被改写）（§4.1、§4.2、§7.4 第 4 条）。"""
    store = TariffPlanStore()
    base = store.get_plan(OFFICIAL_110_ID)
    with pytest.raises(ValidationError) as excinfo:
        store.save_user_override(base, override_note="   ")
    assert "覆盖说明" in str(excinfo.value)

    override = store.save_user_override(base, override_note="按合同调整平段电价")
    assert override.tariff_plan_id == OFFICIAL_110_ID + USER_PLAN_ID_SUFFIX
    assert override.user_overridden is True
    assert "按合同调整平段电价" in override.override_note
    # 原内置版本保持不变
    assert store.get_plan(OFFICIAL_110_ID).user_overridden is False


def test_store_select_by_date_and_structure() -> None:
    """按计费日期 + 计费方式选取版本；两套计划并存时按日期各取所辖版本。

    * 2026-01 账单 → 官方 2026-01 版（110 千伏两部制）；
    * 2025 年账单 → 官方 2026-01 版不覆盖，回落到项目资料版（其生效期自 2025-01-01 起）；
    * 两套都不覆盖的日期 → 中文报错，不得猜一个版本。
    """
    store = TariffPlanStore()
    chosen = store.select(date(2026, 1, 15), voltage_level="110千伏", tariff_structure="two_part")
    assert chosen.tariff_plan_id == OFFICIAL_110_ID

    fallback = store.select(date(2025, 11, 1), voltage_level="110千伏", tariff_structure="two_part")
    assert fallback.tariff_plan_id == MATERIAL_ID

    with pytest.raises(ValidationError) as excinfo:
        store.select(date(2024, 6, 1), voltage_level="110千伏", tariff_structure="two_part")
    assert "没有覆盖" in str(excinfo.value)


def test_market_direct_variant_replaces_only_energy_component() -> None:
    """市场化直购变体只替换电能量分项，其余分项沿用官方表；市场电价必须由用户录入（§4.1）。"""
    store = TariffPlanStore()
    variant = store.market_direct_variant(
        OFFICIAL_110_ID, market_energy_price_yuan_per_kwh=0.45, line_loss_price_yuan_per_kwh=0.02
    )
    assert variant.market_mode is MarketMode.RETAIL_MARKET
    assert variant.tariff_plan_id.endswith("_MARKET")
    names = {component.name for component in variant.price_components if component.included_in_tou_price}
    assert "市场化直购电能量电价（用户录入）" in names
    assert "电度输配电价" in names and "政府性基金及附加" in names
    # 基础电价 = 0.45 + 0.02；固定分项 = 0.0884 + 0.082273 + 0.0452
    from cenep.calculation.tariff_plan_engine import base_price_yuan_per_kwh, fixed_price_yuan_per_kwh

    assert base_price_yuan_per_kwh(variant) == pytest.approx(0.47)
    assert fixed_price_yuan_per_kwh(variant) == pytest.approx(0.0884 + 0.082273 + 0.0452)
    # 官方计划未被改动
    assert store.get_plan(OFFICIAL_110_ID).market_mode is MarketMode.UTILITY_AGENT

    with pytest.raises(ValidationError):
        store.market_direct_variant(OFFICIAL_110_ID, market_energy_price_yuan_per_kwh=0.0)


# --------------------------------------------------------------------------- #
# 服务层：查询、校验、对比（§7.3）
# --------------------------------------------------------------------------- #
def test_service_plan_summaries_include_source_and_scope() -> None:
    """计划摘要必须含来源、文号、适用范围与各时段价格，供报告与界面展示。"""
    service = TariffService(_project())
    rows = {row["tariff_plan_id"]: row for row in service.plan_summaries()}
    official = rows[OFFICIAL_110_ID]
    assert official["状态"] == TariffPlanStatus.VERIFIED.label
    assert "鄂发改价管" in str(official["政策文号"])
    assert official["适用电压等级"] == "110千伏"
    assert official["尖峰_元每千瓦时"] == pytest.approx(1.012099)
    assert official["最大需量_元每千瓦月"] == pytest.approx(39.0)
    assert official["基础电价_元每千瓦时"] == pytest.approx(0.398113)
    assert isinstance(official["时段小时数"], dict)


def test_service_validate_plan_for_bill_period() -> None:
    """按账单账期校验：2025 年账单不能用 2026-01 版官方计划（§2.3 第 3 条）。"""
    service = TariffService(_project(_bill()))
    outcome = service.validate_plan_for_bill("BILL-2026-03", OFFICIAL_110_ID)
    assert outcome.usable_for_formal_use is False
    assert any(issue.code == "T06" for issue in outcome.issues_of("ERROR"))


def test_service_compares_official_with_material() -> None:
    """官方 2026-01 版 vs 项目资料版：逐项差异必须列出（两套并存）。"""
    service = TariffService(_project())
    comparison = service.compare_official_with_material()
    assert comparison.same_time_periods is False
    assert comparison.same_float_multipliers is False
    assert any(item.item == "尖峰浮动系数" for item in comparison.items)
    assert any(item.item == "高峰时段" for item in comparison.items)


def test_service_describe_plan_choice_never_decides_for_user() -> None:
    """选择提示必须列出候选计划并要求用户核对，不得替用户决定。"""
    service = TariffService(_project())
    text = service.describe_plan_choice(service.list_plans())
    assert "请选择并核对" in text
    assert "不替用户决定" in text
    assert text.count("·") >= 8


# --------------------------------------------------------------------------- #
# 服务层：按账单反算实际电价（§4.1 第 3 条、§2.3）
# --------------------------------------------------------------------------- #
def test_build_plan_from_bill_with_explicit_period_prices() -> None:
    """口径一：直接录入账单实际分时单价，软件**原样采用**（优先级最高的价格来源）。"""
    bill = _bill()
    service = TariffService(_project(bill))
    unit_prices = {
        TariffPeriod.SHARP_PEAK: 1.05,
        TariffPeriod.PEAK: 0.85,
        TariffPeriod.FLAT: 0.65,
        TariffPeriod.VALLEY: 0.43,
    }
    plan = service.build_plan_from_bill(
        bill.bill_id,
        template_plan_id=OFFICIAL_110_ID,
        period_unit_price=unit_prices,
        note="按账单实际分时单价录入",
    )
    from cenep.calculation.tariff_plan_engine import effective_period_prices

    prices = effective_period_prices(plan)
    for period, price in unit_prices.items():
        assert prices[period] == pytest.approx(price)
    assert plan.tariff_plan_id.endswith("_FROM_2025-03")
    assert plan.effective_from == bill.billing_period_start
    assert plan.effective_to == bill.billing_period_end
    assert plan.user_overridden is True
    assert "按账单实际分时单价录入" in plan.override_note
    # 模板计划未被改写
    assert service.get_plan(OFFICIAL_110_ID).user_overridden is False


def test_build_plan_from_bill_requires_all_periods() -> None:
    """直接录入分时单价时必须覆盖全部时段，缺一个即中文报错（不得按平段兜底，§4.2）。"""
    bill = _bill()
    service = TariffService(_project(bill))
    with pytest.raises(ValidationError) as excinfo:
        service.build_plan_from_bill(
            bill.bill_id,
            template_plan_id=OFFICIAL_110_ID,
            period_unit_price={TariffPeriod.FLAT: 0.65},
        )
    assert "全部时段" in str(excinfo.value)


def test_build_plan_from_bill_rejects_negative_price() -> None:
    """负单价必须是中文报错。"""
    bill = _bill()
    service = TariffService(_project(bill))
    with pytest.raises(ValidationError) as excinfo:
        service.build_plan_from_bill(
            bill.bill_id,
            template_plan_id=OFFICIAL_110_ID,
            period_unit_price={
                TariffPeriod.SHARP_PEAK: -1.0,
                TariffPeriod.PEAK: 0.85,
                TariffPeriod.FLAT: 0.65,
                TariffPeriod.VALLEY: 0.43,
            },
        )
    assert "不能为负" in str(excinfo.value)


def test_build_plan_from_bill_with_component_values() -> None:
    """口径二：由分项数值按模板浮动系数合成；未知分项键必须是中文报错。"""
    bill = _bill()
    service = TariffService(_project(bill))
    plan = service.build_plan_from_bill(
        bill.bill_id,
        template_plan_id=OFFICIAL_110_ID,
        component_values={
            TariffComponentType.MARKET_ENERGY: 0.42,
            TariffComponentType.LINE_LOSS: 0.02,
            TariffComponentType.TRANSMISSION_DISTRIBUTION: 0.0884,
            TariffComponentType.SYSTEM_OPERATION: 0.03,
            TariffComponentType.GOVERNMENT_FUND: 0.0452,
        },
        note="按账单分项均价反算",
    )
    from cenep.calculation.tariff_plan_engine import (
        base_price_yuan_per_kwh,
        effective_period_prices,
        fixed_price_yuan_per_kwh,
    )

    base = base_price_yuan_per_kwh(plan)
    fixed = fixed_price_yuan_per_kwh(plan)
    assert base == pytest.approx(0.44)
    assert fixed == pytest.approx(0.0884 + 0.03 + 0.0452)
    prices = effective_period_prices(plan)
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(base * 2.0 + fixed)
    assert prices[TariffPeriod.VALLEY] == pytest.approx(base * 0.45 + fixed)

    with pytest.raises(ValidationError) as excinfo:
        service.build_plan_from_bill(
            bill.bill_id,
            template_plan_id=OFFICIAL_110_ID,
            component_values={"不存在的分项": 0.1},
        )
    assert "未能匹配" in str(excinfo.value)


def test_build_plan_from_bill_without_any_price_raises() -> None:
    """两种口径都没提供时必须中文报错。"""
    bill = _bill()
    service = TariffService(_project(bill))
    with pytest.raises(ValidationError) as excinfo:
        service.build_plan_from_bill(bill.bill_id, template_plan_id=OFFICIAL_110_ID)
    assert "必须提供" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 服务层：复算、校准与报告行（§7.4、§8.1）
# --------------------------------------------------------------------------- #
def test_service_recompute_with_hourly_energy() -> None:
    """提供 24 点逐小时电量时，服务层按计划时段归并后复算（§7.4 第 1 条）。"""
    bill = _bill(energy_sharp_kwh=None, energy_peak_kwh=None, energy_flat_kwh=None, energy_valley_kwh=None)
    service = TariffService(_project(bill))
    derived = service.build_plan_from_bill(
        bill.bill_id,
        template_plan_id=OFFICIAL_110_ID,
        period_unit_price={
            TariffPeriod.SHARP_PEAK: 1.05,
            TariffPeriod.PEAK: 0.85,
            TariffPeriod.FLAT: 0.65,
            TariffPeriod.VALLEY: 0.43,
        },
        note="校准用",
    )
    hourly = [1_000_000.0 / 24] * 24
    outcome = service.recompute_bill(
        bill.bill_id,
        derived.tariff_plan_id,
        hourly_energy_kwh=hourly,
        require_verified=False,
    )
    # 3 月：尖峰 2h、高峰 6h、平段 8h、低谷 8h
    expected = (
        2 * (1_000_000.0 / 24) * 1.05
        + 6 * (1_000_000.0 / 24) * 0.85
        + 8 * (1_000_000.0 / 24) * 0.65
        + 8 * (1_000_000.0 / 24) * 0.43
    )
    assert outcome.recomputed_energy_charge_yuan == pytest.approx(expected, rel=1e-9)


def test_service_recompute_all_and_report_rows() -> None:
    """年度校准 + 报告行：账单实录与软件复算必须分列（§8.1）。"""
    bills = []
    for month in (1, 2, 3):
        bills.append(
            _bill(
                bill_id=f"BILL-2025{month:02d}",
                billing_period_start=date(2025, month, 1),
                billing_period_end=date(2025, month, 28),
                billing_month=f"2025-{month:02d}",
            )
        )
    service = TariffService(_project(*bills))
    derived = service.build_plan_from_bill(
        bills[0].bill_id,
        template_plan_id=OFFICIAL_110_ID,
        period_unit_price={
            TariffPeriod.SHARP_PEAK: 1.05,
            TariffPeriod.PEAK: 0.85,
            TariffPeriod.FLAT: 0.65,
            TariffPeriod.VALLEY: 0.43,
        },
        note="校准用",
    )
    summary = service.calibrate_bills_with_plan(bills, derived, year=2025, require_verified=False)
    assert summary.bill_count == 3
    rows = service.calibration_rows_for_report(summary)
    header = rows[0]
    assert "账单实录总额（元）" in header and "软件复算合计（元，仅建模部分）" in header
    assert "净差异（元）" in header
    assert len(rows) == 4


def test_service_plan_rows_for_report_has_source_block() -> None:
    """报告行必须包含来源、文号、链接、状态与全部时段价格（§8.1「电价版本与来源」表）。"""
    service = TariffService(_project())
    rows = service.plan_rows_for_report(OFFICIAL_110_ID)
    labels = [row[0] for row in rows]
    for required in ("电价计划名称", "核验状态", "政策文号", "来源链接", "数据抓取日期", "基础电价", "尖峰电度电价"):
        assert required in labels
    assert any(row[0] == "时段规则" for row in rows)


def test_service_unknown_bill_or_plan_raises_chinese() -> None:
    """账单或计划不存在时必须是中文报错。"""
    service = TariffService(_project())
    with pytest.raises(ValidationError):
        service.get_bill("不存在")
    with pytest.raises(ValidationError):
        service.get_plan("不存在")


def test_material_plan_saved_and_compared_alongside_official() -> None:
    """资料版计划必须能独立保存并与官方版并存（不得只留一套）。"""
    store = TariffPlanStore()
    custom = build_dfl_material_plan(base_price_yuan_per_kwh=0.5)
    store.save_plan(custom)
    plans = store.list_plans()
    assert len(plans) == 8
    assert store.get_plan(MATERIAL_ID).status is TariffPlanStatus.DRAFT
    comparison = store.compare(OFFICIAL_110_ID, MATERIAL_ID)
    assert comparison.has_difference is True
    assert "并存" in "".join(comparison.messages)


def test_store_delete_plan() -> None:
    """删除计划返回删除条数（0 = 原本不存在）。"""
    store = TariffPlanStore()
    assert store.delete_plan("不存在") == 0
    assert store.delete_plan(MATERIAL_ID) == 1
    assert len(store.list_plans()) == 7


def test_builtin_plans_are_copies() -> None:
    """从版本库取出的计划是副本，外部修改不得污染库内数据。"""
    store = TariffPlanStore()
    plan: TariffPlan = store.get_plan(OFFICIAL_110_ID)
    plan.name = "被改动"
    assert store.get_plan(OFFICIAL_110_ID).name != "被改动"
