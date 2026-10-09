"""V2.3 阶段 5：湖北官方电价数据与"两套计划并存"测试（规格书 §4.1、§4.2、§4.3、§7.3、§10 阶段 5）。

本文件把**已核验的官方数据**固化为回归断言，防止后续改动静默改数：

* 官方 2026-01《代理购电工商业用户电价表》7 行价格（单一制 3 + 两部制 4）；
* 六项分项合计与非分时电度电价的一致性（两部制严格相等、单一制差 0.0002）；
* 官方"基础电价"口径与反算浮动系数（尖峰 2.0、低谷 0.45 完全吻合；高峰 1.49~1.495）；
* 官方时段划分（价格表注 2，文号 鄂发改价管〔2024〕77 号）24 小时严格闭合；
* 政府性基金及附加明细三项之和严格等于 0.0452；
* 来源/文号/适用范围/核验状态齐备（§2.3、§4.1）；
* **项目资料版与官方版并存**，且逐项差异被量化（用户明确要求不得只留一套）。
"""

from __future__ import annotations

from datetime import date

import pytest

from cenep.calculation.tariff_plan_engine import (
    base_price_yuan_per_kwh,
    compare_tariff_plans,
    effective_period_prices,
    fixed_price_yuan_per_kwh,
    period_hours_of,
    resolve_slot_period,
    validate_tariff_plan,
)
from cenep.domain.enums import MarketMode, PriceBasis, TariffPeriod, TariffPlanStatus
from cenep.policy.hubei_commercial import (
    DFL_MATERIAL,
    DFL_MATERIAL_SOURCE,
    DFL_MATERIAL_TIME_PERIOD_TEXT,
    DFL_MATERIAL_VS_OFFICIAL_NOTES,
    OFFICIAL_2026_01,
    OFFICIAL_2026_01_COMMON_COMPONENTS,
    OFFICIAL_2026_01_CONSISTENCY_NOTES,
    OFFICIAL_2026_01_DECLARED_MULTIPLIERS,
    OFFICIAL_2026_01_FUND_BREAKDOWN,
    OFFICIAL_2026_01_PRICE_ROWS,
    OFFICIAL_2026_01_SOURCE,
    OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN,
    OFFICIAL_2026_01_TABLE2,
    build_dfl_material_plan,
    build_official_plan,
    builtin_tariff_plans,
    component_sum_of_row,
    implied_multipliers_of_row,
    official_voltage_levels,
)

OFFICIAL_110 = next(p for p in OFFICIAL_2026_01 if p.tariff_plan_id.endswith("TWO_PART_KV110"))
OFFICIAL_1_10 = next(p for p in OFFICIAL_2026_01 if p.tariff_plan_id.endswith("TWO_PART_KV1_10"))


# --------------------------------------------------------------------------- #
# 官方价格表数值（逐行固化）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("structure", "voltage", "non_tou", "td", "sharp", "peak", "flat", "valley", "demand", "capacity"),
    [
        ("single_part", "不满1千伏", 0.735686, 0.2103, 1.133999, 0.930961, 0.735686, 0.516924, None, None),
        ("single_part", "1-10(20)千伏", 0.715686, 0.1903, 1.113999, 0.910961, 0.715686, 0.496924, None, None),
        ("single_part", "35千伏", 0.695686, 0.1703, 1.093999, 0.890961, 0.695686, 0.476924, None, None),
        ("two_part", "1-10(20)千伏", 0.651886, 0.1263, 1.049999, 0.848961, 0.651886, 0.432924, 42.0, 26.3),
        ("two_part", "35千伏", 0.632086, 0.1065, 1.030199, 0.827161, 0.632086, 0.413124, 42.0, 26.3),
        ("two_part", "110千伏", 0.613986, 0.0884, 1.012099, 0.809061, 0.613986, 0.395024, 39.0, 24.4),
        ("two_part", "220千伏及以上", 0.594986, 0.0694, 0.993099, 0.790061, 0.594986, 0.376024, 39.0, 24.4),
    ],
)
def test_official_price_rows_verbatim(
    structure: str,
    voltage: str,
    non_tou: float,
    td: float,
    sharp: float,
    peak: float,
    flat: float,
    valley: float,
    demand: float | None,
    capacity: float | None,
) -> None:
    """官方表一 7 行数值逐项固化（与图片证据一致，不得被静默修改）。"""
    row = next(
        item
        for item in OFFICIAL_2026_01_PRICE_ROWS
        if item.tariff_structure == structure and item.voltage_level == voltage
    )
    assert row.non_tou_yuan_per_kwh == pytest.approx(non_tou)
    assert row.td_energy_yuan_per_kwh == pytest.approx(td)
    assert row.sharp_yuan_per_kwh == pytest.approx(sharp)
    assert row.peak_yuan_per_kwh == pytest.approx(peak)
    assert row.flat_yuan_per_kwh == pytest.approx(flat)
    assert row.valley_yuan_per_kwh == pytest.approx(valley)
    assert row.demand_yuan_per_kw_month == demand
    assert row.capacity_yuan_per_kva_month == capacity


