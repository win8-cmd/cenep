"""月账单估算负荷的模型：可编辑典型负荷模板 + 月电量 + 回归结果（V2.2 §2.2、§3.2、§6.5）。

职责边界（规格书 §1）
--------------------
* 本模块**只描述数据**：模板的形状（权重）、月电量输入、运行参数、回归校验行与结果容器。
  全部公式（权重展开、按 Δt 换算、严格回归到月电量）在
  :mod:`cenep.calculation.load_estimate`。
* 生成的曲线**永远**是 :class:`~cenep.domain.enums.LoadDataSourceType.MONTHLY_BILL_ESTIMATE`
  或 ``SYNTHETIC_TEMPLATE``，即 ``estimated=True``（§0.2 红线）。
  :class:`LoadEstimateResult` 因此在类型层强制 ``is_estimate`` 恒为真，
  并把模板来源、运行参数与全部假设一起带到界面与报告。

“模板是权重，不是事实”（§6.5）
----------------------------
规格书明确：「模板是可编辑的权重曲线，**不是行业实测事实**」。
因此 :class:`TypicalLoadTemplate` 只有 **24 点相对权重**（无量纲、非负、峰值不强制为 1），
不含任何 kWh 数值；月电量由 :class:`MonthlyLoadEnergy` 单独给出。
把"权重曲线"和"电量水平"分开，是 §3.2 的要求：

.. code-block:: text

    E_i = E_month × w_i / Σ_{i∈month} w_i × 60/Δt_min     （先归一化权重，再乘月电量）

**绝不能把归一化权重直接当 kW**（§3.2 末句）。本模块的字段名一律带单位后缀
（``*_kwh`` / ``*_kw`` / ``*_minutes`` / ``_ratio``），避免量纲误读。
"""

from __future__ import annotations

import calendar
from datetime import date

from pydantic import Field, model_validator

from .base import NON_NEG, _Model
from .enums import LoadEstimateSource, LoadValueKind
from .load_data import TIMEZONE_DEFAULT, HighFrequencyLoadDataset

__all__ = [
    "BUILTIN_LOAD_TEMPLATES",
    "DEFAULT_INTERVAL_MINUTES",
    "HOURS_PER_DAY",
    "LoadEstimateParams",
    "LoadEstimateResult",
    "MonthlyLoadEnergy",
    "MonthlyRegressionRow",
    "TypicalLoadTemplate",
    "builtin_template",
    "template_ids",
]

#: 权重曲线的点数（每小时一个权重；§6.5 的模板是"典型日"形状）
HOURS_PER_DAY = 24

#: 估算曲线默认间隔（分钟）；15/30/60 均支持（§6.1、§6.5）
DEFAULT_INTERVAL_MINUTES = 15

#: 判定"白天"的小时区间（含首不含尾），用于 §6.5 的白天/夜间负荷比例口径
DAYTIME_HOURS: tuple[int, ...] = tuple(range(6, 18))

#: 回归容差（kWh/月）：§6.5 要求"每个月的估算间隔电量必须严格回归到输入月电量，
#: 数值误差不超过 1e-6 kWh"。这里按月的间隔数放大，避免 2976 点时浮点累积误判。
REGRESSION_TOLERANCE_KWH = 1e-6


