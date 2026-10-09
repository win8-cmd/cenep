"""交互式时序图表（V2 §49、§50）。

六张图（V2 §49）
--------------
======================  ==========================================================
``load``                全年负荷曲线（Y：kW）
``pv``                  PV 出力，同时显示 Load 与 PV 两条线
``soc``                 储能 SOC 曲线（Y：%）
``typical_day``         典型日四条线：Load / PV / Grid / Storage
``price``               电价曲线（Electricity Price）
``scenario``            方案比较柱状图（NPV / IRR / Payback）
======================  ==========================================================

交互要求（V2 §50）
----------------
* **鼠标缩放与平移**：由 PyQtGraph 的 ``ViewBox`` 原生提供（滚轮缩放、拖拽平移、
  右键菜单可一键复位）；
* **悬停查看数值**：``SignalProxy`` 节流监听 ``sigMouseMoved``，用十字光标
  （两条 ``InfiniteLine``）定位，并把该时刻各系列数值写进图下方的读数标签；
* **时间筛选**：:meth:`TimeSeriesChart.set_range` 支持 ``year`` / ``month`` / ``day``。

数据来源与「界面不得计算」（V2 §61、V1 §109）
-------------------------------------------
所有曲线都直接取自 ``result.time_series_results.hourly`` 的**列**
（``column(name)`` 与 ``timestamps``），本模块**不做任何经济或物理计算**。
唯一的两处换算都是**显示单位换算**，不产生新的经济结论：

* 时间轴把 ``datetime`` 转成 epoch 秒（PyQtGraph 的 ``DateAxisItem`` 需要）；
* SOC 由小数转为百分数显示。

两张表的列名与 :data:`cenep.domain.timeseries_results.HOURLY_COLUMNS` 一致；
若某列缺失（例如无储能项目），对应曲线自动跳过而不是报错。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..domain.results import CalculationResult

pg.setConfigOptions(antialias=True, background="w", foreground="k")

#: 图表类型（V2 §49 的六张图）
CHART_LOAD = "load"
CHART_PV = "pv"
CHART_SOC = "soc"
CHART_TYPICAL_DAY = "typical_day"
CHART_PRICE = "price"
CHART_SCENARIO = "scenario"

#: 全部图表类型与中文标题（界面与测试共用，避免手写字符串）
CHART_TITLES: dict[str, str] = {
    CHART_LOAD: "全年负荷曲线",
    CHART_PV: "光伏出力与负荷",
    CHART_SOC: "储能 SOC",
    CHART_TYPICAL_DAY: "典型日能源流",
    CHART_PRICE: "电价曲线",
    CHART_SCENARIO: "方案比较",
}

#: 时间筛选粒度（V2 §50）
RANGE_YEAR = "year"
RANGE_MONTH = "month"
RANGE_DAY = "day"
RANGE_LABELS: tuple[tuple[str, str], ...] = (
    (RANGE_YEAR, "全年"),
    (RANGE_MONTH, "按月"),
    (RANGE_DAY, "按日"),
)

#: 各曲线使用的列名（数据源列，见 timeseries_results.HOURLY_COLUMNS）
_SERIES_PENS: dict[str, tuple[str, str]] = {
    "load": ("load", "#1F77B4"),
    "pv": ("pv_generation", "#2CA02C"),
    "grid": ("grid_import", "#D62728"),
    "storage": ("storage_discharge", "#9467BD"),
    "price": ("electricity_price", "#FF7F0E"),
    "soc": ("storage_soc_end", "#8C564B"),
}


def _epoch_seconds(timestamps: list) -> np.ndarray:
    """把时间戳列表向量化转成 epoch 秒（PyQtGraph 的日期轴需要）。"""
    if not timestamps:
        return np.empty(0, dtype=float)
    return np.array(timestamps, dtype="datetime64[s]").astype(np.int64).astype(float)


def _month_array(timestamps: list) -> np.ndarray:
    if not timestamps:
        return np.empty(0, dtype=np.int16)
    dt = np.array(timestamps, dtype="datetime64[s]")
    return ((dt.astype("datetime64[M]").astype(np.int64) % 12) + 1).astype(np.int16)


def _day_array(timestamps: list) -> np.ndarray:
    if not timestamps:
        return np.empty(0, dtype="datetime64[D]")
    return np.array(timestamps, dtype="datetime64[s]").astype("datetime64[D]")


def _year_of(timestamps: list) -> int:
    if not timestamps:
        return 0
    return int(np.array(timestamps[0], dtype="datetime64[s]").astype("datetime64[Y]").astype(int)) + 1970


def _series(columns_source: object, name: str) -> np.ndarray | None:
    """按列名取曲线；列不存在或为空时返回 ``None``（调用方跳过该线）。"""
    try:
        values = columns_source.column(name)  # type: ignore[attr-defined]
    except (KeyError, AttributeError):
        return None
    if not values:
        return None
    return np.asarray(values, dtype=float)


class TimeSeriesChart(QWidget):
    """一张可缩放、可悬停读数的时序图（V2 §49、§50）。

    ``kind`` 取 :data:`CHART_TITLES` 的键（不含 ``scenario``，方案比较用
    :class:`ScenarioBarChart`）。
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if kind not in CHART_TITLES or kind == CHART_SCENARIO:
            raise ValueError(f"未知的时序图类型：{kind}")
        self.kind = kind
        self._timestamps: list = []
        self._curves: dict[str, np.ndarray] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.setTitle(CHART_TITLES[kind])
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend(offset=(8, 8))
        self._apply_axis_labels()
        layout.addWidget(self.plot, 1)

        # 十字光标（V2 §50 悬停读数）
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#888888", style=Qt.DashLine))
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#888888", style=Qt.DashLine))
        self._vline.setZValue(10)
        self._hline.setZValue(10)
        self.plot.addItem(self._vline, ignoreBounds=True)
        self.plot.addItem(self._hline, ignoreBounds=True)
        self._vline.hide()
        self._hline.hide()
        self._proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved
        )

        self._readout = QLabel("悬停查看数值；滚轮缩放、拖拽平移、右键复位")
        self._readout.setStyleSheet("color:#555555;")
        layout.addWidget(self._readout)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#B00020;")
        layout.addWidget(self._status)

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _apply_axis_labels(self) -> None:
        units = {
            CHART_LOAD: ("时间", "kW"),
            CHART_PV: ("时间", "kW"),
            CHART_SOC: ("时间", "SOC %"),
            CHART_TYPICAL_DAY: ("小时", "kW"),
            CHART_PRICE: ("时间", "元/kWh"),
        }
        x_label, y_label = units.get(self.kind, ("时间", ""))
        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", y_label)

    # ------------------------------------------------------------------ #
    # 数据装配（只读 CalculationResult，不计算）
    # ------------------------------------------------------------------ #
    def set_result(self, result: CalculationResult | None) -> None:
        """装入计算结果；无 V2 结果时清空并提示（不报错）。"""
        self.plot.clear()
        self._curves.clear()
        self._timestamps = []
        self.plot.addItem(self._vline, ignoreBounds=True)
        self.plot.addItem(self._hline, ignoreBounds=True)

        report = result.time_series_results if result is not None else None
        hourly = report.hourly if report is not None else None
        if hourly is None or len(hourly) == 0:
            self._status.setText("未启用时序仿真")
            self._readout.setText("")
            return

        self._status.setText("")
        self._timestamps = list(hourly.timestamps)
        x = _epoch_seconds(self._timestamps)

        if self.kind == CHART_TYPICAL_DAY:
            self._plot_typical_day(hourly)
            return

        wanted = {
            CHART_LOAD: ("load",),
            CHART_PV: ("load", "pv"),
            CHART_SOC: ("soc",),
            CHART_PRICE: ("price",),
        }[self.kind]

        for key in wanted:
            name, colour = _SERIES_PENS[key]
            values = _series(hourly, name)
            if values is None or values.size != x.size:
                continue
            if self.kind == CHART_SOC:
                values = values * 100.0  # 显示单位换算：小数 → 百分数
            self._curves[key] = values
            self.plot.plot(x, values, pen=pg.mkPen(colour, width=1.4), name=key)

    def _plot_typical_day(self, hourly: object) -> None:
        """典型日四线图（Load / PV / Grid / Storage）：取全年第一个工作日。"""
        indices = self._representative_day_indices(hourly)
        hours = np.arange(indices.size, dtype=float)
        for key in ("load", "pv", "grid", "storage"):
            name, colour = _SERIES_PENS[key]
            values = _series(hourly, name)
            if values is None or values.size == 0:
                continue
            picked = values[indices]
            self._curves[key] = picked
            self.plot.plot(hours, picked, pen=pg.mkPen(colour, width=1.6), name=key)

    def _representative_day_indices(self, hourly: object) -> np.ndarray:
        """选一个代表日：优先全年第一个工作日，找不到则取第一天。"""
        days = _day_array(self._timestamps)
        if days.size == 0:
            return np.empty(0, dtype=int)
        counts = np.unique(days, return_counts=True)
        for day, count in zip(*counts):
            if count >= 24:
                return np.flatnonzero(days == day)[:24]
        return np.arange(min(24, days.size))

    # ------------------------------------------------------------------ #
    # 时间筛选（V2 §50）
    # ------------------------------------------------------------------ #
    def set_range(self, mode: str = RANGE_YEAR, month: int = 1, day: int = 1) -> None:
        """按 ``year`` / ``month`` / ``day`` 筛选显示区间。"""
        if not self._timestamps:
            return
        x = _epoch_seconds(self._timestamps)
        base = _year_of(self._timestamps)

        if mode == RANGE_YEAR:
            mask = np.ones(x.size, dtype=bool)
            # 「全年」显式触发自动量程：窗口尚未绘制时 ViewBox 还停在默认 0~1，
            # 显式 autoRange 才能让可视跨度反映整年数据（也让测试可断言）。
            self.plot.enableAutoRange()
            self.plot.getPlotItem().vb.autoRange(padding=0.02)
            return
        elif mode == RANGE_MONTH:
            mask = _month_array(self._timestamps) == int(month)
        elif mode == RANGE_DAY:
            target = np.datetime64(f"{base:04d}-{int(month):02d}-{int(day):02d}", "D")
            mask = _day_array(self._timestamps) == target
        else:
            raise ValueError(f"未知的时间筛选粒度：{mode}")

        idx = np.flatnonzero(mask)
        if idx.size == 0:
            self.plot.setXRange(0, 1, padding=0.02)
            return

        x_sel = x[idx]
        lo, hi = float(x_sel.min()), float(x_sel.max())
        if hi <= lo:
            hi = lo + 3600.0
        self.plot.setXRange(lo, hi, padding=0.02)

        if self.kind != CHART_TYPICAL_DAY:
            for key, values in self._curves.items():
                if values.size == x.size:
                    self._curves[key] = values  # 数据不裁剪，仅缩放视图（保持内存友好）

    # ------------------------------------------------------------------ #
    # 悬停读数（V2 §50）
    # ------------------------------------------------------------------ #
    def _on_mouse_moved(self, event: tuple) -> None:
        if not self._timestamps or not self._curves:
            return
        pos = event[0]
        vb = self.plot.getPlotItem().vb
        if not self.plot.sceneBoundingRect().contains(pos):
            self._vline.hide()
            self._hline.hide()
            return
        point = vb.mapSceneToView(pos)
        x = _epoch_seconds(self._timestamps)
        index = int(np.clip(np.searchsorted(x, point.x()), 0, x.size - 1))
        self._vline.setPos(float(x[index]))
        self._hline.setPos(float(point.y()))
        self._vline.show()
        self._hline.show()
        self._readout.setText(self._format_readout(index))

    def _format_readout(self, index: int) -> str:
        if self.kind == CHART_TYPICAL_DAY:
            stamp = f"第 {index} 小时"
        else:
            stamp = f"{self._timestamps[index]:%Y-%m-%d %H:%M}"
        parts = []
        for key, values in self._curves.items():
            if index < values.size:
                parts.append(f"{key}={values[index]:,.2f}")
        return f"{stamp}  " + "  ".join(parts)

    # ------------------------------------------------------------------ #
    # 测试与导出辅助
    # ------------------------------------------------------------------ #
    def series_point_count(self) -> int:
        """当前已绘制的曲线点数（用于测试断言与时间筛选验证）。"""
        return max((v.size for v in self._curves.values()), default=0)

    def curve_names(self) -> tuple[str, ...]:
        return tuple(self._curves)

    def visible_x_span_seconds(self) -> float:
        """当前 X 轴可视跨度（秒），用于验证时间筛选确实生效。"""
        (lo, hi), _ = self.plot.getPlotItem().vb.viewRange()
        return float(hi - lo)

    def export_png(self, path: str | Path) -> bool:
        """导出当前视图为 PNG（V2 §50 图表可导出）。失败返回 ``False``，不抛异常。"""
        try:
            pixmap = self.plot.grab()
            if pixmap.isNull():
                return False
            return bool(pixmap.save(str(path), "PNG"))
        except Exception:  # noqa: BLE001 - 导出失败不影响主流程
            return False


