"""光伏消纳结果模型（规格书 V2.2 §2.4、§3.3、§6.3 C、§9.2；阶段 4）。

职责边界（规格书 §1）
--------------------
* 本模块**只描述"消纳结果长什么样"**，不含任何计算：四项指标、逐月汇总、
  能量平衡误差与"口径说明"全部由 :mod:`cenep.calculation.self_consumption` 计算后填入。
* :class:`MetricCaliber`（指标口径）是**结果的组成部分**，不是注释：
  规格书 §3.3「指标命名规则」与 §12「不得用未经说明的默认值制造精确结果」要求
  指标与口径必须同时呈现，因此口径随结果对象一起传递到界面、Excel 与 PDF。
  口径文本的**唯一权威来源**是 :data:`cenep.calculation.self_consumption.CALIBERS`。

四项指标的分母口径（规格书 §3.3，逐字对应）
------------------------------------------

.. code-block:: text

    E_self,i   = min(E_load,i, E_pv,i)               每个时间间隔
    E_export,i = max(E_pv,i − E_load,i, 0)
    E_import,i = max(E_load,i − E_pv,i, 0)

    R_self     = E_self   / E_pv      光伏自用率（“消纳率”主指标）
    R_coverage = E_self   / E_load    负荷覆盖率
    R_export   = E_export / E_pv      光伏上网率
    R_grid     = E_import / E_load    电网依赖率

**四项指标全部是电量口径（kWh ÷ kWh），没有一项是"时间覆盖率"**：
``R_coverage`` 的"覆盖率"指**电量覆盖率**（自发自用电量占企业总用电量的比例），
不是"有光伏出力的时间点占比"。其分母为 ``E_load``（负荷电量），
而 ``R_export`` 与 ``R_self`` 的分母为 ``E_pv``（光伏发电量）；
``R_grid`` 的分母为 ``E_load``。这一点必须写死，否则"上网率/电网依赖率"极易被混用。

边界（§3.3）：分母为 0 时返回 ``None``（界面显示"不适用"），**不得显示 0%**。

来源标签红线（§0.2）
------------------
:attr:`SelfConsumptionResult.is_based_on_estimate` 由负荷数据集的
:class:`~cenep.domain.enums.LoadDataSourceType` 推导，**估算曲线算出的消纳率与收益
必须同时携带"基于估算"的标记**：界面用 :attr:`estimate_badge` 显示橙色提示，
Excel/PDF 由 :meth:`assumption_lines` 写入"关键假设"。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from .base import NON_NEG, _Model
from .enums import LoadDataSourceType, LoadQualityStatus

__all__ = [
    "METRIC_KEYS",
    "METRIC_ORDER",
    "MetricCaliber",
    "MonthlySelfConsumptionRow",
    "SelfConsumptionResult",
]

#: 四项指标的固定顺序（界面、Excel、PDF 三处共用；不得只显示含糊的"消纳率"，§3.3）
METRIC_ORDER: tuple[str, ...] = (
    "self_consumption",
    "load_coverage",
    "export",
    "grid_dependency",
)

#: 指标键集合（供静态检查与"四项必须齐备"的测试断言）
METRIC_KEYS: frozenset[str] = frozenset(METRIC_ORDER)


class MetricCaliber(_Model):
    """一个指标的**完整口径**（公式、分子、分母、单位、边界、说明）。

    规格书 §3.3「指标命名规则」要求「不得仅显示含糊的"消纳率"而不写口径」；
    §12 要求「任何一环没有数据支撑时，都必须给出明确警告和计算口径」。
    因此口径不是可选注释，而是随结果一起流动的数据。
    """

    key: str = Field(description="指标键，见 METRIC_ORDER")
    name: str = Field(description="指标中文名（含公式摘要，界面直接显示）")
    formula: str = Field(description="公式（纯文本，可显示在报告单元格里）")
    numerator: str = Field(description="分子是什么（含单位 kWh）")
    denominator: str = Field(description="分母是什么（含单位 kWh）")
    unit: str = Field(default="比例（无量纲，0~1）", description="单位与量纲")
    boundary: str = Field(description="边界条件：分母为 0 时如何显示")
    scope: str = Field(description="统计口径：逐间隔求和范围")
    note: str = Field(default="", description="补充说明（常见误用）")

    def text(self) -> str:
        """一行完整口径说明（界面提示与报告 assumptions 直接使用）。"""
        return (
            f"{self.name}＝{self.formula}；分子＝{self.numerator}，分母＝{self.denominator}，"
            f"单位＝{self.unit}；{self.scope}；边界：{self.boundary}"
            + (f"；{self.note}" if self.note else "")
        )


class MonthlySelfConsumptionRow(_Model):
    """逐月消纳明细（规格书 §6.3 C「月度自用率趋势」；§9.2）。

    单位：电量 kWh；比例无量纲 0~1（分母为 0 时为 ``None`` = 不适用）。
    """

    year: int = 0
    month: int = 0
    month_key: str = Field(default="", description="YYYY-MM")
    interval_count: int = 0

    load_energy_kwh: float = 0.0
    pv_generation_kwh: float = 0.0
    pv_used_on_site_kwh: float = 0.0
    pv_export_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    load_unserved_kwh: float = 0.0

    self_consumption_rate: float | None = None
    load_coverage_rate: float | None = None
    export_rate: float | None = None
    grid_dependency_rate: float | None = None

    energy_balance_error_kwh: float = 0.0
    max_interval_balance_error_kwh: float = 0.0

    def rate_text(self, key: str) -> str:
        """按指标键取显示文本；``None`` 一律显示"不适用"（§3.3）。"""
        value = {
            "self_consumption": self.self_consumption_rate,
            "load_coverage": self.load_coverage_rate,
            "export": self.export_rate,
            "grid_dependency": self.grid_dependency_rate,
        }.get(key)
        return "不适用" if value is None else f"{value:.2%}"


class SelfConsumptionResult(_Model):
    """光伏消纳分析结果（规格书 §2.4 字段清单 + §3.3 四项指标与口径）。

    字段单位：能量 **kWh**，比例**无量纲小数**，误差 **kWh**，间隔 **分钟**。
    """

    # ---- §2.4 规定的字段 ----
    load_energy_kwh: float = NON_NEG
    pv_generation_kwh: float = NON_NEG
    pv_used_on_site_kwh: float = NON_NEG
    pv_export_kwh: float = NON_NEG
    grid_import_kwh: float = NON_NEG
    load_unserved_kwh: float = Field(
        default=0.0,
        ge=0.0,
        description="未被任何电源满足的负荷（不含储能时恒为 0；不允许电网供电时可能为正）",
    )
    self_consumption_rate: float | None = None
    load_coverage_rate: float | None = None
    export_rate: float | None = None
    grid_dependency_rate: float | None = None
    energy_balance_error_kwh: float = 0.0
    interval_minutes: int = Field(default=60, gt=0, le=1440)
    data_quality_status: LoadQualityStatus = LoadQualityStatus.VALID
    assumptions: list[str] = Field(default_factory=list, description="口径与假设（必须随结果输出）")

    # ---- 阶段 4 追加（均为默认值，向后兼容；§6.3 C、§6.4、§0.2）----
    max_interval_balance_error_kwh: float = Field(
        default=0.0, ge=0.0, description="逐间隔能量平衡最大误差 kWh（§3.3 要求 ≤ 容差）"
    )
    balance_tolerance_kwh: float = Field(default=1e-6, gt=0.0, description="平衡容差 kWh（§3.3）")
    is_balanced: bool = Field(default=False, description="逐间隔守恒是否全部在容差内（§3.3）")
    point_count: int = Field(default=0, ge=0, description="参与统计的时间间隔数")
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="时间轴覆盖率")
    interpolated_intervals: int = Field(
        default=0, ge=0, description="按明确规则插补的间隔数（§6.4 要求记录）"
    )

    monthly: list[MonthlySelfConsumptionRow] = Field(
        default_factory=list, description="逐月明细（月度自用率趋势，§6.3 C）"
    )
    calibers: list[MetricCaliber] = Field(
        default_factory=list, description="四项指标的完整口径（界面/报告必须一起显示，§3.3）"
    )

    # ---- 来源标签（§0.2 红线：估算数据不得伪装成实测）----
    load_source_type: LoadDataSourceType = LoadDataSourceType.HIGH_FREQUENCY_IMPORT
    is_based_on_estimate: bool = Field(
        default=False,
        description="是否基于估算负荷（月账单估算/模板合成）。为真时消纳率与收益必须带「基于估算」标记",
    )
    load_provenance_text: str = Field(default="", description="负荷曲线来源说明（界面/报告直接显示）")
    pv_provenance_text: str = Field(default="", description="光伏出力来源说明")
    pv_source_is_estimate: bool = Field(default=False, description="光伏曲线本身是否为估算/模板")

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _check_estimate_consistency(self) -> SelfConsumptionResult:
        """估算标记必须与负荷来源标签自洽（§0.2 红线，结构性防线）。"""
        if not self.load_source_type.is_measured and not self.is_based_on_estimate:
            raise ValueError(
                f"负荷来源为「{self.load_source_type.label}」时，消纳结果必须同时携带"
                "「基于估算」标记（is_based_on_estimate=True）；否则估算负荷会被当成实测曲线，"
                "违反 V2.2 §0.2 红线"
            )
        if self.load_source_type.is_measured and self.is_based_on_estimate:
            raise ValueError(
                "负荷来源为实测高频导入时不得标记为「基于估算」；"
                "请检查数据来源标签与估算标记是否被混用（V2.2 §0.2）"
            )
        return self

    # ------------------------------------------------------------------ #
    # 派生视图（界面与报告三处共用，避免各自拼字符串）
    # ------------------------------------------------------------------ #
    @property
    def estimate_badge(self) -> str:
        """来源徽标：估算数据必须显著标记（§0.2、§6.3 A、§8.1）。"""
        return self.load_source_type.report_badge

    @property
    def provenance_text(self) -> str:
        """一行来源说明：哪条曲线是实测、哪条是估算（规格书硬约束①）。"""
        kind = "估算" if self.is_based_on_estimate else "实测"
        parts = [f"负荷来源：{self.load_source_type.label}（{kind}）"]
        if self.load_provenance_text:
            parts.append(self.load_provenance_text)
        if self.pv_provenance_text:
            parts.append(f"光伏来源：{self.pv_provenance_text}")
        if self.is_based_on_estimate:
            parts.append("⚠ 本页消纳率与由其推导的收益均基于估算负荷，不是实测结论")
        return "｜".join(parts)

    def assumption_lines(self) -> list[str]:
        """报告"关键假设"章节的全部文本（含口径与来源标记，§0.2、§3.3、§12）。"""
        lines: list[str] = [line for line in self.assumptions if line]
        lines.append(self.provenance_text)
        for caliber in self.calibers:
            lines.append(caliber.text())
        return lines

    def metric_rows(self) -> list[tuple[str, str, str]]:
        """四项指标的 ``(键, 显示值, 口径摘要)``，按 :data:`METRIC_ORDER` 固定顺序。

        界面、Excel 的「消纳率分析」表与 PDF 的「光伏消纳率」章节**共用本方法**，
        保证三处口径完全一致（§3.3「不得仅显示含糊的消纳率」）。
        """
        by_key = {caliber.key: caliber for caliber in self.calibers}
        values = {
            "self_consumption": self.self_consumption_rate,
            "load_coverage": self.load_coverage_rate,
            "export": self.export_rate,
            "grid_dependency": self.grid_dependency_rate,
        }
        rows: list[tuple[str, str, str]] = []
        for key in METRIC_ORDER:
            caliber = by_key.get(key)
            value = values[key]
            rows.append(
                (
                    key,
                    "不适用" if value is None else f"{value:.2%}",
                    caliber.text() if caliber is not None else "",
                )
            )
        return rows

    def rate_of(self, key: str) -> float | None:
        """按指标键取比例；未知键抛 ``KeyError``（避免界面静默显示空值）。"""
        if key not in METRIC_KEYS:
            raise KeyError(f"未知的消纳指标键：{key}（可用：{'、'.join(METRIC_ORDER)}）")
        return {
            "self_consumption": self.self_consumption_rate,
            "load_coverage": self.load_coverage_rate,
            "export": self.export_rate,
            "grid_dependency": self.grid_dependency_rate,
        }[key]

    def energy_rows(self) -> list[tuple[str, float, str]]:
        """电量与平衡量：``(中文名, 数值 kWh, 说明)``，供界面表格与报告使用。"""
        return [
            ("负荷电量", self.load_energy_kwh, "Σ E_load,i（kWh）"),
            ("光伏发电量", self.pv_generation_kwh, "Σ E_pv,i（kWh）"),
            (
                "自发自用电量",
                self.pv_used_on_site_kwh,
                "Σ min(E_load,i, E_pv,i)（kWh）",
            ),
            ("上网电量", self.pv_export_kwh, "Σ max(E_pv,i − E_load,i, 0)（kWh）"),
            ("电网购电量", self.grid_import_kwh, "Σ max(E_load,i − E_pv,i, 0)（kWh）"),
            (
                "未满足负荷电量",
                self.load_unserved_kwh,
                "无电源满足的负荷（不含储能时恒为 0 kWh）",
            ),
            (
                "能量平衡误差",
                self.energy_balance_error_kwh,
                f"全年合计误差（kWh），容差 {self.balance_tolerance_kwh:g} kWh/间隔",
            ),
        ]
