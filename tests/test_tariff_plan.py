"""V2.3 阶段 5：版本化电价计划模型与时段引擎测试（规格书 §2.3、§4.2、§7.7）。

覆盖：
* ``HH:MM`` 解析、跨午夜、``24:00`` 边界；
* 月份范围与"7-8 月与其他月份不同"的分月规则；
* 时段覆盖完整 / 缺口 / 冲突（重叠）的中文报错；
* ``price_multiplier`` 与 ``direct_price_yuan_per_kwh`` 的价格来源口径（§2.3 关键约束）；
* 校验规则编号（T01~T13）与"能否用于正式复算"的阻断语义（§4.2、§7.4）；
* 按计费日期 + 适用范围选取电价版本（§2.3、§4.1 第 6、7 条）；
* 两套计划并存时的逐项差异（§4.2、§7.3）。
"""

from __future__ import annotations

from datetime import date

import pytest

from cenep.calculation.tariff_plan_engine import (
    base_price_yuan_per_kwh,
    compare_tariff_plans,
    compose_period_price,
    effective_period_prices,
    fixed_price_yuan_per_kwh,
    hourly_price_series,
    implied_multiplier_from_prices,
    period_hours_of,
    require_formal_use,
    resolve_slot_period,
    select_tariff_plan,
    validate_tariff_plan,
)
from cenep.calculation.errors import ValidationError
from cenep.domain.enums import (
    DemandBillingMode,
    MarketMode,
    PriceBasis,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
)
from cenep.domain.tariff_models import (
    MINUTES_PER_DAY,
    TariffPlan,
    TariffPriceComponent,
    TariffTimePeriodRule,
    format_hhmm,
    parse_hhmm,
)

# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _component(
    name: str,
    value: float,
    *,
    adjustable: bool,
    kind: TariffComponentType = TariffComponentType.OTHER,
) -> TariffPriceComponent:
    return TariffPriceComponent(
        component_type=kind,
        name=name,
        unit=TariffComponentUnit.YUAN_PER_KWH,
        value=value,
        included_in_tou_price=True,
        adjustable_by_tou=adjustable,
    )


def _plan(
    rules: list[TariffTimePeriodRule],
    *,
    plan_id: str = "TEST_PLAN",
    status: TariffPlanStatus = TariffPlanStatus.VERIFIED,
    components: list[TariffPriceComponent] | None = None,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = date(2026, 1, 31),
    voltage: str | None = "110千伏",
    structure: str | None = "two_part",
) -> TariffPlan:
    return TariffPlan(
        tariff_plan_id=plan_id,
        name=f"测试电价计划 {plan_id}",
        province="湖北",
        effective_from=effective_from,
        effective_to=effective_to,
        source_name="单元测试构造，非真实电价",
        source_document_number="测试文号〔2026〕0 号",
        source_fetched_at=date(2026, 1, 1),
        status=status,
        applicable_voltage_levels=[voltage] if voltage else [],
        applicable_tariff_structures=[structure] if structure else [],
        market_mode=MarketMode.UTILITY_AGENT,
        time_period_rules=rules,
        price_components=components
        if components is not None
        else [
            _component("基础电价分项", 0.4, adjustable=True),
            _component("固定分项", 0.2, adjustable=False),
        ],
        capacity_charge_yuan_per_kva_month=26.3,
        demand_charge_yuan_per_kw_month=39.0,
        demand_billing_mode=DemandBillingMode.DEMAND,
        base_price_definition="测试口径：基础电价 = 参与浮动的分项合计",
        notes="仅用于测试",
    )