def test_common_components_and_fund_breakdown() -> None:
    """公共分项与政府性基金明细固化；明细三项之和严格等于 0.0452。"""
    assert OFFICIAL_2026_01_COMMON_COMPONENTS == {
        "agent_purchase_price": 0.384470,
        "line_loss_price": 0.013643,
        "government_fund": 0.0452,
        "system_operation": 0.082273,
    }
    assert sum(OFFICIAL_2026_01_FUND_BREAKDOWN.values()) == pytest.approx(0.0452, abs=1e-9)
    assert OFFICIAL_2026_01_FUND_BREAKDOWN == {
        "农网还贷资金": 0.02,
        "大中型水库移民后期扶持资金": 0.0062,
        "可再生能源电价附加": 0.019,
    }
    # 系统运行费明细之和 ≠ 表列合计（官方表内部不一致 0.0043）：以表列合计为计费依据并披露
    assert sum(OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN.values()) == pytest.approx(0.077973, abs=1e-9)
    assert OFFICIAL_2026_01_COMMON_COMPONENTS["system_operation"] - sum(
        OFFICIAL_2026_01_SYSTEM_OPERATION_BREAKDOWN.values()
    ) == pytest.approx(0.0043, abs=1e-9)
    assert any("0.0043" in note for note in OFFICIAL_2026_01_CONSISTENCY_NOTES)


def test_table2_key_values() -> None:
    """表二（代理购电价格表）关键值固化。"""
    assert OFFICIAL_2026_01_TABLE2["代理工商业购电价格_元每千瓦时"] == pytest.approx(0.384470)
    assert OFFICIAL_2026_01_TABLE2["代理工商业购电量规模_万千瓦时"] == pytest.approx(581200.0006)
    assert OFFICIAL_2026_01_TABLE2["采购优先发电电量_万千瓦时"] + OFFICIAL_2026_01_TABLE2[
        "采购市场化发电电量_万千瓦时"
    ] == pytest.approx(581200.0, abs=0.01)
    assert OFFICIAL_2026_01_TABLE2["系统运行费用折合度电水平_元每千瓦时"] == pytest.approx(0.082273)


# --------------------------------------------------------------------------- #
# 分项合计一致性（官方表内部核对）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("row", OFFICIAL_2026_01_PRICE_ROWS, ids=lambda row: f"{row.tariff_structure}-{row.voltage_level}")
def test_component_sum_matches_listed_price(row) -> None:
    """分项合计与表列非分时电度电价一致：两部制**严格相等**，单一制高 0.0002（已披露）。"""
    difference = component_sum_of_row(row) - row.non_tou_yuan_per_kwh
    if row.tariff_structure == "two_part":
        assert difference == pytest.approx(0.0, abs=1e-9)
    else:
        assert difference == pytest.approx(0.0002, abs=1e-9)


