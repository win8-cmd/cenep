"""PDF 报告导出（规范 §110、§111、§148、§154；V2.1 §8.1；V2.2 §6.3）。

报告结构固定 18 部分：

封面 / 项目概况 / 输入参数 / **账单事实与校验（V2.1 新增）** / 负荷分析 /
**负荷估算与光伏消纳（V2.2 阶段 4 新增）** / PV时序分析 /
储能SOC分析 / 能源流 / 电费分析 / 储能收益 / 投资 / 现金流 / 经济指标 / 方案比较 /
敏感性 / 风险 / 参数来源 / 免责声明

**铁律**：数据全部来自 :class:`CalculationResult`、:class:`Project`（含账单段与负荷数据集段）、
账单服务 ``BillService`` 与负荷消纳服务 ``LoadProfileService`` **已经算好的结果**
（本模块只接收 ``SelfConsumptionResult`` / ``LoadPortrait`` 对象，不做任何计算，
规范 §8、§109）。因此"消纳率"与"口徑"在本模块里只能被**打印**，不能被**推导**。

「负荷估算与光伏消纳」章节的硬要求（V2.2 §0.2、§3.3、§12）：

* **估算与实测必须一眼可辨**：估算曲线在本章以"估算数据（不是实测）"徽标与
  ⚠ 提示显著标注，并与实测曲线使用不同的口径说明；
* **四项指标必须写清口径**：光伏自用率 / 负荷覆盖率 / 上网率 / 电网依赖率的
  分子、分母、单位与边界逐条列出（口径文本来自
  :data:`cenep.calculation.self_consumption.CALIBERS`，与界面、Excel 完全同源）；
* 结果自带的 ``assumptions`` 逐条进入「关键假设与数据缺口」。

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
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
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

from .. import APP_NAME
from ..application.bill_service import BillService
from ..domain.bill_models import BILL_FIELD_LABELS, label_of
from ..domain.models import Project
from ..domain.results import CalculationResult
from ..infrastructure.logging_setup import get_logger

logger = get_logger()

#: 报告章节顺序。V2 §66 将 V1 §110 的 15 章重组为 16 部分；
#: V2.1 §8.1（阶段 2）在「输入参数」之后插入「账单事实与校验」；
#: V2.2 §6.3（阶段 4）在「负荷分析」之后插入「负荷估算与光伏消纳」，
#: 因此共 **18 部分**（既有 16 部分的名目与内容一项未丢，只是编号顺延）。
REPORT_SECTIONS = [
    "项目概况",
    "输入参数",
    "账单事实与校验",
    "负荷分析",
    "负荷估算与光伏消纳",
    "PV时序分析",
    "储能SOC分析",
    "能源流",
    "电费分析",
    "储能收益",
    "投资",
    "现金流",
    "经济指标",
    "方案比较",
    "敏感性",
    "风险",
    "参数来源",
    "免责声明",
]

#: 账单字段「未提供」的统一文案（与界面 / Excel 一致，V2.1 §2.1：None ≠ 0）
BILL_NOT_PROVIDED_TEXT = "账单未提供"

#: 无账单时的说明段落（§8.2：旧项目 / 无账单项目照常出报告）
BILL_EMPTY_TEXT = "本项目尚未录入电费账单。"
BILL_EMPTY_HOWTO = (
    "录入方法：在「月度账单」页手动录入账期、电量与费用分项；"
    "或点击「下载导入模板」，按《CENEP_电费账单导入模板.xlsx》填写后经「导入向导」"
    "六步（选文件 → 选表 → 映射列 → 预览 → 校验 → 确认）导入；也可直接导入已有 Excel / CSV 账单表。"
)
BILL_BOUNDARY_NOTE = (
    "账单事实与模拟结果分界：本章只列账单事实（手动录入 / Excel 导入 / 用户标注的估算）。"
    "账单复算、账单模拟与光储方案节省额属于阶段 5 / 6，当前为『待确认 / 未建模』，"
    "本报告不包含任何模拟账单结论（V2.1 §1、§3.4、§8.1）。"
)

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


# --------------------------------------------------------------------------- #
# V2 图表（V2 §66；用 reportlab.graphics 直接绘制，不引入新依赖）
#
# 设计约束：只画**抽样**数据（典型日 24 点、12 个月），不把 8760 点画进报告，
# 否则 PDF 体积与渲染时间都会失控。图表数据一律取自 CalculationResult。
# --------------------------------------------------------------------------- #
_CHART_FRAME = "#BFBFBF"


def _chart_lines(
    title: str,
    categories: list[str],
    series: list[tuple[str, list[float], str]],
    width: float = 460.0,
    height: float = 180.0,
) -> Drawing:
    """折线图：多序列共用横轴，用于典型日曲线与 SOC 曲线。"""
    d = Drawing(width, height)
    left, right, top, bottom = 46.0, 8.0, 26.0, 24.0
    plot_w = max(width - left - right, 10.0)
    plot_h = max(height - top - bottom, 10.0)
    d.add(
        Rect(
            left,
            bottom,
            plot_w,
            plot_h,
            strokeColor=colors.HexColor(_CHART_FRAME),
            fillColor=None,
        )
    )
    d.add(String(left, height - 13, title, fontSize=8))

    all_values = [v for _label, values, _color in series for v in values] or [0.0]
    vmax = max(all_values) if max(all_values) > 0 else 1.0
    n = max(len(categories), 1)
    step = max(n - 1, 1)

    for _label, values, color in series:
        points: list[float] = []
        for i, value in enumerate(values[:n]):
            points.append(left + plot_w * i / step)
            points.append(bottom + plot_h * max(min(value / vmax, 1.0), 0.0))
        if len(points) >= 4:
            d.add(PolyLine(points, strokeColor=colors.HexColor(color), strokeWidth=0.9))

    for i in range(0, n, 4):
        if i < len(categories):
            d.add(String(left + plot_w * i / step - 4, bottom - 11, categories[i], fontSize=6))
    d.add(String(2, bottom + plot_h - 7, f"{vmax:,.0f}", fontSize=6))
    d.add(String(2, bottom - 2, "0", fontSize=6))

    legend_x = left + 4
    for label, _values, color in series:
        d.add(
            Line(
                legend_x,
                height - 20,
                legend_x + 10,
                height - 20,
                strokeColor=colors.HexColor(color),
                strokeWidth=1.6,
            )
        )
        d.add(String(legend_x + 13, height - 23, label, fontSize=7))
        legend_x += 13 + 12 * len(label) + 16
    return d


def _chart_bars(
    title: str,
    categories: list[str],
    values: list[float],
    width: float = 460.0,
    height: float = 165.0,
    color: str = "#4472C4",
) -> Drawing:
    """柱状图：单序列，用于逐月电量。"""
    d = Drawing(width, height)
    left, right, top, bottom = 46.0, 8.0, 26.0, 24.0
    plot_w = max(width - left - right, 10.0)
    plot_h = max(height - top - bottom, 10.0)
    d.add(
        Rect(
            left,
            bottom,
            plot_w,
            plot_h,
            strokeColor=colors.HexColor(_CHART_FRAME),
            fillColor=None,
        )
    )
    d.add(String(left, height - 13, title, fontSize=8))

    vmax = max(values) if values and max(values) > 0 else 1.0
    n = max(len(values), 1)
    slot = plot_w / n
    bar_w = slot * 0.6
    for i, value in enumerate(values):
        bar_h = plot_h * max(min(value / vmax, 1.0), 0.0)
        x = left + slot * i + (slot - bar_w) / 2
        d.add(
            Rect(
                x,
                bottom,
                bar_w,
                bar_h,
                strokeColor=None,
                fillColor=colors.HexColor(color),
            )
        )
    for i in range(n):
        if i < len(categories):
            d.add(String(left + slot * i + slot * 0.28, bottom - 11, categories[i], fontSize=6))
    d.add(String(2, bottom + plot_h - 7, f"{vmax:,.0f}", fontSize=6))
    d.add(String(2, bottom - 2, "0", fontSize=6))
    return d


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


# --------------------------------------------------------------------------- #
# V2.1 账单章节的显示格式（§8.1；只做格式化，不做任何计算）
#
# ``None`` 一律显示「账单未提供」/「无法判断」，**绝不显示 0**（V2.1 §2.1）。
# --------------------------------------------------------------------------- #
def _bill_kwh(value: float | None) -> str:
    return BILL_NOT_PROVIDED_TEXT if value is None else f"{value:,.2f} kWh"


def _bill_yuan(value: float | None) -> str:
    return BILL_NOT_PROVIDED_TEXT if value is None else f"{value:,.2f} 元"


def _bill_price(value: float | None) -> str:
    return "无法计算（账单未提供电量或金额）" if value is None else f"{value:,.4f} 元/kWh"


def _bill_diff(value: float | None, suffix: str = "") -> str:
    return BILL_NOT_PROVIDED_TEXT if value is None else f"{value:,.4f}{suffix}"


def _bill_tristate(value: bool | None) -> str:
    """三态：``True`` 一致 / ``False`` 超容差 / ``None`` 无法判断（数据不足，不按 0）。"""
    if value is None:
        return "无法判断（账单未提供相关字段）"
    return "一致" if value else "超容差"


def _pdf_metric_rows(result) -> list[list[str]]:
    """四项消纳指标的 ``[中文名, 显示值, 口径全文]``（V2.2 §3.3、§12）。

    口径全文与界面、Excel 完全同源（都来自
    :data:`cenep.calculation.self_consumption.CALIBERS`），
    因此报告里的"消纳率"永远带着它的分子与分母，不会被读成另一种口径。
    分母为 0 时显示"不适用"，**不显示 0%**（§3.3）。
    """
    rows: list[list[str]] = []
    for caliber in result.calibers:
        value = result.rate_of(caliber.key)
        rows.append(
            [
                caliber.name,
                "不适用" if value is None else f"{value:.2%}",
                caliber.text(),
            ]
        )
    return rows


class PdfExporter:
    """把 :class:`CalculationResult` 渲染为 PDF 报告（规范 §110）。"""

    def build_story(
        self,
        project: Project,
        result: CalculationResult,
        styles: dict,
        *,
        self_consumption=None,
        load_portrait=None,
    ) -> list:
        """构造 reportlab 文档流；拆出来便于测试与复用。

        :param self_consumption: 可选的 :class:`cenep.domain.self_consumption_result.SelfConsumptionResult`
            （V2.2 阶段 4）。为 ``None`` 时本章输出"尚未执行消纳分析"的中文说明，
            **不臆造任何数值**；既有调用方（只传三个位置参数）行为完全不变。
        :param load_portrait: 可选的 :class:`cenep.calculation.load_portrait.LoadPortrait`
            （负荷画像）。为 ``None`` 时只输出消纳部分。
        """
        self._self_consumption = self_consumption
        self._load_portrait = load_portrait
        story: list = []

        # ---------- 封面 ----------
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


        # ---------- 一、项目概况 ----------
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


        # ---------- 二、输入参数 ----------
        story.append(Paragraph("二、输入参数", styles["h1"]))
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


        # ---------- 输入参数·技术 ----------
        story.append(Paragraph("（一）技术参数", styles["h2"]))
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


        # ---------- 输入参数·电价 ----------
        story.append(Paragraph("（二）电价参数", styles["h2"]))
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


        # ---------- V2 时序章节（三~八）----------
        # ================= V2 时序章节（三~八，V2 §66）=================
        # 无时序结果时每章仍须出现并给出说明，保证 V1 项目导出的报告结构完整。
        ts = result.time_series_results
        has_ts = ts is not None and ts.hourly is not None and len(ts.hourly) > 0
        _NO_TS = (
            "本项目未启用时序仿真，本章无数据。启用方法：在「参数 → 时序仿真」中打开开关，"
            "填写负荷曲线、光伏曲线与分时电价三条曲线后重新计算。"
        )
        _summer = (
            [i for i, t in enumerate(ts.hourly.timestamps) if t.month == 7 and t.day == 15]
            if has_ts
            else []
        )
        _hours = [f"{h:02d}" for h in range(24)]

        # ---------- 三、账单事实与校验（V2.1 §8.1，阶段 2 新增） ----------
        self._bill_section(story, project, styles)


        # ---------- 四、负荷分析 ----------
        story.append(Paragraph("四、负荷分析", styles["h1"]))
        if not has_ts:
            story.append(Paragraph(_NO_TS, styles["body"]))
        else:
            load = ts.hourly.column("load")
            row_lp = [
                ["年用电量（kWh）", _fmt_energy(float(sum(load)))],
                ["最大负荷（kW）", _fmt_num(max(load))],
                ["最小负荷（kW）", _fmt_num(min(load))],
                ["平均负荷（kW）", _fmt_num(float(sum(load)) / max(len(load), 1))],
                ["逐时点数", f"{len(load):,}（{ts.resolution.label}）"],
                ["取得方式", project.timeseries.load.mode.label],
                ["年负荷增长率", _fmt_pct(project.timeseries.load.annual_growth_rate)],
            ]
            story.append(self._table(["项目", "取值"], row_lp, styles, right_align={1}))
            if _summer:
                series = [
                    ("负荷", [float(load[i]) for i in _summer], "#C00000"),
                    ("光伏", [float(ts.hourly.column("pv_generation")[i]) for i in _summer], "#2E75B6"),
                ]
                story.append(Spacer(1, 3 * mm))
                story.append(_chart_lines("图 1  典型日（7 月 15 日）负荷与光伏出力", _hours, series))

        # ---------- 五、负荷估算与光伏消纳（V2.2 §6.3 C、§3.3；阶段 4 新增） ----------
        self._load_consumption_section(story, project, styles)

        # ---------- 六、PV时序分析 ----------
        story.append(Paragraph("六、PV时序分析", styles["h1"]))
        if not has_ts:
            story.append(Paragraph(_NO_TS, styles["body"]))
        else:
            pv = ts.hourly.column("pv_generation")
            cap = result.pv_capacity_kwp or 0.0
            total_pv = float(sum(pv))
            row_pv = [
                ["年发电量（kWh）", _fmt_energy(total_pv)],
                ["等效利用小时（h）", _fmt_num(total_pv / cap if cap > 0 else 0.0)],
                ["最大出力（kW）", _fmt_num(max(pv))],
                ["弃光电量（kWh）", _fmt_energy(ts.metrics.annual_pv_curtailment)],
                ["自用率（V2 §22）", _fmt_pct(ts.metrics.self_consumption_rate)],
                ["自给率（V2 §23）", _fmt_pct(ts.metrics.self_sufficiency_rate)],
            ]
            story.append(self._table(["项目", "取值"], row_pv, styles, right_align={1}))
            months = [t.month for t in ts.hourly.timestamps]
            monthly = [
                float(sum(pv[i] for i in range(len(pv)) if months[i] == m)) for m in range(1, 13)
            ]
            story.append(Spacer(1, 3 * mm))
            story.append(
                _chart_bars(
                    "图 2  逐月上网电量（kWh）",
                    [f"{m}月" for m in range(1, 13)],
                    [
                        float(
                            sum(
                                ts.hourly.column("grid_export")[i]
                                for i in range(len(pv))
                                if months[i] == m
                            )
                        )
                        for m in range(1, 13)
                    ],
                )
            )

        # ---------- 六、储能SOC分析 ----------
        story.append(Paragraph("七、储能SOC分析", styles["h1"]))
        # 判据用「仿真中是否真的充放过电」而不是 result.storage_energy_kwh：
        # 当项目类型为 COMMERCIAL_PV（has_storage=False）但配置了储能时，
        # 引擎的年度模型不认储能，result.storage_* 会是 0，而 V2 时序仿真实际用了储能。
        _st_charge = ts.metrics.annual_storage_charge if has_ts else 0.0
        _st_discharge = ts.metrics.annual_storage_discharge if has_ts else 0.0
        _cap = float(project.storage.storage_energy_kwh) or float(result.storage_energy_kwh or 0.0)
        _power = float(project.storage.storage_power_kw) or float(result.storage_power_kw or 0.0)
        if not has_ts:
            story.append(Paragraph(_NO_TS, styles["body"]))
        elif _cap <= 0.0 and (_st_charge + _st_discharge) <= 0.0:
            story.append(Paragraph("本项目未配置储能，无 SOC 数据。", styles["body"]))
        else:
            soc = ts.hourly.column("storage_soc_end")
            row_soc = [
                ["储能容量（kWh）", _fmt_num(_cap)],
                ["储能功率（kW）", _fmt_num(_power)],
                ["SOC 下限 / 上限", f"{_fmt_pct(project.timeseries.dispatch.soc_min)} / {_fmt_pct(project.timeseries.dispatch.soc_max)}"],
                ["SOC 实际最低 / 最高", f"{_fmt_pct(min(soc))} / {_fmt_pct(max(soc))}"],
                ["年充电量（kWh）", _fmt_energy(ts.metrics.annual_storage_charge)],
                ["年放电量（kWh）", _fmt_energy(ts.metrics.annual_storage_discharge)],
                ["其中电网充电（kWh）", _fmt_energy(ts.metrics.annual_grid_charge)],
                ["等效循环次数（实际 / 配置）", f"{_fmt_num(ts.metrics.equivalent_cycles)} / {_fmt_num(ts.metrics.configured_cycles)}"],
                ["调度策略", ts.dispatch_strategy.label],
            ]
            story.append(self._table(["项目", "取值"], row_soc, styles, right_align={1}))
            if _summer:
                story.append(Spacer(1, 3 * mm))
                story.append(
                    _chart_lines(
                        "图 3  典型日（7 月 15 日）储能 SOC 曲线",
                        _hours,
                        [("SOC", [float(soc[i]) * 100.0 for i in _summer], "#548235")],
                    )
                )

        # ---------- 七、能源流 ----------
        story.append(Paragraph("八、能源流", styles["h1"]))
        bal = result.energy_balance
        if bal is None:
            story.append(Paragraph(_NO_TS, styles["body"]))
        else:
            row_flow = [
                ["光伏发电（kWh）", _fmt_energy(bal.pv_generation)],
                ["　→ 直接供负荷", _fmt_energy(bal.pv_to_load)],
                ["　→ 给储能充电", _fmt_energy(bal.pv_to_storage)],
                ["　→ 上网", _fmt_energy(bal.pv_to_grid)],
                ["　→ 弃光", _fmt_energy(bal.pv_curtailed)],
                ["电网购电（kWh）", _fmt_energy(bal.grid_import)],
                ["　→ 供负荷", _fmt_energy(bal.grid_to_load)],
                ["　→ 给储能充电", _fmt_energy(bal.grid_to_storage)],
                ["储能放电（kWh）", _fmt_energy(bal.storage_discharge)],
                ["负荷（kWh）", _fmt_energy(bal.load_total)],
                ["上网电量（kWh）", _fmt_energy(bal.grid_export)],
                ["供给合计（kWh）", _fmt_energy(bal.supply_total)],
                ["需求合计（kWh）", _fmt_energy(bal.demand_total)],
                ["平衡误差（kWh）", f"{bal.error:.3e}"],
                ["容差（kWh）", f"{bal.tolerance:.1e}"],
                ["是否平衡（V2 §19）", "是" if bal.is_balanced else "否"],
            ]
            story.append(self._table(["能源流项目", "数值"], row_flow, styles, right_align={1}))

        # ---------- 八、电费分析 ----------
        story.append(Paragraph("九、电费分析", styles["h1"]))
        if not has_ts:
            story.append(Paragraph(_NO_TS, styles["body"]))
        else:
            m = ts.metrics
            row_fee = [
                ["基准电费（元）", _fmt_money(m.baseline_electricity_cost)],
                ["实际电费（元）", _fmt_money(m.actual_electricity_cost)],
                ["电费节省（元）", _fmt_money(m.electricity_cost_saving)],
                ["　其中：光伏自用节省（元）", _fmt_money(m.pv_self_consumption_saving)],
                ["　其中：储能套利收益（元）", _fmt_money(m.storage_arbitrage_revenue)],
                ["基准需量电费（元）", _fmt_money(m.baseline_demand_cost)],
                ["实际需量电费（元）", _fmt_money(m.actual_demand_cost)],
                ["需量电费节省（元）", _fmt_money(m.demand_cost_saving)],
                ["最大需量 削减前 / 后（kW）", f"{_fmt_num(m.peak_demand_before)} / {_fmt_num(m.peak_demand_after)}"],
            ]
            story.append(self._table(["项目", "金额"], row_fee, styles, right_align={1}))
            story.append(Spacer(1, 2 * mm))
            story.append(
                Paragraph(
                    "口径说明：电费节省按现金口径计算（Σ 负荷×电价 − Σ 购电×电价）；"
                    "其分解项之和与之严格相等（光伏自用节省 + 储能套利收益），"
                    "其中光伏给储能充电的电量按 0 计价，避免与自用节省重复计算（V2 §28、§29）。",
                    styles["small"],
                )
            )

        # ---------- 九、储能收益 ----------
        story.append(Paragraph("十、储能收益", styles["h1"]))
        if not has_ts:
            story.append(Paragraph(_NO_TS, styles["body"]))
        else:
            m = ts.metrics
            row_st = [
                ["储能套利收益（元）", _fmt_money(m.storage_arbitrage_revenue)],
                ["储能容量收益（元）", _fmt_money(m.storage_capacity_revenue)],
                ["储能辅助服务收益（元）", _fmt_money(m.storage_ancillary_revenue)],
                ["其他收益（元）", _fmt_money(m.other_revenue)],
                ["储能收益合计（元）", _fmt_money(m.storage_arbitrage_revenue + m.storage_capacity_revenue + m.storage_ancillary_revenue + m.other_revenue)],
                ["等效循环次数（V2 §24）", _fmt_num(m.equivalent_cycles)],
                ["光伏上网收入（元）", _fmt_money(m.pv_export_revenue)],
                ["首年收益合计（元）", _fmt_money(m.total_benefit)],
            ]
            story.append(self._table(["项目", "金额"], row_st, styles, right_align={1}))
            story.append(Spacer(1, 2 * mm))
            story.append(
                Paragraph(
                    "套利收益按逐时实际充放电计算：Σ(放电量×替代电价 − 充电量×充电电价)，"
                    "不使用「放电量 × 峰谷价差」的简化口径（V2 §29）。",
                    styles["small"],
                )
            )


        # ---------- 十、投资 ----------
        story.append(Paragraph("十一、投资", styles["h1"]))
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


        # ---------- 十一、现金流 ----------
        story.append(Paragraph("十二、现金流", styles["h1"]))

        story.append(Paragraph("（一）运营成本", styles["h2"]))
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


        # ---------- 现金流·年度 ----------
        story.append(Paragraph("（二）年度现金流", styles["h2"]))
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


        # ---------- 十二、经济指标 ----------
        story.append(Paragraph("十三、经济指标", styles["h1"]))

        story.append(Paragraph("（一）收益测算", styles["h2"]))
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


        # ---------- 经济指标·指标 ----------
        story.append(PageBreak())
        story.append(Paragraph("（二）经济指标", styles["h2"]))
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


        # ---------- 十三、方案比较 ----------
        story.append(Paragraph("十四、方案比较", styles["h1"]))
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


        # ---------- 十四、敏感性 ----------
        story.append(Paragraph("十五、敏感性", styles["h1"]))
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


        # ---------- 十五、风险 ----------
        story.append(Paragraph("十六、风险", styles["h1"]))
        story.append(Paragraph(self._risk_text(project, result), styles["body"]))


        # ---------- 十六、参数来源 ----------
        story.append(Paragraph("十七、参数来源", styles["h1"]))
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


        # ---------- 十六、参数来源·说明 ----------
        story.append(Paragraph("（一）测算说明", styles["h2"]))
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
        story.append(Paragraph("十八、免责声明", styles["h1"]))

        story.append(Paragraph(DISCLAIMER, styles["small"]))
        story.append(Paragraph(DISCLAIMER_2, styles["small"]))

        return story

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _load_consumption_section(
        self, story: list, project: Project, styles: dict
    ) -> None:
        """「五、负荷估算与光伏消纳」章节（V2.2 §6.3 C、§3.3、§0.2；阶段 4 新增）。

        * 数据来源：``LoadProfileService`` 已算好的
          :class:`~cenep.domain.self_consumption_result.SelfConsumptionResult`
          与 :class:`~cenep.calculation.load_portrait.LoadPortrait`；
        * **本方法不做任何算术**：四项指标、逐月明细、口径文本与假设全部来自结果对象
          （§0.2"核心公式仍在 calculation/"）；
        * 无结果时输出"尚未执行消纳分析"的中文说明与操作指引，**不缺章节、不报错**；
        * 分母为 0 的指标显示"不适用"，**不显示 0%**（§3.3）。
        """
        story.append(Paragraph("五、负荷估算与光伏消纳", styles["h1"]))
        result = getattr(self, "_self_consumption", None)
        portrait = getattr(self, "_load_portrait", None)

        if result is None and portrait is None:
            story.append(
                Paragraph(
                    "尚未执行负荷与消纳分析：本项目还没有可用的负荷数据集"
                    "（实测高频负荷导入或月账单估算），或本次导出未附带消纳结果。",
                    styles["body"],
                )
            )
            story.append(
                Paragraph(
                    "操作路径：在「负荷与消纳」页面 A 区选择数据来源 → 生成估算曲线或导入实测曲线 → "
                    "在 C 区点击「计算光伏消纳四项指标」→ 再导出报告。"
                    "在没有任何负荷数据时，本报告**不提供**任何消纳率结论"
                    "（V2.2 §6.4、§12：不得用未经说明的默认值制造精确结果）。",
                    styles["small"],
                )
            )
            return

        if portrait is not None:
            story.append(Paragraph("（一）负荷数据来源与质量", styles["h2"]))
            rows = [[name, value] for name, value in portrait.portrait_rows()]
            story.append(self._table(["项目", "取值"], rows, styles))
            if portrait.monthly:
                story.append(Spacer(1, 3 * mm))
                story.append(Paragraph("逐月电量与负荷率", styles["h2"]))
                story.append(
                    self._table(
                        ["月份", "电量 kWh", "最大功率 kW", "平均功率 kW", "负荷率", "缺失点"],
                        [
                            [
                                item.month_key,
                                _fmt_energy(item.energy_kwh),
                                _fmt_num(item.peak_power_kw),
                                _fmt_num(item.avg_power_kw),
                                _fmt_pct(item.load_factor),
                                str(item.missing_value_count),
                            ]
                            for item in portrait.monthly
                        ],
                        styles,
                        right_align={1, 2, 3, 4},
                    )
                )
            if portrait.estimated:
                story.append(Spacer(1, 2 * mm))
                story.append(
                    Paragraph(
                        "⚠ 本负荷曲线为【估算曲线，不是实测】：由月电量与可编辑典型负荷模板生成，"
                        "估算方法与全部假设见本章（四）。任何基于该曲线的消纳率与收益均只能表述为"
                        "『基于估算』（V2.2 §0.2 红线）。",
                        styles["small"],
                    )
                )

        if result is None:
            story.append(
                Paragraph(
                    "本次导出未附带消纳计算结果，因此不列出四项指标；"
                    "请先在「负荷与消纳」页面 C 区执行消纳分析。",
                    styles["body"],
                )
            )
            return

        story.append(Paragraph("（二）数据来源标签（实测 / 估算必须区分）", styles["h2"]))
        story.append(
            self._table(
                ["项目", "内容"],
                [
                    ["负荷来源标签", result.load_source_type.label],
                    ["是否估算数据", "估算（不是实测）" if result.is_based_on_estimate else "实测导入"],
                    ["来源徽标", result.estimate_badge],
                    ["负荷曲线说明", result.load_provenance_text or "—"],
                    ["光伏曲线说明", result.pv_provenance_text or "—"],
                    ["时间覆盖率（≠负荷覆盖率）", _fmt_pct(result.coverage_ratio)],
                    ["数据质量等级", result.data_quality_status.label],
                    ["时间间隔 / 间隔数", f"{result.interval_minutes} 分钟 / {result.point_count:,}"],
                ],
                styles,
            )
        )

        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("（三）电量与四项消纳指标（含计算口径）", styles["h2"]))
        story.append(
            self._table(
                ["项目", "数值（kWh）", "口径"],
                [[name, f"{value:,.3f}", note] for name, value, note in result.energy_rows()],
                styles,
                right_align={1},
            )
        )
        story.append(Spacer(1, 3 * mm))
        story.append(
            self._table(
                ["指标", "数值", "口径（分子 / 分母 / 单位 / 边界）"],
                [
                    [name, value, caliber]
                    for name, value, caliber in _pdf_metric_rows(result)
                ],
                styles,
            )
        )
        story.append(
            Paragraph(
                f"逐间隔能量平衡：最大误差 {result.max_interval_balance_error_kwh:.3e} kWh、"
                f"全年合计误差 {result.energy_balance_error_kwh:.3e} kWh，"
                f"容差 {result.balance_tolerance_kwh:g} kWh；"
                f"{'逐间隔守恒校验通过' if result.is_balanced else '★守恒校验未通过'}。"
                "不含储能时逐间隔满足「负荷 = 自发自用 + 购电」与「光伏 = 自发自用 + 上网」"
                "（V2.2 §3.3、§19）。",
                styles["small"],
            )
        )

        if result.monthly:
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph("（四）逐月消纳明细（月度自用率趋势）", styles["h2"]))
            story.append(
                self._table(
                    [
                        "月份",
                        "负荷 kWh",
                        "光伏 kWh",
                        "自用 kWh",
                        "上网 kWh",
                        "购电 kWh",
                        "自用率",
                        "负荷覆盖率",
                        "上网率",
                        "电网依赖率",
                    ],
                    [
                        [
                            row.month_key,
                            _fmt_energy(row.load_energy_kwh),
                            _fmt_energy(row.pv_generation_kwh),
                            _fmt_energy(row.pv_used_on_site_kwh),
                            _fmt_energy(row.pv_export_kwh),
                            _fmt_energy(row.grid_import_kwh),
                            row.rate_text("self_consumption"),
                            row.rate_text("load_coverage"),
                            row.rate_text("export"),
                            row.rate_text("grid_dependency"),
                        ]
                        for row in result.monthly
                    ],
                    styles,
                    right_align={1, 2, 3, 4, 5},
                )
            )

        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("（五）关键假设、口径与数据缺口", styles["h2"]))
        for line in result.assumption_lines():
            story.append(Paragraph(f"• {line}", styles["small"]))

    def _bill_section(self, story: list, project: Project, styles: dict) -> None:
        """「三、账单事实与校验」章节（V2.1 §8.1、§3.1、§5.5；阶段 2 新增）。

        * 数据来源：``project.bills``（账单**事实**）与
          ``BillService.reconcile_all()`` 已算好的差异与假设；
        * **本方法不做任何算术**——求和、差异、平均电价全部由计算层产出（§0.2）；
        * 无账单时输出说明段落与录入方法，**不缺章节、不报错**（§8.2）；
        * ``None``（账单未提供）一律显示「账单未提供」，不显示 0（§2.1）。
        """
        story.append(Paragraph("三、账单事实与校验", styles["h1"]))
        bills = sorted(
            project.bills,
            key=lambda bill: (bill.billing_period_start, bill.billing_period_end, bill.meter_id or ""),
        )
        if not bills:
            story.append(Paragraph(BILL_EMPTY_TEXT, styles["body"]))
            story.append(Paragraph(BILL_EMPTY_HOWTO, styles["body"]))
            story.append(Paragraph(BILL_BOUNDARY_NOTE, styles["small"]))
            return

        service = BillService(project)
        outcomes = {item.bill_id: item for item in service.reconcile_all()}
        summary = service.annual_summary()

        # ---- （一）账单概况与月度趋势 ----
        sources: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for bill in bills:
            sources[bill.source_type.label] = sources.get(bill.source_type.label, 0) + 1
            statuses[bill.quality_status.label] = statuses.get(bill.quality_status.label, 0) + 1
        story.append(
            self._table(
                ["项目", "内容"],
                [
                    ["账单条数", f"{len(bills)} 条"],
                    [
                        "覆盖账单月份",
                        f"{len(summary.months_covered)} 个月："
                        + "、".join(summary.months_covered),
                    ],
                    [
                        "数据来源构成",
                        "；".join(f"{name} {count} 条" for name, count in sorted(sources.items())),
                    ],
                    [
                        "数据质量状态",
                        "；".join(f"{name} {count} 条" for name, count in sorted(statuses.items())),
                    ],
                    ["年度月份覆盖率", f"{summary.coverage_ratio:.2%}（{len(summary.months_covered)}/12）"],
                    ["年度总购电量", _bill_kwh(summary.total_energy_kwh)],
                    ["年度账单总额", _bill_yuan(summary.total_amount_yuan)],
                    ["平均综合电价", _bill_price(summary.average_price_yuan_per_kwh)],
                    [
                        "是否可直接相加",
                        "是（每月账单周期完整且不重叠）"
                        if summary.can_sum_directly
                        else "否——存在缺月 / 重叠 / 跨月或非自然月账期，不得作为完整年度基准账单",
                    ],
                    [
                        "缺失月份",
                        "、".join(summary.missing_months) if summary.missing_months else "无（12 个月齐全）",
                    ],
                    [
                        "跨月 / 非自然月账期",
                        f"{len(summary.cross_month_bills)} / {len(summary.non_natural_month_bills)} 条"
                        + (
                            "：" + "、".join(summary.cross_month_bills[:5])
                            if summary.cross_month_bills
                            else ""
                        ),
                    ],
                ],
                styles,
            )
        )
        story.append(
            Paragraph(
                "口径：平均综合电价 P_avg = 账单总额 ÷ 总购电量，**仅为账单统计口径**，"
                "不等于光伏自用电量的边际节省电价（固定基本电费、需量电费、税费等未必随购电量同比例变化，§3.1）。",
                styles["small"],
            )
        )
        story.append(Spacer(1, 3 * mm))

        story.append(Paragraph("（一）月度趋势", styles["h2"]))
        monthly_rows = [
            [
                item.billing_month,
                str(item.bill_count),
                _bill_kwh(item.energy_total_kwh),
                _bill_yuan(item.amount_total_yuan),
                _bill_price(item.average_price_yuan_per_kwh),
                "是" if item.has_cross_month else "否",
                item.quality_status.label,
            ]
            for item in summary.monthly
        ]
        if not monthly_rows:  # pragma: no cover - bills 非空时 monthly 必非空
            monthly_rows = [["—", "—", "—", "—", "—", "—", "—"]]
        story.append(
            self._table(
                ["月份", "账单条数", "总购电量", "账单总额", "平均综合电价", "跨月账期", "质量状态"],
                monthly_rows,
                styles,
                right_align={1, 2, 3, 4},
            )
        )
        story.append(Spacer(1, 3 * mm))

        # ---- （二）账单事实明细 ----
        story.append(Paragraph("（二）账单事实明细（按账期排序）", styles["h2"]))
        raw_rows = [
            [
                bill.billing_month,
                f"{bill.billing_period_start:%Y-%m-%d} ~ {bill.billing_period_end:%Y-%m-%d}"
                + ("（跨月）" if bill.is_cross_month else ""),
                bill.meter_id or BILL_NOT_PROVIDED_TEXT,
                _bill_kwh(bill.energy_total_kwh),
                _bill_yuan(bill.bill_total_yuan),
                bill.source_type.label,
                bill.quality_status.label,
            ]
            for bill in bills
        ]
        story.append(
            self._table(
                ["账单月份", "账期", "计量点", "总购电量", "账单总额", "数据来源", "质量状态"],
                raw_rows,
                styles,
                right_align={3, 4},
            )
        )
        story.append(Spacer(1, 3 * mm))

        # ---- （三）校验与差异 ----
        story.append(Paragraph("（三）校验与差异（ΔE / ΔC 与分层核对）", styles["h2"]))
        check_rows = []
        for bill in bills:
            item = outcomes.get(bill.bill_id)
            if item is None:  # pragma: no cover - 两者同源
                continue
            issue_text = "；".join(
                f"{issue.level}[{issue.code or '—'}]"
                f"{label_of(issue.field) if issue.field else ''}：{issue.message}"
                for issue in item.issues
            )
            check_rows.append(
                [
                    bill.billing_month,
                    _bill_diff(item.energy_difference_kwh, " kWh"),
                    _bill_tristate(item.energy_consistent),
                    _bill_diff(item.amount_difference_yuan, " 元"),
                    _bill_tristate(item.amount_consistent),
                    _bill_tristate(item.energy_sub_consistent),
                    item.quality_status.label,
                    issue_text or "未发现问题",
                ]
            )
        if not check_rows:  # pragma: no cover
            check_rows = [["—"] * 8]
        story.append(
            self._table(
                [
                    "月份",
                    "分时电量合计差 ΔE",
                    "电量是否一致",
                    "费用分项合计差 ΔC",
                    "金额是否一致",
                    "电度电费二层核对",
                    "质量状态",
                    "问题清单（ERROR / WARNING / INFO）",
                ],
                check_rows,
                styles,
                right_align={1, 3},
            )
        )
        story.append(
            Paragraph(
                "口径：ΔE = 总电量 − Σ（账单**已提供**的分时时段电量）；ΔC = 账单总额 − Σ一层费用分项。"
                "账单未提供的字段一律按『未知』处理，**不按 0 计入**任何合计（§2.1、§3.1）。",
                styles["small"],
            )
        )
        story.append(Spacer(1, 3 * mm))

        story.append(Paragraph("（四）口径假设与事项（assumptions）", styles["h2"]))
        assumptions: list[str] = []
        for message in summary.messages:
            assumptions.append(message)
        seen: set[str] = set()
        for bill in bills:
            item = outcomes.get(bill.bill_id)
            if item is None:  # pragma: no cover
                continue
            for text in item.assumptions:
                if text not in seen:
                    seen.add(text)
                    assumptions.append(text)
        if not assumptions:  # pragma: no cover - summary.messages 恒非空
            assumptions.append("无。")
        for text in assumptions:
            story.append(Paragraph(f"• {text}", styles["body"]))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(BILL_BOUNDARY_NOTE, styles["small"]))

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
    def export(
        self,
        project: Project,
        result: CalculationResult,
        path: str | Path,
        *,
        self_consumption=None,
        load_portrait=None,
    ) -> Path:
        """导出 PDF 报告，返回实际写入路径。

        后缀处理用字符串拼接而**不是** ``Path.with_suffix()``：项目名常含 ``2061.8kWp``、``V1.2`` 这类
        带小数点的片段，``with_suffix`` 会把它们当后缀截掉。

        ``self_consumption`` / ``load_portrait`` 为 V2.2 阶段 4 追加的**可选**参数
        （来自 ``LoadProfileService``）；既有调用方不传时输出"尚未执行消纳分析"说明。
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
            author=APP_NAME,
            subject="工商业新能源项目前期经济测算",
        )
        story = self.build_story(
            project,
            result,
            styles,
            self_consumption=self_consumption,
            load_portrait=load_portrait,
        )
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