class TypicalLoadTemplate(_Model):
    """可编辑的典型日负荷**权重**模板（V2.2 §6.5、§3.2）。

    ``workday_weights`` / ``weekend_weights`` / ``holiday_weights`` 各 24 点，
    表示该日类型下**每小时相对权重**（非负；峰值不要求等于 1，只有相对比例有意义）。
    ``weekend_weights`` 为空表示"周末按工作日 × 周末运行比例"（运行比例在
    :class:`LoadEstimateParams` 里，属用户运行参数，不属模板）。
    """

    template_id: str = ""
    name: str = ""
    description: str = Field(default="", description="模板说明与适用工况（中文）")
    industry: str = Field(default="", description="适用行业（仅为提示，不参与计算）")

    workday_weights: list[float] = Field(
        default_factory=list, description="工作日 24 点相对权重（无量纲，非负）"
    )
    weekend_weights: list[float] = Field(
        default_factory=list, description="休息日 24 点相对权重；空 = 工作日 × 周末运行比例"
    )
    holiday_weights: list[float] = Field(
        default_factory=list, description="节假日 24 点相对权重；空 = 休息日权重"
    )

    is_builtin: bool = Field(default=False, description="是否内置模板")
    editable: bool = Field(default=True, description="是否允许用户编辑（内置模板也可编辑副本）")
    notes: str = Field(default="", description="使用提示（模板不是行业实测事实，§6.5）")

    @model_validator(mode="after")
    def _check_weights(self) -> TypicalLoadTemplate:
        for field_name, label in (
            ("workday_weights", "工作日"),
            ("weekend_weights", "休息日"),
            ("holiday_weights", "节假日"),
        ):
            weights = getattr(self, field_name)
            if not weights:
                continue
            if len(weights) != HOURS_PER_DAY:
                raise ValueError(
                    f"典型负荷模板「{self.name or self.template_id}」的{label}权重必须为 "
                    f"{HOURS_PER_DAY} 点（每小时一个权重），实际 {len(weights)} 点；"
                    f"请检查模板（V2.2 §6.5）"
                )
            bad = [i for i, v in enumerate(weights) if v is None or float(v) < 0.0]
            if bad:
                raise ValueError(
                    f"典型负荷模板「{self.name or self.template_id}」的{label}权重出现负值或空值"
                    f"（第 {bad[:5]} 点）；权重必须是非负数（V2.2 §3.2：w_i 为非负权重）"
                )
            if float(sum(float(v) for v in weights)) <= 0.0:
                raise ValueError(
                    f"典型负荷模板「{self.name or self.template_id}」的{label}权重合计为 0，"
                    f"无法归一化出日电量分布；请至少给一个小时的权重填写正值（V2.2 §3.2）"
                )
        return self

    @property
    def has_workday_template(self) -> bool:
        return bool(self.workday_weights)


def _t(*values: float) -> list[float]:
    return [float(v) for v in values]


#: 内置可编辑模板（§6.5 列举：单班生产、双班生产、连续生产、工作日为主、办公型）。
#:
#: **这些是系统默认形状，不是行业实测事实**（§6.5 原文）。它们的唯一作用是
#: 在用户只有月电量、没有逐时形状时给出一个**可解释、可编辑**的起点；
#: 用户必须按自身工况修改权重，或改用实测高频导入（``LoadDataSourceType.HIGH_FREQUENCY_IMPORT``）。
BUILTIN_LOAD_TEMPLATES: tuple[TypicalLoadTemplate, ...] = (
    TypicalLoadTemplate(
        template_id="single_shift",
        name="单班生产",
        description="白班为主（08:00–17:00），夜班基本停产，仅保留照明与值班负荷",
        industry="离散制造 / 机加工",
        workday_weights=_t(
            0.10, 0.10, 0.10, 0.10, 0.10, 0.15, 0.30, 0.70,
            0.95, 1.00, 1.00, 1.00, 0.85, 1.00, 1.00, 1.00,
            0.90, 0.55, 0.25, 0.15, 0.12, 0.12, 0.11, 0.10,
        ),
        notes="单班制的夜班保留 ~10% 基荷；如实际有夜班请改用双班制或连续生产模板。",
        is_builtin=True,
    ),
    TypicalLoadTemplate(
        template_id="double_shift",
        name="双班生产",
        description="两班制（约 08:00–24:00），夜间保留空压机、照明与值班负荷",
        industry="汽车零部件 / 电子装配",
        workday_weights=_t(
            0.30, 0.28, 0.28, 0.28, 0.30, 0.38, 0.50, 0.70,
            0.90, 0.98, 1.00, 1.00, 0.92, 0.98, 1.00, 0.98,
            0.92, 0.85, 0.75, 0.70, 0.62, 0.50, 0.42, 0.35,
        ),
        notes="两班制覆盖 16 小时；夜班基荷由 0.28~0.35 的权重体现。",
        is_builtin=True,
    ),
    TypicalLoadTemplate(
        template_id="continuous",
        name="连续生产",
        description="三班连续运行（24 小时），峰谷差异小、负荷率接近 1",
        industry="化工 / 冶金 / 造纸",
        workday_weights=_t(
            0.92, 0.90, 0.90, 0.90, 0.92, 0.95, 0.97, 1.00,
            1.00, 1.00, 1.00, 1.00, 0.98, 1.00, 1.00, 1.00,
            1.00, 0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93,
        ),
        notes="连续生产模板的日峰谷差很小；若实际存在检修停机，请在运行参数里填写停产日。",
        is_builtin=True,
    ),
    TypicalLoadTemplate(
        template_id="workday_dominant",
        name="工作日为主",
        description="仅工作日生产、周末基本停产（周末负荷约 10%）；适合行政班与间歇生产",
        industry="通用制造 / 仓储",
        workday_weights=_t(
            0.12, 0.12, 0.12, 0.12, 0.12, 0.18, 0.35, 0.72,
            0.95, 1.00, 1.00, 1.00, 0.90, 1.00, 1.00, 0.98,
            0.88, 0.55, 0.28, 0.18, 0.15, 0.14, 0.13, 0.12,
        ),
        notes="周末/节假日按运行参数中的『周末运行比例』缩放（默认 0.35，可改为 0.10）。",
        is_builtin=True,
    ),
    TypicalLoadTemplate(
        template_id="office",
        name="办公型",
        description="写字楼 / 办公负荷（约 07:30–19:00），午间与夜间低谷明显",
        industry="办公楼 / 商业",
        workday_weights=_t(
            0.08, 0.08, 0.08, 0.08, 0.08, 0.10, 0.20, 0.55,
            0.90, 1.00, 1.00, 0.85, 0.55, 0.95, 1.00, 0.95,
            0.70, 0.35, 0.18, 0.12, 0.10, 0.09, 0.09, 0.08,
        ),
        notes="办公型午休时段（12 时）权重下调；如为商场/酒店请自行编辑权重。",
        is_builtin=True,
    ),
)