def _full_day_rules(
    *, sharp_start: str = "18:00", sharp_end: str = "20:00", priority: int = 0
) -> list[TariffTimePeriodRule]:
    """一套 24 小时严格闭合的时段规则（不含 7、8 月差异）。"""
    return [
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            start_time=sharp_start,
            end_time=sharp_end,
            direct_price_yuan_per_kwh=1.0,
            price_multiplier=2.0,
            priority=priority + 3,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK,
            start_time="16:00",
            end_time="18:00",
            direct_price_yuan_per_kwh=0.8,
            price_multiplier=1.5,
            priority=priority + 2,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK,
            start_time="20:00",
            end_time="24:00",
            direct_price_yuan_per_kwh=0.8,
            price_multiplier=1.5,
            priority=priority + 2,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT,
            start_time="06:00",
            end_time="12:00",
            direct_price_yuan_per_kwh=0.6,
            price_multiplier=1.0,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT,
            start_time="14:00",
            end_time="16:00",
            direct_price_yuan_per_kwh=0.6,
            price_multiplier=1.0,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY,
            start_time="00:00",
            end_time="06:00",
            direct_price_yuan_per_kwh=0.38,
            price_multiplier=0.45,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY,
            start_time="12:00",
            end_time="14:00",
            direct_price_yuan_per_kwh=0.38,
            price_multiplier=0.45,
        ),
    ]


# --------------------------------------------------------------------------- #
# HH:MM 解析（§4.2）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [("00:00", 0), ("06:30", 390), ("23:59", 1439), ("24:00", MINUTES_PER_DAY)],
)
def test_parse_hhmm_valid(text: str, expected: int) -> None:
    """合法时刻解析为"当日第几分钟"。"""
    assert parse_hhmm(text) == expected
    assert format_hhmm(expected) == text


@pytest.mark.parametrize("text", ["", "8", "08-30", "25:00", "12:60", "24:30", "a:b"])
def test_parse_hhmm_invalid_raises_chinese(text: str) -> None:
    """非法时刻必须是**中文**报错，且说明正确格式。"""
    with pytest.raises(ValueError) as excinfo:
        parse_hhmm(text)
    message = str(excinfo.value)
    assert "HH:MM" in message or "范围" in message or "不合法" in message


def test_rule_zero_length_rejected() -> None:
    """起止相同的零长度时段必须被拒绝（中文）。"""
    with pytest.raises(ValueError) as excinfo:
        TariffTimePeriodRule(period=TariffPeriod.FLAT, start_time="08:00", end_time="08:00")
    assert "时长为 0" in str(excinfo.value)


def test_rule_bad_month_rejected() -> None:
    """月份超范围必须被拒绝（中文）。"""
    with pytest.raises(ValueError) as excinfo:
        TariffTimePeriodRule(period=TariffPeriod.FLAT, months=[13], start_time="08:00", end_time="09:00")
    assert "1~12" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 跨午夜与月份规则（§4.2）
# --------------------------------------------------------------------------- #
def test_cross_midnight_rule_covers() -> None:
    """跨午夜时段（23:00-次日 07:00）必须正确覆盖 23、0、6 点，但不覆盖 7 点。"""
    rule = TariffTimePeriodRule(period=TariffPeriod.VALLEY, start_time="23:00", end_time="07:00")
    assert rule.crosses_midnight is True
    assert rule.duration_hours == 8.0
    for hour in (23, 0, 3, 6):
        assert rule.covers_minute(hour * 60) is True
    for hour in (7, 12, 22):
        assert rule.covers_minute(hour * 60) is False


def test_end_24_00_is_not_cross_midnight() -> None:
    """``22:00-24:00`` 不应被判为跨午夜，长度为 2 小时。"""
    rule = TariffTimePeriodRule(period=TariffPeriod.PEAK, start_time="22:00", end_time="24:00")
    assert rule.crosses_midnight is False
    assert rule.duration_hours == 2.0
    assert rule.covers_minute(23 * 60) is True