def test_implied_multipliers_match_policy_values() -> None:
    """反算浮动系数：尖峰 2.0、平段 1.0、低谷 0.45 完全吻合；高峰 ≈1.49。"""
    for row in OFFICIAL_2026_01_PRICE_ROWS:
        implied = implied_multipliers_of_row(row)
        if row.tariff_structure == "two_part":
            assert implied[TariffPeriod.SHARP_PEAK] == pytest.approx(2.0, abs=1e-5)
            # 表列绝对电价按 6 位小数公布，反算低谷系数与 0.45 相差 < 4e-7
            assert implied[TariffPeriod.VALLEY] == pytest.approx(0.45, abs=1e-5)
        else:
            # 单一制因分项合计高 0.0002，基础电价略小，系数偏差 ≤0.0011
            assert implied[TariffPeriod.SHARP_PEAK] == pytest.approx(2.0, abs=1.1e-3)
            assert implied[TariffPeriod.VALLEY] == pytest.approx(0.45, abs=1.1e-3)
        assert implied[TariffPeriod.PEAK] == pytest.approx(1.49, abs=0.006)


def test_declared_multipliers_and_t10_warning_for_peak() -> None:
    """官方计划的政策声明系数为 2.0/1.5/1.0/0.45；高峰与表列值不一致必须报 T10（而非改数）。"""
    assert OFFICIAL_2026_01_DECLARED_MULTIPLIERS[TariffPeriod.SHARP_PEAK] == 2.00
    assert OFFICIAL_2026_01_DECLARED_MULTIPLIERS[TariffPeriod.VALLEY] == 0.45
    validation = validate_tariff_plan(OFFICIAL_110, as_of=date(2026, 1, 10))
    assert validation.usable_for_formal_use is True
    issue = next(issue for issue in validation.issues if issue.code == "T10")
    assert issue.level == "WARNING" and issue.blocked is False
    assert "高峰时段" in issue.message and "-0.003981" in issue.message
    # 计费依据仍是官方公布的绝对电价，未被改动
    assert effective_period_prices(OFFICIAL_110)[TariffPeriod.PEAK] == pytest.approx(0.809061)


# --------------------------------------------------------------------------- #
# 来源、适用范围、核验状态（§2.3、§4.1、§4.3）
# --------------------------------------------------------------------------- #
def test_source_metadata_is_complete_and_never_fabricated() -> None:
    """官方计划必须带来源名称/URL/文号/抓取日期/核验状态；URL 只在来源字典中维护一处。"""
    assert OFFICIAL_2026_01_SOURCE["url"].startswith("https://")
    assert OFFICIAL_2026_01_SOURCE["document_number"] == "鄂发改价管〔2024〕77 号"
    assert OFFICIAL_2026_01_SOURCE["fetched_at"] == "2026-01-08"
    for plan in OFFICIAL_2026_01:
        assert plan.status is TariffPlanStatus.VERIFIED
        assert plan.source_url == OFFICIAL_2026_01_SOURCE["url"]
        assert plan.source_document_number == "鄂发改价管〔2024〕77 号"
        assert plan.source_fetched_at == date(2026, 1, 8)
        assert plan.verified_at is not None
        assert plan.effective_from == date(2026, 1, 1)
        assert plan.effective_to == date(2026, 1, 31)
        assert plan.is_example is False
        assert plan.applicable_voltage_levels
        assert plan.applicable_tariff_structures
        assert plan.market_mode is MarketMode.UTILITY_AGENT


def test_all_seven_plans_built_with_expected_ids() -> None:
    """7 个电压等级/计费方式组合全部生成，且 ID 唯一。"""
    assert len(OFFICIAL_2026_01) == 7
    ids = [plan.tariff_plan_id for plan in OFFICIAL_2026_01]
    assert len(set(ids)) == 7
    assert official_voltage_levels() == ["不满1千伏", "1-10(20)千伏", "35千伏", "110千伏", "220千伏及以上"]


def test_official_plan_prices_equal_table_rows() -> None:
    """构造出的计划分时电价必须与官方表列绝对电价逐项相等。"""
    prices = effective_period_prices(OFFICIAL_110)
    row = next(
        item
        for item in OFFICIAL_2026_01_PRICE_ROWS
        if item.tariff_structure == "two_part" and item.voltage_level == "110千伏"
    )
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(row.sharp_yuan_per_kwh)
    assert prices[TariffPeriod.PEAK] == pytest.approx(row.peak_yuan_per_kwh)
    assert prices[TariffPeriod.FLAT] == pytest.approx(row.flat_yuan_per_kwh)
    assert prices[TariffPeriod.VALLEY] == pytest.approx(row.valley_yuan_per_kwh)