def template_ids() -> tuple[str, ...]:
    """全部内置模板 ID（供界面下拉与测试断言）。"""
    return tuple(template.template_id for template in BUILTIN_LOAD_TEMPLATES)


def builtin_template(template_id: str) -> TypicalLoadTemplate:
    """按 ID 取内置模板的**副本**（用户编辑不会污染内置模板）。

    :raises KeyError: 未知模板 ID（调用方须转成中文错误再显示给用户）
    """
    for template in BUILTIN_LOAD_TEMPLATES:
        if template.template_id == template_id:
            return template.model_copy(deep=True)
    raise KeyError(template_id)


class MonthlyLoadEnergy(_Model):
    """一个自然月的用电量输入（V2.2 §3.2、§6.5）。单位 **kWh**。"""

    year: int = Field(gt=2000, le=2100, description="年份")
    month: int = Field(ge=1, le=12, description="月份 1~12")
    energy_kwh: float = Field(gt=0.0, description="该月用电量 kWh（必须大于 0）")
    source: LoadEstimateSource = LoadEstimateSource.MANUAL
    note: str = Field(default="", description="来源备注（如账单编号、计量点）")

    @property
    def month_key(self) -> str:
        """``YYYY-MM``（与账单页 ``billing_month`` 同口径，便于对齐）。"""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def day_count(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]