def test_month_specific_rules_switch_by_month() -> None:
    """同一计划中"7-8 月"与"其他月份"的尖峰时刻必须按月切换（§4.2）。"""
    rules = [
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            months=[7, 8],
            start_time="20:00",
            end_time="22:00",
            direct_price_yuan_per_kwh=1.0,
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            months=[1, 2, 3, 4, 5, 6, 9, 10, 11, 12],
            start_time="18:00",
            end_time="20:00",
            direct_price_yuan_per_kwh=1.0,
        ),
    ]
    plan = _plan(rules)
    assert resolve_slot_period(plan, 7, 19, slot_minutes=60) is None or resolve_slot_period(plan, 7, 19, slot_minutes=60) is not TariffPeriod.SHARP_PEAK
    assert resolve_slot_period(plan, 7, 20, slot_minutes=60) is TariffPeriod.SHARP_PEAK
    assert resolve_slot_period(plan, 11, 19, slot_minutes=60) is TariffPeriod.SHARP_PEAK
    assert resolve_slot_period(plan, 11, 20, slot_minutes=60) is not TariffPeriod.SHARP_PEAK


# --------------------------------------------------------------------------- #
# 覆盖与冲突检测（§4.2、§7.7）
# --------------------------------------------------------------------------- #
def test_full_day_coverage_no_gap_no_conflict() -> None:
    """24 小时严格闭合的计划：每月覆盖 24 小时，无缺口无冲突。"""
    plan = _plan(_full_day_rules())
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert validation.usable_for_formal_use is True
    for item in validation.coverage:
        assert item.is_complete is True, item.messages
        assert item.covered_hours == 24.0
        assert item.gap_hours == 0.0
    assert validation.issues_of("ERROR") == []


def test_missing_hours_reported_in_chinese() -> None:
    """缺少时段覆盖（12:00-14:00 未定义）必须给出**中文**缺口报告并阻断正式复算。"""
    rules = [rule for rule in _full_day_rules() if not (rule.start_time == "12:00")]
    plan = _plan(rules)
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert validation.usable_for_formal_use is False
    errors = validation.issues_of("ERROR")
    assert any(issue.code == "T02" for issue in errors)
    message = next(issue.message for issue in errors if issue.code == "T02")
    assert "12:00-14:00" in message and "缺" in message
    assert validation.coverage[0].gaps == ["12:00-14:00（2 小时）"]


def test_overlapping_periods_reported_in_chinese() -> None:
    """同时段冲突（同一小时落在两条不同时段规则上）必须给出**中文**报告并阻断。"""
    rules = list(_full_day_rules())
    # 追加一条与 18:00-20:00 重叠的"高峰"规则 → 18:00-20:00 同时是尖峰与高峰
    rules.append(
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK,
            start_time="17:00",
            end_time="19:00",
            direct_price_yuan_per_kwh=0.8,
            priority=1,
        )
    )
    plan = _plan(rules)
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert validation.usable_for_formal_use is False
    codes = {issue.code for issue in validation.issues_of("ERROR")}
    assert "T03" in codes
    message = next(issue.message for issue in validation.issues_of("ERROR") if issue.code == "T03")
    assert "同时段冲突" in message
    assert validation.coverage[0].conflict_slots > 0


def test_priority_and_last_declared_wins() -> None:
    """优先级大者优先；相同优先级时后声明者覆盖（与既有 tariff_series 语义一致）。"""
    plan = _plan(
        [
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT, start_time="08:00", end_time="09:00", priority=0
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.PEAK, start_time="08:00", end_time="09:00", priority=5
            ),
        ]
    )
    assert resolve_slot_period(plan, 3, 8, slot_minutes=60) is TariffPeriod.PEAK

    same_priority = _plan(
        [
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT, start_time="08:00", end_time="09:00", priority=1
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.PEAK, start_time="08:00", end_time="09:00", priority=1
            ),
        ]
    )
    assert resolve_slot_period(same_priority, 3, 8, slot_minutes=60) is TariffPeriod.PEAK


def test_uncovered_hour_is_none_not_flat() -> None:
    """未定义时段必须返回 ``None``，**不得**兜底成平段（账单复算是正式计费口径）。"""
    plan = _plan(
        [TariffTimePeriodRule(period=TariffPeriod.FLAT, start_time="08:00", end_time="09:00")]
    )
    assert resolve_slot_period(plan, 3, 8, slot_minutes=60) is TariffPeriod.FLAT
    assert resolve_slot_period(plan, 3, 20, slot_minutes=60) is None