def test_base_price_definition_matches_components() -> None:
    """官方"基础电价"= 代理购电价 + 线损折价 = 0.398113；固定分项 = 输配 + 系统运行 + 基金。"""
    for plan in OFFICIAL_2026_01:
        assert base_price_yuan_per_kwh(plan) == pytest.approx(0.398113, abs=1e-9)
    assert fixed_price_yuan_per_kwh(OFFICIAL_110) == pytest.approx(
        0.0884 + 0.082273 + 0.0452, abs=1e-9
    )
    assert "扣除政府性基金及附加、电度输配电价和系统运行费折价" in OFFICIAL_110.base_price_definition


def test_only_adjustable_components_float() -> None:
    """只有"代理购电价 + 线损折价"参与浮动，其余分项默认不浮动（§2.3 关键约束）。"""
    adjustable = {
        c.name for c in OFFICIAL_110.price_components if c.included_in_tou_price and c.adjustable_by_tou
    }
    fixed = {
        c.name for c in OFFICIAL_110.price_components if c.included_in_tou_price and not c.adjustable_by_tou
    }
    assert adjustable == {"代理购电价（工商业）", "代理工商业上网环节线损费用折价"}
    assert fixed == {"电度输配电价", "系统运行费折价", "政府性基金及附加"}
    # 明细分项不重复计入电度电价合计
    assert all(not c.included_in_tou_price for c in OFFICIAL_110.price_components if "明细" in c.name)


# --------------------------------------------------------------------------- #
# 时段划分（官方价格表注 2）24 小时闭合
# --------------------------------------------------------------------------- #
def test_official_time_periods_tile_24_hours_for_every_month() -> None:
    """官方时段划分在 12 个月上都严格闭合 24 小时、无缺口、无冲突（§7.7 验收）。"""
    validation = validate_tariff_plan(OFFICIAL_110, as_of=date(2026, 1, 10))
    assert len(validation.coverage) == 12
    for item in validation.coverage:
        assert item.is_complete is True, (item.month, item.gaps, item.overlaps)
        assert item.covered_hours == 24.0
        assert item.period_hours == {
            "SHARP_PEAK": 2.0,
            "PEAK": 6.0,
            "FLAT": 8.0,
            "VALLEY": 8.0,
        }


@pytest.mark.parametrize(
    ("month", "hour", "expected"),
    [
        (11, 18, TariffPeriod.SHARP_PEAK),
        (11, 19, TariffPeriod.SHARP_PEAK),
        (11, 20, TariffPeriod.PEAK),
        (11, 23, TariffPeriod.PEAK),
        (11, 6, TariffPeriod.FLAT),
        (11, 12, TariffPeriod.VALLEY),
        (11, 13, TariffPeriod.VALLEY),
        (7, 20, TariffPeriod.SHARP_PEAK),
        (7, 21, TariffPeriod.SHARP_PEAK),
        (7, 18, TariffPeriod.PEAK),
        (7, 19, TariffPeriod.PEAK),
        (7, 23, TariffPeriod.PEAK),
        (7, 12, TariffPeriod.VALLEY),
    ],
)
def test_official_period_boundaries(month: int, hour: int, expected: TariffPeriod) -> None:
    """官方时段边界逐点验证（含 7、8 月与其他月份的切换）。"""
    assert resolve_slot_period(OFFICIAL_110, month, hour, slot_minutes=60) is expected


def test_month_groups_distinguish_summer_and_others() -> None:
    """月份分组必须区分 7-8 月与其他月份（即使各时段小时数相同）。"""
    grouped = period_hours_of(OFFICIAL_110)
    assert set(grouped) == {"7-8月", "其他月份（1-6、9-12 月）"}


