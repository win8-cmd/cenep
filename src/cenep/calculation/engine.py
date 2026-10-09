"""统一计算引擎（规范 §82、§147）。

**唯一入口**：``calculation_engine.calculate(project) -> CalculationResult``

执行顺序严格遵循规范 §82 的 31 步。GUI / Excel / PDF 一律只能调用本引擎（规范 §148–§150）。

调用方式::

    from cenep.calculation.engine import calculation_engine
    result = calculation_engine.calculate(project)
"""

from __future__ import annotations

from ..domain.enums import ProjectType, ScenarioType, SensitivityVariable, SourceType
from ..domain.models import Project
from ..domain.provenance import ParameterRegistry
from ..domain.results import AnnualResult, CalculationResult, ScenarioSummary, SensitivityRow
from . import cashflow as cf
from . import financial_metrics as fm
from . import financing as fin
from . import investment as inv
from . import opex as opx
from . import pv as pvmod
from . import revenue as rev
from . import storage as st
from . import tax as taxmod
from . import validator
from .scenario import apply_delta, delta_for
from .sensitivity import applicable_variables, apply_variable

#: 计算口径说明（规范 §161：任何一个数字都必须能追溯到参数和公式）
_CALIBER_NOTES: tuple[str, ...] = (
    "Year 0 为建设/初始投资期，Year 1 为第一个运营年度（规范 §15）。",
    "光伏电量分配顺序：直接自用 → 储能 → 余电上网（规范 §23、§46）；进入储能的电量不计光伏自用收益，不存在重复计算（规范 §47）。",
    "储能套利按 §43 原文计算：全部充电量按充电电价计价，其中包含由光伏转入储能的电量。",
    "储能收益分为套利、容量、辅助服务、其他四类分别列示（规范 §44）；容量/辅助/其他收益 V1 按用户输入或政策模板的固定值计取，不随年份增长。",
    "LCOE 采用光伏口径：成本 = 光伏应承担投资（总投资 − 储能投资）+ 全部光伏相关运营成本"
    "（光伏运维费 + 屋顶租金 + 保险费 + 管理费 + 其他费用）+ 光伏设备更换；电量 = 光伏发电量。",
    "LCOS 采用储能口径：成本 = 储能投资 + 储能运维费 + 电芯更换投资；电量 = 储能放电量。",
    "LCOE/LCOS 成本均不含融资利息（规范 §76）；管理费、保险费与其他费用按项目级共享成本全额计入 LCOE（纯光伏项目即全额计入）。",
    "LCOE/LCOS 可选抵减项（规范 §75）：lcoe_vat_deductible_ratio 按投资比例抵减增值税进项；"
    "lcoe_residual_credit 为真时按光伏/储能投资比例分摊残值并在第 N 年抵减。两者默认关闭。",
    "ROI = 生命周期累计净收益（EBITDA − 现金税费）÷ 初始总投资（规范 §78）。",
    "项目IRR 使用「EBITDA − 现金税费 − 投资 − 更换投资 + 残值」的融资前现金流（规范 §67）。",
    "资本金IRR 使用「EBITDA − 现金税费 − 利息 − 还本 − 资本金投入 − 更换投资 + 残值」的现金流（规范 §68）。",
    "利息按 §65 以期初/期末平均贷款余额计算，不使用「原始贷款 × 利率」的简化口径。",
    "折旧采用直线法，应折旧基数 = 总投资 × 可折旧比例 × (1 − 残值率)（规范 §57、§58）。",
    "V1 税务为简化模型：增值税进项抵扣与留抵退税不建模；附加税费与其他税费以不含税收入为基数；不建模亏损跨年弥补（规范 §59）。",
    "所得税 = max(EBT, 0) × 所得税率（规范 §61）。",
    "所有政策参数均来自政策 Profile 并带版本与出处，未在代码中硬编码（规范 §34、§137）。",
)