# --------------------------------------------------------------------------- #
# 价格口径（§2.3 关键约束）
# --------------------------------------------------------------------------- #
def test_base_and_fixed_price_split() -> None:
    """基础电价 = Σ 参与浮动分项；固定分项 = Σ 不参与浮动分项。"""
    plan = _plan(
        _full_day_rules(),
        components=[
            _component("代理购电价", 0.384470, adjustable=True, kind=TariffComponentType.MARKET_ENERGY),
            _component("线损折价", 0.013643, adjustable=True, kind=TariffComponentType.LINE_LOSS),
            _component("输配电价", 0.0884, adjustable=False, kind=TariffComponentType.TRANSMISSION_DISTRIBUTION),
            _component("基金", 0.0452, adjustable=False, kind=TariffComponentType.GOVERNMENT_FUND),
        ],
    )
    assert base_price_yuan_per_kwh(plan) == pytest.approx(0.398113, abs=1e-9)
    assert fixed_price_yuan_per_kwh(plan) == pytest.approx(0.1336, abs=1e-9)


def test_missing_base_components_returns_none_not_zero() -> None:
    """没有任何可用分项时基础电价返回 ``None``，**绝不返回 0**（避免造出 0 元电价）。"""
    plan = _plan(_full_day_rules(), components=[])
    assert base_price_yuan_per_kwh(plan) is None
    assert fixed_price_yuan_per_kwh(plan) is None


def test_direct_price_is_authoritative_when_both_given() -> None:
    """同时给出直接单价与浮动系数时，按 ``price_basis`` 取直接单价；系数只作参考。"""
    plan = _plan(_full_day_rules())
    prices = effective_period_prices(plan)
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(1.0)
    assert prices[TariffPeriod.FLAT] == pytest.approx(0.6)
    # 系数口径反算：0.4×2.0+0.2 = 1.0，恰好与直接单价一致 → 不应报 T10
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert validation.issues_of("WARNING") == [] or all(
        issue.code != "T10" for issue in validation.issues
    )


def test_multiplier_basis_derives_price_from_components() -> None:
    """``price_basis=multiplier`` 时，价格由 基础电价 × 浮动系数 + 固定分项 推导。"""
    rules = [
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            start_time="00:00",
            end_time="24:00",
            price_multiplier=2.0,
            price_basis=PriceBasis.MULTIPLIER,
        )
    ]
    plan = _plan(rules)
    prices = effective_period_prices(plan)
    assert prices[TariffPeriod.SHARP_PEAK] == pytest.approx(compose_period_price(
        base_price_yuan_per_kwh=0.4, price_multiplier=2.0, fixed_price_yuan_per_kwh=0.2
    ))


def test_price_consistency_issue_reported_not_silently_changed() -> None:
    """直接单价与浮动系数口径不一致时必须报 T10，且**不改动**直接单价。"""
    rules = [
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK,
            start_time="00:00",
            end_time="24:00",
            direct_price_yuan_per_kwh=0.85,  # 反算应为 0.4×1.5+0.2 = 0.8
            price_multiplier=1.5,
        )
    ]
    plan = _plan(rules)
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert any(issue.code == "T10" for issue in validation.issues)
    issue = next(issue for issue in validation.issues if issue.code == "T10")
    assert issue.level == "WARNING" and issue.blocked is False
    assert "直接单价" in issue.message and "0.850000" in issue.message
    # 直接单价保持不变（不得为对上公式而改官方数值）
    assert effective_period_prices(plan)[TariffPeriod.PEAK] == pytest.approx(0.85)