# --------------------------------------------------------------------------- #
# 项目资料版与官方版**并存**（用户明确要求）
# --------------------------------------------------------------------------- #
def test_builtin_plans_keep_both_versions() -> None:
    """内置库必须同时包含官方 2026-01 版（7 个）与项目资料版（1 个）。"""
    plans = builtin_tariff_plans()
    assert len(plans) == 8
    ids = {plan.tariff_plan_id for plan in plans}
    assert "HUBEI_DFL_MATERIAL_TOU_RATIO" in ids
    assert sum(1 for plan in plans if plan.tariff_plan_id.startswith("HUBEI_COMMERCIAL_2026_01")) == 7
    # 返回副本：外部修改不得污染内置定义
    plans[0].name = "被改动"
    assert builtin_tariff_plans()[0].name != "被改动"


def test_material_plan_is_draft_without_guessed_prices() -> None:
    """资料版默认是草稿、不预填任何电价，且正式复算被 T04/T05 阻断（§4.1、§0.2 红线）。"""
    assert DFL_MATERIAL.status is TariffPlanStatus.DRAFT
    assert all(rule.direct_price_yuan_per_kwh is None for rule in DFL_MATERIAL.time_period_rules)
    assert all(rule.price_basis is PriceBasis.MULTIPLIER for rule in DFL_MATERIAL.time_period_rules)
    validation = validate_tariff_plan(DFL_MATERIAL, as_of=date(2025, 11, 1), require_verified=True)
    assert validation.usable_for_formal_use is False
    codes = {issue.code for issue in validation.issues_of("ERROR")}
    assert {"T04", "T05"} <= codes


def test_material_plan_multipliers_verbatim() -> None:
    """资料版浮动系数逐项固化：尖峰 180%、高峰 149%、平段 100%、低谷 48%。"""
    multipliers = {rule.period: rule.price_multiplier for rule in DFL_MATERIAL.time_period_rules}
    assert multipliers[TariffPeriod.SHARP_PEAK] == pytest.approx(1.80)
    assert multipliers[TariffPeriod.PEAK] == pytest.approx(1.49)
    assert multipliers[TariffPeriod.FLAT] == pytest.approx(1.00)
    assert multipliers[TariffPeriod.VALLEY] == pytest.approx(0.48)


def test_material_plan_source_and_period_text() -> None:
    """资料版必须记录出处（项目初步设计文件）与时段原文。"""
    assert "发电量计算(N型组件).xlsx" in DFL_MATERIAL_SOURCE["name"]
    assert "9:00-15:00" in DFL_MATERIAL_TIME_PERIOD_TEXT
    assert "23:00-次日 7:00" in DFL_MATERIAL_TIME_PERIOD_TEXT
    assert "发电量计算(N型组件).xlsx" in (DFL_MATERIAL.source_name or "")
    # 资料未给出文号与链接 → 必须留空并说明，不得编造
    assert DFL_MATERIAL.source_url is None
    assert DFL_MATERIAL.source_document_number is None
    assert "未给出基础电价" in DFL_MATERIAL.base_price_definition


def test_material_plan_with_user_base_price_becomes_computable() -> None:
    """用户核验基础电价后，资料版可算出各时段价格（口径 = 基础电价×系数 + 政府性基金）。"""
    plan = build_dfl_material_plan(base_price_yuan_per_kwh=0.5, government_fund_yuan_per_kwh=0.0452)
    prices = effective_period_prices(plan)
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(0.5 * 1.8 + 0.0452)
    assert prices[TariffPeriod.PEAK] == pytest.approx(0.5 * 1.49 + 0.0452)
    assert prices[TariffPeriod.FLAT] == pytest.approx(0.5 * 1.0 + 0.0452)
    assert prices[TariffPeriod.VALLEY] == pytest.approx(0.5 * 0.48 + 0.0452)
    assert all(rule.direct_price_yuan_per_kwh is not None for rule in plan.time_period_rules)


def test_material_plan_time_periods_tile_24_hours() -> None:
    """资料版时段同样严格闭合 24 小时（2 + 6 + 8 + 8 = 24）。"""
    for month in range(1, 13):
        coverage = validate_tariff_plan(DFL_MATERIAL, as_of=date(2025, month, 1), require_verified=False)
        item = next(entry for entry in coverage.coverage if entry.month == month)
        assert item.is_complete is True, (month, item.gaps)
        assert item.period_hours == {"SHARP_PEAK": 2.0, "PEAK": 6.0, "FLAT": 8.0, "VALLEY": 8.0}


