"""输入校验与守恒校验（规范 §112、§113、§114、§132）。

**面向用户的报错统一从这里产生**，全部为中文，并携带出错字段名，
保证 GUI 能定位到具体控件（规范 §132：不能抛裸 ``ValueError``）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.enums import ProjectType, TariffMode
from ..domain.models import Project
from ..domain.results import AnnualResult
from .errors import EnergyBalanceError, ValidationError
from .pv import ENERGY_BALANCE_TOLERANCE


def _require(condition: bool, message: str, field: str) -> None:
    if not condition:
        raise ValidationError(message, field=field)


def validate_ratios(project: Project) -> None:
    """比例类参数必须落在 0~1（规范 §112）。"""
    t = project.tariff
    if t.tariff_mode == TariffMode.TOU:
        total = t.peak_ratio + t.flat_ratio + t.valley_ratio
        _require(
            abs(total - 1.0) <= 1e-6,
            f"峰、平、谷电量比例之和必须等于 1（当前为 {total:.6f}）",
            "tariff.peak_ratio",
        )
    _require(0.0 <= project.pv.self_consumption_ratio <= 1.0, "光伏自用比例必须在 0~100% 之间", "pv.self_consumption_ratio")
    _require(0.0 <= project.pv.curtailment_rate <= 1.0, "限电率必须在 0~100% 之间", "pv.curtailment_rate")
    _require(0.0 <= project.tax.residual_value_ratio <= 1.0, "残值率必须在 0~100% 之间", "tax.residual_value_ratio")
    _require(0.0 <= project.financing.debt_ratio <= 1.0, "贷款比例必须在 0~100% 之间", "financing.debt_ratio")
    _require(0.0 <= project.discount_rate <= 0.5, "折现率必须在 0~50% 之间", "discount_rate")


def validate_project(project: Project) -> None:
    """完整输入校验（规范 §112、§114）。

    在计算开始前调用；任何一条不通过都会抛出带字段名的中文错误。
    """
    # ---- 基本规模 ----
    _require(project.analysis_period > 0, "项目生命周期必须大于 0", "analysis_period")

    ptype = project.basic_info.project_type
    if ptype in (ProjectType.COMMERCIAL_PV, ProjectType.PV_STORAGE):
        cap = project.pv.pv_capacity_kwp
        if cap is None:
            cap = (
                project.pv.usable_roof_area_m2 / project.pv.area_per_kwp
                if project.pv.area_per_kwp > 0
                else 0.0
            )
        _require(cap > 0, "光伏装机容量必须大于 0（可直接输入容量，或输入可利用屋顶面积与单位面积容量）", "pv.pv_capacity_kwp")
        _require(project.pv.equivalent_hours > 0, "年等效利用小时必须大于 0", "pv.equivalent_hours")
        _require(0.0 < project.pv.performance_ratio <= 1.0, "性能比必须大于 0 且不超过 100%", "pv.performance_ratio")
        _require(0.0 <= project.pv.annual_degradation_rate <= 0.5, "光伏年衰减率必须在 0~50% 之间", "pv.annual_degradation_rate")

    if ptype in (ProjectType.COMMERCIAL_STORAGE, ProjectType.PV_STORAGE):
        s = project.storage
        _require(s.storage_energy_kwh > 0, "储能容量必须大于 0", "storage.storage_energy_kwh")
        _require(s.storage_power_kw > 0, "储能功率必须大于 0", "storage.storage_power_kw")
        _require(0.0 < s.depth_of_discharge <= 1.0, "放电深度 DoD 必须大于 0 且不超过 100%", "storage.depth_of_discharge")
        _require(s.annual_cycles >= 0, "年循环次数不能为负数", "storage.annual_cycles")
        _require(0.0 <= s.annual_degradation_rate <= 0.5, "储能年衰减率必须在 0~50% 之间", "storage.annual_degradation_rate")

        for name, eff in (
            ("充电效率", s.charge_efficiency),
            ("放电效率", s.discharge_efficiency),
            ("往返效率", s.round_trip_efficiency),
        ):
            if eff is not None:
                _require(0.0 < eff <= 1.0, f"储能{name}不能大于 100% 或小于等于 0", "storage.efficiency")

        if s.charge_efficiency is not None and s.discharge_efficiency is not None:
            round_trip = s.charge_efficiency * s.discharge_efficiency
            _require(
                round_trip > 0,
                "储能充电效率与放电效率的乘积必须大于 0",
                "storage.efficiency",
            )

        if s.replacement_year is not None:
            _require(
                s.replacement_year <= project.analysis_period,
                "更换电芯年份不能超过项目生命周期",
                "storage.replacement_year",
            )

    # ---- 电量与电价非负（规范 §112）----
    _require(project.load.annual_load_kwh >= 0, "年用电量不能为负数", "load.annual_load_kwh")
    t = project.tariff
    for label, value, field in (
        ("固定电价", t.average_price, "tariff.average_price"),
        ("峰电价", t.peak_price, "tariff.peak_price"),
        ("平电价", t.flat_price, "tariff.flat_price"),
        ("谷电价", t.valley_price, "tariff.valley_price"),
        ("市场电价", t.market_price, "tariff.market_price"),
        ("余电上网电价", t.export_price, "tariff.export_price"),
    ):
        _require(value >= 0, f"{label}不能为负数", field)
    if t.tariff_mode == TariffMode.CUSTOM:
        _require(t.custom_avoided_price is not None, "自定义电价模式必须填写替代电价", "tariff.custom_avoided_price")
        _require(t.custom_charge_price is not None, "自定义电价模式必须填写充电电价", "tariff.custom_charge_price")

    # ---- 税率/利率区间（规范 §112）----
    _require(0.0 <= project.tax.vat_rate <= 1.0, "增值税率必须在 0~100% 之间", "tax.vat_rate")
    _require(0.0 <= project.tax.income_tax_rate <= 1.0, "所得税率必须在 0~100% 之间", "tax.income_tax_rate")
    _require(0.0 <= project.financing.interest_rate <= 1.0, "贷款利率必须在 0~100% 之间", "financing.interest_rate")

    validate_ratios(project)

    # ---- 融资一致性（规范 §114）----
    f = project.financing
    if f.enabled:
        _require(
            abs(f.debt_ratio + f.equity_ratio - 1.0) <= 1e-6,
            f"贷款比例与资本金比例之和必须等于 1（当前为 {f.debt_ratio + f.equity_ratio:.6f}）",
            "financing.debt_ratio",
        )
        _require(f.loan_term > 0, "贷款期限必须大于 0", "financing.loan_term")
        _require(
            f.grace_period < f.loan_term,
            "宽限期必须小于贷款期限，否则无法还本",
            "financing.grace_period",
        )

    # ---- 投资 ----
    _require(project.investment.pv_capex_per_kw >= 0, "光伏单位投资不能为负数", "investment.pv_capex_per_kw")
    _require(project.investment.storage_capex_per_kwh >= 0, "储能单位投资不能为负数", "investment.storage_capex_per_kwh")


def validate_energy_balance(annual_results: Sequence[AnnualResult], tolerance: float = ENERGY_BALANCE_TOLERANCE) -> None:
    """能量守恒校验（规范 §113）。

    ``PVGeneration = PVDirectUse + PVToStorage + PVExport + PVLoss``，误差必须 ≤ 1e-6。
    """
    for row in annual_results:
        lhs = row.pv_generation_kwh
        rhs = row.pv_self_use_kwh + row.pv_to_storage_kwh + row.pv_export_kwh + row.pv_loss_kwh
        error = lhs - rhs
        if abs(error) > tolerance:
            raise EnergyBalanceError(
                f"第 {row.year} 年光伏电量不守恒：发电量 {lhs:.6f} kWh，"
                f"分配合计 {rhs:.6f} kWh，误差 {error:.6f} kWh",
                field="pv.energy_balance",
            )


def validate_storage_balance(annual_results: Sequence[AnnualResult], tolerance: float = 1e-6) -> None:
    """储能充放电口径校验：年充电量必须 ≥ 由光伏转入的电量。"""
    for row in annual_results:
        if row.pv_to_storage_kwh - row.storage_charge_kwh > tolerance:
            raise EnergyBalanceError(
                f"第 {row.year} 年储能充电量小于光伏转入电量，请检查储能参数",
                field="storage.charge_energy",
            )