class ScenarioBarChart(QWidget):
    """方案比较柱状图（V2 §49 第 6 张图、§44）。

    数据取自 ``result.scenario_results``；为空时显示"未执行方案比较"。
    """

    #: 对比指标：(ScenarioResult 字段名, 中文名, 换算倍数)
    METRICS: tuple[tuple[str, str, float], ...] = (
        ("project_npv", "NPV（万元）", 1e-4),
        ("project_irr", "IRR（%）", 100.0),
        ("static_payback", "回收期（年）", 1.0),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.metric_box = QComboBox()
        for _, label, _ in self.METRICS:
            self.metric_box.addItem(label)
        row = QHBoxLayout()
        row.addWidget(QLabel("对比指标"))
        row.addWidget(self.metric_box)
        row.addStretch(1)
        layout.addLayout(row)

        self.plot = pg.PlotWidget()
        self.plot.setTitle(CHART_TITLES[CHART_SCENARIO])
        self.plot.showGrid(y=True, alpha=0.25)
        layout.addWidget(self.plot, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#B00020;")
        layout.addWidget(self._status)

        self._names: list[str] = []
        self._values: np.ndarray = np.empty(0, dtype=float)
        self.metric_box.currentIndexChanged.connect(lambda _: self._redraw())

    def set_result(self, result: CalculationResult | None) -> None:
        self._names = []
        self._values = np.empty(0, dtype=float)
        scenarios = list(result.scenario_results) if result is not None else []
        if not scenarios:
            self.plot.clear()
            self._status.setText("未执行方案比较")
            return
        self._status.setText("")
        self._scenarios = scenarios
        self._redraw()

    def _redraw(self) -> None:
        scenarios = getattr(self, "_scenarios", [])
        if not scenarios:
            return
        field, _, factor = self.METRICS[self.metric_box.currentIndex()]
        names: list[str] = []
        values: list[float] = []
        for item in scenarios:
            raw = getattr(item, field, None)
            if raw is None:
                continue
            names.append(item.label or item.name or "方案")
            values.append(float(raw) * factor)
        self._names = names
        self._values = np.asarray(values, dtype=float)

        self.plot.clear()
        if not names:
            self._status.setText("当前指标在所有方案中均无法计算")
            return
        self._status.setText("")
        axis = self.plot.getPlotItem().getAxis("bottom")
        axis.setTicks([[(i, n) for i, n in enumerate(names)]])
        self.plot.plot(
            np.arange(len(values), dtype=float),
            self._values,
            pen=None,
            symbol="o",
            symbolSize=10,
            symbolBrush="#1F77B4",
        )
        for i, value in enumerate(values):
            text = pg.TextItem(f"{value:,.2f}", anchor=(0.5, 1.0), color="#333333")
            text.setPos(float(i), value)
            self.plot.addItem(text)

    def bar_count(self) -> int:
        return int(self._values.size)

    def bar_values(self) -> np.ndarray:
        return self._values


def create_chart(kind: str) -> QWidget:
    """图表工厂：按类型返回组件（V2 §49）。

    ``scenario`` 返回 :class:`ScenarioBarChart`，其余返回 :class:`TimeSeriesChart`。
    工厂**不缓存**实例，调用方按需创建；所有 Qt 对象都在调用时构造，
    因此模块 import 不依赖 ``QApplication``。
    """
    if kind == CHART_SCENARIO:
        return ScenarioBarChart()
    return TimeSeriesChart(kind)


def create_range_selector() -> tuple[QWidget, QComboBox, QComboBox, QComboBox]:
    """构造「全年 / 按月 / 按日」时间筛选控件（V2 §50）。

    返回 ``(容器, 粒度下拉, 月份下拉, 日期下拉)``，供页面接线到
    :meth:`TimeSeriesChart.set_range`。
    """
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)

    mode = QComboBox()
    for value, label in RANGE_LABELS:
        mode.addItem(label, value)
    month = QComboBox()
    for m in range(1, 13):
        month.addItem(f"{m} 月", m)
    day = QComboBox()
    for d in range(1, 32):
        day.addItem(f"{d} 日", d)

    row.addWidget(QLabel("时间范围"))
    row.addWidget(mode)
    row.addWidget(month)
    row.addWidget(day)
    row.addStretch(1)
    return box, mode, month, day


# --------------------------------------------------------------------------- #
# V2.2 阶段 4：负荷/光伏叠加曲线与月度消纳趋势（§6.3 B、§6.3 C）
# --------------------------------------------------------------------------- #
# 说明：这两个部件是**纯展示**组件，只做"电量 → 功率"与"小数 → 百分数"这两项
# **显示单位换算**，不做任何消纳、经济或统计计算（口径与数值全部来自 application 层的
# ``SelfConsumptionResult`` / 负荷数据集）。因此界面层仍然满足 V2 §61、§148 与
# ``tests/test_gui.py`` 的"界面不得引入计算逻辑"静态扫描。
#
# 重要：新部件的类型常量**刻意不加入** :data:`CHART_TITLES` —— 该字典是
# "V2 §49 时序仿真的六张图"的权威清单，``TimeSeriesPage`` 与既有测试都按它遍历建图；
# 往里加键会让时序页去构造消纳页专用的部件（既有回归会失败）。因此这里用独立常量。
# --------------------------------------------------------------------------- #
CHART_LOAD_PV = "load_pv"
CHART_MONTHLY_RATE = "monthly_rate"

#: 负荷/光伏叠加曲线标题
LOAD_PV_TITLE = "负荷与光伏叠加曲线（kW）"
#: 月度光伏自用率趋势标题
MONTHLY_RATE_TITLE = "月度光伏自用率趋势"


class LoadPvChart(QWidget):
    """负荷 / 光伏叠加曲线（V2.2 §6.3 C「光伏与负荷叠加曲线」）。

    数据由调用方（页面）从 ``LoadProfileService`` 取来后写入，本部件不算任何东西；
    唯一的换算是把**间隔电量 kWh 除以 Δt_h 得到平均功率 kW**（显示单位换算）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timestamps: list = []
        self._load_kw = np.empty(0, dtype=float)
        self._pv_kw = np.empty(0, dtype=float)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.setTitle(LOAD_PV_TITLE)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend(offset=(8, 8))
        self.plot.setLabel("bottom", "时间")
        self.plot.setLabel("left", "kW")
        layout.addWidget(self.plot, 1)
        self.readout = QLabel("悬停查看数值；滚轮缩放、拖拽平移、右键复位")
        self.readout.setStyleSheet("color:#555555;")
        layout.addWidget(self.readout)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#B00020;")
        layout.addWidget(self.status)

    def set_series(
        self,
        timestamps: list,
        load_energy_kwh,
        pv_energy_kwh,
        interval_minutes: int,
    ) -> None:
        """写入两条曲线（电量 kWh → 显示用平均功率 kW）。"""
        self.plot.clear()
        self.plot.addLegend(offset=(8, 8))
        self._timestamps = list(timestamps)
        delta_hours = max(interval_minutes, 1) / 60.0
        self._load_kw = np.asarray(load_energy_kwh, dtype=float) / delta_hours
        self._pv_kw = np.asarray(pv_energy_kwh, dtype=float) / delta_hours
        if not self._timestamps or self._load_kw.size == 0:
            self.status.setText("尚未选择负荷数据集")
            self.readout.setText("")
            return
        self.status.setText("")
        x = _epoch_seconds(self._timestamps)
        size = min(x.size, self._load_kw.size, self._pv_kw.size)
        self.plot.plot(x[:size], self._load_kw[:size], pen=pg.mkPen("#1F77B4", width=1.2), name="负荷 kW")
        self.plot.plot(x[:size], self._pv_kw[:size], pen=pg.mkPen("#2CA02C", width=1.2), name="光伏 kW")
        self.plot.getPlotItem().vb.autoRange(padding=0.02)

    def set_range(self, mode: str = RANGE_YEAR, month: int = 1, day: int = 1) -> None:
        """按 ``year`` / ``month`` / ``day`` 缩放视图（数据不裁剪）。"""
        if not self._timestamps:
            return
        x = _epoch_seconds(self._timestamps)
        if mode == RANGE_YEAR:
            self.plot.getPlotItem().vb.autoRange(padding=0.02)
            return
        if mode == RANGE_MONTH:
            mask = _month_array(self._timestamps) == int(month)
        elif mode == RANGE_DAY:
            base = _year_of(self._timestamps)
            target = np.datetime64(f"{base:04d}-{int(month):02d}-{int(day):02d}", "D")
            mask = _day_array(self._timestamps) == target
        else:
            raise ValueError(f"未知的时间筛选粒度：{mode}")
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            self.plot.setXRange(0, 1, padding=0.02)
            return
        lo, hi = float(x[idx].min()), float(x[idx].max())
        self.plot.setXRange(lo, hi if hi > lo else lo + 3600.0, padding=0.02)

    def series_point_count(self) -> int:
        return int(min(self._load_kw.size, self._pv_kw.size))


class MonthlyRateChart(QWidget):
    """月度光伏自用率趋势（V2.2 §6.3 C「月度自用率趋势」）。

    数值直接来自 ``SelfConsumptionResult.monthly``（分母为 0 的月份显示"不适用"，
    不画点），本部件不做任何比例计算。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.plot = pg.PlotWidget()
        self.plot.setTitle(MONTHLY_RATE_TITLE)
        self.plot.showGrid(y=True, alpha=0.25)
        self.plot.setLabel("bottom", "月份（1–12）")
        self.plot.setLabel("left", "自用率 %")
        layout.addWidget(self.plot, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#B00020;")
        layout.addWidget(self.status)
        self._months: list[int] = []
        self._rates: np.ndarray = np.empty(0, dtype=float)

    def set_rows(self, monthly_rows) -> None:
        """写入逐月行（``MonthlySelfConsumptionRow`` 列表）。"""
        self.plot.clear()
        months: list[int] = []
        rates: list[float] = []
        for row in monthly_rows or []:
            rate = getattr(row, "self_consumption_rate", None)
            if rate is None:
                continue
            months.append(int(row.month))
            rates.append(float(rate) * 100.0)
        self._months = months
        self._rates = np.asarray(rates, dtype=float)
        if not months:
            self.status.setText("无可用月度自用率（光伏发电量为 0 的月份显示为「不适用」）")
            return
        self.status.setText("")
        self.plot.plot(
            np.asarray(months, dtype=float),
            self._rates,
            pen=pg.mkPen("#2CA02C", width=1.4),
            symbol="o",
            symbolSize=8,
            symbolBrush="#2CA02C",
        )
        for month, rate in zip(months, rates):
            text = pg.TextItem(f"{rate:.1f}%", anchor=(0.5, 1.4), color="#333333")
            text.setPos(float(month), float(rate))
            self.plot.addItem(text)
        self.plot.getPlotItem().vb.autoRange(padding=0.12)

    def month_count(self) -> int:
        return len(self._months)

    def rate_values(self) -> np.ndarray:
        return self._rates


__all__ = [
    "CHART_LOAD",
    "CHART_LOAD_PV",
    "CHART_MONTHLY_RATE",
    "CHART_PRICE",
    "CHART_PV",
    "CHART_SCENARIO",
    "CHART_SOC",
    "CHART_TITLES",
    "CHART_TYPICAL_DAY",
    "LOAD_PV_TITLE",
    "MONTHLY_RATE_TITLE",
    "RANGE_DAY",
    "RANGE_LABELS",
    "RANGE_MONTH",
    "RANGE_YEAR",
    "LoadPvChart",
    "MonthlyRateChart",
    "ScenarioBarChart",
    "TimeSeriesChart",
    "create_chart",
    "create_range_selector",
]