def test_period_division_differs_between_official_and_material() -> None:
    """时段划分逐点差异（业务核心发现）：官方 12:00-14:00 为低谷、资料版为高峰区间内的平段。"""
    # 官方：9:00-12:00 平段，12:00-14:00 低谷，14:00-15:00 平段
    assert resolve_slot_period(OFFICIAL_110, 11, 10, slot_minutes=60) is TariffPeriod.FLAT
    assert resolve_slot_period(OFFICIAL_110, 11, 12, slot_minutes=60) is TariffPeriod.VALLEY
    assert resolve_slot_period(OFFICIAL_110, 11, 14, slot_minutes=60) is TariffPeriod.FLAT
    # 资料版：9:00-15:00 整段高峰
    for hour in (10, 12, 14):
        assert resolve_slot_period(DFL_MATERIAL, 11, hour, slot_minutes=60) is TariffPeriod.PEAK
    # 官方 18:00-20:00 尖峰，资料版 18:00-20:00 落在平段（15:00-20:00）
    assert resolve_slot_period(OFFICIAL_110, 11, 19, slot_minutes=60) is TariffPeriod.SHARP_PEAK
    assert resolve_slot_period(DFL_MATERIAL, 11, 19, slot_minutes=60) is TariffPeriod.FLAT
    # 官方 23:00-24:00 高峰，资料版为低谷
    assert resolve_slot_period(OFFICIAL_110, 11, 23, slot_minutes=60) is TariffPeriod.PEAK
    assert resolve_slot_period(DFL_MATERIAL, 11, 23, slot_minutes=60) is TariffPeriod.VALLEY
    # 官方 06:00-07:00 平段，资料版为低谷
    assert resolve_slot_period(OFFICIAL_110, 11, 6, slot_minutes=60) is TariffPeriod.FLAT
    assert resolve_slot_period(DFL_MATERIAL, 11, 6, slot_minutes=60) is TariffPeriod.VALLEY


def test_comparison_quantifies_every_difference() -> None:
    """两套计划逐项差异：时段、浮动系数（尖峰 200% vs 180%、低谷 45% vs 48%）都列出。"""
    comparison = compare_tariff_plans(OFFICIAL_110, DFL_MATERIAL)
    assert comparison.same_time_periods is False
    assert comparison.same_float_multipliers is False
    items = {item.item: item for item in comparison.items}
    assert "尖峰时段" in items and "高峰时段" in items and "平段时段" in items and "谷段时段" in items
    sharp = items["尖峰浮动系数"]
    assert "200%" in sharp.value_a and "180%" in sharp.value_b
    assert "+20 个百分点" in sharp.difference
    valley = items["谷段浮动系数"]
    assert "45%" in valley.value_a and "48%" in valley.value_b
    assert "-3 个百分点" in valley.difference
    peak = items["高峰浮动系数"]
    assert "150%" in peak.value_a and "149%" in peak.value_b
    # 容需量电价一方缺失 → 也必须作为差异列出
    assert any(item.category == "容需量电价" for item in comparison.items)
    assert any("并存" in message for message in comparison.messages)


def test_notes_document_evidence_level() -> None:
    """差异结论必须标注证据等级（资料版为原始文件；"旧版政策"只有线索级证据）。"""
    text = "\n".join(DFL_MATERIAL_VS_OFFICIAL_NOTES)
    assert "未能确证" in text
    assert "线索级" in text
    assert "并存" in text
    assert "0.45" in text


def test_official_plan_for_retail_market_notes_product_scope() -> None:
    """官方表是代理购电产品：市场化直购用户使用时必须在使用说明中提示适用范围（§4.1 第 6 条）。"""
    plan = build_official_plan(
        OFFICIAL_2026_01_PRICE_ROWS[-1], market_mode=MarketMode.RETAIL_MARKET
    )
    assert plan.market_mode is MarketMode.RETAIL_MARKET
    assert "代理购电" in plan.notes and "逐时" in plan.notes