def test_implied_multiplier_helper() -> None:
    """反算浮动系数：``(时段价 − 固定分项) / (平段价 − 固定分项)``。"""
    assert implied_multiplier_from_prices(
        period_price=1.0, flat_price=0.6, fixed_price=0.2
    ) == pytest.approx(2.0)
    assert implied_multiplier_from_prices(
        period_price=0.3, flat_price=0.6, fixed_price=0.2
    ) == pytest.approx(0.25)
    assert implied_multiplier_from_prices(period_price=1.0, flat_price=0.2, fixed_price=0.2) is None


# --------------------------------------------------------------------------- #
# 校验规则与阻断语义（§4.2、§7.4、§7.7）
# --------------------------------------------------------------------------- #
def test_no_rules_blocks_formal_use() -> None:
    """没有任何时段规则 → T01 阻断。"""
    plan = _plan([])
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert {issue.code for issue in validation.issues_of("ERROR")} >= {"T01"}
    assert validation.usable_for_formal_use is False


def test_missing_price_blocks_formal_use() -> None:
    """某时段缺价格 → T04 阻断，并提示"仅允许保存草稿"。"""
    rules = _full_day_rules()
    rules[0] = rules[0].model_copy(
        update={"direct_price_yuan_per_kwh": None, "price_multiplier": None}
    )
    plan = _plan(rules)
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    issue = next(issue for issue in validation.issues_of("ERROR") if issue.code == "T04")
    assert "缺少价格" in issue.message and "草稿" in issue.message
    assert validation.usable_for_formal_use is False


def test_draft_plan_blocked_and_draft_savable() -> None:
    """未核验计划：``require_verified=True`` 阻断、``False`` 仅提示（草稿可保存）。"""
    plan = _plan(_full_day_rules(), status=TariffPlanStatus.DRAFT)
    strict = validate_tariff_plan(plan, as_of=date(2026, 1, 10), require_verified=True)
    assert strict.usable_for_formal_use is False
    assert any(issue.code == "T05" and issue.level == "ERROR" for issue in strict.issues)
    loose = validate_tariff_plan(plan, as_of=date(2026, 1, 10), require_verified=False)
    assert loose.usable_for_formal_use is True
    assert any(issue.code == "T05" and issue.level == "INFO" for issue in loose.issues)


def test_expired_plan_blocked_with_chinese_reason() -> None:
    """超出失效日期 → T06 阻断，并说明"已过期"。"""
    plan = _plan(_full_day_rules(), effective_to=date(2026, 1, 31))
    validation = validate_tariff_plan(plan, as_of=date(2026, 3, 1))
    issue = next(issue for issue in validation.issues_of("ERROR") if issue.code == "T06")
    assert "已过期" in issue.message
    assert validation.usable_for_formal_use is False


def test_before_effective_date_blocked() -> None:
    """基准日早于生效日期 → T06 阻断。"""
    plan = _plan(_full_day_rules(), effective_from=date(2026, 1, 1))
    validation = validate_tariff_plan(plan, as_of=date(2025, 12, 1))
    assert any(issue.code == "T06" for issue in validation.issues_of("ERROR"))


def test_missing_source_and_scope_are_warnings() -> None:
    """缺来源 / 缺适用范围是 WARNING（不阻断），但必须提示。"""
    plan = TariffPlan(
        tariff_plan_id="NO_SOURCE",
        name="无来源计划",
        province="湖北",
        effective_from=date(2026, 1, 1),
        status=TariffPlanStatus.DRAFT,
        applicable_voltage_levels=[],
        applicable_tariff_structures=[],
        time_period_rules=_full_day_rules(),
        price_components=[
            _component("基础", 0.4, adjustable=True),
            _component("固定", 0.2, adjustable=False),
        ],
    )
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10), require_verified=False)
    codes = {issue.code for issue in validation.issues_of("WARNING")}
    assert {"T07", "T08"} <= codes
    assert validation.usable_for_formal_use is True


