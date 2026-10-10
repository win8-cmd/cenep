"""V2.5 账单**数据分类口径**的落地验收测试（需求方已逐条确认）。

本文件把"口径"变成**可执行的硬约束**，覆盖六件事：

1. **运营费用不重复计入**：``operation_fee_detail``（第 5 页 B/C 类，样本总合计 −295,720.67 元）
   只留档——把它取任意值（含 None / 正负 / 极大极小），电费与电价计算的**数值结果逐位不变**；
2. **无功电量不入模型**：账单模型里**没有**任何无功相关字段；
3. **丢弃字段不入模型**：没有 示数 / 倍率 / 抄见 / 变损 / 加减 相关字段（线损**费用**分项是
   ①类必需字段，与电量明细里的"线损（电量）"不是一回事，**保留**）；
4. **逐时电价优先**：``hourly_energy_tariff`` 存在时电价取值来自它，而**不是**平均电价或
   政府峰谷系数；市场化直购客户企图用政府峰谷系数时抛**中文**错误；
5. **``meter_groups`` 已入库**：账单模型有该字段且有值，导入时**不再报「无法识别」**，
   同时"真正未知的结构化字段"仍会被中文提示（没有把校验删掉）；
6. **版本号**：``__version__ == "2.5.0"``，且 ``schema_version`` /
   ``CALCULATION_ENGINE_VERSION`` / ``BILL_SECTION_SCHEMA_VERSION`` **未动**（规格书 §0.2）。

真实账单属于业务资料，**只读**且不入库；找不到资料时相关用例自动跳过（不报错）。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from cenep import __version__
from cenep.calculation.bill_calculator import (
    annual_bill_summary,
    apply_quality,
    monthly_summary,
    reconcile_bill,
)
from cenep.calculation.bill_price_source import (
    CUSTOMER_KIND_AGENCY_PURCHASE,
    CUSTOMER_KIND_MARKET_DIRECT,
    PRICE_PRIORITY,
    PRICE_SOURCE_BILL_AVERAGE,
    PRICE_SOURCE_GOV_TOU_FACTOR,
    PRICE_SOURCE_HOURLY_TABLE,
    BillEnergyPriceResolution,
    hourly_avoided_prices,
    resolve_bill_energy_price,
)
from cenep.calculation.bill_recalculator import recompute_bill
from cenep.calculation.errors import ValidationError
from cenep.data.bill_importer import (
    BILL_COLUMNS,
    ColumnMapping,
    build_bill_from_row,
    preview_bill_import,
)
from cenep.data.bill_pdf_importer import parse_bill_pdf_full
from cenep.data.quality import score_bill_quality
from cenep.domain.bill_models import (
    BILL_FIELD_LABELS,
    HOURLY_PRICE_HOURS,
    ElectricityBill,
    HourlyEnergyPricePoint,
    MeterGroupDetail,
    OperationFeeDetail,
    OperationFeeItem,
)
from cenep.domain.enums import (
    BillSourceType,
    DemandBillingMode,
    TariffComponentType,
    TariffComponentUnit,
    TariffPeriod,
    TariffPlanStatus,
    TariffStructure,
)
from cenep.domain.tariff_models import TariffPlan, TariffPriceComponent, TariffTimePeriodRule
from cenep.infrastructure.migration import (
    BILL_SECTION_SCHEMA_VERSION,
    CALCULATION_ENGINE_VERSION,
)
from cenep.infrastructure.project_file import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 真实账单资料定位（只读；找不到就跳过，绝不修改资料）
# --------------------------------------------------------------------------- #
_BILL_DIR = Path(
    r"C:\Users\Administrator\Desktop\参考资料\东风本田\东风本田汽车有限公司(第三工厂)"
    r"\东风本田第三工厂分布式光伏项目——初步设计2.26"
    r"\东风本田第三工厂分布式光伏项目——初步设计2.26\收资\25年电费"
)
_SAMPLE_BILL = _BILL_DIR / "东本三厂10月账单.pdf"

#: 样本账单（2025-10）第 5 页运营费用总合计（账单原件事实）
SAMPLE_OPERATION_TOTAL_YUAN = -295_720.67


def _require_sample() -> Path:
    if not _SAMPLE_BILL.exists():
        pytest.skip(f"未找到真实样本账单（只读资料）：{_SAMPLE_BILL}")
    return _SAMPLE_BILL


# --------------------------------------------------------------------------- #
# 通用工具：模型 → **数值叶子**（"逐位不变"断言的比较对象）
# --------------------------------------------------------------------------- #
def _numeric_leaves(
    payload: object,
    prefix: str = "",
    *,
    skip: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """递归收集载荷里的**数值 / 布尔 / None 叶子**，路径为键。

    为什么要这样比较：口径要求的是"**计算结果的数值逐位不变**"，而中文说明文本
    （``messages`` / ``assumptions`` / ``notes``）本来就会随口径披露而变化——
    它们不是计算结果。本函数天然只保留数字与三态值，字符串一律不进结果，
    因此用它做 ``==`` 比较就是最严格的"逐位不变"断言。
    """
    out: dict[str, object] = {}
    if isinstance(payload, dict):
        for key, item in payload.items():
            if key in skip:
                continue
            out.update(_numeric_leaves(item, f"{prefix}.{key}", skip=skip))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            out.update(_numeric_leaves(item, f"{prefix}[{index}]", skip=skip))
    elif isinstance(payload, (bool, int, float)) or payload is None:
        out[prefix] = payload
    return out


def _as_payload(item: object) -> object:
    """把 pydantic 模型 / dataclass 统一成可递归遍历的载荷。"""
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return dataclasses.asdict(item)  # type: ignore[arg-type]
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")  # type: ignore[attr-defined]
    return item


def _numeric_dump(model: object, *, skip: frozenset[str] = frozenset()) -> dict[str, object]:
    """Pydantic 模型 / dataclass / 模型列表的数值叶子。"""
    if isinstance(model, (list, tuple)):
        return _numeric_leaves([_as_payload(item) for item in model], skip=skip)
    return _numeric_leaves(_as_payload(model), skip=skip)


def _operation_fee(total: float) -> OperationFeeDetail:
    """构造一份自洽的运营费用明细（B/C 小计与条目之和一致，否则模型层会中文报错）。"""
    b_items = [OperationFeeItem(name="B1偏差考核电费", amount_yuan=10_000.0)]
    c_items = [OperationFeeItem(name="C4发电侧超额获利回收电费", amount_yuan=-(10_000.0 + total))]
    return OperationFeeDetail(
        total_yuan=total,
        b_increase_total_yuan=10_000.0,
        b_increase_items=b_items,
        c_decrease_total_yuan=-(10_000.0 + total),
        c_decrease_items=c_items,
    )


def _base_bill(**overrides: Any) -> ElectricityBill:
    """可手算账单：分时电量 100/200/300/400（合计 1000 kWh），总额 4450 元。"""
    payload: dict[str, Any] = {
        "bill_id": "BILL-CALIBER",
        "project_id": "口径测试项目",
        "billing_period_start": date(2026, 3, 1),
        "billing_period_end": date(2026, 3, 31),
        "voltage_level": "110千伏",
        "tariff_structure": TariffStructure.TWO_PART,
        "energy_total_kwh": 1000.0,
        "energy_sharp_kwh": 100.0,
        "energy_peak_kwh": 200.0,
        "energy_flat_kwh": 300.0,
        "energy_valley_kwh": 400.0,
        "energy_charge_yuan": 600.0,
        "demand_charge_yuan": 3900.0,
        "billing_demand_kw": 100.0,
        "contract_capacity_kva": 100.0,
        "power_factor_adjustment_yuan": -50.0,
        "bill_total_yuan": 4450.0,
        "source_type": BillSourceType.EXCEL,
        "source_file_name": "口径测试账单.xlsx",
        "source_row_number": 2,
        "meter_groups": [
            MeterGroupDetail(
                meters=["4230000100027673777"],
                tariff="用电户-110千伏-工商业两部制",
                energy={"total": 700.0, "sharp": 100.0, "peak": 200.0, "flat": 300.0, "valley": 100.0},
            ),
            MeterGroupDetail(
                meters=["定比0.03"],
                tariff="用电户-35千伏-非居民照明-工商业单一制",
                is_ratio_submeter=True,
                parent_meters=["4230000100027673777"],
                energy={"total": 300.0, "valley": 300.0},
            ),
        ],
    }
    payload.update(overrides)
    return ElectricityBill(**payload)


def _hourly_points(
    *, energy: float = 100.0, direct: float = 0.30, loss: float = 0.02
) -> list[HourlyEnergyPricePoint]:
    """24 小时电价表：逐时电量与价格都相同（便于手算加权价 = direct + loss）。"""
    return [
        HourlyEnergyPricePoint(
            hour=hour,
            energy_kwh=energy,
            direct_trade_price_yuan_per_kwh=direct,
            line_loss_price_yuan_per_kwh=loss,
        )
        for hour in HOURLY_PRICE_HOURS
    ]


def _verified_plan() -> TariffPlan:
    """24 小时闭合、四时段价格可手算的测试电价计划（用于账单复算）。"""
    prices = {
        TariffPeriod.SHARP_PEAK: 1.00,
        TariffPeriod.PEAK: 0.80,
        TariffPeriod.FLAT: 0.60,
        TariffPeriod.VALLEY: 0.38,
    }
    rules = [
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY, start_time="00:00", end_time="06:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.VALLEY],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT, start_time="06:00", end_time="12:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.FLAT],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.VALLEY, start_time="12:00", end_time="14:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.VALLEY],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.FLAT, start_time="14:00", end_time="16:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.FLAT],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK, start_time="16:00", end_time="18:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.PEAK],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.SHARP_PEAK, start_time="18:00", end_time="20:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.SHARP_PEAK],
        ),
        TariffTimePeriodRule(
            period=TariffPeriod.PEAK, start_time="20:00", end_time="24:00",
            direct_price_yuan_per_kwh=prices[TariffPeriod.PEAK],
        ),
    ]
    return TariffPlan(
        tariff_plan_id="TEST_CALIBER",
        name="口径测试电价计划",
        province="湖北",
        # 覆盖 2025-01 ~ 2026-12：既覆盖手算账单（2026-03），也覆盖真实样本账单（2025-10）
        effective_from=date(2025, 1, 1),
        effective_to=date(2026, 12, 31),
        source_name="单元测试构造",
        source_document_number="测试〔2026〕0 号",
        source_fetched_at=date(2026, 1, 1),
        status=TariffPlanStatus.VERIFIED,
        applicable_voltage_levels=["110千伏"],
        applicable_tariff_structures=["two_part"],
        time_period_rules=rules,
        price_components=[
            TariffPriceComponent(
                component_type=TariffComponentType.MARKET_ENERGY,
                name="测试基础分项",
                unit=TariffComponentUnit.YUAN_PER_KWH,
                value=0.40,
                adjustable_by_tou=True,
            ),
        ],
        capacity_charge_yuan_per_kva_month=26.3,
        demand_charge_yuan_per_kw_month=39.0,
        demand_billing_mode=DemandBillingMode.DEMAND,
        base_price_definition="测试口径",
        notes="仅用于测试",
    )


# --------------------------------------------------------------------------- #
# 1. 运营费用不重复计入（V2.5 §5 ④：只留档，绝不进入电价或费用的计算口径）
# --------------------------------------------------------------------------- #
class TestOperationFeeNeverCounted:
    """把运营费用取任意值，电费/电价计算的**数值结果必须逐位不变**。"""

    #: 覆盖：未提供、0、正、负、样本真值、极大极小
    _VALUES: tuple[float | None, ...] = (
        None,
        0.0,
        18_513.53,
        -314_234.20,
        SAMPLE_OPERATION_TOTAL_YUAN,
        -10_000_000.0,
        10_000_000.0,
    )

    def _snapshot(self, bill: ElectricityBill) -> dict[str, object]:
        """一张账单上**全部**电费/电价计算的数值结果（不含中文说明文本）。

        ``skip={"operation_fee_detail"}``：被跳过的只有"运营费用这个事实本身"
        （它当然会随取值变化）——**其余每一个数字都必须逐位不变**。
        """
        plan = _verified_plan()
        skip = frozenset({"operation_fee_detail"})
        data: dict[str, object] = {}
        data.update({f"bill.{k}": v for k, v in _numeric_dump(bill, skip=skip).items()})
        data.update({f"reconcile.{k}": v for k, v in _numeric_dump(reconcile_bill(bill)).items()})
        data.update({f"quality.{k}": v for k, v in _numeric_dump(apply_quality(bill), skip=skip).items()})
        data.update({f"score.{k}": v for k, v in _numeric_dump(score_bill_quality(bill)).items()})
        data.update({f"monthly.{k}": v for k, v in _numeric_dump(monthly_summary([bill])).items()})
        data.update({f"annual.{k}": v for k, v in _numeric_dump(annual_bill_summary([bill], year=2026)).items()})
        data.update({f"recompute.{k}": v for k, v in _numeric_dump(
            recompute_bill(bill, plan)
        ).items()})
        data.update({f"price.{k}": v for k, v in _numeric_dump(
            resolve_bill_energy_price(bill)
        ).items()})
        return data

    def test_operation_fee_value_never_changes_any_number(self):
        """核心断言：7 种取值的数值快照**完全相等**（逐位）。"""
        baseline = self._snapshot(_base_bill(operation_fee_detail=None))
        assert baseline, "数值快照不应为空，否则断言毫无意义"
        for value in self._VALUES:
            if value is None:
                continue
            candidate = self._snapshot(_base_bill(operation_fee_detail=_operation_fee(value)))
            assert candidate == baseline, (
                f"运营费用取 {value} 元时电费/电价计算发生了变化："
                f"{ {k: (baseline.get(k), candidate.get(k)) for k in set(baseline) | set(candidate) if baseline.get(k) != candidate.get(k)} }"
            )

    def test_operation_fee_is_absent_from_every_charge_component(self):
        """运营费用不得出现在任何"费用分项"清单里（否则会被求和进总额）。"""
        from cenep.domain.bill_models import (
            BILL_ENERGY_SUB_CHARGE_FIELDS,
            BILL_LEVEL1_CHARGE_FIELDS,
            BILL_PERIOD_CHARGE_FIELDS,
        )

        for fields in (
            BILL_LEVEL1_CHARGE_FIELDS,
            BILL_ENERGY_SUB_CHARGE_FIELDS,
            BILL_PERIOD_CHARGE_FIELDS,
        ):
            assert "operation_fee_detail" not in fields
            # 注意：``system_operation_charge_yuan``（系统运行费）是①类必需分项，
            # 名字里含 "operation" 但不是运营费用；这里只针对运营费用本身的命名。
            assert not any(name.startswith("operation") for name in fields)
            assert not any("operation_fee" in name for name in fields)

        bill = _base_bill(operation_fee_detail=_operation_fee(SAMPLE_OPERATION_TOTAL_YUAN))
        used = set(reconcile_bill(bill).amount_components_used)
        assert not any(name.startswith("operation") for name in used), used

    def test_operation_fee_totals_are_not_added_to_any_sum(self):
        """把运营费用从 −1000 万改成 +1000 万，ΔC 与复算合计必须一模一样。"""
        low = reconcile_bill(_base_bill(operation_fee_detail=_operation_fee(-10_000_000.0)))
        high = reconcile_bill(_base_bill(operation_fee_detail=_operation_fee(10_000_000.0)))
        assert low.amount_component_sum_yuan == high.amount_component_sum_yuan
        assert low.amount_difference_yuan == high.amount_difference_yuan
        assert low.energy_difference_kwh == high.energy_difference_kwh
        plan = _verified_plan()
        assert (
            recompute_bill(_base_bill(operation_fee_detail=_operation_fee(-10_000_000.0)), plan)
            .recomputed_subtotal_yuan
            == recompute_bill(_base_bill(operation_fee_detail=_operation_fee(10_000_000.0)), plan)
            .recomputed_subtotal_yuan
        )

    def test_operation_fee_is_only_disclosed_in_chinese(self):
        """只留档：账单保存该事实，并由中文说明披露"只留档、重复计入即重复计算"。"""
        bill = _base_bill(operation_fee_detail=_operation_fee(SAMPLE_OPERATION_TOTAL_YUAN))
        assert bill.operation_fee_detail is not None
        outcome = reconcile_bill(bill)
        text = "".join(outcome.messages) + "".join(outcome.assumptions)
        assert "只留档" in text
        assert "重复计算" in text
        assert "已包含在账单总电费里" in text

    def test_real_sample_bill_numbers_identical_regardless_of_operation_fee(self):
        """真实账单（运营费用 −295,720.67 元）同样必须逐位不变。"""
        bill = preview_bill_import(_require_sample()).rows[0].bill
        assert bill.operation_fee_detail is not None
        assert bill.operation_fee_detail.total_yuan == pytest.approx(SAMPLE_OPERATION_TOTAL_YUAN)
        without = bill.model_copy(update={"operation_fee_detail": None})
        zeroed = bill.model_copy(update={"operation_fee_detail": _operation_fee(0.0)})
        huge = bill.model_copy(update={"operation_fee_detail": _operation_fee(9_999_999.0)})
        plan = _verified_plan()
        base = _numeric_dump(recompute_bill(without, plan))
        for other in (zeroed, huge):
            assert _numeric_dump(recompute_bill(other, plan)) == base
            assert _numeric_dump(reconcile_bill(other)) == _numeric_dump(reconcile_bill(without))
            assert (
                resolve_bill_energy_price(other).avoided_price_yuan_per_kwh
                == resolve_bill_energy_price(without).avoided_price_yuan_per_kwh
            )


# --------------------------------------------------------------------------- #
# 2. 无功电量不入模型（V2.5 §5 ③）
# --------------------------------------------------------------------------- #
class TestReactiveEnergyNotModeled:
    """正向无功电量已确认丢弃：账单模型里不得有任何无功相关字段。"""

    _FORBIDDEN = ("无功", "reactive", "varh", "kvar", "q_kwh")

    def test_no_reactive_field_on_bill_model(self):
        names = list(ElectricityBill.model_fields)
        for name in names:
            lowered = name.lower()
            assert "无功" not in name, name
            assert not any(token in lowered for token in self._FORBIDDEN), name

    def test_no_reactive_label_in_field_dictionary(self):
        for field, label in BILL_FIELD_LABELS.items():
            assert "无功" not in field
            assert "无功" not in label, (field, label)
            assert "reactive" not in field.lower()

    def test_no_reactive_field_on_structured_fact_models(self):
        for model in (MeterGroupDetail, HourlyEnergyPricePoint, OperationFeeDetail):
            for name in model.model_fields:
                assert "无功" not in name
                assert "reactive" not in name.lower(), name

    def test_bill_columns_have_no_reactive_column(self):
        headers = " ".join(column.header for column in BILL_COLUMNS)
        assert "无功" not in headers
        assert "正向无功" not in headers

    def test_reactive_energy_is_not_in_parsed_payload(self):
        """真实账单第 2~3 页确实印着"正向无功"，但解析结果里**不得**出现该字段。"""
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        assert set(extras) <= {"hourly_energy_tariff", "operation_fee_detail", "meter_groups"}
        for group in extras.get("meter_groups") or []:
            assert "无功" not in json.dumps(group["energy"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 3. 丢弃字段不入模型（V2.5 §5 ③）
# --------------------------------------------------------------------------- #
class TestDiscardedFieldsNotModeled:
    """示数 / 倍率 / 抄见 / 变损 / 加减 是中间过程量，只保留最终"计费电量"。"""

    _DISCARDED = ("示数", "倍率", "抄见", "变损", "加减")

    def test_no_discarded_field_on_bill_model(self):
        for name in ElectricityBill.model_fields:
            assert not any(token in name for token in self._DISCARDED), name

    def test_no_discarded_label_in_field_dictionary(self):
        for field, label in BILL_FIELD_LABELS.items():
            for token in self._DISCARDED:
                assert token not in label, (field, label, token)

    def test_no_discarded_column_in_template(self):
        headers = " ".join(column.header for column in BILL_COLUMNS)
        for token in self._DISCARDED:
            assert token not in headers, token

    def test_meter_group_energy_keeps_only_billed_energy(self):
        """分组明细只保留示数类型（计费电量口径），不得混入示数 / 倍率 / 抄见 / 变损 / 加减 / 无功。"""
        allowed = {"total", "sharp", "peak", "flat", "valley", "offpeak"}
        _row, _notes, extras = parse_bill_pdf_full(_require_sample())
        groups = extras.get("meter_groups") or []
        assert groups, "样本账单必须有计量分组"
        for group in groups:
            assert set(group["energy"]) <= allowed, group["energy"]
            assert set(group) == {
                "meters",
                "tariff",
                "is_ratio_submeter",
                "parent_meters",
                "energy",
            }, sorted(group)
            for token in (*self._DISCARDED, "无功"):
                assert token not in json.dumps(group, ensure_ascii=False), (token, group)

    def test_line_loss_charge_field_is_kept(self):
        """口径例外必须写清：**上网环节线损费用**是①类必需的费用分项，**未丢弃**。"""
        assert "line_loss_charge_yuan" in ElectricityBill.model_fields
        assert "line_loss_charge_yuan" in BILL_FIELD_LABELS
        assert "线损" in BILL_FIELD_LABELS["line_loss_charge_yuan"]

    def test_meter_group_rejects_middle_process_quantities_in_chinese(self):
        """模型层拦截：中间过程量（示数/倍率/抄见/变损/加减）不得塞进计费电量字典。"""
        for key in ("示数", "倍率", "抄见电量", "变损", "线损", "加减", "无功"):
            with pytest.raises(Exception) as exc:
                MeterGroupDetail(meters=["M1"], energy={key: 1.0})
            message = str(exc.value)
            assert "示数类型" in message, (key, message)
            assert "中间过程量" in message, (key, message)
            assert f"「{key}」" in message, (key, message)

    def test_meter_group_accepts_only_declared_energy_tags(self):
        group = MeterGroupDetail(
            meters=["M1"],
            energy={"total": 100.0, "sharp": 10.0, "offpeak": 20.0},
        )
        assert group.total_kwh == pytest.approx(100.0)
        assert group.kind_label == "电能表"
        ratio = MeterGroupDetail(meters=["定比0.03"], is_ratio_submeter=True, energy={})
        assert ratio.kind_label == "定比分表"

    def test_bill_keeps_required_caliber_one_fields(self):
        """①类字段必须齐全（账期 / 分时电量 / 逐时电价 / 电压等级与计费方式 / 需量 / 对账锚点）。"""
        required = {
            "billing_period_start",
            "billing_period_end",
            "energy_sharp_kwh",
            "energy_peak_kwh",
            "energy_flat_kwh",
            "energy_valley_kwh",
            "hourly_energy_tariff",
            "voltage_level",
            "tariff_structure",
            "billing_demand_kw",
            "demand_rate_yuan_per_kw_month",
            "energy_total_kwh",
            "bill_total_yuan",
        }
        assert required <= set(ElectricityBill.model_fields)


# --------------------------------------------------------------------------- #
# 4. 逐时电价优先（V2.5 §5 ⑤）
# --------------------------------------------------------------------------- #
class TestHourlyPricePriority:
    """``hourly_energy_tariff`` 存在时，电价必须来自它——不是平均电价，也不是政府峰谷系数。"""

    _GOV_TOU = {
        TariffPeriod.SHARP_PEAK: 2.00,
        TariffPeriod.PEAK: 1.50,
        TariffPeriod.FLAT: 1.00,
        TariffPeriod.VALLEY: 0.45,
    }

    def test_hourly_table_wins_over_average_and_gov_factors(self):
        # 账单平均电价 = 600 / 1000 = 0.60 元/kWh；逐时交易价格 = 0.30 元/kWh
        bill = _base_bill(hourly_energy_tariff=_hourly_points(direct=0.30, loss=0.02))
        # 即便客户类型是"代理购电"（本可使用系数）且显式给了系数，逐时电价仍必须优先
        outcome = resolve_bill_energy_price(
            bill,
            customer_kind=CUSTOMER_KIND_AGENCY_PURCHASE,
            gov_tou_prices=self._GOV_TOU,
        )
        assert outcome.source == PRICE_SOURCE_HOURLY_TABLE
        assert outcome.used_hourly_table is True
        # 默认口径 = 逐时**直接交易价格**（需求方确认的"真实替代电价"）
        assert outcome.avoided_price_yuan_per_kwh == pytest.approx(0.30)
        # 明确不等于平均电价、也不等于任何政府峰谷系数电价
        assert outcome.bill_average_price_yuan_per_kwh == pytest.approx(0.60)
        assert outcome.avoided_price_yuan_per_kwh != pytest.approx(0.60)
        for price in self._GOV_TOU.values():
            assert outcome.avoided_price_yuan_per_kwh != pytest.approx(price)
        assert outcome.gov_tou_prices_yuan_per_kwh == {}

    def test_priority_order_is_written_down_in_code(self):
        assert PRICE_PRIORITY == (
            PRICE_SOURCE_HOURLY_TABLE,
            PRICE_SOURCE_BILL_AVERAGE,
            PRICE_SOURCE_GOV_TOU_FACTOR,
        )
        assert "仅适用于代理购电客户" in PRICE_SOURCE_GOV_TOU_FACTOR

    def test_hourly_price_comes_from_the_table_hour_by_hour(self):
        points = [
            HourlyEnergyPricePoint(
                hour=hour,
                energy_kwh=100.0,
                direct_trade_price_yuan_per_kwh=0.5 if hour <= 9 else 0.25,
                line_loss_price_yuan_per_kwh=0.01,
            )
            for hour in HOURLY_PRICE_HOURS
        ]
        outcome = resolve_bill_energy_price(_base_bill(hourly_energy_tariff=points))
        assert outcome.hours == HOURLY_PRICE_HOURS
        assert outcome.price_at_hour(1) == pytest.approx(0.50)
        assert outcome.price_at_hour(9) == pytest.approx(0.50)
        assert outcome.price_at_hour(10) == pytest.approx(0.25)
        assert outcome.price_at_hour(24) == pytest.approx(0.25)
        with pytest.raises(ValidationError) as exc:
            outcome.price_at_hour(25)
        assert "1~24" in exc.value.message

    def test_include_line_loss_switch_is_honoured(self):
        """默认 = 逐时直接交易价格；显式开启才叠加"上网环节线损价格"（全额成本口径）。"""
        bill = _base_bill(hourly_energy_tariff=_hourly_points(direct=0.30, loss=0.02))
        default = resolve_bill_energy_price(bill)
        full_cost = resolve_bill_energy_price(bill, include_line_loss=True)
        assert default.avoided_price_yuan_per_kwh == pytest.approx(0.30)
        assert full_cost.avoided_price_yuan_per_kwh == pytest.approx(0.32)
        assert "直接交易价格" in "".join(default.assumptions)
        assert "全额成本口径" in "".join(full_cost.assumptions)

    def test_average_is_used_only_when_no_hourly_table(self):
        bill = _base_bill()  # 无 hourly_energy_tariff
        outcome = resolve_bill_energy_price(
            bill,
            customer_kind=CUSTOMER_KIND_AGENCY_PURCHASE,
            gov_tou_prices=self._GOV_TOU,
        )
        assert outcome.source == PRICE_SOURCE_BILL_AVERAGE
        assert outcome.avoided_price_yuan_per_kwh == pytest.approx(0.60)
        text = "".join(outcome.messages) + "".join(outcome.assumptions)
        assert "降级" in text

    def test_gov_factor_rejected_for_market_direct_customer(self):
        """市场化直购客户**不得**套用湖北政府峰谷系数 → 中文错误。"""
        stripped = ElectricityBill(
            bill_id="BILL-NO-PRICE",
            billing_period_start=date(2026, 3, 1),
            billing_period_end=date(2026, 3, 31),
        )
        with pytest.raises(ValidationError) as exc:
            resolve_bill_energy_price(
                stripped,
                customer_kind=CUSTOMER_KIND_MARKET_DIRECT,
                gov_tou_prices=self._GOV_TOU,
            )
        message = exc.value.message
        assert "不得" in message
        assert "代理购电" in message
        assert "200%" in message and "150%" in message and "45%" in message

    def test_gov_factor_allowed_for_agency_purchase_customer(self):
        stripped = ElectricityBill(
            bill_id="BILL-NO-PRICE",
            billing_period_start=date(2026, 3, 1),
            billing_period_end=date(2026, 3, 31),
        )
        outcome = resolve_bill_energy_price(
            stripped,
            customer_kind=CUSTOMER_KIND_AGENCY_PURCHASE,
            gov_tou_prices=self._GOV_TOU,
        )
        assert outcome.source == PRICE_SOURCE_GOV_TOU_FACTOR
        assert outcome.avoided_price_yuan_per_kwh == pytest.approx(
            (2.00 + 1.50 + 1.00 + 0.45) / 4
        )
        assert "仅适用于电网代理购电客户" in "".join(outcome.messages)

    def test_incomplete_hourly_table_raises_chinese_error(self):
        """表存在但不完整：必须报中文错，**不得**静默降级到平均电价或政府峰谷系数。"""
        points = [
            HourlyEnergyPricePoint(hour=hour, direct_trade_price_yuan_per_kwh=0.3)
            for hour in range(1, 24)
        ]
        # 用 model_copy 绕过模型层的"表必须完整"校验，直接验证计算层的分支行为
        bill = _base_bill().model_copy(update={"hourly_energy_tariff": points})
        with pytest.raises(ValidationError) as exc:
            resolve_bill_energy_price(bill)
        assert "24 小时电量电价" in exc.value.message
        assert "不得退回账单平均电价或政府峰谷系数" in exc.value.message

    def test_no_source_at_all_raises_chinese_error(self):
        stripped = ElectricityBill(
            bill_id="BILL-EMPTY",
            billing_period_start=date(2026, 3, 1),
            billing_period_end=date(2026, 3, 31),
        )
        with pytest.raises(ValidationError) as exc:
            resolve_bill_energy_price(stripped)
        assert "无法确定替代电价" in exc.value.message
        assert "臆造" in exc.value.message

    def test_illegal_customer_kind_raises_chinese_error(self):
        with pytest.raises(ValidationError) as exc:
            resolve_bill_energy_price(_base_bill(), customer_kind="随便写的类型")
        assert "无法识别" in exc.value.message
        assert CUSTOMER_KIND_AGENCY_PURCHASE in exc.value.message

    def test_hourly_avoided_prices_returns_24_points(self):
        hours, prices, energies = hourly_avoided_prices(
            _base_bill(hourly_energy_tariff=_hourly_points())
        )
        assert hours == HOURLY_PRICE_HOURS
        assert len(prices) == 24 and len(energies) == 24
        # 默认口径：逐时直接交易价格（不含上网环节线损价格）
        assert all(price == pytest.approx(0.30) for price in prices)
        full_hours, full_prices, _full_energies = hourly_avoided_prices(
            _base_bill(hourly_energy_tariff=_hourly_points()), include_line_loss=True
        )
        assert full_hours == HOURLY_PRICE_HOURS
        assert all(price == pytest.approx(0.32) for price in full_prices)

    def test_real_sample_prefers_hourly_trade_price(self):
        """真实账单：逐时加权价 ≈ 0.41，而账单平均价 ≈ 0.59 → 必须取逐时价。"""
        bill = preview_bill_import(_require_sample()).rows[0].bill
        outcome = resolve_bill_energy_price(bill)
        assert isinstance(outcome, BillEnergyPriceResolution)
        assert outcome.source == PRICE_SOURCE_HOURLY_TABLE
        assert outcome.hours == HOURLY_PRICE_HOURS
        assert outcome.avoided_price_yuan_per_kwh is not None
        assert outcome.bill_average_price_yuan_per_kwh is not None
        assert outcome.avoided_price_yuan_per_kwh < outcome.bill_average_price_yuan_per_kwh
        # 实测区间（样本账单账单原件）：逐时直接交易价格落在 0.20~0.50 元/kWh
        assert all(0.20 <= price <= 0.50 for price in outcome.hourly_prices_yuan_per_kwh)

    def test_real_sample_matches_requirement_providers_measured_range(self):
        """需求方实测口径的**逐条复现**（V2.5 §5 ⑤ 的验收锚点）。

        需求方原文："其逐时交易价格才是真实的替代电价（实测 1–9 时 0.416~0.437，
        10–13 时 0.239~0.295）"。默认口径必须正好复现该区间——
        这既证明取值来自 24 小时表，也证明口径是"直接交易价格"而非平均价或政府峰谷系数。
        """
        bill = preview_bill_import(_require_sample()).rows[0].bill
        outcome = resolve_bill_energy_price(bill)
        morning = [outcome.price_at_hour(hour) for hour in range(1, 10)]
        noon = [outcome.price_at_hour(hour) for hour in range(10, 14)]
        assert all(price is not None for price in (*morning, *noon))
        # 与需求方实测值**逐位复现**（报价保留三位小数）
        assert (round(min(morning), 3), round(max(morning), 3)) == (0.416, 0.437), morning
        assert (round(min(noon), 3), round(max(noon), 3)) == (0.239, 0.295), noon

        # 对照：同一账期若按"代理购电 + 湖北政府峰谷系数"取价（基础电价 0.398113 元/kWh），
        # 得到的时段电价与逐时实际价格毫无关系 —— 证明逐时口径不可被系数口径替代。
        # 注意：必须用一张**既无逐时表也无平均电价**的账单，否则会先命中优先级①②。
        base_price = 0.398113
        gov_prices = {
            TariffPeriod.SHARP_PEAK: 2.00 * base_price,
            TariffPeriod.PEAK: 1.50 * base_price,
            TariffPeriod.FLAT: 1.00 * base_price,
            TariffPeriod.VALLEY: 0.45 * base_price,
        }
        agency = ElectricityBill(
            bill_id="BILL-AGENCY-CONTRAST",
            billing_period_start=bill.billing_period_start,
            billing_period_end=bill.billing_period_end,
        )
        fallback = resolve_bill_energy_price(
            agency,
            customer_kind=CUSTOMER_KIND_AGENCY_PURCHASE,
            gov_tou_prices=gov_prices,
        )
        assert fallback.source == PRICE_SOURCE_GOV_TOU_FACTOR
        assert fallback.gov_tou_prices_yuan_per_kwh
        for price in fallback.gov_tou_prices_yuan_per_kwh.values():
            assert outcome.avoided_price_yuan_per_kwh != pytest.approx(price)

    def test_real_sample_row_level_prices_come_from_the_hourly_table(self):
        """逐时电价必须**等于账单第 4 页的直接交易价格**（逐条比对，不是只看区间）。"""
        bill = preview_bill_import(_require_sample()).rows[0].bill
        by_hour = {
            point.hour: point.direct_trade_price_yuan_per_kwh
            for point in bill.hourly_energy_tariff
        }
        outcome = resolve_bill_energy_price(bill)
        for hour in HOURLY_PRICE_HOURS:
            assert outcome.price_at_hour(hour) == pytest.approx(by_hour[hour])
            # 与「上网环节线损价格」无关：默认口径不叠加该项
            assert outcome.price_at_hour(hour) == pytest.approx(
                bill.hourly_energy_tariff[hour - 1].direct_trade_price_yuan_per_kwh
            )

    def test_service_delegates_without_own_formula(self):
        """应用层只转调，不含公式（§0.2：核心规则在 calculation/）。"""
        from cenep.application.bill_service import BillService
        from cenep.domain.models import Project

        service = BillService(Project())
        service.create_bill(**_bill_fields())
        outcome = service.resolve_energy_price(service.bills[0].bill_id)
        assert outcome.source in PRICE_PRIORITY


def _bill_fields() -> dict[str, Any]:
    """``BillService.create_bill`` 所需的字段（日期用字符串，服务层负责转换）。"""
    return {
        "billing_period_start": "2026-03-01",
        "billing_period_end": "2026-03-31",
        "energy_total_kwh": 1000.0,
        "energy_charge_yuan": 600.0,
        "bill_total_yuan": 4450.0,
    }


# --------------------------------------------------------------------------- #
# 5. meter_groups 已入库 + 导入不再报「无法识别」（V2.5 缺陷修复）
# --------------------------------------------------------------------------- #
class TestMeterGroupsPersisted:
    """采用方案 A：给账单加字段把它存下来，逐电能表的分时明细不丢、可追溯。"""

    def test_field_exists_and_defaults_to_empty(self):
        bill = ElectricityBill(
            bill_id="B-EMPTY",
            billing_period_start=date(2026, 1, 1),
            billing_period_end=date(2026, 1, 31),
        )
        assert "meter_groups" in ElectricityBill.model_fields
        assert bill.meter_groups == []

    def test_old_nep_payload_without_meter_groups_still_loads(self):
        """追加式扩展的硬要求：旧项目文件里没有该键，反序列化必须照常成功。"""
        legacy = {
            "bill_id": "BILL-20260101-20260131-MAIN",
            "billing_period_start": "2026-01-01",
            "billing_period_end": "2026-01-31",
            "energy_total_kwh": 12345.0,
            "bill_total_yuan": 6789.0,
            "source_type": "excel",
        }
        bill = ElectricityBill(**legacy)
        assert bill.meter_groups == []
        assert bill.energy_total_kwh == pytest.approx(12345.0)

    def test_meter_groups_survive_roundtrip(self):
        bill = _base_bill()
        restored = ElectricityBill(**bill.model_dump(mode="json"))
        assert len(restored.meter_groups) == 2
        assert restored.meter_groups[1].is_ratio_submeter is True
        assert restored.meter_groups[1].parent_meters == ["4230000100027673777"]
        assert restored.meter_groups[1].total_kwh == pytest.approx(300.0)

    def test_adding_meter_groups_does_not_change_existing_fields(self):
        """字段语义不变：加字段前后**既有字段**（除新键外）完全一致。"""
        with_groups = _base_bill()
        without = with_groups.model_copy(update={"meter_groups": []})
        after = with_groups.model_dump(mode="json")
        before = without.model_dump(mode="json")
        after.pop("meter_groups")
        before.pop("meter_groups")
        assert before == after
        assert without.meter_groups == []

    def test_build_bill_from_row_accepts_meter_groups_without_notice(self):
        """结构化字段 ``meter_groups`` 现在**可识别**：不报「无法识别」，且写进账单。"""
        extras = {
            "meter_groups": [
                {
                    "meters": ["4230000100027673777"],
                    "tariff": "用电户-110千伏-工商业两部制",
                    "is_ratio_submeter": False,
                    "parent_meters": [],
                    "energy": {"total": 700.0, "sharp": 100.0, "peak": 200.0, "flat": 300.0, "valley": 100.0},
                },
                {
                    "meters": ["定比0.03"],
                    "tariff": "用电户-35千伏-非居民照明-工商业单一制",
                    "is_ratio_submeter": True,
                    "parent_meters": ["4230000100027673777"],
                    "energy": {"total": 300.0, "valley": 300.0},
                },
            ]
        }
        bill, errors, notices = build_bill_from_row(
            _template_row(),
            ColumnMapping(mapping={column.field: column.header for column in BILL_COLUMNS if column.field}),
            sheet_name="月账单",
            row_number=2,
            project_id="口径测试项目",
            file_name="口径测试.xlsx",
            bill_extras=extras,
        )
        assert errors == []
        assert bill is not None
        assert not any("无法识别" in notice for notice in notices), notices
        assert len(bill.meter_groups) == 2
        assert bill.meter_groups[1].parent_meters == ["4230000100027673777"]

    def test_truly_unknown_structured_field_is_still_reported_in_chinese(self):
        """没有把校验删掉：真正未知的结构化字段仍会被中文提示并忽略。"""
        bill, errors, notices = build_bill_from_row(
            _template_row(),
            ColumnMapping(mapping={column.field: column.header for column in BILL_COLUMNS if column.field}),
            sheet_name="月账单",
            row_number=2,
            project_id="口径测试项目",
            file_name="口径测试.xlsx",
            bill_extras={"some_unknown_fact": 1},
        )
        assert errors == []
        assert bill is not None
        assert any("无法识别的结构化字段" in notice and "some_unknown_fact" in notice for notice in notices)

    def test_real_pdf_bill_has_meter_groups_and_no_unknown_notice(self):
        preview = preview_bill_import(_require_sample())
        bill = preview.rows[0].bill
        assert bill is not None
        assert len(bill.meter_groups) >= 2
        # 定比分表必须被识别出来，并带上"上级电能表编号"（来源追溯）
        ratio_groups = [group for group in bill.meter_groups if group.is_ratio_submeter]
        assert ratio_groups, [group.describe() for group in bill.meter_groups]
        assert ratio_groups[0].parent_meters
        # 至少有一个分组给出了逐时段计费电量（其余是只有表头的空组，如实留空、不臆造）
        assert any(group.energy for group in bill.meter_groups)
        assert any(group.total_kwh is not None for group in bill.meter_groups)
        # 合并后等于账单总电量（主表 + 定比分表）
        merged = sum((group.total_kwh or 0.0) for group in bill.meter_groups)
        assert merged == pytest.approx(bill.energy_total_kwh)
        joined = " ".join(
            message for row in preview.rows for message in row.messages
        )
        assert "无法识别" not in joined, joined
        assert "meter_groups" not in joined, joined

    def test_real_pdf_meter_groups_are_disclosed_as_traceability_only(self):
        preview = preview_bill_import(_require_sample())
        bill = preview.rows[0].bill
        text = "".join(reconcile_bill(bill).assumptions)
        assert "计量分组" in text
        assert "不单独参与电价计算" in text


def _template_row() -> dict[str, Any]:
    """按模板中文表头构造一行可用账单（让 ``build_bill_from_row`` 走通）。"""
    headers = {column.field: column.header for column in BILL_COLUMNS if column.field}
    row: dict[str, Any] = {
        headers["billing_period_start"]: "2026-03-01",
        headers["billing_period_end"]: "2026-03-31",
        headers["energy_total_kwh"]: 1000.0,
        headers["energy_sharp_kwh"]: 100.0,
        headers["energy_peak_kwh"]: 200.0,
        headers["energy_flat_kwh"]: 300.0,
        headers["energy_valley_kwh"]: 400.0,
        headers["energy_charge_yuan"]: 600.0,
        headers["bill_total_yuan"]: 4450.0,
    }
    return row


# --------------------------------------------------------------------------- #
# 6. 版本号 2.5.0 + 冻结接口未动（规格书 §0.2）
# --------------------------------------------------------------------------- #
class TestVersion2_5_0:
    def test_package_version_is_2_5_0(self):
        assert __version__ == "2.5.0"

    def test_module_docstring_mentions_v25(self):
        import cenep

        doc = cenep.__doc__ or ""
        assert "V2.5" in doc
        assert "只留档" in doc

    def test_frozen_interfaces_unchanged(self):
        """§0.2 冻结：``schema_version`` / 计算引擎版本 / 账单段版本**未动**。"""
        assert SCHEMA_VERSION == "2.0"
        assert CALCULATION_ENGINE_VERSION == "2.0.0"
        assert BILL_SECTION_SCHEMA_VERSION == "1.0"

    def test_pyproject_version_and_description(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'version = "2.5.0"' in text
        assert "V2.5" in text

    def test_version_info_resource_is_2_5_0(self):
        text = (ROOT / "build" / "version_info.txt").read_text(encoding="utf-8")
        assert "filevers=(2, 5, 0, 0)" in text
        assert "prodvers=(2, 5, 0, 0)" in text
        assert 'StringStruct("FileVersion", "2.5.0.0")' in text
        assert 'StringStruct("ProductVersion", "2.5.0")' in text
        # 冻结项在注释里被明确标注为"不变"
        assert 'schema_version = "2.0"' in text
        assert 'CALCULATION_ENGINE_VERSION = "2.0.0"' in text

    def test_no_stale_2_4_version_assertion_left_in_tests(self):
        """旧版本号断言必须已同步（否则升级会立刻红）。"""
        contract = (ROOT / "tests" / "test_v24_report_contract.py").read_text(encoding="utf-8")
        assert 'assert __version__ == "2.5.0"' in contract
        assert 'assert __version__ == "2.0.0"' not in contract
        assert 'filevers=(2, 5, 0, 0)' in contract
