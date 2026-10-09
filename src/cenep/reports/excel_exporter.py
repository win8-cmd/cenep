"""Excel 导出（规范 §108、§109、§148、§154；V2.1 §8.1）。

铁律
----
**数据全部来自 :class:`CalculationResult`、:class:`Project` 的账单段与账单服务
（``BillService``，只取它已经算好的核对结果），本模块不做任何计算。**
可以在表内展示公式文本，但数值必须与软件计算结果一致（规范 §109）。

V1 的 13 张工作表（规范 §108）：

1. 项目概况  2. 基础参数  3. 技术参数  4. 电价参数  5. 投资参数  6. 运维参数
7. 融资参数  8. 年度现金流  9. 财务指标  10. 敏感性分析  11. 情景分析
12. 政策依据  13. 参数来源

V2 起为 **27 张**：V1 的 13 张全部保留且相对顺序不变，V2 §67 新增 11 张时序表，
V2.1 §8.1 再新增「账单原始数据」「账单校验」两张，V2.2 §6.3 再新增「消纳率分析」一张。

V2.4 §8.1（阶段 7）再**追加** 5 张：

28. 月度电费分析  29. 负荷数据质量  30. 电价版本与来源  31. 方案电费对比
32. 计算假设与警告

追加方式与 V2.1 / V2.2 完全一致：**只新增、不删改**，既有的 27 张表名、顺序与内容一字未动，
新表在无对应数据时输出中文说明（「不缺表、不报错、不臆造数值」）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..application.bill_service import BillService
from ..application.scenario_service import ScenarioBillService
from ..application.tariff_service import TariffService
from ..domain.bill_models import BILL_FIELD_LABELS, label_of
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.migration import (
    BILL_SECTION_SCHEMA_VERSION,
    CALCULATION_ENGINE_VERSION,
    CURRENT_SCHEMA_VERSION,
)

logger = get_logger()

#: 工作表清单。
#:
#: V1 的 13 张（§108）**全部保留且相对顺序不变**；V2 按 §67「至少包含」新增 11 张时序相关表
#: （储能与调度、负荷曲线、光伏曲线、分时电价、8760时序仿真、能量平衡、年度汇总、
#: 收益分解、方案比较、方案寻优、数据质量）；V2.1 §8.1 再新增 2 张账单表
#: （账单原始数据、账单校验）；V2.2 §6.3 再新增 1 张消纳分析表（消纳率分析），共 **27 张**。
#: 这是 V2/V2.1/V2.2 对 V1 §108 的**正当超集扩展**，不是破坏性变更：
#: 新表在无对应数据时输出中文说明（«本项目未启用时序仿真» / «本项目尚未录入电费账单» /
#: «尚未执行负荷与消纳分析»），V1 项目、无账单项目与无负荷项目仍可正常导出。
SHEET_NAMES = [
    # —— V1 原有（映射 V2 §67 的 Overview / Project / Base Parameters / Storage ——）
    "项目概况",
    "基础参数",
    "技术参数",
    # —— V2 新增（Dispatch Config / Load Profile / PV Profile）——
    "储能与调度",
    "负荷曲线",
    "光伏曲线",
    # —— V1 原有 ——
    "电价参数",
    # —— V2 新增（Tariff Profile 的时段规则部分）——
    "分时电价",
    # —— V1 原有 ——
    "投资参数",
    "运维参数",
    "融资参数",
    # —— V2 新增（Hourly Simulation / Energy Balance / Annual Summary / Revenue）——
    "8760时序仿真",
    "能量平衡",
    "年度汇总",
    "收益分解",
    # —— V1 原有 ——
    "年度现金流",
    "财务指标",
    "敏感性分析",
    "情景分析",
    # —— V2 新增（Scenario / Optimization / Data Quality）——
    "方案比较",
    "方案寻优",
    "数据质量",
    # —— V2.1 新增（§8.1）——
    "账单原始数据",
    "账单校验",
    # —— V2.2 阶段 4 新增（§6.3 C、§3.3）——
    "消纳率分析",
    # —— V1 原有 ——
    "政策依据",
    "参数来源",
    # —— V2.4 阶段 7 新增（§8.1：追加式扩展，既有 27 张一字未动）——
    "月度电费分析",
    "负荷数据质量",
    "电价版本与来源",
    "方案电费对比",
    "计算假设与警告",
]

# --------------------------------------------------------------------------- #
# V2.4 §8.1（阶段 7）：四类数据口径的**统一图例**
#
# 规格书 §8.1 硬要求：「报告必须区分：实际账单、软件复算、模型估算、方案模拟。
# 不能用同一颜色/标题混为一谈。」因此这里定义一处权威文案，Excel 与 PDF 同源引用。
# --------------------------------------------------------------------------- #
CALIBER_LEGEND: tuple[tuple[str, str], ...] = (
    (
        "① 实际账单",
        "用户手动录入或从账单 Excel 导入的**账单事实**，未经任何软件加工；"
        "缺失字段显示「账单未提供」，**不按 0 计入**（V2.1 §2.1）。",
    ),
    (
        "② 软件复算",
        "按用户核验过的电价计划与电量逐时/逐月重算的金额，**只用于与①比较**；"
        "差异超容差即标注「未通过账单校准」（V2.3 §7.4）。",
    ),
    (
        "③ 模型估算",
        "由月电量 + 可编辑典型负荷模板生成的估算曲线及据此得到的消纳率，"
        "**不是实测**；来源标签一律带「估算」（V2.2 §0.2、§6.5）。",
    ),
    (
        "④ 方案模拟",
        "在相同负荷与相同电价计划下，对无光伏 / 仅光伏 / 仅储能 / 光储四场景的模拟账单"
        "（V2.3 §7.1）；与①的差额是**唯一**收益入口（V2.3 §7.6）。",
    ),
)

#: 表格里用来标注口径的短标签（避免用户把四类数据看成一类）
CALIBER_TAG_ACTUAL = "实际账单"
CALIBER_TAG_RECOMPUTED = "软件复算"
CALIBER_TAG_ESTIMATED = "模型估算"
CALIBER_TAG_SIMULATED = "方案模拟"

_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_TITLE_FONT = Font(bold=True, size=14)
_HEADER_FONT = Font(bold=True)
_BOLD = Font(bold=True)
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

#: 参数配色对应的填充色（规范 §144）：蓝=用户输入，绿=系统计算，黄=假设，灰=政策模板
SOURCE_FILL = {
    "USER_INPUT": PatternFill("solid", fgColor="DDEBF7"),
    "CALCULATED": PatternFill("solid", fgColor="E2EFDA"),
    "ASSUMPTION": PatternFill("solid", fgColor="FFF2CC"),
    "SYSTEM_DEFAULT": PatternFill("solid", fgColor="FFF2CC"),
    "EXPERIENCE": PatternFill("solid", fgColor="FFF2CC"),
    "POLICY": PatternFill("solid", fgColor="EDEDED"),
    "CONTRACT": PatternFill("solid", fgColor="DDEBF7"),
    "HISTORICAL": PatternFill("solid", fgColor="DDEBF7"),
}

_MONEY = "#,##0.00"
_MONEY0 = "#,##0"
_PCT = "0.00%"
_NUM4 = "#,##0.0000"


def _write_title(ws, title: str, ncols: int) -> int:
    ws.cell(row=1, column=1, value=title).font = _TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(ncols, 2))
    return 3


def _write_header(ws, row: int, headers: list[str]) -> None:
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _write_header_at(ws, row: int, headers: list[str]) -> int:
    """写表头并**返回下一行行号**。

    ``_write_header`` 本身返回 ``None``（V1 既有签名，不改动以免影响既有调用），
    因此凡是要接着往下写的场景用本函数，避免 ``row = _write_header(...)`` 把行号写成 None。
    """
    _write_header(ws, row, headers)
    return row + 1


def _write_rows(ws, start_row: int, rows: list[list], formats: dict[int, str] | None = None) -> int:
    formats = formats or {}
    row = start_row
    for data in rows:
        for col, value in enumerate(data, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = _BORDER
            if col in formats and isinstance(value, (int, float)):
                cell.number_format = formats[col]
        row += 1
    return row


def _auto_width(ws, max_width: int = 42) -> None:
    for column in ws.columns:
        length = 0
        letter = get_column_letter(column[0].column)
        for cell in column:
            if cell.value is not None:
                length = max(length, len(str(cell.value)))
        ws.column_dimensions[letter].width = min(max(length + 4, 10), max_width)


def _fmt(value: float | None, digits: int = 4, suffix: str = "") -> str:
    if value is None:
        return "无法计算"
    return f"{value:.{digits}f}{suffix}"


# --------------------------------------------------------------------------- #
# 各表内容
# --------------------------------------------------------------------------- #
def _sheet_overview(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("项目概况")
    row = _write_title(ws, "项目概况", 4)
    info = project.basic_info
    rows = [
        ["项目名称", info.project_name],
        ["项目类型", info.project_type.label],
        ["省份", info.province],
        ["城市", info.city],
        ["业主名称", info.customer_name or ""],
        ["所属行业", info.industry or ""],
        ["评价日期", info.evaluation_date.isoformat()],
        ["备注", info.notes or ""],
        ["", ""],
        ["装机规模", ""],
        ["光伏装机容量（kWp）", result.pv_capacity_kwp],
        ["储能功率（kW）", result.storage_power_kw],
        ["储能容量（kWh）", result.storage_energy_kwh],
        ["储能时长（h）", result.storage_duration_hours],
        ["计算期（年）", result.analysis_period],
        ["", ""],
        ["总投资（元）", result.total_capex],
        ["其中：贷款金额（元）", result.loan_amount],
        ["其中：资本金（元）", result.equity_amount],
        ["", ""],
        ["导出时间", datetime.now().isoformat(timespec="seconds")],
    ]
    _write_rows(ws, row, rows, formats={2: _MONEY})
    for r in range(row, row + len(rows)):
        ws.cell(row=r, column=1).font = _BOLD
    _auto_width(ws)


def _sheet_basic(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("基础参数")
    row = _write_title(ws, "基础参数", 3)
    _write_header(ws, row, ["参数", "取值", "单位"])
    t = project.tax
    data = [
        ["项目生命周期", project.analysis_period, "年"],
        ["折现率", project.discount_rate, "小数"],
        ["折旧年限", t.depreciation_years, "年"],
        ["折旧方法", t.depreciation_method.label, ""],
        ["残值率", t.residual_value_ratio, "小数"],
        ["可折旧投资占比", t.depreciable_capex_ratio, "小数"],
        ["增值税率", t.vat_rate, "小数"],
        ["所得税率", t.income_tax_rate, "小数"],
        ["附加税费率", t.surcharge_rate, "小数"],
        ["其他税费率", t.other_tax_rate, "小数"],
        ["收入为含税口径", "是" if t.revenue_is_vat_inclusive else "否", ""],
        ["LCOE 增值税抵扣比例", t.lcoe_vat_deductible_ratio, "小数"],
        ["LCOE 抵减残值现值", "是" if t.lcoe_residual_credit else "否", ""],
        ["年用电量", project.load.annual_load_kwh, "kWh"],
        ["年工作天数", project.load.working_days, "天"],
        ["白天负荷占比", project.load.daytime_load_ratio, "小数"],
        ["夜间负荷占比", project.load.nighttime_load_ratio, "小数"],
        ["年用电量增长率", project.load.annual_load_growth_rate, "小数"],
        ["首年光伏发电量", result.first_year_generation, "kWh"],
        ["首年自用电量", result.first_year_self_use_energy, "kWh"],
        ["首年上网电量", result.first_year_export_energy, "kWh"],
        ["首年收入", result.first_year_revenue, "元"],
        ["首年运维费", result.first_year_opex, "元"],
        ["经营期年均收入", result.annual_revenue, "元"],
        ["经营期年均运维费", result.annual_opex, "元"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_technical(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("技术参数")
    row = _write_title(ws, "技术参数", 3)
    _write_header(ws, row, ["参数", "取值", "单位"])
    p, s = project.pv, project.storage
    data = [
        ["光伏：直接输入容量", p.pv_capacity_kwp or "", "kWp"],
        ["光伏：屋顶总面积", p.roof_area_m2, "m²"],
        ["光伏：可利用屋顶面积", p.usable_roof_area_m2, "m²"],
        ["光伏：单位容量占用面积", p.area_per_kwp, "m²/kWp"],
        ["光伏：年等效利用小时", p.equivalent_hours, "h"],
        ["光伏：性能比", p.performance_ratio, "小数"],
        ["光伏：年衰减率", p.annual_degradation_rate, "小数"],
        ["光伏：限电率", p.curtailment_rate, "小数"],
        ["光伏：自发自用比例", p.self_consumption_ratio, "小数"],
        ["光伏：设备更换年份", p.replacement_year or "不更换", "年"],
        ["光伏：设备更换单价", p.replacement_cost_per_kwp, "元/kWp"],
        ["光伏：设备更换支出（计算）", p.replacement_cost_per_kwp * (p.pv_capacity_kwp or 0.0), "元"],
        ["储能：功率", s.storage_power_kw, "kW"],
        ["储能：容量", s.storage_energy_kwh, "kWh"],
        ["储能：时长（计算）", result.storage_duration_hours, "h"],
        ["储能：年循环次数", s.annual_cycles, "次/年"],
        ["储能：放电深度 DoD", s.depth_of_discharge, "小数"],
        ["储能：年衰减率", s.annual_degradation_rate, "小数"],
        ["储能：更换电芯年份", s.replacement_year or "", "年"],
        ["储能：充电电价", s.charge_price if s.charge_price is not None else "按电价参数推导", "元/kWh"],
        ["储能：放电替代电价", s.discharge_avoided_price if s.discharge_avoided_price is not None else "按电价参数推导", "元/kWh"],
        ["储能：年容量收益", s.annual_capacity_revenue, "元"],
        ["储能：年辅助服务收益", s.annual_ancillary_revenue, "元"],
        ["储能：年其他收益", s.annual_other_revenue, "元"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_tariff(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("电价参数")
    row = _write_title(ws, "电价参数", 3)
    _write_header(ws, row, ["参数", "取值", "单位"])
    t = project.tariff
    data = [
        ["电价模式", t.tariff_mode.label, ""],
        ["固定电价", t.average_price, "元/kWh"],
        ["峰电价", t.peak_price, "元/kWh"],
        ["平电价", t.flat_price, "元/kWh"],
        ["谷电价", t.valley_price, "元/kWh"],
        ["峰电量比例", t.peak_ratio, "小数"],
        ["平电量比例", t.flat_ratio, "小数"],
        ["谷电量比例", t.valley_ratio, "小数"],
        ["市场电价", t.market_price, "元/kWh"],
        ["余电上网电价", t.export_price, "元/kWh"],
        ["绿电价格", t.green_energy_price, "元/kWh"],
        ["绿色环境价值", t.green_environmental_value, "元/kWh"],
        ["替代电价（用户覆盖）", t.avoided_price_override if t.avoided_price_override is not None else "未覆盖", "元/kWh"],
        ["充电电价（用户覆盖）", t.charge_price_override if t.charge_price_override is not None else "未覆盖", "元/kWh"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_investment(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("投资参数")
    row = _write_title(ws, "投资参数", 3)
    _write_header(ws, row, ["项目", "金额（元）", "说明"])
    data = [[k, v, ""] for k, v in result.capex_breakdown.items()]
    data.append(["总投资", result.total_capex, "各项之和"])
    data.append(["光伏单位投资（元/W）", result.unit_investment.get("yuan_per_w", 0.0), "总投资 ÷ 光伏容量"])
    data.append(["储能单位投资（元/Wh）", result.unit_investment.get("yuan_per_wh", 0.0), "总投资 ÷ 储能容量"])
    data.append(["投资模式", project.investment.mode.value, "UNIT_PRICE 或 DETAILED"])
    _write_rows(ws, row + 1, data, formats={2: _MONEY})
    ws.cell(row=row + len(data), column=1).font = _BOLD
    _auto_width(ws)


def _sheet_opex(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("运维参数")
    row = _write_title(ws, "运维参数", 3)
    _write_header(ws, row, ["参数", "取值", "单位"])
    o = project.opex
    first = result.annual_results[0] if result.annual_results else None
    data = [
        ["光伏运维（输入值）", o.pv_opex, "按模式"],
        ["光伏运维模式", o.pv_opex_mode.value, ""],
        ["储能运维（输入值）", o.storage_opex, "按模式"],
        ["储能运维模式", o.storage_opex_mode.value, ""],
        ["保险费（输入值）", o.insurance, "按模式"],
        ["管理费", o.management_cost, "元/年"],
        ["其他费用", o.other_opex, "元/年"],
        ["屋顶租金模式", o.roof_rent_mode.value, ""],
        ["屋顶租金单价", o.rent_per_m2, "元/m²"],
        ["屋顶容量租金单价", o.rent_per_kw, "元/kWp"],
        ["屋顶固定租金", o.annual_fixed_rent, "元/年"],
        ["运维费用年增长率", o.annual_opex_growth_rate, "小数"],
        ["首年运维费合计（计算）", result.first_year_opex, "元"],
        ["首年折旧（计算）", first.depreciation if first else 0.0, "元"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_financing(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("融资参数")
    row = _write_title(ws, "融资参数", 3)
    _write_header(ws, row, ["参数", "取值", "单位"])
    f = project.financing
    data = [
        ["是否融资", "是" if f.enabled else "否", ""],
        ["贷款比例", f.debt_ratio, "小数"],
        ["资本金比例", f.equity_ratio, "小数"],
        ["贷款利率", f.interest_rate, "小数"],
        ["贷款期限", f.loan_term, "年"],
        ["宽限期", f.grace_period, "年"],
        ["还款方式", f.repayment_method.label, ""],
        ["贷款金额（计算）", result.loan_amount, "元"],
        ["资本金金额（计算）", result.equity_amount, "元"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_cashflow(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("年度现金流")
    row = _write_title(ws, "年度现金流", 8)
    headers = [
        "年份",
        "负荷（kWh）",
        "发电量（kWh）",
        "自用电量（kWh）",
        "上网电量（kWh）",
        "储能放电量（kWh）",
        "总收入（元）",
        "运维费（元）",
        "折旧（元）",
        "EBITDA（元）",
        "利息（元）",
        "税费（元）",
        "项目现金流（元）",
        "资本金现金流（元）",
        "累计项目现金流（元）",
    ]
    _write_header(ws, row, headers)
    data = [
        [
            r.year,
            r.load_kwh,
            r.pv_generation_kwh,
            r.pv_self_use_kwh,
            r.pv_export_kwh,
            r.storage_discharge_kwh,
            r.total_revenue,
            r.opex,
            r.depreciation,
            r.ebitda,
            r.interest,
            r.cash_tax,
            r.project_cashflow,
            r.equity_cashflow,
            r.cumulative_project_cashflow,
        ]
        for r in result.annual_results
    ]
    # Year 0 行（规范 §69）
    data.insert(
        0,
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, result.project_cashflows[0], result.equity_cashflows[0], result.cumulative_cashflow[0]],
    )
    formats = {c: (_MONEY0 if c in (1,) else _MONEY) for c in range(2, 16)}
    _write_rows(ws, row + 1, data, formats=formats)
    _auto_width(ws, max_width=20)
    ws.freeze_panes = ws.cell(row=row + 1, column=2).coordinate


def _sheet_metrics(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("财务指标")
    row = _write_title(ws, "财务指标", 3)
    _write_header(ws, row, ["指标", "数值", "说明"])
    data = [
        ["总投资（元）", result.total_capex, "各项投资之和"],
        ["首年发电量（kWh）", result.first_year_generation, "首年光伏发电量"],
        ["首年收入（元）", result.first_year_revenue, "含光伏与储能收益"],
        ["项目财务内部收益率（所得税后）", _fmt(result.project_irr, 6), "融资前现金流 IRR"],
        ["资本金财务内部收益率", _fmt(result.equity_irr, 6), "资本金现金流 IRR"],
        ["项目财务净现值（元）", result.project_npv, f"折现率 {project.discount_rate:.2%}"],
        ["资本金财务净现值（元）", result.equity_npv, f"折现率 {project.discount_rate:.2%}"],
        ["静态投资回收期（年）", _fmt(result.static_payback, 2), "自 Year 0 起算；未回收则显示“未回收”"],
        ["动态投资回收期（年）", _fmt(result.discounted_payback, 2), "自 Year 0 起算；未回收则显示“未回收”"],
        ["LCOE（元/kWh）", _fmt(result.lcoe, 4), "光伏口径，成本不含融资利息"],
        ["LCOS（元/kWh）", _fmt(result.lcos, 4), "储能口径"],
        ["ROI", _fmt(result.roi, 4), "生命周期累计净收益 ÷ 初始总投资"],
        ["最低 DSCR", _fmt(result.min_dscr, 4), "CFADS ÷ 还本付息"],
        ["生命周期累计净现金流（元）", result.cumulative_cashflow[-1] if result.cumulative_cashflow else 0.0, "项目现金流累计"],
    ]
    _write_rows(ws, row + 1, data, formats={2: _MONEY})
    ws.column_dimensions["B"].width = 22
    _auto_width(ws)


def _sheet_sensitivity(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("敏感性分析")
    row = _write_title(ws, "敏感性分析（一次只改变一个参数）", 7)
    _write_header(
        ws,
        row,
        ["变化因素", "变化率", "项目IRR", "资本金IRR", "项目NPV（元）", "静态回收期（年）", "敏感度系数"],
    )
    data = [
        [
            r.variable_label,
            r.change,
            r.project_irr if r.project_irr is not None else "无法计算",
            r.equity_irr if r.equity_irr is not None else "无法计算",
            r.project_npv,
            r.static_payback if r.static_payback is not None else "未回收",
            r.coefficient if r.coefficient is not None else "",
        ]
        for r in result.sensitivity
    ]
    _write_rows(ws, row + 1, data, formats={2: _PCT, 3: _PCT, 4: _PCT, 5: _MONEY, 6: _NUM4, 7: _NUM4})
    _auto_width(ws)
    ws.freeze_panes = ws.cell(row=row + 1, column=1).coordinate


def _sheet_scenario(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("情景分析")
    row = _write_title(ws, "情景分析（全部从基准情景复制）", 7)
    _write_header(
        ws,
        row,
        ["情景", "总投资（元）", "首年收入（元）", "项目IRR", "资本金IRR", "项目NPV（元）", "静态回收期（年）", "情景乘数"],
    )
    data = [
        [
            s.label,
            s.total_capex,
            s.first_year_revenue,
            s.project_irr if s.project_irr is not None else "无法计算",
            s.equity_irr if s.equity_irr is not None else "无法计算",
            s.project_npv,
            s.static_payback if s.static_payback is not None else "未回收",
            "、".join(s.deltas) if s.deltas else "基准",
        ]
        for s in result.scenarios
    ]
    _write_rows(ws, row + 1, data, formats={2: _MONEY, 3: _MONEY, 4: _PCT, 5: _PCT, 6: _MONEY, 7: _NUM4})
    _auto_width(ws)


def _sheet_policy(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("政策依据")
    row = _write_title(ws, "政策依据", 3)
    _write_header(ws, row, ["项目", "内容", ""])
    policy = project.policy
    if policy is None:
        data = [
            ["政策文件", "未关联政策 Profile", ""],
            ["提示", "本测算未采用内置政策参数，所有电价均为用户输入，请自行核对现行政策。", ""],
        ]
    else:
        data = [
            ["政策名称", policy.policy_name, ""],
            ["政策版本", policy.policy_version, ""],
            ["生效日期", policy.effective_date.isoformat() if policy.effective_date else "", ""],
            ["失效日期", policy.expiry_date.isoformat() if policy.expiry_date else "未标注", ""],
            ["适用范围（省份）", policy.province, ""],
            ["价格机制", policy.pricing_mechanism, ""],
            ["市场电价", policy.market_price, "元/kWh"],
            ["机制电价", policy.mechanism_price, "元/kWh"],
            ["机制电量比例", policy.mechanism_volume_ratio, "小数"],
            ["绿电价格", policy.green_energy_price, "元/kWh"],
            ["绿色环境价值", policy.green_environmental_value, "元/kWh"],
            ["来源", policy.source, ""],
            ["来源链接", policy.source_url, ""],
            ["备注", policy.notes, ""],
            ["报告披露", f"本测算采用政策：{policy.display_version}", ""],
        ]
    _write_rows(ws, row + 1, data, formats={2: _NUM4})
    _auto_width(ws, max_width=60)


def _sheet_sources(wb: Workbook, project: Project, result: CalculationResult) -> None:
    ws = wb.create_sheet("参数来源")
    row = _write_title(ws, "参数来源（规范 §83、§91）", 8)
    _write_header(
        ws,
        row,
        ["参数", "取值", "单位", "来源类型", "来源名称", "来源日期", "来源链接", "是否假设值/备注"],
    )
    data = []
    for key, meta in result.parameter_sources.items():
        data.append(
            [
                key,
                meta.get("value", ""),
                meta.get("unit", ""),
                meta.get("source_type_label", meta.get("source_type", "")),
                meta.get("source_name", ""),
                meta.get("source_date", ""),
                meta.get("source_url", ""),
                ("假设值；" if meta.get("is_assumption") else "") + str(meta.get("note", "")),
            ]
        )
    _write_rows(ws, row + 1, data)
    # 按来源类型着色（规范 §144）
    for idx, (key, meta) in enumerate(result.parameter_sources.items(), start=row + 1):
        fill = SOURCE_FILL.get(str(meta.get("source_type", "")))
        if fill is not None:
            ws.cell(row=idx, column=4).fill = fill
    _auto_width(ws, max_width=48)


# --------------------------------------------------------------------------- #
# V2 时序相关表（V2 §67）
#
# 设计要点：
# * 无 V2 时序结果时**照常生成**这些表，内容写"本项目未启用时序仿真"及切换方法，
#   保证 V1 项目也能导出（V1 兼容性承诺）；
# * 「8760 时序仿真」表**只写抽样**（每月 1 日 24 小时 + 夏冬各一个典型日），
#   不把 8760×25 列全写进去——否则工作簿会大到无法打开；
# * 全部写计算好的数值，**不写任何公式**（V1 §109、V2 §61）。
# --------------------------------------------------------------------------- #
#: 「8760 时序仿真」表的列定义（列名 → TimeSeriesResultSet 的列名）
_HOURLY_SHEET_COLUMNS: list[tuple[str, str]] = [
    ("负荷（kWh）", "load"),
    ("光伏发电（kWh）", "pv_generation"),
    ("光伏→负荷", "pv_to_load"),
    ("光伏→储能", "pv_to_storage"),
    ("光伏→上网", "pv_to_grid"),
    ("弃光", "pv_curtailed"),
    ("电网→负荷", "grid_to_load"),
    ("电网→储能", "grid_to_storage"),
    ("储能充电", "storage_charge"),
    ("储能放电", "storage_discharge"),
    ("SOC 期初", "storage_soc_start"),
    ("SOC 期末", "storage_soc_end"),
    ("储能→负荷", "load_from_storage"),
    ("购电量", "grid_import"),
    ("上网量", "grid_export"),
    ("购电电价", "electricity_price"),
    ("上网电价", "export_price"),
    ("电费（元）", "electricity_cost"),
    ("上网收入（元）", "export_revenue"),
    ("储能收益（元）", "storage_revenue"),
    ("总收益（元）", "total_revenue"),
    ("净电费（元）", "net_electricity_cost_placeholder"),
]


def _not_enabled_note(ws, row: int, title: str, extra: str = "") -> None:
    """时序未启用时的统一说明块（V2 §67：不得缺表或报错）。"""
    ws.cell(row=row, column=1, value="本项目未启用时序仿真。").font = _BOLD
    row += 1
    ws.cell(
        row,
        column=1,
        value="启用方法：在「参数 → 时序仿真」中打开开关，填写负荷曲线、光伏曲线与分时电价后重新计算。",
    )
    row += 1
    if extra:
        ws.cell(row, column=1, value=extra)
        row += 1
    ws.cell(row, column=1, value=f"（{title} 需要时序仿真结果；V1 年度模式不产生逐时数据。）")


def _sample_hourly_indices(rs) -> list[int]:
    """抽样下标：每月 1 日 24 小时 + 夏（7/15）冬（1/15）各一个典型日。"""
    picked = {i for i, ts in enumerate(rs.timestamps) if ts.day == 1}
    for month, day in ((1, 15), (7, 15)):
        picked |= {
            i for i, ts in enumerate(rs.timestamps) if ts.month == month and ts.day == day
        }
    return sorted(picked)


def _sheet_storage_dispatch(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """储能与调度（V2 §10–§15、§20、§21、§71）。"""
    ws = wb.create_sheet("储能与调度")
    row = _write_title(ws, "储能与调度参数（V2 §10–§15、§20、§21）", 3)
    d = project.timeseries.dispatch
    s = project.storage
    rows = [
        ["—— 时序仿真 ——", "", ""],
        ["是否启用时序仿真", "是" if project.timeseries.enabled else "否", ""],
        [
            "时序仿真状态",
            "已启用" if project.timeseries.enabled else "未启用（本项目走 V1 年度模式，调度为配置项而非仿真结果）",
            "V2 §1.1 兼容性",
        ],
        ["调度策略", d.strategy.label, "V2 §12–§14"],
        ["", "", ""],
        ["—— 储能规模（V1 参数）——", "", ""],
        ["储能功率（kW）", s.storage_power_kw, ""],
        ["储能容量（kWh）", s.storage_energy_kwh, ""],
        ["储能时长（h）", result.storage_duration_hours, "功率 / 容量"],
        ["配置年循环次数", s.annual_cycles, "V2 §24"],
        ["放电深度 DoD", s.depth_of_discharge, ""],
        ["年衰减率", s.annual_degradation_rate, "V2 §25"],
        ["更换年份", s.replacement_year or "不更换", "V2 §26"],
        ["更换投资（元）", s.replacement_capex, ""],
        ["", "", ""],
        ["—— SOC 与效率（V2 §10）——", "", ""],
        ["SOC 下限", d.soc_min, ""],
        ["SOC 上限", d.soc_max, ""],
        ["起始 SOC", d.initial_soc, ""],
        ["可用容量占比", d.soc_max - d.soc_min, "SOC 上限 − 下限"],
        ["充电效率 η_charge", d.charge_efficiency, "V2 §10.3：充电乘效率"],
        ["放电效率 η_discharge", d.discharge_efficiency, "V2 §10.4：放电除效率"],
        ["往返效率 η_c × η_d", d.round_trip_efficiency, "与 V1 的 0.88 同口径"],
        ["", "", ""],
        ["—— 功率与阈值（V2 §11、§12）——", "", ""],
        ["最大充电功率（kW）", d.max_charge_power or "按额定功率", "0 表示按额定"],
        ["最大放电功率（kW）", d.max_discharge_power or "按额定功率", "0 表示按额定"],
        ["充电价格阈值（元/kWh）", d.charge_price_threshold, "峰谷套利策略用"],
        ["放电价格阈值（元/kWh）", d.discharge_price_threshold, "峰谷套利策略用"],
        ["", "", ""],
        ["—— 开关（V2 §20、§21）——", "", ""],
        ["允许光伏给储能充电", "是" if d.charge_from_pv else "否", ""],
        ["允许电网给储能充电", "是" if d.allow_grid_charge else "否", "V2 §21 默认否"],
        ["策略主动从电网充电", "是" if d.charge_from_grid else "否", ""],
        ["允许储能向电网放电", "是" if d.allow_export else "否", "V2 §20 默认否"],
        ["允许峰谷套利", "是" if d.allow_arbitrage else "否", ""],
    ]
    _write_rows(ws, row, rows, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_load_profile(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """负荷曲线（V2 §8、§67 Load Profile）。"""
    ws = wb.create_sheet("负荷曲线")
    row = _write_title(ws, "负荷曲线（V2 §8）", 4)
    cfg = project.timeseries.load
    rows = [
        ["取得方式", cfg.mode.label, "", ""],
        ["年用电量（kWh）", cfg.annual_energy_kwh, "", "TYPICAL_DAY 模式下为归一化目标"],
        ["年负荷增长率", cfg.annual_growth_rate, "", "V2 §8.3"],
        ["缺失数据处理策略", cfg.missing_data_policy.label, "", "V2 §53"],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    ts = result.time_series_results
    if ts is None or ts.hourly is None or len(ts.hourly) == 0:
        _not_enabled_note(ws, row, "负荷曲线")
        _auto_width(ws)
        return

    load = ts.hourly.column("load")
    rows = [
        ["—— 实际逐时结果 ——", "", "", ""],
        ["年电量（kWh）", float(sum(load)), "", ""],
        ["最大负荷（kW）", max(load), "", f"分辨率 {ts.resolution.label}"],
        ["最小负荷（kW）", min(load), "", ""],
        ["平均负荷（kW）", float(sum(load)) / max(len(load), 1), "", ""],
        ["逐时点数", len(load), "", "V2 §7"],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["月份", "电量（kWh）", "最大负荷（kW）", "最小负荷（kW）"])
    # 列式结果集只存数列，月份需从时间戳派生（时间戳即唯一主时间索引，V2 §7）
    months = [t.month for t in ts.hourly.timestamps]
    monthly: list[list] = []
    for m in range(1, 13):
        picked = [i for i in range(len(load)) if months[i] == m]
        if not picked:
            continue
        vals = [load[i] for i in picked]
        monthly.append([m, float(sum(vals)), max(vals), min(vals)])
    _write_rows(ws, row, monthly, formats={2: _NUM4, 3: _NUM4, 4: _NUM4})
    _auto_width(ws)


def _sheet_pv_profile(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """光伏曲线（V2 §9、§67 PV Profile）。"""
    ws = wb.create_sheet("光伏曲线")
    row = _write_title(ws, "光伏出力曲线（V2 §9）", 4)
    cfg = project.timeseries.pv
    rows = [
        ["取得方式", cfg.mode.label, "", ""],
        ["曲线对应容量（kWp）", cfg.capacity_kwp or project.pv.pv_capacity_kwp or 0.0, "", ""],
        ["年等效利用小时（h）", cfg.equivalent_hours, "", "V2 §9.1"],
        ["性能比 PR", cfg.performance_ratio, "", ""],
        ["年衰减率", project.pv.annual_degradation_rate, "", "V2 §9.2"],
        ["年增长率", cfg.annual_growth_rate, "", ""],
        ["缺失数据处理策略", cfg.missing_data_policy.label, "", "V2 §53"],
        ["", "", "", ""],
        ["—— 退化口径（V1 参数）——", "", "", ""],
        ["V1 年等效利用小时（h）", project.pv.equivalent_hours, "", ""],
        ["V1 装机容量（kWp）", project.pv.pv_capacity_kwp or 0.0, "", ""],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    ts = result.time_series_results
    if ts is None or ts.hourly is None or len(ts.hourly) == 0:
        _not_enabled_note(ws, row, "光伏曲线")
        _auto_width(ws)
        return

    pv = ts.hourly.column("pv_generation")
    capacity = result.pv_capacity_kwp or 0.0
    total = float(sum(pv))
    rows = [
        ["—— 实际逐时结果 ——", "", "", ""],
        ["年发电量（kWh）", total, "", ""],
        ["最大出力（kW）", max(pv), "", ""],
        ["等效利用小时（h）", total / capacity if capacity > 0 else 0.0, "", "发电量 / 容量"],
        ["弃光电量（kWh）", ts.metrics.annual_pv_curtailment, "", "V2 §9.3"],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["月份", "发电量（kWh）", "最大出力（kW）", "等效小时（h）"])
    months = [t.month for t in ts.hourly.timestamps]
    monthly: list[list] = []
    for m in range(1, 13):
        picked = [i for i in range(len(pv)) if months[i] == m]
        if not picked:
            continue
        vals = [pv[i] for i in picked]
        s = float(sum(vals))
        monthly.append([m, s, max(vals), s / capacity if capacity > 0 else 0.0])
    _write_rows(ws, row, monthly, formats={2: _NUM4, 3: _NUM4, 4: _NUM4})
    _auto_width(ws)


def _sheet_tou_tariff(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """分时电价（V2 §17、§18、§67 Tariff Profile）。"""
    ws = wb.create_sheet("分时电价")
    row = _write_title(ws, "分时电价模板（V2 §17、§18）", 5)
    cfg = project.timeseries.tariff
    profile = cfg.profile
    rows = [
        ["电价模板名称", profile.name or "（未命名）", "", "", ""],
        ["区域", profile.region, "", "", ""],
        ["生效日期", profile.effective_date.isoformat() if profile.effective_date else "未指定", "", "", ""],
        ["电价年度变化率", cfg.annual_growth_rate, "", "V2 §17.2", ""],
        ["计入需量电费", "是" if cfg.demand_charge_enabled else "否", "", "V2 §18", ""],
        ["计入基本电费", "是" if cfg.basic_charge_enabled else "否", "", "", ""],
        ["", "", "", "", ""],
        ["尖峰电价（元/kWh）", profile.sharp_peak_price, "", "", ""],
        ["高峰电价（元/kWh）", profile.peak_price, "", "", ""],
        ["平段电价（元/kWh）", profile.flat_price, "", "", ""],
        ["谷段电价（元/kWh）", profile.valley_price, "", "", ""],
        ["深谷电价（元/kWh）", profile.deep_valley_price, "", "", ""],
        ["自定义时段电价（元/kWh）", profile.custom_price, "", "", ""],
        ["上网电价（元/kWh）", profile.export_price, "", "不随购电电价增长", ""],
        ["需量电价（元/kW·月）", profile.demand_charge, "", "", ""],
        ["基本电费（元/月）", profile.basic_charge, "", "", ""],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    if not project.timeseries.enabled:
        ws.cell(
            row,
            column=1,
            value=(
                "本项目未启用时序仿真：上表为分时电价**模板配置**（时段规则与各时段电价），"
                "启用后才会据其生成逐时电价曲线。"
            ),
        )
        row += 2

    row = _write_header_at(ws, row, ["时段规则", "月份", "日类型", "小时", "说明"])
    rules = [
        [
            r.period.label,
            "、".join(str(m) for m in r.months) or "全年",
            "、".join(t.label for t in r.day_types) or "全部",
            "、".join(str(h) for h in r.hours) or "（未指定）",
            "多条规则命中同一小时时，按声明顺序后者覆盖前者",
        ]
        for r in profile.time_periods
    ]
    row = _write_rows(ws, row, rules)
    if not rules:
        ws.cell(row=row, column=1, value="未定义时段规则（全部小时按平段处理）")
    _auto_width(ws)


def _sheet_hourly_simulation(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """8760 时序仿真（抽样；V2 §6、§67 Hourly Simulation）。"""
    ws = wb.create_sheet("8760时序仿真")
    row = _write_title(ws, "8760 小时时序仿真（抽样展示）", 8)
    ts = result.time_series_results
    if ts is None or ts.hourly is None or len(ts.hourly) == 0:
        _not_enabled_note(ws, row, "8760 时序仿真")
        _auto_width(ws)
        return

    rs = ts.hourly
    total_points = len(rs)
    ws.cell(row=row, column=1, value="抽样说明").font = _BOLD
    row += 1
    ws.cell(
        row,
        column=1,
        value=(
            f"完整 8760 逐时数据（共 {total_points:,} 点 × {len(_HOURLY_SHEET_COLUMNS) + 3} 列）"
            "不写入本表，以免工作簿过大；如需全量数据，请从 .nep 项目文件或 JSON 结果导出获取。"
        ),
    )
    row += 1
    ws.cell(row, column=1, value="本表内容：每月 1 日 24 小时 + 1 月 15 日与 7 月 15 日两个典型日。")
    row += 2

    header = ["时间", *[name for name, _ in _HOURLY_SHEET_COLUMNS], "动作", "调度原因"]
    row = _write_header_at(ws, row, header)

    indices = _sample_hourly_indices(rs)
    data: list[list] = []
    for i in indices:
        row_obj = rs.row(i)
        values: list = [row_obj.timestamp.strftime("%Y-%m-%d %H:%M")]
        for _, key in _HOURLY_SHEET_COLUMNS:
            if key == "net_electricity_cost_placeholder":
                values.append(row_obj.electricity_cost - row_obj.export_revenue)
            else:
                values.append(getattr(row_obj, key))
        values.append(row_obj.dispatch_action.label)
        values.append(row_obj.dispatch_reason)
        data.append(values)

    _write_rows(ws, row, data, formats={c: _NUM4 for c in range(2, 24)})
    _auto_width(ws)


def _sheet_energy_balance(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """能量平衡（V2 §19、§70、§67 Energy Balance）。"""
    ws = wb.create_sheet("能量平衡")
    row = _write_title(ws, "全场站能量平衡（V2 §19、§70）", 3)
    bal = result.energy_balance
    if bal is None:
        _not_enabled_note(ws, row, "能量平衡")
        _auto_width(ws)
        return

    rows = [
        ["平衡式", "PV + 购电 + 储能放电 = 负荷 + 储能充电 + 上网 + 弃光", ""],
        ["", "", ""],
        ["—— 供给侧（kWh）——", "", ""],
        ["光伏发电", bal.pv_generation, ""],
        ["其中：光伏→负荷", bal.pv_to_load, ""],
        ["其中：光伏→储能", bal.pv_to_storage, ""],
        ["其中：光伏→上网", bal.pv_to_grid, ""],
        ["其中：弃光", bal.pv_curtailed, ""],
        ["电网购电", bal.grid_import, ""],
        ["其中：电网→负荷", bal.grid_to_load, ""],
        ["其中：电网→储能", bal.grid_to_storage, ""],
        ["储能放电", bal.storage_discharge, ""],
        ["供给合计", bal.supply_total, ""],
        ["", "", ""],
        ["—— 需求侧（kWh）——", "", ""],
        ["负荷", bal.load_total, ""],
        ["储能充电", bal.storage_charge, ""],
        ["上网电量", bal.grid_export, ""],
        ["需求合计", bal.demand_total, ""],
        ["", "", ""],
        ["—— 校验（V2 §19）——", "", ""],
        ["平衡误差（kWh）", bal.error, "供给合计 − 需求合计"],
        ["最大逐时误差（kWh）", bal.max_hourly_error, ""],
        ["容差（kWh）", bal.tolerance, "超过即判定计算失败"],
        ["是否平衡", "是" if bal.is_balanced else "否", ""],
    ]
    _write_rows(ws, row, rows, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_annual_summary(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """年度汇总（V2 §67 Annual Summary、§32）。"""
    ws = wb.create_sheet("年度汇总")
    row = _write_title(ws, "年度汇总（V2 §32）", 8)
    header = [
        "年份", "负荷（kWh）", "光伏发电（kWh）", "自用电量（kWh）", "上网电量（kWh）",
        "光伏转储能（kWh）", "储能充电（kWh）", "储能放电（kWh）", "总收入（元）",
        "运维费（元）", "EBITDA（元）", "项目现金流（元）", "累计现金流（元）",
    ]
    row = _write_header_at(ws, row, header)
    data = [
        [
            r.year, r.load_kwh, r.pv_generation_kwh, r.pv_self_use_kwh, r.pv_export_kwh,
            r.pv_to_storage_kwh, r.storage_charge_kwh, r.storage_discharge_kwh,
            r.total_revenue, r.opex, r.ebitda, r.project_cashflow,
            r.cumulative_project_cashflow,
        ]
        for r in result.annual_results
    ]
    _write_rows(ws, row, data, formats={c: _NUM4 for c in range(2, 14)})
    if not project.timeseries.enabled:
        ws.cell(
            row=row + len(data) + 1,
            column=1,
            value=(
                "本项目未启用时序仿真：上表为 V1 年度模型结果；"
                "自用率、自给率、等效循环、需量削减等时序指标需启用时序仿真后生成。"
            ),
        )
    _auto_width(ws)


def _sheet_revenue(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """收益分解（V2 §27–§31、§67 Revenue）。"""
    ws = wb.create_sheet("收益分解")
    row = _write_title(ws, "收益分解与电费节省（V2 §27–§31）", 4)
    ts = result.time_series_results
    if ts is None:
        _not_enabled_note(ws, row, "收益分解")
        _auto_width(ws)
        return

    m = ts.metrics
    rows = [
        ["—— 首年（时序仿真口径）——", "", "", ""],
        ["基准电费（元）", m.baseline_electricity_cost, "", "V2 §28：无 PV 无储能"],
        ["实际电费（元）", m.actual_electricity_cost, "", "Σ 购电量 × 电价"],
        ["电费节省（元）", m.electricity_cost_saving, "", "基准 − 实际"],
        ["　其中：光伏自用节省（元）", m.pv_self_consumption_saving, "", "Σ 光伏→负荷 × 电价"],
        ["　其中：储能套利收益（元）", m.storage_arbitrage_revenue, "",
         "Σ 储能→负荷 × 电价 − Σ 电网→储能 × 电价"],
        ["分解校验偏差（元）", m.pv_self_consumption_saving + m.storage_arbitrage_revenue
         - m.electricity_cost_saving, "", "必须≈0，否则存在重复计算（V2 §29）"],
        ["", "", "", ""],
        ["光伏上网收入（元）", m.pv_export_revenue, "", "V2 §27"],
        ["储能容量收益（元）", m.storage_capacity_revenue, "", ""],
        ["储能辅助服务收益（元）", m.storage_ancillary_revenue, "", ""],
        ["其他收益（元）", m.other_revenue, "", ""],
        ["需量电费节省（元）", m.demand_cost_saving, "", "V2 §30"],
        ["首年总收益（元）", m.total_benefit, "", "V2 §31"],
        ["", "", "", ""],
        ["—— 关键运行指标（V2 §41）——", "", "", ""],
        ["自用率", m.self_consumption_rate, "", "V2 §22：光伏自用电量 ÷ 光伏发电量"],
        ["自给率", m.self_sufficiency_rate, "", "V2 §23：光伏自用电量 ÷ 负荷电量"],
        ["等效循环次数（实际）", m.equivalent_cycles, "", "V2 §24"],
        ["等效循环次数（配置）", m.configured_cycles, "", ""],
        ["最大需量削减（kW）", m.demand_saving, "", f"削减前 {_fmt(m.peak_demand_before)} / 削减后 {_fmt(m.peak_demand_after)}"],
        ["年购电量（kWh）", m.annual_grid_purchase, "", ""],
        ["年上网电量（kWh）", m.annual_grid_export, "", ""],
        ["年弃光电量（kWh）", m.annual_pv_curtailment, "", ""],
        ["", "", "", ""],
        ["—— 全周期累计 ——", "", "", ""],
        ["全周期总收入（元）", sum(r.total_revenue for r in result.annual_results), "", ""],
        ["全周期运维费（元）", sum(r.opex for r in result.annual_results), "", ""],
        ["全周期 EBITDA（元）", sum(r.ebitda for r in result.annual_results), "", ""],
        ["全周期现金税费（元）", sum(r.cash_tax for r in result.annual_results), "", ""],
    ]
    if ts.baseline is not None:
        rows.insert(1, ["基准方案最大需量（kW）", ts.baseline.peak_demand_kw, "", "V2 §42"])
        rows.insert(2, ["基准方案需量电费（元）", ts.baseline.annual_demand_cost, "", ""])
    _write_rows(ws, row, rows, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_scenario_compare(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """方案比较（V2 §43、§44、§67 Scenario）。"""
    ws = wb.create_sheet("方案比较")
    row = _write_title(ws, "方案比较（V2 §43、§44）", 8)
    scenarios = result.scenario_results
    if not scenarios:
        ws.cell(row=row, column=1, value="未执行方案比较。").font = _BOLD
        row += 1
        ws.cell(
            row,
            column=1,
            value="说明：方案比较需要在「方案分析」中扫描候选方案（V2 §45、§46）后生成；"
            "本项目的 result.scenario_results 为空。",
        )
        _auto_width(ws)
        return

    header = [
        "方案", "光伏容量（kWp）", "储能功率（kW）", "储能容量（kWh）", "总投资（元）",
        "项目 IRR", "项目 NPV（元）", "静态回收期（年）", "LCOE（元/kWh）", "LCOS（元/kWh）",
        "年节省（元）", "自用率", "自给率", "等效循环",
    ]
    row = _write_header_at(ws, row, header)
    data = [
        [
            s.label or s.name,
            s.pv_capacity_kwp,
            s.storage_power_kw,
            s.storage_energy_kwh,
            s.total_capex,
            s.project_irr if s.project_irr is not None else "无法计算",
            s.project_npv,
            s.static_payback if s.static_payback is not None else "未回收",
            s.lcoe if s.lcoe is not None else "不适用",
            s.lcos if s.lcos is not None else "不适用",
            s.annual_saving,
            s.self_consumption_rate,
            s.self_sufficiency_rate,
            s.equivalent_cycles,
        ]
        for s in scenarios
    ]
    _write_rows(ws, row, data, formats={5: _NUM4, 7: _NUM4, 11: _NUM4})
    _auto_width(ws)


def _sheet_optimization(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """方案寻优（V2 §45–§48、§67 Optimization）。"""
    ws = wb.create_sheet("方案寻优")
    row = _write_title(ws, "方案扫描与寻优（V2 §45–§48）", 6)
    opt = result.optimization_results
    if opt is None:
        ws.cell(row=row, column=1, value="未执行方案寻优。").font = _BOLD
        row += 1
        ws.cell(
            row,
            column=1,
            value="说明：寻优需要在「方案分析」中设定扫描维度与目标（默认最大 NPV，V2 §47）后运行；"
            "本项目的 result.optimization_results 为空。",
        )
        _auto_width(ws)
        return

    rows = [
        ["优化目标", opt.objective.label, "", "V2 §47"],
        ["扫描维度", "、".join(opt.scan_variables) or "（未记录）", "", "V2 §46"],
        ["候选方案数", len(opt.candidates), "", ""],
        ["最优方案编号", opt.best_run_id, "", ""],
        ["最优方案取值", "；".join(f"{k} = {v:g}" for k, v in opt.best_variables.items()) or "（无）",
         "", ""],
        ["耗时（秒）", opt.elapsed_seconds, "", "V2 §86"],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["约束条件（V2 §14）", "", "", ""])
    for text in opt.constraints or ["（未记录约束）"]:
        ws.cell(row=row, column=1, value=text)
        row += 1
    row += 1

    row = _write_header_at(ws, row, ["为什么该方案最优（V2 §48 禁止黑盒）", "", "", ""])
    for text in opt.explanation or ["（未提供说明）"]:
        ws.cell(row=row, column=1, value=text)
        row += 1
    row += 1

    row = _write_header_at(ws, row, ["候选方案", "目标值", "光伏（kWp）", "储能（kWh）", "可行", "备注"])
    data = [
        [
            c.run_id,
            c.objective_value if c.feasible else "不可行",
            c.variables.get("pv_capacity_kwp", ""),
            c.variables.get("storage_energy_kwh", ""),
            "是" if c.feasible else "否",
            c.note,
        ]
        for c in opt.candidates[:200]
    ]
    _write_rows(ws, row, data, formats={2: _NUM4})
    _auto_width(ws)


def _sheet_data_quality(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """数据质量（V2 §55、§67 Data Quality）。"""
    ws = wb.create_sheet("数据质量")
    row = _write_title(ws, "数据质量评分（V2 §55）", 4)
    q = result.data_quality
    if q is None:
        ws.cell(row=row, column=1, value="不适用：本项目未导入外部时序数据。").font = _BOLD
        row += 1
        ws.cell(
            row,
            column=1,
            value="说明：数据质量评分只针对导入的负荷 / 光伏 / 电价曲线（V2 §51–§55）。"
            "本项目使用典型日曲线或年电量口径，无导入数据可评分。",
        )
        _auto_width(ws)
        return

    rows = [
        ["总分（0~100）", q.score, "", "V2 §55"],
        ["等级", q.level_label, "", ""],
        ["完整性", q.completeness, "", "缺失时间点比例"],
        ["连续性", q.continuity, "", "时间间隔是否规整"],
        ["异常值", q.outlier, "", ""],
        ["来源可信度", q.source_credibility, "", "按 source_type 映射"],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["级别", "类别", "问题描述", "数量", "示例"])
    data = [
        [i.level, i.category, i.message, i.count, "；".join(i.samples)]
        for i in q.issues
    ]
    _write_rows(ws, row, data, formats={4: _MONEY0})
    if not data:
        ws.cell(row=row, column=1, value="未发现问题。")
    _auto_width(ws)


# --------------------------------------------------------------------------- #
# V2.1 账单工作表（规格书 §8.1；阶段 2）
#
# 设计要点（与既有报表一致，§109 / §61 / V2.1 §2.1）：
# * 全部写已经算好 / 解析好的值，**工作簿内公式数必须为 0**（既有测试会断言）；
# * 电量 / 金额为 ``None`` 时写文本「账单未提供」，**绝不写 0**（V2.1 §2.1）；
# * 无账单时两张表**照常生成**，写「本项目尚未录入电费账单」与录入方法，不缺表、不报错
#   （V2.1 §8.2：旧项目打开后显示空状态，不自动生成虚构数据）；
# * 「账单校验」的差异明细取自应用服务 ``BillService.reconcile_all()``，
#   报表层**不做任何算术**（V2.1 §0.2：公式唯一实现在 calculation/）。
# --------------------------------------------------------------------------- #
#: 账单字段「未提供」的统一文案（与界面 ``ui/pages.py`` 保持一致，V2.1 §2.1）
BILL_NOT_PROVIDED_TEXT = "账单未提供"

#: 无账单时的说明（§8.2：不得缺表、不得报错）
BILL_EMPTY_TEXT = "本项目尚未录入电费账单。"
BILL_EMPTY_HOWTO = (
    "录入方法：① 在「月度账单」页手动录入（账期、电量与费用分项）；"
    "② 在「月度账单」页点击「下载导入模板」，按《CENEP_电费账单导入模板.xlsx》填写后"
    "经「导入向导」六步（选文件 → 选表 → 映射列 → 预览 → 校验 → 确认）导入；"
    "③ 也可直接导入已有 Excel / CSV 账单表。"
)
BILL_EMPTY_NOTE = (
    "说明：账单事实（本表）与账单模拟 / 复算结果分开保存；"
    "账单复算与光储方案模拟属于阶段 5 / 6，当前为『待确认 / 未建模』（V2.1 §1、§8.1）。"
)

#: 「账单原始数据」表的列（字段名 → 取 :data:`BILL_FIELD_LABELS` 的中文列名，含单位）
_BILL_RAW_FIELDS: tuple[str, ...] = (
    "billing_month",
    "billing_period_start",
    "billing_period_end",
    "meter_id",
    "customer_name",
    "voltage_level",
    "tariff_structure",
    "contract_capacity_kva",
    "billing_demand_kw",
    "energy_total_kwh",
    "energy_sharp_kwh",
    "energy_peak_kwh",
    "energy_flat_kwh",
    "energy_valley_kwh",
    "energy_offpeak_kwh",
    "market_purchase_charge_yuan",
    "transmission_distribution_charge_yuan",
    "line_loss_charge_yuan",
    "system_operation_charge_yuan",
    "government_fund_charge_yuan",
    "energy_charge_yuan",
    "basic_capacity_charge_yuan",
    "demand_charge_yuan",
    "power_factor_adjustment_yuan",
    "other_charge_yuan",
    "vat_yuan",
    "adjustment_charge_yuan",
    "bill_total_yuan",
    "source_type",
    "source_file_name",
    "source_row_number",
    "notes",
    "quality_status",
)

#: 需要写成「账单未提供」而不是 0 的数值字段（电量 + 全部金额，V2.1 §2.1）
_BILL_NUMERIC_FIELDS = frozenset(
    field for field in _BILL_RAW_FIELDS if field.endswith("_kwh") or field.endswith("_yuan")
)


def _bill_cell(bill, field: str):
    """把一个账单字段转成可写入单元格的值（``None`` → 「账单未提供」，不写 0）。"""
    value = getattr(bill, field, None)
    if field in _BILL_NUMERIC_FIELDS:
        return BILL_NOT_PROVIDED_TEXT if value is None else value
    if field in ("billing_period_start", "billing_period_end"):
        return value.isoformat() if value is not None else BILL_NOT_PROVIDED_TEXT
    if field == "source_type":
        return bill.source_type.label
    if field == "quality_status":
        return bill.quality_status.label
    if field == "tariff_structure":
        return bill.tariff_structure.label
    if value is None or not str(value).strip():
        return BILL_NOT_PROVIDED_TEXT
    return value


def _bill_sort_key(bill):
    """按账期排序（起始日 → 结束日 → 计量点），保证导出顺序稳定可复核。"""
    return (bill.billing_period_start, bill.billing_period_end, bill.meter_id or "")


def _write_bill_empty_note(ws, row: int) -> int:
    """无账单时的说明块（§8.2：两张表照常生成、写清录入方法）。"""
    ws.cell(row=row, column=1, value=BILL_EMPTY_TEXT).font = _BOLD
    row += 1
    for text in (BILL_EMPTY_HOWTO, BILL_EMPTY_NOTE):
        ws.cell(row=row, column=1, value=text)
        row += 1
    return row


def _sheet_bill_raw(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """账单原始数据（V2.1 §2.1、§8.1）：账单事实逐条明细，按账期排序。"""
    ws = wb.create_sheet("账单原始数据")
    headers = ["序号", "账单编号", *[BILL_FIELD_LABELS[f] for f in _BILL_RAW_FIELDS], "跨月账期", "数据质量说明"]
    row = _write_title(ws, "账单原始数据（账单事实，按账期排序；V2.1 §2.1）", len(headers))
    bills = sorted(project.bills, key=_bill_sort_key)
    if not bills:
        _write_bill_empty_note(ws, row)
        _auto_width(ws, max_width=60)
        return

    row = _write_header_at(ws, row, headers)
    data = []
    for index, bill in enumerate(bills, start=1):
        data.append(
            [
                index,
                bill.bill_id,
                *[_bill_cell(bill, field) for field in _BILL_RAW_FIELDS],
                "是" if bill.is_cross_month else "否",
                "；".join(bill.quality_messages) if bill.quality_messages else "无",
            ]
        )
    formats = {
        column: _NUM4
        for column, field in enumerate(["", "", *_BILL_RAW_FIELDS], start=1)
        if field in _BILL_NUMERIC_FIELDS
    }
    end = _write_rows(ws, row, data, formats=formats)
    row = end + 1
    ws.cell(row=row, column=1, value=BILL_EMPTY_NOTE)
    row += 1
    ws.cell(
        row,
        column=1,
        value=(
            f"共 {len(bills)} 条账单；电量为 None 的分项在表中写「{BILL_NOT_PROVIDED_TEXT}」，"
            "不按 0 计入任何合计（V2.1 §2.1）。"
        ),
    )
    _auto_width(ws, max_width=48)


def _sheet_bill_check(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """账单校验（V2.1 §3.1、§5.5、§8.1）：差异明细与口径假设。"""
    ws = wb.create_sheet("账单校验")
    row = _write_title(ws, "账单校验（差异与口径假设；V2.1 §3.1、§5.5）", 4)
    bills = sorted(project.bills, key=_bill_sort_key)
    if not bills:
        _write_bill_empty_note(ws, row)
        _auto_width(ws, max_width=60)
        return

    # 校验明细取自账单服务（内部委托 calculation/bill_calculator），报表层不做算术
    outcomes = BillService(project).reconcile_all()
    by_id = {item.bill_id: item for item in outcomes}
    invalid = sum(1 for bill in bills if bill.quality_status.value == "invalid")
    warning = sum(1 for bill in bills if bill.quality_status.value == "warning")
    valid = len(bills) - invalid - warning

    row = _write_rows(
        ws,
        row,
        [
            ["核对账单条数", len(bills), "", "按账期排序"],
            ["有效 / 有警告 / 无效", f"{valid} / {warning} / {invalid}", "", "无效账单不得视为可用事实"],
            ["分时电量合计差 ΔE", "见下表", "", "ΔE = 总电量 − Σ分时电量（§3.1）"],
            ["费用分项合计差 ΔC", "见下表", "", "ΔC = 账单总额 − Σ一层费用分项（§3.1）"],
        ],
    )
    row += 1

    row = _write_header_at(
        ws,
        row,
        [
            "账单编号",
            "账单月份",
            "分时电量合计差 ΔE（kWh）",
            "电量容差（kWh）",
            "电量是否一致",
            "未提供时段",
            "费用分项合计差 ΔC（元）",
            "金额容差（元）",
            "金额是否一致",
            "电度电费二层差（元）",
            "二层是否一致",
            "质量状态",
            "问题清单",
        ],
    )
    data = []
    for bill in bills:
        item = by_id.get(bill.bill_id)
        if item is None:  # pragma: no cover - reconcile_all 与账单列表同源，不会缺
            continue
        issues = "；".join(
            f"{issue.level}[{issue.code or '—'}]{label_of(issue.field) if issue.field else ''}：{issue.message}"
            for issue in item.issues
        )
        data.append(
            [
                bill.bill_id,
                bill.billing_month,
                _fmt(item.energy_difference_kwh, 4) if item.energy_difference_kwh is not None else BILL_NOT_PROVIDED_TEXT,
                item.energy_tolerance_kwh,
                _tristate(item.energy_consistent),
                "、".join(label_of(f) for f in item.energy_missing_periods)
                if item.energy_missing_periods
                else "无",
                _fmt(item.amount_difference_yuan, 4)
                if item.amount_difference_yuan is not None
                else BILL_NOT_PROVIDED_TEXT,
                item.amount_tolerance_yuan,
                _tristate(item.amount_consistent),
                _fmt(item.energy_sub_difference_yuan, 4)
                if item.energy_sub_difference_yuan is not None
                else BILL_NOT_PROVIDED_TEXT,
                _tristate(item.energy_sub_consistent),
                item.quality_status.label,
                issues or "未发现问题",
            ]
        )
    row = _write_rows(ws, row, data, formats={3: _NUM4, 4: _NUM4, 7: _NUM4, 8: _NUM4, 10: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["口径与假设（assumptions，逐条披露，§3.1）", "适用账单", "", ""])
    assumptions = []
    seen: set[str] = set()
    for bill in bills:
        item = by_id.get(bill.bill_id)
        if item is None:  # pragma: no cover
            continue
        for text in item.assumptions:
            if text not in seen:
                seen.add(text)
                assumptions.append([text, bill.bill_id, "", ""])
    for text in (BILL_EMPTY_NOTE, BILL_NOT_PROVIDED_TEXT + " 的分项不参与合计，也不会被填成 0。"):
        if text not in seen:
            seen.add(text)
            assumptions.append([text, "全部账单", "", ""])
    _write_rows(ws, row, assumptions)
    if not assumptions:  # pragma: no cover - 上面至少写入两条通用假设
        ws.cell(row=row, column=1, value="无。")
    _auto_width(ws, max_width=60)


def _tristate(value: bool | None) -> str:
    """三态显示：``True`` 一致 / ``False`` 超容差 / ``None`` 无法判断（缺数据，不按 0）。"""
    if value is None:
        return "无法判断（账单未提供相关字段）"
    return "一致" if value else "超容差"


# --------------------------------------------------------------------------- #
# V2.2 阶段 4：消纳率分析（§6.3 C、§3.3、§0.2）
# --------------------------------------------------------------------------- #
CONSUMPTION_EMPTY_TEXT = "尚未执行负荷与消纳分析。"
CONSUMPTION_EMPTY_HOWTO = (
    "操作路径：在「负荷与消纳」页面 A 区选择数据来源（月账单估算或实测高频负荷导入）→ "
    "生成/导入负荷曲线 → 在 C 区点击「计算光伏消纳四项指标」→ 再导出报告。"
)
CONSUMPTION_EMPTY_NOTE = (
    "在没有任何负荷数据时，本表**不提供**任何消纳率结论："
    "不得用未经说明的默认值制造「精确结果」（V2.2 §6.4、§12）。"
)


def _sheet_self_consumption(wb: Workbook, self_consumption, load_portrait) -> None:
    """「消纳率分析」表（V2.2 §6.3 C、§3.3）。

    **本函数不做任何计算**：四项指标、逐月明细、口径文本与假设全部来自
    ``LoadProfileService`` 已算好的 ``SelfConsumptionResult`` / ``LoadPortrait``；
    唯一允许的字符串处理是格式化显示。

    估算数据在本表中以「估算数据（不是实测）」徽标与独立行显著标注（§0.2 红线）。
    """
    ws = wb.create_sheet("消纳率分析")
    headers = [
        "项目",
        "实测/估算",
        "数值",
        "单位",
        "计算口径（分子 / 分母 / 单位 / 边界）",
    ]
    result = self_consumption
    if result is None and load_portrait is None:
        row = _write_title(ws, "消纳率分析（V2.2 §3.3）", len(headers))
        ws.cell(row=row, column=1, value=CONSUMPTION_EMPTY_TEXT).font = _BOLD
        row += 1
        for text in (CONSUMPTION_EMPTY_HOWTO, CONSUMPTION_EMPTY_NOTE):
            ws.cell(row=row, column=1, value=text)
            row += 1
        _auto_width(ws, max_width=70)
        return

    title = (
        f"消纳率分析（V2.2 §3.3；负荷来源：{result.load_source_type.label}"
        f"{'，估算数据' if result.is_based_on_estimate else '，实测数据'}）"
        if result is not None
        else "消纳率分析（仅有负荷画像，未执行消纳分析）"
    )
    row = _write_title(ws, title, len(headers))
    row = _write_header_at(ws, row, headers)

    source_mark = (
        "估算（不是实测）" if result is not None and result.is_based_on_estimate else "实测"
    )
    data: list[list] = []

    if load_portrait is not None:
        data.append(["—— 负荷数据来源与质量（§6.3 B、§0.2）——", "", "", "", ""])
        for name, value in load_portrait.portrait_rows():
            data.append(
                [name, "估算（不是实测）" if load_portrait.estimated else "实测", value, "", ""]
            )
        data.append(["—— 逐月电量与负荷率 ——", "", "", "", ""])
        for item in load_portrait.monthly:
            data.append(
                [
                    f"{item.month_key} 电量",
                    source_mark,
                    item.energy_kwh,
                    "kWh",
                    f"该月最大功率 {item.peak_power_kw:,.2f} kW、平均功率 "
                    f"{item.avg_power_kw:,.2f} kW、负荷率 {item.load_factor:.2%}、"
                    f"缺失点 {item.missing_value_count} 个",
                ]
            )

    if result is not None:
        data.append(["—— 电量（§3.3）——", "", "", "", ""])
        for name, value, note in result.energy_rows():
            data.append([name, source_mark, value, "kWh", note])
        data.append(["—— 四项指标（含计算口径，§3.3、§12）——", "", "", "", ""])
        for caliber in result.calibers:
            value = result.rate_of(caliber.key)
            data.append(
                [
                    caliber.name,
                    source_mark,
                    "不适用" if value is None else value,
                    "比例（0~1）" if value is not None else "—",
                    caliber.text(),
                ]
            )
        data.append(["—— 逐月消纳明细（月度自用率趋势）——", "", "", "", ""])
        for item in result.monthly:
            data.append(
                [
                    f"{item.month_key} 光伏自用率",
                    source_mark,
                    item.self_consumption_rate if item.self_consumption_rate is not None else "不适用",
                    "比例（0~1）" if item.self_consumption_rate is not None else "—",
                    f"该月：负荷 {item.load_energy_kwh:,.3f} kWh、光伏 {item.pv_generation_kwh:,.3f} kWh、"
                    f"自用 {item.pv_used_on_site_kwh:,.3f} kWh、上网 {item.pv_export_kwh:,.3f} kWh、"
                    f"购电 {item.grid_import_kwh:,.3f} kWh；负荷覆盖率 {item.rate_text('load_coverage')}、"
                    f"上网率 {item.rate_text('export')}、电网依赖率 {item.rate_text('grid_dependency')}",
                ]
            )
        data.append(["—— 关键假设、口径与数据缺口（§0.2、§12）——", "", "", "", ""])
        for line in result.assumption_lines():
            data.append(["假设", source_mark, "", "", line])

    row = _write_rows(ws, row, data)
    _auto_width(ws, max_width=80)


# --------------------------------------------------------------------------- #
# V2.4 §8.1（阶段 7）：新增 5 张工作表
#
# 设计要点（与本文件既有约定一致）：
# * **工作簿内公式数必须为 0**（V1 §109 / V2 §61）：全部写算好的值；
# * 无数据时**照常生成**并给出中文说明与操作路径，不缺表、不报错（§8.2）；
# * 数值全部来自应用服务已经算好的结果对象
#   （``BillService`` / ``TariffService`` / ``ScenarioBillService`` / ``LoadPortrait``），
#   报表层**不做任何算术**（§0.2：核心公式唯一实现在 ``calculation/``）；
# * 四类数据（实际账单 / 软件复算 / 模型估算 / 方案模拟）在每张表里都带口径标签，
#   并用 :data:`CALIBER_LEGEND` 统一说明（§8.1「不能用同一颜色/标题混为一谈」）。
# --------------------------------------------------------------------------- #
#: 计划版本表在既有数据里最多展示几条（避免把版本库全量倒进报告）
_PLAN_ROWS_LIMIT = 4

MONTHLY_BILL_EMPTY_TEXT = "本项目尚未录入电费账单，无法进行月度电费分析。"
LOAD_QUALITY_EMPTY_TEXT = "本项目尚未导入或估算任何负荷数据集，无负荷数据质量可评分。"
LOAD_QUALITY_HOWTO = (
    "操作路径：在「负荷与消纳」页面 A 区选择「月账单估算」或「实测高频负荷导入」，"
    "生成 / 导入负荷曲线后回到本表重新导出报告。"
)
TARIFF_VERSION_NOTE = (
    "说明：电价计划存放于软件的**版本库（SQLite）**，不写进 .nep 项目文件；"
    "本表只列出报告导出时版本库中登记的计划及其来源与核验状态（§7.3、§8.1）。"
)
TARIFF_VERSION_HOWTO = (
    "操作路径：在「月度账单」页或电价计划区载入内置计划 / 按账单反算 / 手工录入，"
    "并填写来源、文号、适用范围与核验人后重新导出报告。"
)
SCENARIO_COMPARE_EMPTY_TEXT = "尚未执行光储四场景账单对比，本表无方案模拟数据。"
SCENARIO_COMPARE_HOWTO = (
    "操作路径：在「负荷与消纳」页面「D 光储场景对比」中选择电价计划并点击"
    "「执行四场景对比」（无光伏 / 仅光伏 / 仅储能 / 光储，V2.3 §7.1）。"
)
SCENARIO_COMPARE_BOUNDARY = (
    "口径分界：本表全部为**方案模拟**结果（④），不得与「账单原始数据」表的**实际账单**（①）"
    "或「账单校验」表的软件复算（②）混为一谈；账单节省额是唯一收益入口，"
    "储能套利 / 光伏自用只做分解展示、**不再次累加**（V2.3 §7.6）。"
)
ASSUMPTION_EMPTY_NOTE = "本项目没有额外的计算假设或警告需要披露。"
ASSUMPTION_BOUNDARY = (
    "口径分界（§8.1）：① 实际账单来自用户录入/导入；② 软件复算按核验过的电价计划重算，"
    "仅用于与①比较；③ 模型估算由月电量 + 可编辑模板生成，不是实测；"
    "④ 方案模拟是四场景对比结果。四类数据在本工作簿的不同表中分列，请勿混用。"
)


def _plan_summaries_for_report() -> list[dict[str, object]]:
    """从电价**版本库**取计划摘要；版本库不可用时返回空列表（表照常生成）。"""
    try:
        service = TariffService(Project())
        return service.plan_summaries()
    except Exception as exc:  # pragma: no cover - 版本库异常不得阻断报告导出
        logger.warning("读取电价版本库失败，电价版本与来源表将只输出说明：%s", exc)
        return []


def _sheet_monthly_bill_analysis(wb: Workbook, project: Project, result: CalculationResult) -> None:
    """月度电费分析（V2.4 §8.1；数据同源于 V2.1 §3.1、§5.1）。"""
    ws = wb.create_sheet("月度电费分析")
    headers = [
        "月份",
        "账单条数",
        "总购电量（kWh）",
        "账单总额（元）",
        "平均综合电价（元/kWh）",
        "跨月账期",
        "是否重叠",
        "质量状态",
        "电量缺失条数",
        "金额缺失条数",
        "说明与警告",
    ]
    row = _write_title(
        ws,
        f"月度电费分析（口径：{CALIBER_TAG_ACTUAL}；V2.1 §3.1、V2.4 §8.1）",
        len(headers),
    )
    bills = sorted(project.bills, key=_bill_sort_key)
    if not bills:
        _write_bill_empty_note(ws, row)
        _auto_width(ws, max_width=60)
        return

    service = BillService(project)
    summary = service.annual_summary()
    outcomes = {item.bill_id: item for item in service.reconcile_all()}
    messages_by_month: dict[str, list[str]] = {}
    for bill in bills:
        item = outcomes.get(bill.bill_id)
        texts = {
            f"{issue.level}[{issue.code or '—'}]：{issue.message}"
            for issue in (item.issues if item is not None else [])
        }
        texts.update(bill.quality_messages)
        bucket = messages_by_month.setdefault(bill.billing_month, [])
        for text in sorted(texts):
            if text not in bucket:
                bucket.append(text)

    row = _write_rows(
        ws,
        row,
        [
            ["账单年份", summary.year, "", "", "", "", "", "", "", "", "按账期归属年份"],
            ["覆盖月份数", len(summary.months_covered), "", "", "", "", "", "", "", "",
             "、".join(summary.months_covered) or "无"],
            ["月份覆盖率", summary.coverage_ratio, "", "", "", "", "", "", "", "",
             "已覆盖月 / 12；缺月时年度电量不得直接相加"],
            ["是否可直接相加", "是" if summary.can_sum_directly else "否", "", "", "", "", "", "", "", "",
             "「否」时本表只作逐月展示，不得作为完整年度基准账单"],
        ],
        formats={2: _NUM4},
    )
    row += 1

    row = _write_header_at(ws, row, headers)
    data: list[list] = []
    for item in summary.monthly:
        data.append(
            [
                item.billing_month,
                item.bill_count,
                item.energy_total_kwh if item.energy_total_kwh is not None else BILL_NOT_PROVIDED_TEXT,
                item.amount_total_yuan if item.amount_total_yuan is not None else BILL_NOT_PROVIDED_TEXT,
                item.average_price_yuan_per_kwh
                if item.average_price_yuan_per_kwh is not None
                else BILL_NOT_PROVIDED_TEXT,
                "是" if item.has_cross_month else "否",
                "是" if item.has_overlap else "否",
                item.quality_status.label,
                item.energy_missing_bills,
                item.amount_missing_bills,
                "；".join(messages_by_month.get(item.billing_month, [])) or "未发现问题",
            ]
        )
    row = _write_rows(
        ws,
        row,
        data,
        formats={3: _NUM4, 4: _MONEY, 5: _NUM4},
    )
    row += 1

    for text in summary.messages:
        ws.cell(row=row, column=1, value=text)
        row += 1
    for text in summary.assumptions:
        ws.cell(row=row, column=1, value=text)
        row += 1
    ws.cell(
        row,
        column=1,
        value=(
            f"口径：平均综合电价 P_avg = 账单总额 ÷ 总购电量，仅为**{CALIBER_TAG_ACTUAL}统计口径**，"
            "不等于光伏自用电量的边际节省电价（基本电费、需量电费、税费未必随购电量同比例变化，V2.1 §3.1）。"
            f"「{BILL_NOT_PROVIDED_TEXT}」的分项按未知处理，不参与任何合计。"
        ),
    )
    _auto_width(ws, max_width=60)


def _sheet_load_data_quality(wb: Workbook, portrait) -> None:
    """负荷数据质量（V2.4 §8.1；数据来自 ``LoadPortrait``，本函数不做统计）。"""
    ws = wb.create_sheet("负荷数据质量")
    headers = ["项目", "取值", "口径说明"]
    source_word = (
        "无负荷数据" if portrait is None
        else ("估算" if portrait.estimated else "实测")
    )
    row = _write_title(
        ws,
        f"负荷数据质量（{source_word}口径；V2.2 §6.3 B、V2.4 §8.1）",
        len(headers),
    )
    if portrait is None:
        ws.cell(row=row, column=1, value=LOAD_QUALITY_EMPTY_TEXT).font = _BOLD
        row += 1
        for text in (LOAD_QUALITY_HOWTO, CONSUMPTION_EMPTY_NOTE):
            ws.cell(row=row, column=1, value=text)
            row += 1
        _auto_width(ws, max_width=70)
        return

    source_tag = (
        f"{CALIBER_TAG_ESTIMATED}（不是实测）" if portrait.estimated else "实测数据"
    )
    row = _write_header_at(ws, row, headers)
    data: list[list] = [
        ["数据来源", portrait.source_type.label, source_tag],
        ["来源徽标", portrait.source_type.report_badge, f"口径：{source_tag}"],
        ["来源标签（report）", portrait.provenance_text, "界面、Excel、PDF 同源"],
        ["时间间隔", f"{portrait.interval_minutes} 分钟", "左闭右开 [t, t+Δt)"],
        ["数据点数", portrait.point_count, "15 分钟整年 35040 点 / 闰年 35136 点；小时 8760 / 8784"],
        ["时间覆盖率", portrait.coverage_ratio, "有效时段 / 完整年度时段；**不是负荷覆盖率**"],
        ["质量等级", portrait.quality_status.label, "V2 §55 四级"],
        ["缺失点数量", portrait.missing_value_count, "缺失点不得按 0 参与计算"],
        ["是否已年化", "是" if portrait.annualized else "否",
         "年化属估算口径，报告中必须标注（V2.2 §2.2）"],
    ]
    row = _write_rows(ws, row, data, formats={2: _NUM4})
    row += 1

    for name, value in portrait.portrait_rows():
        ws.cell(row=row, column=1, value=name)
        ws.cell(row=row, column=2, value=value)
        row += 1
    row += 1

    row = _write_header_at(
        ws,
        row,
        ["月份", "电量（kWh）", "最大功率（kW）", "平均功率（kW）", "负荷率", "缺失点", "口径"],
    )
    monthly = [
        [
            item.month_key,
            item.energy_kwh,
            item.peak_power_kw,
            item.avg_power_kw,
            item.load_factor,
            item.missing_value_count,
            source_tag,
        ]
        for item in portrait.monthly
    ]
    row = _write_rows(ws, row, monthly, formats={2: _NUM4, 3: _NUM4, 4: _NUM4, 5: _PCT})
    if not monthly:
        ws.cell(row=row, column=1, value="无逐月数据。")
        row += 1
    row += 1

    row = _write_header_at(
        ws,
        row,
        ["典型日类型", "采样天数", "最大功率（kW）", "间隔（分钟）", "口径"],
    )
    typical = [
        [item.day_type, item.sample_days, item.peak_power_kw, item.interval_minutes, source_tag]
        for item in portrait.typical_days
    ]
    row = _write_rows(ws, row, typical, formats={3: _NUM4})
    if not typical:
        ws.cell(row=row, column=1, value="无典型日曲线。")
        row += 1
    row += 1
    ws.cell(
        row,
        column=1,
        value=(
            "口径分界：最大功率是**区间平均功率的最大值**，不是电表计费需量（V2.3 §7.5）；"
            "负荷率 = 平均功率 ÷ 最大功率。" + ASSUMPTION_BOUNDARY
        ),
    )
    _auto_width(ws, max_width=70)


def _sheet_tariff_versions(wb: Workbook, plan_ids: list[str] | None) -> None:
    """电价版本与来源（V2.4 §8.1；行数据来自 ``TariffService.plan_rows_for_report``）。"""
    ws = wb.create_sheet("电价版本与来源")
    headers = ["字段", "取值", "单位", "来源 / 口径"]
    row = _write_title(ws, "电价版本与来源（V2.3 §7.3、§4.1；V2.4 §8.1）", len(headers))
    ids = [plan_id for plan_id in (plan_ids or []) if plan_id][:_PLAN_ROWS_LIMIT]

    if not ids:
        summaries = _plan_summaries_for_report()
        if not summaries:
            ws.cell(row=row, column=1, value="当前版本库中没有可用的电价计划，本表无电价版本数据。").font = _BOLD
            row += 1
            for text in (TARIFF_VERSION_HOWTO, TARIFF_VERSION_NOTE):
                ws.cell(row=row, column=1, value=text)
                row += 1
            _auto_width(ws, max_width=70)
            return
        row = _write_header_at(ws, row, headers)
        data = [
            [
                str(item.get("name", "")),
                str(item.get("status_label", item.get("status", ""))),
                str(item.get("effective_from", "")),
                str(item.get("source_text", item.get("source_name", ""))),
            ]
            for item in summaries
        ]
        row = _write_rows(ws, row, data)
        row += 1
        ws.cell(
            row,
            column=1,
            value=(
                "本表为上表「内置 / 已登记计划」的**摘要**（未指定本次测算采用哪一版）。"
                "如需逐项列出某版本的时段电价与来源，请在界面选定电价计划后重新导出。" + TARIFF_VERSION_NOTE
            ),
        )
        _auto_width(ws, max_width=70)
        return

    service = TariffService(Project())
    for plan_id in ids:
        try:
            rows = service.plan_rows_for_report(plan_id)
        except Exception as exc:
            logger.warning("电价计划 %s 无法取出报告行：%s", plan_id, exc)
            ws.cell(row=row, column=1, value=f"电价计划 {plan_id} 读取失败，本版本未列出。")
            row += 1
            continue
        row = _write_header_at(ws, row, headers)
        row = _write_rows(ws, row, [list(item) for item in rows], formats={2: _NUM4})
        row += 1
    ws.cell(row=row, column=1, value=TARIFF_VERSION_NOTE)
    row += 1
    ws.cell(row=row, column=1, value=ASSUMPTION_BOUNDARY)
    _auto_width(ws, max_width=70)


def _sheet_scenario_bill_compare(wb: Workbook, scenario_result) -> None:
    """方案电费对比（V2.4 §8.1；行数据来自 ``ScenarioBillService``，报表层不做算术）。

    注意与 V2 既有的 :func:`_sheet_scenario_compare`（工作表「方案比较」，
    ``CalculationResult.scenario_results`` 的容量扫描结果）**不是同一张表**：
    本表是 V2.3 §7.1 的四场景**账单**对比（无光伏 / 仅光伏 / 仅储能 / 光储）。
    """
    ws = wb.create_sheet("方案电费对比")
    row = _write_title(
        ws,
        f"方案电费对比（口径：{CALIBER_TAG_SIMULATED}；V2.3 §7.1、§7.6、V2.4 §8.1）",
        24,
    )
    if scenario_result is None:
        ws.cell(row=row, column=1, value=SCENARIO_COMPARE_EMPTY_TEXT).font = _BOLD
        row += 1
        for text in (SCENARIO_COMPARE_HOWTO, SCENARIO_COMPARE_BOUNDARY):
            ws.cell(row=row, column=1, value=text)
            row += 1
        _auto_width(ws, max_width=70)
        return

    head = [
        ["—— 本次模拟的负荷与电价口径 ——", "", ""],
        ["电价计划编号", scenario_result.tariff_plan_id, "V2.3 §7.3"],
        ["电价计划名称（含版本）", scenario_result.tariff_plan_name, "V2.3 §4.1"],
        ["电价来源与文号", scenario_result.tariff_plan_source or "未提供", "V2.3 §4.3"],
        ["电价核验状态", scenario_result.tariff_plan_status or "未提供", "draft / verified / expired"],
        ["负荷数据来源", scenario_result.load_source or "未提供",
         "实测" if scenario_result.load_is_measured else f"{CALIBER_TAG_ESTIMATED}（不是实测）"],
        ["时间间隔 / 数据点数", f"{scenario_result.interval_minutes} 分钟 / {scenario_result.point_count} 点",
         "四场景必须共用同一时间轴"],
        ["基准年", scenario_result.base_year, "V2 §7"],
        ["唯一运营收益取自场景", scenario_result.reference_scenario.label, "V2.3 §7.6 第 5 条"],
        ["收益去重校验", "通过" if scenario_result.dedup_verified else "未通过",
         f"恒等式最大偏差 {scenario_result.identity_max_deviation_yuan:.6f} 元"],
        ["唯一去重后的年度运营收益（元）", scenario_result.unique_annual_benefit_yuan,
         "送财务引擎的唯一数值（V2.3 §3.5）"],
    ]
    row = _write_rows(ws, row, head, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["—— 四场景年度账单（方案模拟 ④）——", "", "", ""])
    for table_rows in (
        ScenarioBillService.scenario_rows(scenario_result),
        ScenarioBillService.comparison_rows(scenario_result),
        ScenarioBillService.dedup_rows(scenario_result),
    ):
        header, *body = table_rows
        row = _write_header_at(ws, row, [str(cell) for cell in header])
        row = _write_rows(ws, row, [list(line) for line in body])
        row += 1

    manifest = getattr(scenario_result, "dedup", None)
    if manifest is not None:
        rows = ScenarioBillService.dedup_manifest_rows(manifest)
        header, *body = rows
        row = _write_header_at(ws, row, [str(cell) for cell in header])
        row = _write_rows(ws, row, [list(line) for line in body])
        row += 1

    row = _write_header_at(ws, row, ["—— 关键假设与警告（逐条披露，V2.3 §7.7）——", "", ""])
    for text in (*scenario_result.warnings, *scenario_result.assumptions, *scenario_result.messages):
        ws.cell(row=row, column=1, value=text)
        row += 1
    if not (scenario_result.warnings or scenario_result.assumptions or scenario_result.messages):
        ws.cell(row=row, column=1, value="无。")
        row += 1
    row += 1
    ws.cell(row=row, column=1, value=SCENARIO_COMPARE_BOUNDARY)
    _auto_width(ws, max_width=70)


def _sheet_assumptions_warnings(
    wb: Workbook,
    project: Project,
    result: CalculationResult,
    portrait,
    self_consumption,
    scenario_result,
    as_of: datetime,
) -> None:
    """计算假设与警告（V2.4 §8.1；同时披露数据来源、模型版本与四类口径分界）。"""
    ws = wb.create_sheet("计算假设与警告")
    row = _write_title(ws, "计算假设与警告（V2.4 §8.1；V2 §55、V2.1 §3.1、V2.3 §7.6）", 4)
    row = _write_header_at(ws, row, ["项目", "取值", "口径/来源", "—"])

    rows: list[list] = [
        ["—— 报告与模型版本（§8.4 结果页要求）——", "", "", ""],
        ["项目名称", project.basic_info.project_name, "用户输入", ""],
        ["项目文件 schema 版本", project.schema_version, f"当前软件 {CURRENT_SCHEMA_VERSION}", ""],
        ["计算引擎版本", CALCULATION_ENGINE_VERSION, "V2 §94", ""],
        ["账单段版本", BILL_SECTION_SCHEMA_VERSION, "V2.1 §8.2", ""],
        ["政策版本", project.policy.display_version if project.policy is not None else "未关联政策模板",
         "V2 §95", ""],
        ["报告生成时间", as_of.isoformat(timespec="seconds"), "导出时刻", ""],
        ["时序仿真", "已启用" if project.timeseries.enabled else "未启用（V1 年度模式）", "V2 §1.1", ""],
    ]
    row = _write_rows(ws, row, rows)
    row += 1

    rows = [["—— 四类数据口径分界（§8.1：不得混为一谈）——", "", "", ""]]
    rows.extend([[name, text, "规格书 §8.1", ""] for name, text in CALIBER_LEGEND])
    row = _write_rows(ws, row, rows)
    row += 1

    bills = sorted(project.bills, key=_bill_sort_key)
    assumptions = [
        key for key, meta in result.parameter_sources.items() if meta.get("is_assumption")
    ]
    rows = [
        ["—— 输入数据来源与质量等级 ——", "", "", ""],
        ["账单条数", f"{len(bills)} 条", CALIBER_TAG_ACTUAL, ""],
        ["账单含未提供字段的条数",
         sum(1 for bill in bills if bill.quality_messages),
         "账单未提供的字段按未知处理，不按 0（V2.1 §2.1）", ""],
        ["负荷数据集数量", len(project.load_datasets), f"当前激活：{project.active_load_dataset_id or '未选择'}", ""],
        ["负荷数据质量等级",
         portrait.quality_status.label if portrait is not None else "无负荷数据（空状态）",
         (f"{CALIBER_TAG_ESTIMATED}（不是实测）" if portrait is not None and portrait.estimated
          else ("实测" if portrait is not None else "—")), ""],
        ["时间覆盖率（≠负荷覆盖率）",
         portrait.coverage_ratio if portrait is not None else "",
         "V2.2 §6.4", ""],
        ["消纳分析结果",
         "不适用" if self_consumption is None else self_consumption.load_source_type.label,
         "尚未执行消纳分析" if self_consumption is None else CALIBER_TAG_ESTIMATED
         if self_consumption.is_based_on_estimate else "实测", ""],
        ["时序数据质量评分",
         result.data_quality.score if result.data_quality is not None else "不适用",
         result.data_quality.level_label if result.data_quality is not None else "未导入外部时序数据", ""],
        ["登记参数总数 / 其中假设值", f"{len(result.parameter_sources)} / {len(assumptions)}",
         "假设值不得视为正式事实（V2 §83）", ""],
    ]
    row = _write_rows(ws, row, rows, formats={2: _NUM4})
    row += 1

    row = _write_header_at(ws, row, ["—— 假设值清单（parameter_sources.is_assumption）——", "", "", ""])
    row = _write_rows(
        ws,
        row,
        [[key, str(result.parameter_sources[key].get("value", "")),
          str(result.parameter_sources[key].get("source_type_label", "")),
          str(result.parameter_sources[key].get("note", ""))] for key in assumptions],
    )
    if not assumptions:
        ws.cell(row=row, column=1, value="无标记为假设值的参数。")
        row += 1
    row += 1

    row = _write_header_at(ws, row, ["—— 引擎口径说明（result.notes）——", "", "", ""])
    for note in result.notes:
        ws.cell(row=row, column=1, value=note)
        row += 1
    if not result.notes:
        ws.cell(row=row, column=1, value=ASSUMPTION_EMPTY_NOTE)
        row += 1
    row += 1

    row = _write_header_at(ws, row, ["级别", "类别", "问题描述", "数量/示例"])
    quality_issues = result.data_quality.issues if result.data_quality is not None else []
    row = _write_rows(
        ws,
        row,
        [[item.level, item.category, item.message, item.count] for item in quality_issues],
        formats={4: _MONEY0},
    )
    if not quality_issues:
        ws.cell(row=row, column=1, value="时序数据质量未发现问题（或未导入外部时序数据）。")
        row += 1
    row += 1

    row = _write_header_at(ws, row, ["—— 方案模拟警告与假设（V2.3 §7.7）——", "", "", ""])
    scenario_lines = (
        [*scenario_result.warnings, *scenario_result.assumptions]
        if scenario_result is not None
        else []
    )
    for text in scenario_lines:
        ws.cell(row=row, column=1, value=text)
        row += 1
    if not scenario_lines:
        ws.cell(
            row,
            column=1,
            value=f"{SCENARIO_COMPARE_EMPTY_TEXT}{SCENARIO_COMPARE_HOWTO}",
        )
        row += 1
    row += 1
    ws.cell(row=row, column=1, value=ASSUMPTION_BOUNDARY)
    _auto_width(ws, max_width=70)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
class ExcelExporter:
    """把 :class:`CalculationResult` 渲染为 Excel 工作簿（规范 §108、§109）。"""

    def export(
        self,
        project: Project,
        result: CalculationResult,
        path: str | Path,
        *,
        self_consumption=None,
        load_portrait=None,
        plan_ids: list[str] | None = None,
        scenario_result=None,
        as_of: datetime | None = None,
    ) -> Path:
        """导出 Excel；返回实际写入路径。

        后缀处理用字符串拼接而**不是** ``Path.with_suffix()``：项目名常含 ``2061.8kWp``、``V1.2`` 这类
        带小数点的片段，``with_suffix`` 会把它们当后缀截掉（``全屋面2061.8kWp`` → ``全屋面2061.xlsx``）。

        ``self_consumption``（``SelfConsumptionResult``）与 ``load_portrait``（``LoadPortrait``）
        为 V2.2 阶段 4 追加的**可选**参数；既有调用方不传时「消纳率分析」表输出
        "尚未执行负荷与消纳分析"的中文说明，**不缺表、不报错、不臆造数值**。

        V2.4 阶段 7 再追加三个**可选**参数（既有调用方不传时行为不变）：

        :param plan_ids: 本次测算采用的电价计划编号列表（供「电价版本与来源」表；
            不传时该表列出电价版本库中已登记计划的摘要）。
        :param scenario_result: 四场景对比总结果 ``ScenarioBillSet``（供「方案电费对比」表；
            不传时该表输出"尚未执行光储四场景账单对比"及操作路径）。
        :param as_of: 报告生成时间（供「计算假设与警告」表；默认取当前时间）。
        """
        target = Path(path)
        if target.suffix.lower() != ".xlsx":
            target = target.with_name(target.name + ".xlsx")
        target.parent.mkdir(parents=True, exist_ok=True)
        generated_at = as_of or datetime.now()

        wb = Workbook()
        wb.remove(wb.active)  # 去掉默认空表，保证工作表数量与规范一致

        _sheet_overview(wb, project, result)
        _sheet_basic(wb, project, result)
        _sheet_technical(wb, project, result)
        _sheet_storage_dispatch(wb, project, result)
        _sheet_load_profile(wb, project, result)
        _sheet_pv_profile(wb, project, result)
        _sheet_tariff(wb, project, result)
        _sheet_tou_tariff(wb, project, result)
        _sheet_investment(wb, project, result)
        _sheet_opex(wb, project, result)
        _sheet_financing(wb, project, result)
        _sheet_hourly_simulation(wb, project, result)
        _sheet_energy_balance(wb, project, result)
        _sheet_annual_summary(wb, project, result)
        _sheet_revenue(wb, project, result)
        _sheet_cashflow(wb, project, result)
        _sheet_metrics(wb, project, result)
        _sheet_sensitivity(wb, project, result)
        _sheet_scenario(wb, project, result)
        _sheet_scenario_compare(wb, project, result)
        _sheet_optimization(wb, project, result)
        _sheet_data_quality(wb, project, result)
        # —— V2.1 §8.1：账单事实与校验（无账单时输出说明，不缺表）——
        _sheet_bill_raw(wb, project, result)
        _sheet_bill_check(wb, project, result)
        # —— V2.2 §6.3 C：消纳率分析（无负荷数据时输出说明，不缺表）——
        _sheet_self_consumption(wb, self_consumption, load_portrait)
        _sheet_policy(wb, project, result)
        _sheet_sources(wb, project, result)
        # —— V2.4 §8.1（阶段 7）：5 张新增表（无数据时输出说明，不缺表、不报错）——
        _sheet_monthly_bill_analysis(wb, project, result)
        _sheet_load_data_quality(wb, load_portrait)
        _sheet_tariff_versions(wb, plan_ids)
        _sheet_scenario_bill_compare(wb, scenario_result)
        _sheet_assumptions_warnings(
            wb, project, result, load_portrait, self_consumption, scenario_result, generated_at
        )

        if wb.sheetnames != SHEET_NAMES:  # pragma: no cover - 结构性自检
            raise RuntimeError(
                f"工作表顺序与 SHEET_NAMES 不一致：实际 {wb.sheetnames}"
            )

        wb.save(target)
        logger.info("导出 Excel：%s（%d 张工作表）", target, len(wb.sheetnames))
        return target

    def export_sheets(self, project: Project, result: CalculationResult) -> list[str]:
        """只返回将会生成的工作表名列表（供测试与界面提示使用，不做计算）。"""
        return list(SHEET_NAMES)


excel_exporter = ExcelExporter()