def test_two_part_without_prices_warns() -> None:
    """两部制但未提供容量/需量电价 → T11 WARNING。"""
    plan = _plan(_full_day_rules()).model_copy(
        update={
            "capacity_charge_yuan_per_kva_month": None,
            "demand_charge_yuan_per_kw_month": None,
        }
    )
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    assert any(issue.code == "T11" for issue in validation.issues_of("WARNING"))


def test_example_plan_warns_never_formal() -> None:
    """标记为示例数据 → T12 WARNING（不得作为真实电价用于正式结论）。"""
    plan = _plan(_full_day_rules()).model_copy(update={"is_example": True})
    validation = validate_tariff_plan(plan, as_of=date(2026, 1, 10))
    issue = next(issue for issue in validation.issues if issue.code == "T12")
    assert issue.level == "WARNING" and "示例数据" in issue.message


def test_verified_plan_requires_source_and_scope() -> None:
    """``status=VERIFIED`` 的计划必须带来源与适用范围，否则模型层拒绝。"""
    with pytest.raises(ValueError) as excinfo:
        TariffPlan(
            tariff_plan_id="BAD",
            name="缺来源",
            province="湖北",
            effective_from=date(2026, 1, 1),
            status=TariffPlanStatus.VERIFIED,
            applicable_voltage_levels=[],
            applicable_tariff_structures=[],
        )
    assert "已核验" in str(excinfo.value) and "来源" in str(excinfo.value)


def test_require_formal_use_raises_chinese() -> None:
    """不允许用未核验/缺价计划跑正式复算：统一入口抛**中文**异常。"""
    plan = _plan(_full_day_rules(), status=TariffPlanStatus.DRAFT)
    with pytest.raises(ValidationError) as excinfo:
        require_formal_use(plan, as_of=date(2026, 1, 10))
    assert "不能用于正式账单复算" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 版本选取（§2.3、§4.1）
# --------------------------------------------------------------------------- #
def test_select_tariff_plan_by_date_and_scope() -> None:
    """按计费日期 + 电压等级 + 计费方式选取版本；日期不覆盖时抛中文异常。"""
    january = _plan(_full_day_rules(), plan_id="P1", effective_from=date(2026, 1, 1), effective_to=date(2026, 1, 31))
    december = _plan(_full_day_rules(), plan_id="P2", effective_from=date(2026, 12, 1), effective_to=date(2026, 12, 31))
    chosen = select_tariff_plan([january, december], date(2026, 1, 15), voltage_level="110千伏", tariff_structure="two_part")
    assert chosen.tariff_plan_id == "P1"

    with pytest.raises(ValidationError) as excinfo:
        select_tariff_plan([january], date(2026, 7, 1))
    assert "没有覆盖" in str(excinfo.value) and "按日期选取版本" in str(excinfo.value)


def test_select_prefers_verified_over_draft() -> None:
    """同一天有多个版本时优先取"已核验"，其次取生效日期最新者。"""
    draft = _plan(_full_day_rules(), plan_id="DRAFT", status=TariffPlanStatus.DRAFT)
    verified = _plan(_full_day_rules(), plan_id="OK", status=TariffPlanStatus.VERIFIED)
    chosen = select_tariff_plan([draft, verified], date(2026, 1, 15))
    assert chosen.tariff_plan_id == "OK"


def test_select_respects_voltage_level_scope() -> None:
    """计划声明了电压等级时，电压不匹配的版本不得被选中（§4.1 第 6 条）。"""
    plan_110 = _plan(_full_day_rules(), plan_id="KV110", voltage="110千伏")
    plan_10 = _plan(_full_day_rules(), plan_id="KV10", voltage="1-10(20)千伏")
    chosen = select_tariff_plan([plan_110, plan_10], date(2026, 1, 15), voltage_level="110千伏")
    assert chosen.tariff_plan_id == "KV110"


