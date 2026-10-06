"""PDF 报告导出（规范 §110、§111、§148、§154）。

报告结构固定 15 章：

封面 / 项目概况 / 测算条件 / 技术参数 / 电价参数 / 投资估算 / 运营成本 /
收益测算 / 现金流 / 经济指标 / 敏感性分析 / 情景分析 / 风险提示 / 政策依据 / 测算说明

**铁律**：数据全部来自 :class:`CalculationResult` 与 :class:`Project`（仅取参数原值），
本模块不做任何计算（规范 §8、§109）。

中文字体：优先使用系统 TTF（微软雅黑/黑体/宋体），失败时回退到 reportlab 内置的
``STSong-Light``（CID 字体），再失败则回退 Helvetica（会出现乱码，但不会崩溃）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger

logger = get_logger()

#: 报告章节顺序（规范 §110）
REPORT_SECTIONS = [
    "封面",
    "项目概况",
    "测算条件",
    "技术参数",
    "电价参数",
    "投资估算",
    "运营成本",
    "收益测算",
    "现金流",
    "经济指标",
    "敏感性分析",
    "情景分析",
    "风险提示",
    "政策依据",
    "测算说明",
]

#: 免责声明（规范 §111，必须逐字出现）
DISCLAIMER = (
    "本软件用于新能源项目开发阶段的前期经济测算和投资决策辅助，不替代项目正式可行性研究、"
    "工程设计、工程造价咨询、审计、税务咨询、金融机构审查及政府审批文件。"
)
DISCLAIMER_2 = (
    "电价、市场交易、税务、储能收益等政策参数具有时效性，应以项目实施时的最新正式政策及实际合同为准。"
)

_FONT_NAME: str | None = None


def register_cjk_font() -> str:
    """注册并返回可用的中文字体名（结果缓存）。"""
    global _FONT_NAME
    if _FONT_NAME is not None:
        return _FONT_NAME

    candidates: list[tuple[str, int | None]] = [
        (r"C:\Windows\Fonts\msyh.ttc", 0),
        (r"C:\Windows\Fonts\simhei.ttf", None),
        (r"C:\Windows\Fonts\simsun.ttc", 0),
        (r"C:\Windows\Fonts\Deng.ttf", None),
    ]
    for file_path, index in candidates:
        if not Path(file_path).exists():
            continue
        try:
            if index is None:
                pdfmetrics.registerFont(TTFont("CENEP-CJK", file_path))
            else:
                pdfmetrics.registerFont(TTFont("CENEP-CJK", file_path, subfontIndex=index))
            _FONT_NAME = "CENEP-CJK"
            return _FONT_NAME
        except Exception:  # pragma: no cover - 字体损坏等
            continue

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _FONT_NAME = "STSong-Light"
    except Exception:  # pragma: no cover
        _FONT_NAME = "Helvetica"
    return _FONT_NAME


@dataclass
class PdfReport:
    """导出结果回执。"""

    path: Path
    sections: list[str] = field(default_factory=list)


def _styles(font: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName=font, fontSize=22, leading=30),
        "subtitle": ParagraphStyle("st", parent=base["Normal"], fontName=font, fontSize=12, leading=20),
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName=font, fontSize=15, leading=22, spaceBefore=6),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName=font, fontSize=12, leading=18),
        "body": ParagraphStyle("b", parent=base["BodyText"], fontName=font, fontSize=9.5, leading=15),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontName=font, fontSize=8, leading=12),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontName=font, fontSize=8, leading=11),
        "cellr": ParagraphStyle(
            "cr", parent=base["BodyText"], fontName=font, fontSize=8, leading=11, alignment=2
        ),
    }


def _fmt_money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    return "无法计算" if value is None else f"{value:.2%}"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return "未回收" if value is None else f"{value:.{digits}f}"


def _fmt_energy(value: float) -> str:
    return f"{value:,.0f}"


class PdfExporter:
    """把 :class:`CalculationResult` 渲染为 PDF 报告（规范 §110）。"""

    def build_story(self, project: Project, result: CalculationResult, styles: dict) -> list:
        """构造 reportlab 文档流；拆出来便于测试与复用。"""
        story: list = []

        # ---------- 1. 封面 ----------
        story.append(Spacer(1, 40 * mm))
        story.append(Paragraph("工商业新能源项目经济评价报告", styles["title"]))
        story.append(Spacer(1, 10 * mm))
        story.append(Paragraph(f"项目名称：{project.basic_info.project_name}", styles["subtitle"]))
        story.append(Paragraph(f"项目类型：{project.basic_info.project_type.label}", styles["subtitle"]))
        story.append(
            Paragraph(f"项目地点：{project.basic_info.province}{project.basic_info.city}", styles["subtitle"])
        )
        story.append(
            Paragraph(f"评价日期：{project.basic_info.evaluation_date.isoformat()}", styles["subtitle"])
        )
        story.append(
            Paragraph(f"报告生成时间：{datetime.now().isoformat(timespec='seconds')}", styles["subtitle"])
        )
        if project.policy is not None and project.policy.policy_name:
            story.append(Paragraph(f"采用政策：{project.policy.display_version}", styles["subtitle"]))
        story.append(Spacer(1, 20 * mm))
        story.append(Paragraph(DISCLAIMER, styles["small"]))
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(DISCLAIMER_2, styles["small"]))
        story.append(PageBreak())

        # ---------- 2. 项目概况 ----------
        story.append(Paragraph("一、项目概况", styles["h1"]))
        story.append(
            self._table(
                ["项目", "内容"],
                [
                    ["项目名称", project.basic_info.project_name],
                    ["项目类型", project.basic_info.project_type.label],
                    ["省份 / 城市", f"{project.basic_info.province} {project.basic_info.city}"],
                    ["业主名称", project.basic_info.customer_name or "—"],
                    ["所属行业", project.basic_info.industry or "—"],
                    ["计算期", f"{result.analysis_period} 年"],
                    ["光伏容量", f"{result.pv_capacity_kwp:,.2f} kWp"],
                    ["储能规模", f"{result.storage_power_kw:,.2f} kW / {result.storage_energy_kwh:,.2f} kWh"],
                    ["储能时长", f"{result.storage_duration_hours:,.2f} h"],
                    ["总投资", f"{_fmt_money(result.total_capex)} 元"],
                    ["其中：贷款 / 资本金", f"{_fmt_money(result.loan_amount)} / {_fmt_money(result.equity_amount)} 元"],
                ],
                styles,
            )
        )

        # ---------- 3. 测算条件 ----------
        story.append(Paragraph("二、测算条件", styles["h1"]))
        story.append(
            self._table(
                ["项目", "取值"],
                [
                    ["折现率", f"{project.discount_rate:.2%}"],
                    ["年用电量", f"{project.load.annual_load_kwh:,.0f} kWh"],
                    ["年用电量增长率", f"{project.load.annual_load_growth_rate:.2%}"],
                    ["电价模式", project.tariff.tariff_mode.label],
                    ["计算口径说明", "详见“测算说明”章节；所有政策参数均来自政策模板并标注来源。"],
                ],
                styles,
            )
        )

        # ---------- 4. 技术参数 ----------
        story.append(Paragraph("三、技术参数", styles["h1"]))
        p, s = project.pv, project.storage
        story.append(
            self._table(
                ["参数", "取值", "参数", "取值"],
                [
                    ["光伏容量", f"{result.pv_capacity_kwp:,.2f} kWp", "储能功率", f"{s.storage_power_kw:,.2f} kW"],
                    ["年等效利用小时", f"{p.equivalent_hours:,.2f} h", "储能容量", f"{s.storage_energy_kwh:,.2f} kWh"],
                    ["性能比", f"{p.performance_ratio:.4f}", "年循环次数", f"{s.annual_cycles:,.1f}"],
                    ["年衰减率", f"{p.annual_degradation_rate:.2%}", "放电深度 DoD", f"{s.depth_of_discharge:.2%}"],
                    ["限电率", f"{p.curtailment_rate:.2%}", "储能年衰减率", f"{s.annual_degradation_rate:.2%}"],
                    ["自用比例", f"{p.self_consumption_ratio:.2%}", "更换电芯年份", f"{s.replacement_year or '未设置'}"],
                    [
                        "光伏设备更换",
                        (f"第 {p.replacement_year} 年 / {p.replacement_cost_per_kwp:,.2f} 元/kWp"
                         if p.replacement_year else "不更换"),
                        "储能更换投资",
                        f"{s.replacement_capex:,.2f} 元",
                    ],
                ],
                styles,
            )
        )

        # ---------- 5. 电价参数 ----------
        story.append(Paragraph("四、电价参数", styles["h1"]))
        t = project.tariff
        story.append(
            self._table(
                ["参数", "取值（元/kWh）", "参数", "取值"],
                [
                    ["固定电价", f"{t.average_price:.4f}", "峰电量比例", f"{t.peak_ratio:.2%}"],
                    ["峰电价", f"{t.peak_price:.4f}", "平电量比例", f"{t.flat_ratio:.2%}"],
                    ["平电价", f"{t.flat_price:.4f}", "谷电量比例", f"{t.valley_ratio:.2%}"],
                    ["谷电价", f"{t.valley_price:.4f}", "市场电价", f"{t.market_price:.4f}"],
                    ["余电上网电价", f"{t.export_price:.4f}", "绿电价格", f"{t.green_energy_price:.4f}"],
                    ["绿色环境价值", f"{t.green_environmental_value:.4f}", "—", "—"],
                ],
                styles,
            )
        )

        # ---------- 6. 投资估算 ----------
        story.append(Paragraph("五、投资估算", styles["h1"]))
        story.append(
            self._table(
                ["投资项", "金额（元）"],
                [[k, _fmt_money(v)] for k, v in result.capex_breakdown.items()]
                + [["总投资", _fmt_money(result.total_capex)]],
                styles,
                right_align={1},
            )
        )
        story.append(
            Paragraph(
                f"单位投资：{result.unit_investment.get('yuan_per_w', 0.0):,.4f} 元/W（按光伏容量）、"
                f"{result.unit_investment.get('yuan_per_wh', 0.0):,.4f} 元/Wh（按储能容量）。",
                styles["body"],
            )
        )

        # ---------- 7. 运营成本 ----------
        story.append(Paragraph("六、运营成本", styles["h1"]))
        first = result.annual_results[0] if result.annual_results else None
        story.append(
            self._table(
                ["项目", "取值"],
                [
                    ["首年运维费", f"{_fmt_money(result.first_year_opex)} 元"],
                    ["经营期年均运维费", f"{_fmt_money(result.annual_opex)} 元"],
                    ["运维费用年增长率", f"{project.opex.annual_opex_growth_rate:.2%}"],
                    ["首年折旧", f"{_fmt_money(first.depreciation if first else 0.0)} 元"],
                    [
                        "折旧年限 / 残值率",
                        f"{project.tax.depreciation_years} 年 / "
                        f"{project.tax.residual_value_ratio:.2%}（{project.tax.depreciation_method.label}）",
                    ],
                    ["所得税率 / 增值税率", f"{project.tax.income_tax_rate:.2%} / {project.tax.vat_rate:.2%}"],
                    [
                        "LCOE 抵减项",
                        f"增值税抵扣 {project.tax.lcoe_vat_deductible_ratio:.2%}、"
                        f"残值{'抵减' if project.tax.lcoe_residual_credit else '不抵减'}",
                    ],
                ],
                styles,
            )
        )

        # ---------- 8. 收益测算 ----------
        story.append(Paragraph("七、收益测算", styles["h1"]))
        story.append(
            self._table(
                ["收益项", "首年金额（元）"],
                [
                    ["光伏自用收益", _fmt_money(first.pv_self_use_revenue if first else 0.0)],
                    ["光伏上网收益", _fmt_money(first.pv_export_revenue if first else 0.0)],
                    ["储能套利收益", _fmt_money(first.storage_arbitrage_revenue if first else 0.0)],
                    ["储能容量收益", _fmt_money(first.storage_capacity_revenue if first else 0.0)],
                    ["储能辅助服务收益", _fmt_money(first.storage_ancillary_revenue if first else 0.0)],
                    ["储能其他收益", _fmt_money(first.storage_other_revenue if first else 0.0)],
                    ["首年收入合计", _fmt_money(result.first_year_revenue)],
                    ["经营期年均收入", _fmt_money(result.annual_revenue)],
                ],
                styles,
                right_align={1},
            )
        )

        # ---------- 9. 现金流 ----------
        story.append(Paragraph("八、现金流", styles["h1"]))
        cash_rows = [["年份", "发电量(kWh)", "收入(元)", "运维费(元)", "EBITDA(元)", "税费(元)", "项目现金流(元)", "资本金现金流(元)"]]
        cash_rows.append(
            [
                "0",
                "—",
                "—",
                "—",
                "—",
                "—",
                _fmt_money(result.project_cashflows[0]),
                _fmt_money(result.equity_cashflows[0]),
            ]
        )
        for row in result.annual_results:
            cash_rows.append(
                [
                    str(row.year),
                    _fmt_energy(row.pv_generation_kwh),
                    _fmt_money(row.total_revenue),
                    _fmt_money(row.opex),
                    _fmt_money(row.ebitda),
                    _fmt_money(row.cash_tax),
                    _fmt_money(row.project_cashflow),
                    _fmt_money(row.equity_cashflow),
                ]
            )
        story.append(self._table(cash_rows[0], cash_rows[1:], styles, right_align=set(range(1, 8)), repeat=1))

        # ---------- 10. 经济指标 ----------
        story.append(PageBreak())
        story.append(Paragraph("九、经济指标", styles["h1"]))
        story.append(
            self._table(
                ["指标", "数值", "说明"],
                [
                    ["项目财务内部收益率", _fmt_pct(result.project_irr), "所得税后，融资前现金流"],
                    ["资本金财务内部收益率", _fmt_pct(result.equity_irr), "资本金现金流"],
                    ["项目财务净现值", f"{_fmt_money(result.project_npv)} 元", f"折现率 {project.discount_rate:.2%}"],
                    ["资本金财务净现值", f"{_fmt_money(result.equity_npv)} 元", f"折现率 {project.discount_rate:.2%}"],
                    ["静态投资回收期", f"{_fmt_num(result.static_payback)} 年", "自 Year 0 起算"],
                    ["动态投资回收期", f"{_fmt_num(result.discounted_payback)} 年", "自 Year 0 起算"],
                    ["LCOE", "—" if result.lcoe is None else f"{result.lcoe:.4f} 元/kWh", "光伏口径，不含融资利息"],
                    ["LCOS", "—" if result.lcos is None else f"{result.lcos:.4f} 元/kWh", "储能口径，不含融资利息"],
                    ["ROI", "—" if result.roi is None else f"{result.roi:.4f}", "生命周期累计净收益 ÷ 初始总投资"],
                    ["最低 DSCR", "—" if result.min_dscr is None else f"{result.min_dscr:.4f}", "CFADS ÷ 还本付息"],
                ],
                styles,
            )
        )

        # ---------- 11. 敏感性分析 ----------
        story.append(Paragraph("十、敏感性分析", styles["h1"]))
        sens_rows = [["变化因素", "变化率", "项目IRR", "资本金IRR", "项目NPV(元)", "静态回收期(年)"]]
        for row in result.sensitivity:
            sens_rows.append(
                [
                    row.variable_label,
                    f"{row.change:+.0%}",
                    _fmt_pct(row.project_irr),
                    _fmt_pct(row.equity_irr),
                    _fmt_money(row.project_npv),
                    _fmt_num(row.static_payback),
                ]
            )
        if len(sens_rows) == 1:
            sens_rows.append(["—", "—", "—", "—", "—", "—"])
        story.append(self._table(sens_rows[0], sens_rows[1:], styles, right_align={1, 2, 3, 4, 5}))

        # ---------- 12. 情景分析 ----------
        story.append(Paragraph("十一、情景分析", styles["h1"]))
        story.append(
            Paragraph("所有情景均自基准情景复制后施加显式乘数得到，不存在“保守→乐观”的链式推导。", styles["body"])
        )
        scen_rows = [["情景", "总投资(元)", "首年收入(元)", "项目IRR", "资本金IRR", "情景乘数"]]
        for item in result.scenarios:
            scen_rows.append(
                [
                    item.label,
                    _fmt_money(item.total_capex),
                    _fmt_money(item.first_year_revenue),
                    _fmt_pct(item.project_irr),
                    _fmt_pct(item.equity_irr),
                    "、".join(item.deltas) if item.deltas else "基准",
                ]
            )
        if len(scen_rows) == 1:
            scen_rows.append(["—", "—", "—", "—", "—", "—"])
        story.append(self._table(scen_rows[0], scen_rows[1:], styles, right_align={1, 2, 3, 4}))

        # ---------- 13. 风险提示 ----------
        story.append(Paragraph("十二、风险提示", styles["h1"]))
        story.append(Paragraph(self._risk_text(project, result), styles["body"]))

        # ---------- 14. 政策依据 ----------
        story.append(Paragraph("十三、政策依据", styles["h1"]))
        policy = project.policy
        if policy is None:
            story.append(
                Paragraph(
                    "本项目未关联政策模板，全部电价与政策性参数由用户输入，请自行核对项目所在地现行政策。",
                    styles["body"],
                )
            )
        else:
            story.append(
                self._table(
                    ["项目", "内容"],
                    [
                        ["政策名称", policy.policy_name],
                        ["政策版本", policy.policy_version],
                        ["生效日期", policy.effective_date.isoformat() if policy.effective_date else "—"],
                        ["失效日期", policy.expiry_date.isoformat() if policy.expiry_date else "未标注"],
                        ["适用范围", policy.province],
                        ["价格机制", policy.pricing_mechanism],
                        ["市场电价", f"{policy.market_price:.4f} 元/kWh"],
                        ["机制电价", f"{policy.mechanism_price:.4f} 元/kWh"],
                        ["机制电量比例", f"{policy.mechanism_volume_ratio:.2%}"],
                        ["绿电价格 / 环境价值", f"{policy.green_energy_price:.4f} / {policy.green_environmental_value:.4f} 元/kWh"],
                        ["来源", policy.source],
                        ["来源链接", policy.source_url or "—"],
                        ["备注", policy.notes or "—"],
                    ],
                    styles,
                )
            )
            story.append(Paragraph(f"本测算采用政策：{policy.display_version}。", styles["body"]))

        # ---------- 15. 测算说明 ----------
        story.append(Paragraph("十四、测算说明", styles["h1"]))
        for note in result.notes:
            story.append(Paragraph(f"• {note}", styles["body"]))
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("参数来源说明", styles["h2"]))
        source_rows = [["参数", "取值", "来源类型", "备注"]]
        assumptions = 0
        for key, meta in result.parameter_sources.items():
            if meta.get("is_assumption"):
                assumptions += 1
            source_rows.append(
                [
                    key,
                    str(meta.get("value", "")),
                    str(meta.get("source_type_label", "")),
                    ("【假设值】" if meta.get("is_assumption") else "") + str(meta.get("note", "")),
                ]
            )
        if len(source_rows) == 1:
            source_rows.append(["—", "—", "—", "—"])
        story.append(self._table(source_rows[0], source_rows[1:], styles))
        story.append(
            Paragraph(
                f"共登记参数 {len(result.parameter_sources)} 项，其中标记为假设值/系统默认的 {assumptions} 项，"
                "报告中的假设数据不得视为正式事实。",
                styles["small"],
            )
        )
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(DISCLAIMER, styles["small"]))
        story.append(Paragraph(DISCLAIMER_2, styles["small"]))
        return story

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _risk_text(project: Project, result: CalculationResult) -> str:
        """定性风险提示（规范 §106、§143）。

        **不给出"可行/不可行"结论**，只描述与输入参数相关的相对关系与敏感性排序。
        """
        lines: list[str] = []
        lines.append(
            "以下内容为基于本项目输入参数的辅助判断，不构成投资决策结论；"
            "不同投资方、行业、融资条件与项目类别的收益要求不同，请结合自身门槛自行判断。"
        )
        if result.project_irr is not None:
            rel = "高于" if result.project_irr >= project.discount_rate else "低于"
            lines.append(
                f"收益水平：项目财务内部收益率 {result.project_irr:.2%}，{rel}本次采用的折现率 "
                f"{project.discount_rate:.2%}。"
            )
        else:
            lines.append("收益水平：现金流不满足 IRR 求解条件，无法计算内部收益率，请检查投资与收益参数。")
        lines.append(
            f"投资回收能力：静态回收期 {_fmt_num(result.static_payback)} 年，"
            f"动态回收期 {_fmt_num(result.discounted_payback)} 年，计算期为 {result.analysis_period} 年。"
        )
        strongest = [x for x in result.sensitivity if x.coefficient is not None and x.change > 0]
        if strongest:
            top = sorted(strongest, key=lambda x: abs(x.coefficient or 0.0), reverse=True)[:3]
            lines.append(
                "敏感性：对项目 IRR 影响最大的三个因素是 "
                + "、".join(f"{x.variable_label}（敏感度系数 {x.coefficient:.2f}）" for x in top)
                + "。"
            )
        if project.policy is None:
            lines.append("政策依赖程度：本项目未关联政策模板，电价与政策性参数完全取决于用户输入，政策变动风险由使用方自行评估。")
        else:
            lines.append(
                f"政策依赖程度：本项目采用政策模板（版本 {project.policy.policy_version}），"
                "政策调整会直接影响测算结果，请在实施前核对是否为现行版本。"
            )
        if project.financing.enabled and project.financing.debt_ratio > 0:
            dscr = "—" if result.min_dscr is None else f"{result.min_dscr:.2f}"
            lines.append(
                f"融资风险：存在债务资金（贷款比例 {project.financing.debt_ratio:.0%}），"
                f"最低偿债备付率为 {dscr}，请结合贷款行要求自行判断。"
            )
        else:
            lines.append("融资风险：本项目未使用债务资金（或贷款比例为 0），无还本付息压力。")
        lines.append(
            "运行风险：V1 采用年度等效循环模型，未做 8760 小时仿真；实际充放电次数、分时电价结构与"
            "设备可用率波动会影响实际收益，应按保守口径复核。"
        )
        return "<br/>".join(lines)

    @staticmethod
    def _table(
        header: list[str],
        rows: list[list],
        styles: dict,
        right_align: set[int] | None = None,
        repeat: int = 0,
    ) -> Table:
        right_align = right_align or set()
        data = [[Paragraph(str(h), styles["cell"]) for h in header]]
        for row in rows:
            data.append(
                [
                    Paragraph(str(cell), styles["cellr"] if idx in right_align else styles["cell"])
                    for idx, cell in enumerate(row)
                ]
            )
        table = Table(data, repeatRows=repeat, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDEBF7")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return table

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def export(self, project: Project, result: CalculationResult, path: str | Path) -> Path:
        """导出 PDF 报告，返回实际写入路径。

        后缀处理用字符串拼接而**不是** ``Path.with_suffix()``：项目名常含 ``2061.8kWp``、``V1.2`` 这类
        带小数点的片段，``with_suffix`` 会把它们当后缀截掉。
        """
        target = Path(path)
        if target.suffix.lower() != ".pdf":
            target = target.with_name(target.name + ".pdf")
        target.parent.mkdir(parents=True, exist_ok=True)

        font = register_cjk_font()
        styles = _styles(font)
        doc = SimpleDocTemplate(
            str(target),
            pagesize=A4,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=18 * mm,
            bottomMargin=16 * mm,
            title=f"{project.basic_info.project_name} 经济评价报告",
            author="CENEP V1",
            subject="工商业新能源项目前期经济测算",
        )
        story = self.build_story(project, result, styles)
        doc.build(story, onFirstPage=self._decorate, onLaterPages=self._decorate)
        logger.info("导出 PDF：%s", target)
        return target

    @staticmethod
    def _decorate(canvas, doc) -> None:
        """页眉页脚与页码（规范 §110）。"""
        canvas.saveState()
        font = register_cjk_font()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#808080"))
        canvas.drawString(16 * mm, A4[1] - 12 * mm, "工商业新能源项目经济评价报告")
        canvas.drawRightString(A4[0] - 16 * mm, A4[1] - 12 * mm, "前期测算工具输出，非正式可研文件")
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f"第 {doc.page} 页")
        canvas.restoreState()


pdf_exporter = PdfExporter()