class LoadEstimateParams(_Model):
    """月账单估算的**运行参数**（V2.2 §6.5 的"用户可配置"清单，全部可编辑）。

    单位约定：时间为**小时**（``*_hour``）、比例为**小数**（``*_ratio``）、
    间隔为**分钟**（``interval_minutes``）；没有任何电价或金额字段。
    """

    year: int = Field(gt=2000, le=2100, description="估算目标年份，决定平年/闰年")
    interval_minutes: int = Field(
        default=DEFAULT_INTERVAL_MINUTES, description="估算曲线的间隔（分钟）：15 / 30 / 60"
    )
    timezone: str = TIMEZONE_DEFAULT
    value_kind: LoadValueKind = Field(
        default=LoadValueKind.INTERVAL_ENERGY_KWH,
        description="估算输出的数值口径：CENEP 内部统一存间隔电量 kWh（§2.2）",
    )

    template_id: str = Field(default="double_shift", description="典型负荷模板 ID（§6.5）")
    template_name: str = Field(default="", description="模板显示名（留空时由模板填充）")

    # 模板覆盖（空 = 用模板自带权重，§6.5“模板是可编辑的权重曲线”）
    workday_weights: list[float] = Field(default_factory=list, description="工作日 24 点权重覆盖")
    weekend_weights: list[float] = Field(default_factory=list, description="休息日 24 点权重覆盖")
    holiday_weights: list[float] = Field(default_factory=list, description="节假日 24 点权重覆盖")

    # 运行参数（§6.5）
    workdays_per_week: int = Field(default=5, ge=1, le=7, description="每周生产天数")
    shift_start_hour: int = Field(default=0, ge=0, le=23, description="班次开始小时（含）")
    shift_end_hour: int = Field(default=24, ge=1, le=24, description="班次结束小时（不含，可为 24）")
    offshift_run_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "非班次时段负荷相对班内峰值的比例；None（默认）= 不裁剪，保留模板自带形状。"
            "只有用户显式给出该值时，班次开始/结束时间才生效（§6.5）"
        ),
    )
    weekend_run_ratio: float = Field(
        default=0.35, ge=0.0, le=1.0, description="周末负荷相对工作日的整体比例（§6.5）"
    )
    holiday_run_ratio: float = Field(
        default=0.10, ge=0.0, le=1.0, description="节假日负荷相对工作日的整体比例（§6.5）"
    )
    daytime_load_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="白天（06:00–18:00）电量占全月比例；与夜间比例必须同时给出且合计为 1",
    )
    nighttime_load_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0, description="夜间（18:00–次日 06:00）电量占全月比例"
    )
    maintenance_run_ratio: float = Field(
        default=0.0, ge=0.0, le=1.0, description="停产/检修日负荷相对工作日的比例（§6.5）"
    )
    shutdown_days: list[date] = Field(default_factory=list, description="停产/检修日（§6.5）")
    holidays: list[date] = Field(default_factory=list, description="节假日日历（§6.5）")

    annual_energy_kwh: float = Field(
        default=0.0,
        ge=0.0,
        description="年用电量 kWh；仅在缺月电量、需要按比例拆分或均匀分摊时使用（§6.5）",
    )
    monthly_split_ratios: list[float] = Field(
        default_factory=list, description="12 个月度比例；空 = 均匀分摊（默认估算假设）"
    )

    @model_validator(mode="after")
    def _check_params(self) -> LoadEstimateParams:
        if self.interval_minutes not in (15, 30, 60):
            raise ValueError(
                f"估算曲线间隔只支持 15 / 30 / 60 分钟，实际给出 {self.interval_minutes} 分钟；"
                f"其它粒度请先生成 15 分钟曲线再重采样（V2.2 §6.1）"
            )
        if self.timezone != TIMEZONE_DEFAULT:
            raise ValueError(
                f"估算曲线时区只支持 {TIMEZONE_DEFAULT}（中国无夏令时，恒为 UTC+8），"
                f"实际给出 {self.timezone!r}（V2.2 §2.2）"
            )
        if self.shift_start_hour >= self.shift_end_hour:
            raise ValueError(
                f"班次结束小时（{self.shift_end_hour}）必须大于开始小时（{self.shift_start_hour}）；"
                f"跨午夜班次请把开始小时设为 0、结束小时设为实际下班时刻（V2.2 §6.5）"
            )
        for field_name, label in (
            ("workday_weights", "工作日"),
            ("weekend_weights", "休息日"),
            ("holiday_weights", "节假日"),
        ):
            weights = getattr(self, field_name)
            if weights and len(weights) != HOURS_PER_DAY:
                raise ValueError(
                    f"{label}权重覆盖必须是 {HOURS_PER_DAY} 点（每小时一个），实际 {len(weights)} 点；"
                    f"请检查输入（V2.2 §6.5）"
                )
        day, night = self.daytime_load_ratio, self.nighttime_load_ratio
        if (day is None) != (night is None):
            raise ValueError(
                "白天负荷比例与夜间负荷比例必须**同时给出**（两者合计为 1）；"
                "只填一个无法确定另一侧的电量，系统不会替你猜（V2.2 §6.5、§12）"
            )
        if day is not None and night is not None and abs(day + night - 1.0) > 1e-6:
            raise ValueError(
                f"白天负荷比例（{day:.4f}）与夜间负荷比例（{night:.4f}）合计必须为 1，"
                f"实际合计 {day + night:.6f}（V2.2 §6.5）"
            )
        if self.monthly_split_ratios and len(self.monthly_split_ratios) != 12:
            raise ValueError(
                f"月度比例必须是 12 个，实际 {len(self.monthly_split_ratios)} 个（V2.2 §6.5）"
            )
        if self.monthly_split_ratios and sum(self.monthly_split_ratios) <= 0.0:
            raise ValueError("月度比例合计必须大于 0，否则无法把年电量分摊到各月（V2.2 §6.5）")
        if self.monthly_split_ratios and any(v < 0.0 for v in self.monthly_split_ratios):
            raise ValueError("月度比例不得为负值（V2.2 §6.5）")
        # 同一天同时出现在「节假日」与「停产/检修日」不是错误：检修日优先（更零负荷），
        # 该优先级在 calculation/load_estimate.py 中固定实现并写入 assumptions 披露。
        return self

    @property
    def delta_hours(self) -> float:
        """单间隔时长 Δt（小时）。"""
        return self.interval_minutes / 60.0

    @property
    def intervals_per_day(self) -> int:
        return int(round(24 * 60 / self.interval_minutes))

    @property
    def intervals_per_hour(self) -> int:
        return int(round(60 / self.interval_minutes))

    def uses_default_month_split(self) -> bool:
        """是否使用了"年电量均匀分摊"这一默认估算假设（§6.5 必须披露）。"""
        return not self.monthly_split_ratios