# --------------------------------------------------------------------------- #
# 逐时电价与月份分组（§4.2）
# --------------------------------------------------------------------------- #
def test_hourly_price_series_length_and_none() -> None:
    """逐小时电价序列长度正确，未定义时段返回 ``None``（不得当 0）。"""
    plan = _plan(_full_day_rules())
    series = hourly_price_series(plan, 11, slot_minutes=60)
    assert len(series) == 24
    assert series[19] == pytest.approx(1.0)  # 19:00-20:00 为尖峰
    assert series[6] == pytest.approx(0.6)

    partial = _plan(
        [
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT,
                start_time="08:00",
                end_time="09:00",
                direct_price_yuan_per_kwh=0.6,
            )
        ]
    )
    series = hourly_price_series(partial, 11, slot_minutes=60)
    assert series[8] == pytest.approx(0.6)
    assert series[20] is None


def test_period_hours_grouping_by_month() -> None:
    """月份分组必须区分"7-8 月"与"其他月份"（即使小时数相同）。"""
    rules = _full_day_rules()
    rules.append(
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK,
            months=[7, 8],
            start_time="20:00",
            end_time="22:00",
            direct_price_yuan_per_kwh=1.1,
            priority=9,
        )
    )
    monthly_rules = []
    for rule in rules:
        if rule.period is TariffPeriod.SHARP_PEAK and not rule.months:
            monthly_rules.append(rule.model_copy(update={"months": [1, 2, 3, 4, 5, 6, 9, 10, 11, 12]}))
        else:
            monthly_rules.append(rule)
    plan = _plan(monthly_rules)
    grouped = period_hours_of(plan)
    keys = set(grouped)
    assert "7-8月" in keys
    assert any("其他月份" in key for key in keys)


# --------------------------------------------------------------------------- #
# 两套计划并存与逐项差异（§4.2、§7.3）
# --------------------------------------------------------------------------- #
def test_compare_plans_lists_every_difference() -> None:
    """两套计划并存时逐项列出差异：时段、浮动系数、电度电价、容需量、来源。"""
    plan_a = _plan(_full_day_rules())
    plan_b = _plan(
        [
            TariffTimePeriodRule(
                period=TariffPeriod.SHARP_PEAK,
                start_time="20:00",
                end_time="22:00",
                direct_price_yuan_per_kwh=0.9,
                price_multiplier=1.8,
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.PEAK,
                start_time="09:00",
                end_time="15:00",
                direct_price_yuan_per_kwh=0.8,
                price_multiplier=1.49,
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT,
                start_time="07:00",
                end_time="09:00",
                direct_price_yuan_per_kwh=0.6,
                price_multiplier=1.0,
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT,
                start_time="15:00",
                end_time="20:00",
                direct_price_yuan_per_kwh=0.6,
                price_multiplier=1.0,
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.FLAT,
                start_time="22:00",
                end_time="23:00",
                direct_price_yuan_per_kwh=0.6,
                price_multiplier=1.0,
            ),
            TariffTimePeriodRule(
                period=TariffPeriod.VALLEY,
                start_time="23:00",
                end_time="07:00",
                direct_price_yuan_per_kwh=0.3,
                price_multiplier=0.48,
            ),
        ],
        plan_id="TEST_PLAN_B",
    )
    comparison = compare_tariff_plans(plan_a, plan_b)
    assert comparison.has_difference is True
    assert comparison.same_time_periods is False
    assert comparison.same_float_multipliers is False
    categories = {item.category for item in comparison.items}
    assert {"时段", "浮动系数", "电度电价"} <= categories
    # 低谷浮动系数差异必须给出百分点方向
    valley = next(item for item in comparison.items if item.item == "谷段浮动系数")
    assert "45%" in valley.value_a and "48%" in valley.value_b
    assert "3 个百分点" in valley.difference
    assert "并存" in "".join(comparison.messages)


def test_compare_identical_plans_has_no_difference() -> None:
    """完全相同（含 ID 之外的字段）的两套计划应无差异项。"""
    plan = _plan(_full_day_rules())
    comparison = compare_tariff_plans(plan, plan)
    assert comparison.same_time_periods is True
    assert comparison.same_float_multipliers is True
