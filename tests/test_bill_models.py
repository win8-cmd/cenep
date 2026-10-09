"""账单模型与枚举测试（V2.1 §2.1、§3.1、§5.5、§8.2）。

覆盖：字段与单位契约、账期/月份一致性、跨月账期、分时时段枚举（深谷 ≠ 低谷）、
账单标识与重复键、容差默认值、账单随项目保存/重开、旧项目照常打开，以及阶段 1 的红线自检。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError as PydanticValidationError

from cenep.domain.bill_models import (
    BILL_ENERGY_FIELDS,
    BILL_ENERGY_SUB_CHARGE_FIELDS,
    BILL_FIELD_LABELS,
    BILL_LEVEL1_CHARGE_FIELDS,
    ENERGY_PERIOD_FIELDS,
    MONTH_FORMAT,
    SIGNED_CHARGE_FIELDS,
    BillTolerance,
    ElectricityBill,
    billing_month_of,
    duplicate_key_of,
    make_bill_id,
)
from cenep.domain.enums import (
    BillEnergyPeriod,
    BillQualityStatus,
    BillSourceType,
    DuplicateStrategy,
    TariffPeriod,
    TariffStructure,
)
from cenep.infrastructure.migration import (
    BILL_SECTION_SCHEMA_VERSION,
    CURRENT_SCHEMA_VERSION,
    ensure_bill_section,
)
from cenep.infrastructure.project_file import FILE_FORMAT, load_project, save_project

#: 规格书 §2.1 列出的字段清单（逐字对照，防止实现漏字段或改含义）
SPEC_FIELDS: tuple[str, ...] = (
    "bill_id",
    "project_id",
    "billing_period_start",
    "billing_period_end",
    "billing_month",
    "meter_id",
    "customer_name",
    "voltage_level",
    "tariff_structure",
    "contract_capacity_kva",
    "billing_demand_kw",
    "energy_total_kwh",
    "energy_peak_kwh",
    "energy_sharp_kwh",
    "energy_flat_kwh",
    "energy_valley_kwh",
    "energy_offpeak_kwh",
    "energy_charge_yuan",
    "market_purchase_charge_yuan",
    "transmission_distribution_charge_yuan",
    "line_loss_charge_yuan",
    "system_operation_charge_yuan",
    "government_fund_charge_yuan",
    "basic_capacity_charge_yuan",
    "demand_charge_yuan",
    "power_factor_adjustment_yuan",
    "other_charge_yuan",
    "vat_yuan",
    "bill_total_yuan",
    "source_type",
    "source_file_name",
    "source_row_number",
    "notes",
    "created_at",
    "updated_at",
    "quality_status",
    "quality_messages",
)

#: 金额/电量字段（都要有中文名 + 单位，§0.2）
MEASURE_FIELDS: tuple[str, ...] = (
    *BILL_ENERGY_FIELDS,
    *BILL_LEVEL1_CHARGE_FIELDS,
    *BILL_ENERGY_SUB_CHARGE_FIELDS,
    "contract_capacity_kva",
    "billing_demand_kw",
)


def _bill(**kwargs) -> ElectricityBill:
    """构造一条默认合法的账单（2026-01 月）。"""
    base: dict = {
        "bill_id": "B1",
        "project_id": "P1",
        "billing_period_start": date(2026, 1, 1),
        "billing_period_end": date(2026, 1, 31),
    }
    base.update(kwargs)
    return ElectricityBill(**base)


class TestFieldContract:
    def test_all_spec_fields_present(self):
        """§2.1 字段清单必须一条不缺（字段名可按代码风格微调，此处保持与规格一致）。"""
        missing = [name for name in SPEC_FIELDS if name not in ElectricityBill.model_fields]
        assert missing == [], f"缺少规格书 §2.1 字段：{missing}"

    def test_adjustment_field_allows_sign(self):
        """§2.1：电费返还/补退费用独立字段且允许正负。"""
        assert "adjustment_charge_yuan" in ElectricityBill.model_fields
        assert "adjustment_charge_yuan" in SIGNED_CHARGE_FIELDS
        bill = _bill(adjustment_charge_yuan=-120.5)
        assert bill.adjustment_charge_yuan == pytest.approx(-120.5)

    def test_measure_fields_have_chinese_label_with_unit(self):
        """§0.2：字段名带单位或文档明确 —— 中文名里必须出现单位。"""
        for name in MEASURE_FIELDS:
            label = BILL_FIELD_LABELS.get(name, "")
            assert label, f"{name} 缺少中文名"
            assert any(unit in label for unit in ("kWh", "kW", "kVA", "元")), f"{name} 的中文名缺少单位：{label}"

    def test_amounts_default_to_none_not_zero(self):
        """§2.1：账单未提供的分项必须是 None，不得自动填 0。"""
        bill = _bill()
        for name in (*BILL_LEVEL1_CHARGE_FIELDS, *BILL_ENERGY_SUB_CHARGE_FIELDS, "bill_total_yuan"):
            assert getattr(bill, name) is None, f"{name} 默认值应为 None"
        for name in BILL_ENERGY_FIELDS:
            assert getattr(bill, name) is None, f"{name} 默认值应为 None"

    def test_enum_literal_values_match_spec(self):
        """§2.1：三个 Literal 的取值必须与规格书一致。"""
        assert {m.value for m in TariffStructure} == {"single_part", "two_part", "unknown"}
        assert {m.value for m in BillSourceType} == {"manual", "excel", "estimated"}
        assert {m.value for m in BillQualityStatus} == {"valid", "warning", "invalid"}

    def test_timestamps_are_datetime(self):
        bill = _bill()
        assert bill.created_at is not None and bill.updated_at is not None
        before = bill.updated_at
        bill.touch()
        assert bill.updated_at >= before

    def test_quality_defaults(self):
        bill = _bill()
        assert bill.quality_status is BillQualityStatus.VALID
        assert bill.quality_messages == []

    def test_describe_marks_missing_values(self):
        text = _bill().describe()
        assert "未提供" in text
        assert "2026-01" in text


class TestMonthAndPeriodConsistency:
    def test_billing_month_derived_from_start(self):
        """§2.1：billing_month 格式 YYYY-MM；留空时按账期起始日推导。"""
        assert _bill().billing_month == "2026-01"
        assert billing_month_of(date(2026, 12, 5)) == "2026-12"
        assert MONTH_FORMAT == "%Y-%m"

    def test_explicit_consistent_month_kept(self):
        bill = _bill(billing_month="2026-01")
        assert bill.billing_month == "2026-01"

    def test_month_period_mismatch_rejected(self):
        """§2.1：月份和起止日期必须匹配（中文报错）。"""
        with pytest.raises(PydanticValidationError) as exc:
            _bill(billing_month="2026-02")
        assert "账单月份" in str(exc.value)
        assert "不一致" in str(exc.value)

    def test_end_before_start_rejected(self):
        with pytest.raises(PydanticValidationError) as exc:
            _bill(billing_period_start=date(2026, 2, 1), billing_period_end=date(2026, 1, 31))
        assert "不能早于" in str(exc.value)

    def test_single_day_period(self):
        bill = _bill(billing_period_start=date(2026, 3, 5), billing_period_end=date(2026, 3, 5))
        assert bill.period_days == 1
        assert bill.is_cross_month is False

    def test_cross_month_period_allowed_and_flagged(self):
        """§2.1：跨月账单允许，但必须能被识别为跨月，并归属到起始月。"""
        bill = _bill(billing_period_start=date(2026, 1, 26), billing_period_end=date(2026, 2, 25))
        assert bill.is_cross_month is True
        assert bill.billing_month == "2026-01"
        assert bill.period_days == 31

    def test_same_month_different_year_is_not_cross_month(self):
        bill = _bill(billing_period_start=date(2026, 1, 1), billing_period_end=date(2027, 1, 31))
        assert bill.is_cross_month is False  # 同月份号但跨年——由 period_days/年度覆盖检查发现
        assert bill.period_days > 365


class TestEnergyPeriodEnum:
    def test_labels(self):
        assert BillEnergyPeriod.SHARP.label == "尖峰"
        assert BillEnergyPeriod.PEAK.label == "高峰"
        assert BillEnergyPeriod.FLAT.label == "平段"
        assert BillEnergyPeriod.VALLEY.label == "低谷"
        assert BillEnergyPeriod.OFFPEAK.label == "深谷"

    def test_offpeak_is_not_valley(self):
        """§2.1：flat 是平段，因此 offpeak 不得被误解为低谷 —— 深谷与低谷必须分开。"""
        assert BillEnergyPeriod.OFFPEAK.label == "深谷"
        assert "低谷" not in BillEnergyPeriod.OFFPEAK.label
        assert BillEnergyPeriod.OFFPEAK is not BillEnergyPeriod.VALLEY
        assert BillEnergyPeriod.OFFPEAK.field_name == "energy_offpeak_kwh"
        assert BillEnergyPeriod.VALLEY.field_name == "energy_valley_kwh"

    def test_field_names_exist_on_model(self):
        for period in BillEnergyPeriod:
            assert period.field_name in ElectricityBill.model_fields

    def test_all_fields_covered_once(self):
        fields = [ENERGY_PERIOD_FIELDS[p] for p in BillEnergyPeriod]
        assert len(fields) == len(set(fields)) == 5
        for name in fields:
            assert name in BILL_ENERGY_FIELDS

    def test_maps_to_policy_tariff_period(self):
        """账单时段必须与政策侧时段枚举同一套语义（§0.2：时段规则与电价数值分离）。"""
        assert BillEnergyPeriod.SHARP.tariff_period is TariffPeriod.SHARP_PEAK
        assert BillEnergyPeriod.PEAK.tariff_period is TariffPeriod.PEAK
        assert BillEnergyPeriod.FLAT.tariff_period is TariffPeriod.FLAT
        assert BillEnergyPeriod.VALLEY.tariff_period is TariffPeriod.VALLEY
        assert BillEnergyPeriod.OFFPEAK.tariff_period is TariffPeriod.DEEP_VALLEY


class TestIdentityAndDuplicateKey:
    def test_make_bill_id_deterministic(self):
        first = make_bill_id(date(2026, 1, 1), date(2026, 1, 31), "M001")
        second = make_bill_id(date(2026, 1, 1), date(2026, 1, 31), "M001")
        assert first == second == "BILL-20260101-20260131-M001"

    def test_make_bill_id_without_meter(self):
        assert make_bill_id(date(2026, 1, 1), date(2026, 1, 31)).endswith("-MAIN")

    def test_make_bill_id_sanitizes_meter(self):
        generated = make_bill_id(date(2026, 1, 1), date(2026, 1, 31), "表/号 #1")
        assert "/" not in generated and "#" not in generated and " " not in generated

    def test_duplicate_key_combines_project_period_meter(self):
        """§5.5：重复判定 = 项目 + 账期 + 计量点。"""
        key = duplicate_key_of("P1", date(2026, 1, 1), date(2026, 1, 31), "M1")
        assert key == "P1|2026-01-01|2026-01-31|M1"
        assert _bill(meter_id="M1").duplicate_key == "P1|2026-01-01|2026-01-31|M1"

    @pytest.mark.parametrize(
        "change",
        [
            {"project_id": "P2"},
            {"billing_period_start": date(2026, 2, 1), "billing_period_end": date(2026, 2, 28)},
            {"meter_id": "M2"},
        ],
    )
    def test_duplicate_key_changes_with_identity(self, change):
        base = _bill(meter_id="M1").duplicate_key
        args = {"meter_id": "M1", **change}
        assert _bill(**args).duplicate_key != base

    def test_duplicate_key_ignores_amounts(self):
        """金额不同仍是同一条账单（同账期同计量点），否则重复检测会被绕过。"""
        first = _bill(meter_id="M1", bill_total_yuan=100.0).duplicate_key
        second = _bill(meter_id="M1", bill_total_yuan=999.0).duplicate_key
        assert first == second


class TestTolerance:
    def test_default_matches_spec_formula(self):
        """§2.1：默认容差 max(1, 0.5% × 总量)。"""
        tol = BillTolerance()
        assert tol.energy_tolerance(10_000.0) == pytest.approx(50.0)   # 0.5% × 10000
        assert tol.energy_tolerance(100.0) == pytest.approx(1.0)       # 绝对值兜底
        assert tol.amount_tolerance(100_000.0) == pytest.approx(500.0)
        assert tol.amount_tolerance(20.0) == pytest.approx(1.0)

    def test_unknown_total_uses_absolute(self):
        tol = BillTolerance()
        assert tol.energy_tolerance(None) == pytest.approx(1.0)
        assert tol.amount_tolerance(None) == pytest.approx(1.0)

    def test_configurable(self):
        tol = BillTolerance(energy_relative=0.05, amount_absolute_yuan=10.0)
        assert tol.energy_tolerance(1000.0) == pytest.approx(50.0)
        assert tol.amount_tolerance(0.0) == pytest.approx(10.0)


class TestBillsPersistence:
    def test_project_starts_without_bills(self, golden_pv):
        assert golden_pv.bills == []

    def test_save_and_reopen_preserves_bills(self, golden_pv, tmp_path):
        """§5.5 验收：保存、关闭、重开项目后账单数据一致。"""
        golden_pv.bills = [
            _bill(
                bill_id="B1",
                project_id=golden_pv.basic_info.project_name,
                energy_total_kwh=100_000.0,
                energy_sharp_kwh=10_000.0,
                energy_peak_kwh=30_000.0,
                energy_flat_kwh=40_000.0,
                energy_valley_kwh=20_000.0,
                energy_charge_yuan=65_000.0,
                bill_total_yuan=65_000.0,
                meter_id="M001",
                voltage_level="10kV",
                tariff_structure=TariffStructure.SINGLE_PART,
                source_type=BillSourceType.EXCEL,
                source_file_name="账单.xlsx",
                source_row_number=2,
                notes="首月",
            )
        ]
        path = save_project(golden_pv, tmp_path / "含账单")
        reopened = load_project(path)

        assert len(reopened.bills) == 1
        assert reopened.bills[0].model_dump() == golden_pv.bills[0].model_dump()

    def test_envelope_carries_bill_section_version(self, golden_pv, tmp_path):
        """§8.2：新增数据必须有 schema/version 版本标识。"""
        path = save_project(golden_pv, tmp_path / "信封")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["bills_schema_version"] == BILL_SECTION_SCHEMA_VERSION
        assert envelope["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_old_v2_project_without_bills_opens_with_empty_list(self, golden_pv, tmp_path):
        """§8.2：旧项目缺少账单段时正常打开，显示空状态，不生成虚构数据。"""
        payload = json.loads(golden_pv.model_dump_json())
        payload.pop("bills", None)
        path = tmp_path / "旧项目.nep"
        path.write_text(
            json.dumps(
                {
                    "format": FILE_FORMAT,
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "app_version": "2.0.0",
                    "saved_at": "2026-01-01T00:00:00",
                    "project": payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = load_project(path)
        assert loaded.bills == []
        assert any("bills" in note for note in loaded.migration_notes)

    def test_v1_project_opens_with_empty_bills(self, golden_pv, tmp_path):
        """§9.1 回归：V1 项目文件照常打开，账单为空且既有参数不变。"""
        payload = json.loads(golden_pv.model_dump_json())
        for key in ("timeseries", "migration_notes", "bills"):
            payload.pop(key, None)
        payload["schema_version"] = "1.0"
        path = tmp_path / "v1.nep"
        path.write_text(
            json.dumps(
                {
                    "format": FILE_FORMAT,
                    "schema_version": "1.0",
                    "app_version": "1.0.0",
                    "saved_at": "2026-01-01T00:00:00",
                    "project": payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = load_project(path)
        assert loaded.bills == []
        assert loaded.pv.pv_capacity_kwp == golden_pv.pv.pv_capacity_kwp
        assert loaded.load.annual_load_kwh == golden_pv.load.annual_load_kwh
        assert loaded.tariff.peak_price == golden_pv.tariff.peak_price

    def test_ensure_bill_section_is_idempotent(self):
        payload = {"schema_version": "2.0"}
        once, notes = ensure_bill_section(payload)
        assert notes and once["bills"] == []
        # V2.4 §8.2：补齐函数对**单个段**也是幂等的（第二次调用不再补、不再写留痕）
        twice, again = ensure_bill_section(once)
        assert again == []
        assert twice["bills"] == []
        assert twice["migration_notes"] == once["migration_notes"]

    def test_ensure_bill_section_never_touches_existing_bills(self):
        payload = {"bills": [{"bill_id": "X"}]}
        out, notes = ensure_bill_section(payload)
        assert notes == []
        assert out["bills"] == [{"bill_id": "X"}]


class TestStage1RedLines:
    """阶段 1 红线自检（§0.2）。"""

    def test_bill_model_has_no_unit_price_fields(self):
        """账单事实模型不得含"单价"字段：分时单价属于 V2.3 的电价计划（§2.3）。"""
        for name in ElectricityBill.model_fields:
            assert "price" not in name, f"账单模型不应有电价字段：{name}"

    def test_bill_model_holds_no_simulation_results(self):
        """§0.2：账单事实与账单模拟结果必须分开保存。"""
        forbidden = (
            "baseline_bill_yuan",
            "scenario_bill_yuan",
            "bill_saving_yuan",
            "bill_saving_rate",
            "reconciliation_difference_yuan",
            "calculation_warnings",
        )
        for name in forbidden:
            assert name not in ElectricityBill.model_fields, f"账单事实模型混入了模拟结果字段：{name}"

    def test_billing_demand_is_a_separate_concept_from_curve_peak(self):
        """§2.1：账单计费需量与负荷曲线最大值不得混用（不同字段、不同来源）。"""
        assert "billing_demand_kw" in ElectricityBill.model_fields
        for name in ("peak_demand_kw", "max_power_kw", "curve_peak_kw"):
            assert name not in ElectricityBill.model_fields

    def test_hubei_policy_template_still_has_no_prices(self):
        """阶段 1 不核验电价：湖北模板的数值必须仍为 None（阶段 5 才做）。"""
        from cenep.policy.hubei import HUBEI_TEMPLATE

        for name in (
            "market_price",
            "mechanism_price",
            "mechanism_volume_ratio",
            "green_energy_price",
            "green_environmental_value",
            "policy_version",
            "effective_date",
        ):
            assert getattr(HUBEI_TEMPLATE, name) is None, f"阶段 1 不得预填政策数值：{name}"

    def test_duplicate_strategy_covers_spec_choices(self):
        """§5.5：重复处理必须支持 跳过 / 替换 / 保留。"""
        assert {m.value for m in DuplicateStrategy} == {"skip", "replace", "keep_both"}
