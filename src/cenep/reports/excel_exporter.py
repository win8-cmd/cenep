"""Excel 导出（规范 §108、§109、§148、§154）。

铁律
----
**数据全部来自 :class:`CalculationResult`，本模块不做任何计算。**
可以在表内展示公式文本，但数值必须与软件计算结果一致（规范 §109）。

输出为 13 张工作表（规范 §108）：

1. 项目概况  2. 基础参数  3. 技术参数  4. 电价参数  5. 投资参数  6. 运维参数
7. 融资参数  8. 年度现金流  9. 财务指标  10. 敏感性分析  11. 情景分析
12. 政策依据  13. 参数来源
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger

logger = get_logger()

SHEET_NAMES = [
    "项目概况",
    "基础参数",
    "技术参数",
    "电价参数",
    "投资参数",
    "运维参数",
    "融资参数",
    "年度现金流",
    "财务指标",
    "敏感性分析",
    "情景分析",
    "政策依据",
    "参数来源",
]

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
# 对外接口
# --------------------------------------------------------------------------- #
class ExcelExporter:
    """把 :class:`CalculationResult` 渲染为 Excel 工作簿（规范 §108、§109）。"""

    def export(self, project: Project, result: CalculationResult, path: str | Path) -> Path:
        """导出 Excel；返回实际写入路径。

        后缀处理用字符串拼接而**不是** ``Path.with_suffix()``：项目名常含 ``2061.8kWp``、``V1.2`` 这类
        带小数点的片段，``with_suffix`` 会把它们当后缀截掉（``全屋面2061.8kWp`` → ``全屋面2061.xlsx``）。
        """
        target = Path(path)
        if target.suffix.lower() != ".xlsx":
            target = target.with_name(target.name + ".xlsx")
        target.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()
        wb.remove(wb.active)  # 去掉默认空表，保证工作表数量与规范一致

        _sheet_overview(wb, project, result)
        _sheet_basic(wb, project, result)
        _sheet_technical(wb, project, result)
        _sheet_tariff(wb, project, result)
        _sheet_investment(wb, project, result)
        _sheet_opex(wb, project, result)
        _sheet_financing(wb, project, result)
        _sheet_cashflow(wb, project, result)
        _sheet_metrics(wb, project, result)
        _sheet_sensitivity(wb, project, result)
        _sheet_scenario(wb, project, result)
        _sheet_policy(wb, project, result)
        _sheet_sources(wb, project, result)

        wb.save(target)
        logger.info("导出 Excel：%s（%d 张工作表）", target, len(wb.sheetnames))
        return target

    def export_sheets(self, project: Project, result: CalculationResult) -> list[str]:
        """只返回将会生成的工作表名列表（供测试与界面提示使用，不做计算）。"""
        return list(SHEET_NAMES)


excel_exporter = ExcelExporter()