class CalculationEngine:
    """统一计算引擎。无状态、可重入、结果完全确定（规范 §118）。"""

    # ------------------------------------------------------------------ #
    # 公共入口
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        project: Project,
        *,
        include_scenario: bool = True,
        include_sensitivity: bool = True,
        year_override: dict[int, object] | None = None,
    ) -> CalculationResult:
        """按规范 §82 计算并返回唯一的 :class:`CalculationResult`。

        :param year_override: V2 时序口径覆盖表 ``{年份: YearOverride}``（V2 §62）。
            为 ``None`` 时走纯 V1 年度模型，结果与 V1 **逐位一致**（V2 §1.1 兼容性承诺）。
            非空时只替换该年的**电量与收益**，OPEX/折旧/税/融资/现金流/IRR/NPV
            仍由同一套 V1 代码计算（V2 §61 单一计算源）。
        """
        result = self._calculate_core(project, year_override=year_override)

        if include_scenario and project.scenario.enabled:
            result.scenarios = self._run_scenarios(project)
        if include_sensitivity and project.sensitivity.enabled:
            result.sensitivity = self._run_sensitivity(project, result)
        return result

    # ------------------------------------------------------------------ #
    # 核心计算（§82 第 1–27、30、31 步）
    # ------------------------------------------------------------------ #
    def _calculate_core(
        self, project: Project, *, year_override: dict[int, object] | None = None
    ) -> CalculationResult:
        # --- 1. Validate Inputs ---
        validator.validate_project(project)

        ptype: ProjectType = project.basic_info.project_type
        n = int(project.analysis_period)
        rate = float(project.discount_rate)

        registry = ParameterRegistry()
        self._register_parameters(project, registry)

        # --- 2. Calculate Load ---
        load_1 = float(project.load.annual_load_kwh)
        load_growth = float(project.load.annual_load_growth_rate)

        # --- 3. Calculate PV Capacity ---
        pv_capacity, capacity_basis = pvmod.resolve_pv_capacity(
            project.pv.pv_capacity_kwp,
            project.pv.usable_roof_area_m2,
            project.pv.area_per_kwp,
        )
        if not ptype.has_pv:
            pv_capacity = 0.0
            capacity_basis = "NONE"

        # --- 4. Calculate PV Generation（首年）---
        first_year_generation = pvmod.first_year_generation(
            pv_capacity,
            project.pv.equivalent_hours,
            project.pv.performance_ratio,
            project.pv.curtailment_rate,
        )

        # --- 7/8/9 前置：储能容量与效率（规范 §39、§40）---
        storage_energy = float(project.storage.storage_energy_kwh) if ptype.has_storage else 0.0
        storage_power = float(project.storage.storage_power_kw) if ptype.has_storage else 0.0
        storage_hours = st.storage_duration_hours(storage_energy, storage_power)
        if ptype.has_storage:
            charge_eff, discharge_eff, round_trip = st.resolve_efficiencies(
                project.storage.charge_efficiency,
                project.storage.discharge_efficiency,
                project.storage.round_trip_efficiency,
            )
        else:
            charge_eff = discharge_eff = 1.0
            round_trip = 1.0

        # --- 10. Calculate Electricity Price ---
        t = project.tariff
        tariff = rev.resolve_tariff(
            tariff_mode=t.tariff_mode.value,
            average_price=t.average_price,
            peak_price=t.peak_price,
            flat_price=t.flat_price,
            valley_price=t.valley_price,
            peak_ratio=t.peak_ratio,
            flat_ratio=t.flat_ratio,
            valley_ratio=t.valley_ratio,
            market_price=t.market_price,
            custom_avoided_price=t.custom_avoided_price,
            custom_charge_price=t.custom_charge_price,
            export_price=t.export_price,
            avoided_price_override=t.avoided_price_override,
            charge_price_override=t.charge_price_override,
        )
        # 储能可以单独指定放电替代价与充电价（§43）
        discharge_avoided_price = (
            project.storage.discharge_avoided_price
            if project.storage.discharge_avoided_price is not None
            else tariff.avoided_electricity_price
        )
        storage_charge_price = (
            project.storage.charge_price
            if project.storage.charge_price is not None
            else tariff.charge_price
        )

        # --- 15. Calculate CAPEX ---
        i = project.investment
        capex = inv.compute_capex(
            mode=i.mode.value,
            pv_capacity_kwp=pv_capacity,
            storage_energy_kwh=storage_energy,
            pv_capex_per_kw=i.pv_capex_per_kw,
            storage_capex_per_kwh=i.storage_capex_per_kwh,
            pv_capex=i.pv_capex,
            storage_capex=i.storage_capex,
            grid_connection_cost=i.grid_connection_cost,
            roof_cost=i.roof_cost,
            development_cost=i.development_cost,
            engineering_cost=i.engineering_cost,
            construction_cost=i.construction_cost,
            other_capex=i.other_capex,
            contingency=i.contingency,
            detailed_items=i.detailed_items,
        )
        total_capex = capex.total

        # --- 16. Calculate OPEX（首年）---
        o = project.opex
        pv_opex_1 = opx.resolve_opex_item(o.pv_opex, o.pv_opex_mode.value, capex.pv_capex, "opex.pv_opex")
        storage_opex_1 = opx.resolve_opex_item(
            o.storage_opex, o.storage_opex_mode.value, capex.storage_capex, "opex.storage_opex"
        )
        insurance_1 = opx.resolve_opex_item(o.insurance, o.insurance_mode.value, total_capex, "opex.insurance")
        management_1 = opx.resolve_opex_item(
            o.management_cost, o.management_mode.value, total_capex, "opex.management_cost"
        )
        other_opex_1 = opx.resolve_opex_item(o.other_opex, o.other_opex_mode.value, total_capex, "opex.other_opex")
        roof_rent_1 = opx.roof_rent(
            mode=o.roof_rent_mode.value,
            roof_area_m2=project.pv.roof_area_m2,
            rent_per_m2=o.rent_per_m2,
            pv_capacity_kwp=pv_capacity,
            rent_per_kw=o.rent_per_kw,
            annual_fixed_rent=o.annual_fixed_rent,
        )
        opex_breakdown = opx.OpexBreakdown(
            pv_opex=pv_opex_1,
            storage_opex=storage_opex_1,
            roof_rent=roof_rent_1,
            insurance=insurance_1,
            management_cost=management_1,
            other_opex=other_opex_1,
            growth_rate=float(o.annual_opex_growth_rate),
        )
        first_year_opex = opex_breakdown.first_year_total

        # --- 17 前置：折旧参数 ---
        depreciable_capex = total_capex * float(project.tax.depreciable_capex_ratio)
        depreciation_years = int(project.tax.depreciation_years)
        residual_value_ratio = float(project.tax.residual_value_ratio)
        terminal_residual = taxmod.residual_value(depreciable_capex, residual_value_ratio)

        # --- 18. Calculate Financing ---
        f = project.financing
        if f.enabled:
            loan = (
                float(f.loan_amount)
                if f.loan_amount is not None
                else fin.loan_amount_of(total_capex, f.debt_ratio)
            )
        else:
            loan = 0.0
        equity = fin.equity_amount_of(total_capex, loan)
        if loan > 0:
            loan_schedule = fin.build_loan_schedule(
                loan_amount=loan,
                interest_rate=float(f.interest_rate),
                loan_term=int(f.loan_term),
                grace_period=int(f.grace_period),
                repayment_method=f.repayment_method.value,
                analysis_period=n,
            )
        else:
            loan_schedule = [
                fin.LoanYear(year=y, debt_begin=0.0, drawdown=0.0, principal_repayment=0.0, interest=0.0, debt_end=0.0)
                for y in range(1, n + 1)
            ]

        # --- 5/6/8/9/11–14/19–21 逐年循环 ---
        annual: list[AnnualResult] = []
        project_flows: list[float] = [cf.year0_project_cashflow(total_capex)]
        equity_flows: list[float] = [cf.year0_equity_cashflow(equity)]

        # LCOE/LCOS 成本序列的 t=0（投资）与口径开关（规范 §75）
        # * LCOE 的光伏投资基数 = 总投资 − 储能投资：并网、开发、设计、施工等共享投资由
        #   光伏承担，避免只取 pv_capex 而漏计（纯光伏项目即等于总投资）。
        # * lcoe_vat_deductible_ratio > 0 时，按该比例抵减增值税进项（行业/招标通行口径）。
        # * lcoe_residual_credit 为真时，将残值现值作为成本抵减（记在第 N 年）。
        vat_deductible_ratio = float(project.tax.lcoe_vat_deductible_ratio)
        pv_investment_for_lcoe = total_capex - capex.storage_capex
        storage_share = (capex.storage_capex / total_capex) if total_capex > 0 else 0.0
        pv_costs: list[float] = [pv_investment_for_lcoe * (1.0 - vat_deductible_ratio)]
        pv_energies: list[float] = [0.0]
        storage_costs: list[float] = [capex.storage_capex * (1.0 - vat_deductible_ratio)]
        storage_energies: list[float] = [0.0]
        lifecycle_net_profit = 0.0

        for year in range(1, n + 1):
            # 2. 负荷
            load = load_1 * (1.0 + load_growth) ** (year - 1)

            # 4/5. 光伏发电量与衰减
            generation = pvmod.generation_for_year(
                first_year_generation, float(project.pv.annual_degradation_rate), year
            ) if ptype.has_pv else 0.0

            # 7/8/9. 储能年度等效循环
            if ptype.has_storage:
                storage_year = st.storage_year_result(
                    year=year,
                    initial_energy_kwh=storage_energy,
                    depth_of_discharge=float(project.storage.depth_of_discharge),
                    annual_cycles=float(project.storage.annual_cycles),
                    charge_efficiency=charge_eff,
                    discharge_efficiency=discharge_eff,
                    annual_degradation_rate=float(project.storage.annual_degradation_rate),
                    replacement_year=project.storage.replacement_year,
                )
            else:
                storage_year = st.StorageYear(
                    year=year,
                    available_energy_kwh=0.0,
                    discharge_energy_kwh=0.0,
                    charge_energy_kwh=0.0,
                    is_replacement_year=False,
                )

            # 6. 光伏电量分配
            allocation = pvmod.allocate_pv_energy(
                generation=generation,
                load=load,
                self_consumption_ratio=float(project.pv.self_consumption_ratio),
                storage_charge_headroom=storage_year.charge_energy_kwh,
                loss_ratio=float(project.pv.loss_ratio),
            )
            grid_charge = max(storage_year.charge_energy_kwh - allocation.to_storage, 0.0)

            # 11/12/13. 收益
            pv_self_use_revenue = rev.self_use_revenue(allocation.direct_use, tariff.avoided_electricity_price)
            pv_export_revenue = rev.export_revenue(allocation.export, tariff.export_price)
            pv_other_revenue = 0.0

            storage_arbitrage = rev.storage_arbitrage_revenue(
                discharge_energy_kwh=storage_year.discharge_energy_kwh,
                discharge_avoided_price=discharge_avoided_price,
                charge_energy_kwh=storage_year.charge_energy_kwh,
                charge_price=storage_charge_price,
            )
            storage_capacity = float(project.storage.annual_capacity_revenue) if ptype.has_storage else 0.0
            storage_ancillary = float(project.storage.annual_ancillary_revenue) if ptype.has_storage else 0.0
            storage_other = float(project.storage.annual_other_revenue) if ptype.has_storage else 0.0

            # 14. 总收益
            total_revenue = (
                pv_self_use_revenue
                + pv_export_revenue
                + pv_other_revenue
                + storage_arbitrage
                + storage_capacity
                + storage_ancillary
                + storage_other
            )

            # 14b. V2 时序口径覆盖（V2 §62；year_override 为 None 时 V1 路径逐位不变）
            #      位置必须在税与现金流之前：revenue_net / EBITDA / 税费都取自 total_revenue。
            v2 = year_override.get(year) if year_override else None
            if v2 is not None:
                load = float(v2.load)
                pv_self_use_revenue = float(v2.pv_self_use_revenue)
                pv_export_revenue = float(v2.pv_export_revenue)
                storage_arbitrage = float(v2.storage_arbitrage_revenue)
                storage_capacity = float(v2.storage_capacity_revenue)
                storage_ancillary = float(v2.storage_ancillary_revenue)
                storage_other = float(v2.storage_other_revenue)
                total_revenue = float(v2.total_revenue())

            # 16. OPEX
            opex_year = opx.opex_for_year(first_year_opex, float(o.annual_opex_growth_rate), year)

            # 17. 折旧
            depreciation = taxmod.depreciation_for_year(
                depreciable_capex,
                residual_value_ratio,
                depreciation_years,
                year,
                project.tax.depreciation_method.value,
            )

            # 18. 利息
            loan_year = loan_schedule[year - 1]

            # 19. 税
            revenue_net = taxmod.revenue_net(
                total_revenue, float(project.tax.vat_rate), bool(project.tax.revenue_is_vat_inclusive)
            )
            tax_year = taxmod.tax_year_result(
                year=year,
                revenue_net_amount=revenue_net,
                opex=opex_year,
                depreciation=depreciation,
                interest=loan_year.interest,
                income_tax_rate=float(project.tax.income_tax_rate),
                surcharge_rate=float(project.tax.surcharge_rate),
                other_tax_rate=float(project.tax.other_tax_rate),
            )

            # 投资与残值
            # 光伏设备更换（如逆变器第 N 年更换一次）与储能更换同口径计入项目现金流
            pv_replacement = (
                float(project.pv.replacement_cost_per_kwp) * pv_capacity
                if (ptype.has_pv and project.pv.replacement_year == year)
                else 0.0
            )
            replacement_capex = pv_replacement + (
                float(project.storage.replacement_capex)
                if (ptype.has_storage and storage_year.is_replacement_year)
                else 0.0
            )
            residual = terminal_residual if year == n else 0.0

            # 20. 项目现金流
            project_cf = cf.project_cashflow(
                ebitda=tax_year.ebitda,
                cash_tax=tax_year.cash_tax,
                capex=0.0,
                replacement_capex=replacement_capex,
                residual_value=residual,
            )
            # 21. 资本金现金流
            equity_cf = cf.equity_cashflow(
                ebitda=tax_year.ebitda,
                cash_tax=tax_year.cash_tax,
                interest=loan_year.interest,
                principal_repayment=loan_year.principal_repayment,
                equity_capex=0.0,
                replacement_capex=replacement_capex,
                debt_drawdown=0.0,
                residual_value=residual,
            )

            project_flows.append(project_cf)
            equity_flows.append(equity_cf)
            lifecycle_net_profit += tax_year.ebitda - tax_year.cash_tax

            cfads = fm.cfads_of(tax_year.ebitda, tax_year.cash_tax, replacement_capex)
            debt_service = loan_year.debt_service

            annual.append(
                AnnualResult(
                    year=year,
                    load_kwh=load,
                    pv_generation_kwh=v2.pv_generation if v2 else allocation.generation,
                    pv_self_use_kwh=v2.pv_self_use if v2 else allocation.direct_use,
                    pv_export_kwh=v2.pv_export if v2 else allocation.export,
                    pv_to_storage_kwh=v2.pv_to_storage if v2 else allocation.to_storage,
                    pv_loss_kwh=(
                        # V2 覆盖分支：损耗必须与同源的电量口径一致，否则
                        # ``发电量 = 自用 + 进储能 + 上网 + 损耗`` 不成立，
                        # validate_energy_balance 会把**合法**的时序项目判为不守恒。
                        # V2 的分派已保证光伏电量分配闭合，因此残差即该年度的限发电量
                        # （若储能允许上网且上网量大于限发量，残差可为负——此时报告层应
                        # 结合 8760 明细判读，不得据此判定计算失败）。
                        float(v2.pv_generation)
                        - (
                            float(v2.pv_self_use)
                            + float(v2.pv_to_storage)
                            + float(v2.pv_export)
                        )
                        if v2 is not None
                        else allocation.loss
                    ),
                    storage_available_kwh=storage_year.available_energy_kwh,
                    storage_charge_kwh=v2.storage_charge if v2 else storage_year.charge_energy_kwh,
                    storage_discharge_kwh=(
                        v2.storage_discharge if v2 else storage_year.discharge_energy_kwh
                    ),
                    storage_grid_charge_kwh=v2.grid_charge if v2 else grid_charge,
                    pv_self_use_revenue=pv_self_use_revenue,
                    pv_export_revenue=pv_export_revenue,
                    pv_other_revenue=pv_other_revenue,
                    storage_arbitrage_revenue=storage_arbitrage,
                    storage_capacity_revenue=storage_capacity,
                    storage_ancillary_revenue=storage_ancillary,
                    storage_other_revenue=storage_other,
                    total_revenue=total_revenue,
                    revenue_net=revenue_net,
                    opex=opex_year,
                    depreciation=depreciation,
                    ebitda=tax_year.ebitda,
                    ebit=tax_year.ebit,
                    interest=loan_year.interest,
                    ebt=tax_year.ebt,
                    taxable_income=tax_year.taxable_income,
                    income_tax=tax_year.income_tax,
                    surcharge=tax_year.surcharge,
                    other_tax=tax_year.other_tax,
                    cash_tax=tax_year.cash_tax,
                    capex=0.0,
                    replacement_capex=replacement_capex,
                    residual_value=residual,
                    project_cashflow=project_cf,
                    equity_cashflow=equity_cf,
                    debt_begin=loan_year.debt_begin,
                    debt_drawdown=loan_year.drawdown,
                    principal_repayment=loan_year.principal_repayment,
                    debt_end=loan_year.debt_end,
                    cfads=cfads,
                    debt_service=debt_service,
                    dscr=(cfads / debt_service) if debt_service > 0 else None,
                )
            )

            # LCOE / LCOS 的成本与电量序列
            # LCOE 的成本口径必须与现金流中的运维成本一致：除储能专项费用外的全部年运营成本
            # （光伏运维费 + 屋顶租金 + 保险费 + 管理费 + 其他费用）都计入光伏 LCOE 成本。
            # 早期版本只取 pv_opex + roof_rent，导致 LCOE 与报表"运维成本"口径不一致。
            pv_opex_first_year = (
                opex_breakdown.pv_opex
                + opex_breakdown.roof_rent
                + opex_breakdown.insurance
                + opex_breakdown.management_cost
                + opex_breakdown.other_opex
            )
            pv_costs.append(
                pv_opex_first_year * (1.0 + opex_breakdown.growth_rate) ** (year - 1)
                + pv_replacement
            )
            pv_energies.append(generation)
            storage_costs.append(
                opex_breakdown.storage_opex * (1.0 + opex_breakdown.growth_rate) ** (year - 1) + replacement_capex
            )
            storage_energies.append(storage_year.discharge_energy_kwh)

        # --- 30. Validate Energy Balance ---
        validator.validate_energy_balance(annual)
        validator.validate_storage_balance(annual)

        # --- 22/23/24. IRR / NPV / Payback ---
        project_irr = fm.irr(project_flows)
        equity_irr = fm.irr(equity_flows)

        # --- 累计现金流 ---
        cumulative_project = cf.cumulative(project_flows)
        cumulative_equity = cf.cumulative(equity_flows)
        for idx, row in enumerate(annual):
            row.cumulative_project_cashflow = cumulative_project[idx + 1]
            row.cumulative_equity_cashflow = cumulative_equity[idx + 1]

        # --- 25/26. LCOE / LCOS ---
        # 残值抵减（可选，规范 §75）：残值按光伏/储能投资比例分摊，记在第 N 年，
        # 由折现因子自然换算为现值（与招标文件公式中的 VR/(1+i)^N 等价）。
        if bool(project.tax.lcoe_residual_credit):
            pv_costs[-1] -= terminal_residual * (1.0 - storage_share)
            if ptype.has_storage:
                storage_costs[-1] -= terminal_residual * storage_share

        lcoe_value = fm.lcoe(rate, pv_costs, pv_energies) if ptype.has_pv else None
        lcos_value = fm.lcos(rate, storage_costs, storage_energies) if ptype.has_storage else None

        # --- 27. DSCR ---
        min_dscr = fm.minimum_dscr([r.cfads for r in annual], [r.debt_service for r in annual])

        # --- 31. Generate CalculationResult ---
        first_year_revenue = annual[0].total_revenue if annual else 0.0
        annual_revenue_avg = sum(r.total_revenue for r in annual) / n if n else 0.0
        annual_opex_avg = sum(r.opex for r in annual) / n if n else 0.0

        result = CalculationResult(
            project_name=project.basic_info.project_name,
            project_type=ptype.value,
            province=project.basic_info.province,
            city=project.basic_info.city,
            pv_capacity_kwp=pv_capacity,
            storage_power_kw=storage_power,
            storage_energy_kwh=storage_energy,
            storage_duration_hours=storage_hours,
            analysis_period=n,
            total_capex=total_capex,
            unit_investment=inv.unit_investment(total_capex, pv_capacity, storage_energy),
            capex_breakdown={
                "光伏投资": capex.pv_capex,
                "储能投资": capex.storage_capex,
                "并网投资": capex.grid_connection_cost,
                "屋顶费用": capex.roof_cost,
                "开发费用": capex.development_cost,
                "工程费用": capex.engineering_cost,
                "施工费用": capex.construction_cost,
                "其他投资": capex.other_capex,
                "预备费": capex.contingency,
            },
            loan_amount=loan,
            equity_amount=equity,
            first_year_generation=annual[0].pv_generation_kwh if annual else 0.0,
            first_year_self_use_energy=annual[0].pv_self_use_kwh if annual else 0.0,
            first_year_export_energy=annual[0].pv_export_kwh if annual else 0.0,
            first_year_revenue=first_year_revenue,
            first_year_opex=annual[0].opex if annual else 0.0,
            first_year_cashflow=project_flows[1] if len(project_flows) > 1 else 0.0,
            annual_revenue=annual_revenue_avg,
            annual_opex=annual_opex_avg,
            project_irr=project_irr,
            equity_irr=equity_irr,
            project_npv=fm.npv(rate, project_flows),
            equity_npv=fm.npv(rate, equity_flows),
            static_payback=fm.payback_period(project_flows),
            discounted_payback=fm.discounted_payback_period(rate, project_flows),
            lcoe=lcoe_value,
            lcos=lcos_value,
            roi=fm.roi(lifecycle_net_profit, total_capex),
            min_dscr=min_dscr,
            project_cashflows=project_flows,
            equity_cashflows=equity_flows,
            cumulative_cashflow=cumulative_project,
            annual_results=annual,
            parameter_sources=registry.to_dict(),
            notes=list(_CALIBER_NOTES)
            + [
                f"电价解析口径：{tariff.basis}；替代电价 {tariff.avoided_electricity_price:.6f} 元/kWh，"
                f"充电电价 {storage_charge_price:.6f} 元/kWh，上网电价 {tariff.export_price:.6f} 元/kWh。",
                f"光伏容量取值依据：{'用户直接输入' if capacity_basis == 'INPUT' else ('按屋顶面积换算' if capacity_basis == 'AREA' else '不适用')}。",
                f"储能往返效率：{round_trip:.6f}（充电 {charge_eff:.6f} × 放电 {discharge_eff:.6f}）。",
                f"折现率：{rate:.6f}；计算期：{n} 年。",
            ],
        )
        if project.policy is not None and project.policy.policy_name:
            result.notes.append(f"本测算采用政策：{project.policy.display_version}（来源：{project.policy.source}）。")
        return result

    # ------------------------------------------------------------------ #
    # 情景与敏感性（§92、§95）
    # ------------------------------------------------------------------ #
    def _run_scenarios(self, project: Project) -> list[ScenarioSummary]:
        """情景分析：BASE / CONSERVATIVE / OPTIMISTIC，全部从 BASE 复制（§93）。"""
        out: list[ScenarioSummary] = []
        for scenario in (ScenarioType.BASE, ScenarioType.CONSERVATIVE, ScenarioType.OPTIMISTIC):
            delta = delta_for(project, scenario)
            p2 = apply_delta(project, delta)
            r2 = self._calculate_core(p2)
            out.append(
                ScenarioSummary(
                    scenario=scenario.value,
                    label=scenario.label,
                    total_capex=r2.total_capex,
                    first_year_revenue=r2.first_year_revenue,
                    project_irr=r2.project_irr,
                    equity_irr=r2.equity_irr,
                    project_npv=r2.project_npv,
                    static_payback=r2.static_payback,
                    deltas=delta.describe(),
                )
            )
        return out

    def _run_sensitivity(self, project: Project, base: CalculationResult) -> list[SensitivityRow]:
        """敏感性分析：一次只改变一个参数（§95），输出 IRR / NPV / 回收期（§96）。"""
        rows: list[SensitivityRow] = []
        for variable in applicable_variables(project):
            for change in project.sensitivity.steps:
                p2 = apply_variable(project, variable, float(change))
                r2 = self._calculate_core(p2)
                irr_change: float | None = None
                coefficient: float | None = None
                if base.project_irr not in (None, 0.0) and r2.project_irr is not None:
                    irr_change = (r2.project_irr - base.project_irr) / base.project_irr
                    if abs(float(change)) > 1e-12:
                        coefficient = irr_change / float(change)
                rows.append(
                    SensitivityRow(
                        variable=variable.value,
                        variable_label=variable.label,
                        change=float(change),
                        project_irr=r2.project_irr,
                        equity_irr=r2.equity_irr,
                        project_npv=r2.project_npv,
                        static_payback=r2.static_payback,
                        irr_change=irr_change,
                        coefficient=coefficient,
                    )
                )
        return rows

    # ------------------------------------------------------------------ #
    # 参数来源登记（§83）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _register_parameters(project: Project, registry: ParameterRegistry) -> None:
        """登记重要参数的来源（规范 §83、§84、§91）。"""
        policy = project.policy
        policy_source = policy.source if policy else ""
        policy_url = policy.source_url if policy else ""

        # 用户输入
        for key, value, unit in (
            ("analysis_period", project.analysis_period, "年"),
            ("discount_rate", project.discount_rate, "小数"),
            ("load.annual_load_kwh", project.load.annual_load_kwh, "kWh"),
            ("load.annual_load_growth_rate", project.load.annual_load_growth_rate, "小数"),
            ("pv.pv_capacity_kwp", project.pv.pv_capacity_kwp, "kWp"),
            ("pv.equivalent_hours", project.pv.equivalent_hours, "h"),
            ("pv.performance_ratio", project.pv.performance_ratio, "小数"),
            ("pv.annual_degradation_rate", project.pv.annual_degradation_rate, "小数"),
            ("pv.curtailment_rate", project.pv.curtailment_rate, "小数"),
            ("pv.self_consumption_ratio", project.pv.self_consumption_ratio, "小数"),
            ("storage.storage_power_kw", project.storage.storage_power_kw, "kW"),
            ("storage.storage_energy_kwh", project.storage.storage_energy_kwh, "kWh"),
            ("storage.annual_cycles", project.storage.annual_cycles, "次/年"),
            ("storage.depth_of_discharge", project.storage.depth_of_discharge, "小数"),
            ("storage.annual_degradation_rate", project.storage.annual_degradation_rate, "小数"),
            ("investment.pv_capex_per_kw", project.investment.pv_capex_per_kw, "元/kWp"),
            ("investment.storage_capex_per_kwh", project.investment.storage_capex_per_kwh, "元/kWh"),
            ("tax.vat_rate", project.tax.vat_rate, "小数"),
            ("tax.income_tax_rate", project.tax.income_tax_rate, "小数"),
            ("tax.depreciation_years", project.tax.depreciation_years, "年"),
            ("tax.residual_value_ratio", project.tax.residual_value_ratio, "小数"),
            ("financing.debt_ratio", project.financing.debt_ratio, "小数"),
            ("financing.interest_rate", project.financing.interest_rate, "小数"),
            ("financing.loan_term", project.financing.loan_term, "年"),
        ):
            registry.register_value(key, value, unit=unit, source_type=SourceType.USER_INPUT)

        # 政策参数
        if policy is not None:
            for key, value, unit in (
                ("policy.market_price", policy.market_price, "元/kWh"),
                ("policy.mechanism_price", policy.mechanism_price, "元/kWh"),
                ("policy.mechanism_volume_ratio", policy.mechanism_volume_ratio, "小数"),
                ("policy.green_energy_price", policy.green_energy_price, "元/kWh"),
                ("policy.green_environmental_value", policy.green_environmental_value, "元/kWh"),
            ):
                registry.register_value(
                    key,
                    value,
                    unit=unit,
                    source_type=SourceType.POLICY,
                    source_name=policy.source,
                    source_url=policy.source_url,
                    note=policy.display_version,
                )

        # 系统默认/假设值
        registry.register_value(
            "scenario.conservative",
            "、".join(project.scenario.conservative.describe()) or "全 1",
            source_type=SourceType.ASSUMPTION,
            is_assumption=True,
            note="情景乘数为系统默认假设值，可在情景设置中修改",
        )
        registry.register_value(
            "scenario.optimistic",
            "、".join(project.scenario.optimistic.describe()) or "全 1",
            source_type=SourceType.ASSUMPTION,
            is_assumption=True,
            note="情景乘数为系统默认假设值，可在情景设置中修改",
        )
        registry.register_value(
            "sensitivity.steps",
            "、".join(f"{s:+.0%}" for s in project.sensitivity.steps),
            source_type=SourceType.SYSTEM_DEFAULT,
            note="敏感性分析步长",
        )
        if policy is not None:
            registry.register_value(
                "policy.version",
                policy.display_version,
                source_type=SourceType.POLICY,
                source_name=policy.source,
                source_url=policy.source_url,
                note=policy_url,
            )

        # 写入项目对象，便于 .nep 保存与报告"参数来源"表
        project.parameter_registry = registry.items()


#: 全局唯一计算引擎实例（规范 §147）
calculation_engine = CalculationEngine()


def calculate(project: Project, **kwargs) -> CalculationResult:
    """模块级快捷函数，等价于 ``calculation_engine.calculate(project)``。"""
    return calculation_engine.calculate(project, **kwargs)


__all__ = ["CalculationEngine", "calculation_engine", "calculate", "SensitivityVariable"]