class MonthlyRegressionRow(_Model):
    """单月"估算间隔电量合计 ↔ 输入月电量"的回归校验行（§6.5、§9.2）。

    单位：电量 kWh；``residual_ratio`` 无量纲。
    """

    year: int = 0
    month: int = 0
    month_key: str = ""
    source: LoadEstimateSource = LoadEstimateSource.MANUAL
    day_count: int = Field(default=0, ge=0, description="该月自然日数")
    interval_count: int = Field(default=0, ge=0, description="该月估算间隔数")
    input_energy_kwh: float = 0.0
    estimated_energy_kwh: float = 0.0
    residual_kwh: float = Field(default=0.0, description="估算合计 − 输入月电量（kWh）")
    residual_ratio: float = Field(default=0.0, description="残差 / 输入月电量")
    within_tolerance: bool = Field(default=False, description="|残差| ≤ 容差（§6.5：1e-6 kWh 量级）")
    estimated_peak_power_kw: float = Field(
        default=0.0, ge=0.0, description="该月估算最大功率 kW（= 最大间隔电量 ÷ Δt）"
    )
    estimated_avg_power_kw: float = Field(
        default=0.0, ge=0.0, description="该月估算平均功率 kW（= 月电量 ÷ 该月小时数）"
    )
    tolerance_kwh: float = REGRESSION_TOLERANCE_KWH


class LoadEstimateResult(_Model):
    """月账单估算的完整结果（V2.2 §6.5、§3.2；规格书 §0.2 红线）。

    :attr:`is_estimate` **恒为 True**：由月电量 + 权重模板生成的曲线在定义上就是估算，
    模型层面不允许把它标成实测（有单元测试锁定）。
    """

    dataset: HighFrequencyLoadDataset
    params: LoadEstimateParams
    template_id: str = ""
    template_name: str = ""
    monthly: list[MonthlyRegressionRow] = Field(default_factory=list)
    total_input_energy_kwh: float = 0.0
    total_estimated_energy_kwh: float = 0.0
    max_abs_residual_kwh: float = 0.0
    tolerance_kwh: float = REGRESSION_TOLERANCE_KWH
    all_months_regressed: bool = False
    assumptions: list[str] = Field(default_factory=list)
    is_estimate: bool = Field(default=True, description="恒为 True：估算曲线不得伪装成实测（§0.2）")

    @model_validator(mode="after")
    def _force_estimate(self) -> LoadEstimateResult:
        if not self.is_estimate:
            raise ValueError(
                "月电量 + 权重模板生成的曲线是**估算**数据，is_estimate 不得为 False；"
                "如需实测曲线请使用高频负荷导入（V2.2 §0.2 红线）"
            )
        if self.dataset.estimated is not True or self.dataset.source_type.is_measured:
            raise ValueError(
                "估算结果携带的负荷数据集必须标记为估算（source_type=月账单估算/模板合成、"
                "estimated=True），否则界面与报告无法区分实测与估算（V2.2 §0.2 红线）"
            )
        return self

    @property
    def provenance_text(self) -> str:
        """来源说明（界面橙色提示与报告"估算依据"章节共用）。"""
        return (
            f"⚠ 估算负荷曲线（不是实测）：由 {self.params.template_name or self.template_name}"
            f"「{self.template_id}」权重模板 + 月电量回归生成，"
            f"间隔 {self.params.interval_minutes} 分钟；"
            f"{self.dataset.source_type.report_badge}"
        )

    def caliber_lines(self) -> list[str]:
        """估算依据（§6.5「结果报告必须有『估算依据』章节」）——口径 + 假设 + 回归结果。"""
        lines: list[str] = [self.provenance_text]
        lines.append(
            "估算公式（V2.2 §3.2）：E_i = E_month × w_i ÷ Σ_{i∈month} w_i × 60 ÷ Δt_min，"
            "其中 w_i 为**非负权重**（不是 kW）；每月 ΣE_i 严格回归到输入月电量。"
        )
        lines.append(
            f"月电量回归：合计输入 {self.total_input_energy_kwh:,.3f} kWh、"
            f"估算合计 {self.total_estimated_energy_kwh:,.3f} kWh、"
            f"最大单月残差 {self.max_abs_residual_kwh:.3e} kWh"
            f"（容差 {self.tolerance_kwh:g} kWh，{'全部月份通过' if self.all_months_regressed else '★存在超限月份'}）"
        )
        lines.extend(self.assumptions)
        return [line for line in lines if line]
