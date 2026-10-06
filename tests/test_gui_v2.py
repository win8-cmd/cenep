"""V2 界面测试（V2 §5、§49–§50、§61、§68–§71、§88）。

覆盖四类风险：

1. **参数页完整性**：新增的 5 个时序段能构造表单，字段路径可读可写，
   CHOICE 选项与枚举成员一致（防止手写字符串拼错）；
2. **空结果健壮性**：未启用时序仿真时页面必须给出提示且**不报错**；
3. **数值一致性**：页面显示的指标必须等于 ``CalculationResult`` 里的值（V2 §105）；
4. **界面不得计算**（V2 §61）：静态扫描界面源码，禁止出现任何计算调用。
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cenep.application.calculation_service import CalculationService  # noqa: E402
from cenep.domain.enums import (  # noqa: E402
    DispatchStrategy,
    LoadProfileMode,
    MissingDataPolicy,
    PVProfileMode,
    Resolution,
)
from cenep.domain.models import Project  # noqa: E402
from cenep.ui.charts import (  # noqa: E402
    CHART_LOAD,
    CHART_PRICE,
    CHART_PV,
    CHART_SCENARIO,
    CHART_SOC,
    CHART_TITLES,
    CHART_TYPICAL_DAY,
    RANGE_DAY,
    RANGE_MONTH,
    RANGE_YEAR,
    ScenarioBarChart,
    TimeSeriesChart,
    create_chart,
    create_range_selector,
)
from cenep.ui.field_spec import Kind, SectionForm, get_path, set_path  # noqa: E402
from cenep.ui.main_window import MainWindow  # noqa: E402
from cenep.ui.pages import TimeSeriesPage  # noqa: E402
from cenep.ui.sections import (  # noqa: E402
    ALL_SECTIONS,
    TIMESERIES_SECTION,
    TS_DISPATCH_SECTION,
    TS_LOAD_SECTION,
    TS_PV_SECTION,
    TS_TARIFF_SECTION,
)

from test_v2_integration import build_project  # noqa: E402

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "cenep" / "ui"

#: V2 新增的 5 个参数段
V2_SECTIONS = (
    TIMESERIES_SECTION,
    TS_LOAD_SECTION,
    TS_PV_SECTION,
    TS_TARIFF_SECTION,
    TS_DISPATCH_SECTION,
)

#: 界面源码中禁止出现的计算调用（沿用 tests/test_gui.py 的口径，V2 §61）
FORBIDDEN_TOKENS = [
    "financial_metrics",
    "npv(",
    "irr(",
    "payback_period",
    "lcoe(",
    "lcos(",
]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _dispose_chart_widgets(qapp):
    """每个用例结束后释放本用例创建的无父窗口控件。

    本文件会创建大量 PyQtGraph 控件（每张图内部有多个 graphics item）。
    若全部留给进程退出时回收，会显著抬高同一次 pytest 进程的内存与句柄压力，
    进而影响**其它测试文件**（实测会让 ``tests/test_selftest.py`` 偶发失败）。
    因此用后即弃，把进程内的活动控件数量压到最小。
    """
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, (TimeSeriesChart, ScenarioBarChart, TimeSeriesPage)):
            widget.setParent(None)
            widget.close()
            widget.deleteLater()
    QApplication.processEvents()


@pytest.fixture(scope="module")
def v2_result(qapp):
    """规范 §85 的 V2 Golden Case 计算结果（启用时序仿真）。"""
    return CalculationService().calculate(build_project())


@pytest.fixture(scope="module")
def v1_result(qapp):
    """关闭时序仿真的结果，用于验证「未启用时序仿真」的降级路径。"""
    project = build_project()
    project.timeseries.enabled = False
    return CalculationService().calculate(project)


# --------------------------------------------------------------------------- #
# 任务 A：参数页字段
# --------------------------------------------------------------------------- #
class TestV2ParameterSections:
    def test_section_count_and_fields(self):
        """V2 §5：参数页要包含时序仿真相关分组，且每组都有字段。

        字段总数按**全部分组**里的 ``timeseries.*`` 路径统计，而不是只看本文件
        声明的那 5 个段 —— 字段可能被合理归并到别的分组（例如
        ``timeseries.balance_tolerance`` 属数值校验参数，已并入「计算设置」），
        按路径统计才不会因搬动而失效。
        """
        assert len(ALL_SECTIONS) == 14
        for section in V2_SECTIONS:
            assert section.fields, f"{section.title} 没有字段"

        v2_paths = [
            spec.path
            for section in ALL_SECTIONS
            for spec in section.fields
            if spec.path.startswith("timeseries.")
        ]
        assert len(v2_paths) == 40, f"V2 时序参数应为 40 个，实际 {len(v2_paths)}"
        assert len(set(v2_paths)) == len(v2_paths), "存在重复的字段路径"

    def test_v2_fields_reachable_from_every_section(self):
        """无论字段落在哪个分组，其路径都必须可在 ``Project`` 上访问。"""
        project = Project()
        for section in ALL_SECTIONS:
            for spec in section.fields:
                if spec.path.startswith("timeseries."):
                    get_path(project, spec.path)

    @pytest.mark.parametrize("section", V2_SECTIONS, ids=lambda s: s.title)
    def test_section_form_builds(self, qapp, section):
        form = SectionForm(section)
        assert form is not None
        assert len(section.fields) > 0

    def test_all_paths_round_trip(self):
        """每个新增字段都必须能在 ``Project`` 上读-写-读往返。"""
        project = Project()
        for section in V2_SECTIONS:
            for spec in section.fields:
                original = get_path(project, spec.path)
                set_path(project, spec.path, original)
                assert get_path(project, spec.path) == original, spec.path

    def test_all_paths_exist_on_project(self):
        project = Project()
        for section in V2_SECTIONS:
            for spec in section.fields:
                get_path(project, spec.path)  # 不存在会抛异常

    def test_choice_options_match_enums(self):
        """CHOICE 的取值必须来自枚举成员，防止手写字符串拼错。"""
        expected = {
            "timeseries.resolution": {m.value for m in Resolution},
            "timeseries.load.mode": {m.value for m in LoadProfileMode},
            "timeseries.pv.mode": {m.value for m in PVProfileMode},
            "timeseries.load.missing_data_policy": {m.value for m in MissingDataPolicy},
            "timeseries.pv.missing_data_policy": {m.value for m in MissingDataPolicy},
            "timeseries.dispatch.strategy": {m.value for m in DispatchStrategy},
        }
        seen = set()
        for section in V2_SECTIONS:
            for spec in section.fields:
                if spec.kind is not Kind.CHOICE:
                    continue
                values = {value for value, _ in spec.choices}
                assert values, f"{spec.path} 的 choices 为空"
                assert all(label for _, label in spec.choices), f"{spec.path} 缺少中文标签"
                if spec.path in expected:
                    assert values == expected[spec.path], spec.path
                    seen.add(spec.path)
        assert seen == set(expected), f"未覆盖的 CHOICE 字段：{set(expected) - seen}"

    def test_toggle_fields_are_bool(self):
        paths = {spec.path: spec.kind for section in V2_SECTIONS for spec in section.fields}
        assert paths["timeseries.enabled"] is Kind.BOOL
        assert paths["timeseries.dispatch.allow_grid_charge"] is Kind.BOOL
        assert paths["timeseries.tariff.demand_charge_enabled"] is Kind.BOOL

    def test_numeric_ranges_match_model(self):
        """字段范围必须与模型的 ge/le 一致（SOC 0~1、增长率 -0.5~1）。"""
        specs = {spec.path: spec for section in V2_SECTIONS for spec in section.fields}
        assert specs["timeseries.dispatch.soc_min"].kind is Kind.PERCENT
        assert specs["timeseries.dispatch.soc_min"].maximum == 100.0
        assert specs["timeseries.load.annual_growth_rate"].minimum == -50.0
        assert specs["timeseries.pv.capacity_kwp"].kind is Kind.OPTIONAL_FLOAT


# --------------------------------------------------------------------------- #
# 任务 C：图表组件
# --------------------------------------------------------------------------- #
class TestCharts:
    @pytest.mark.parametrize("kind", sorted(CHART_TITLES))
    def test_chart_constructs_and_accepts_result(self, qapp, kind, v2_result):
        chart = create_chart(kind)
        chart.set_result(v2_result)
        assert chart is not None

    def test_series_point_counts(self, qapp, v2_result):
        """§49：时序图为 8760 点，典型日为 24 点。"""
        for kind in (CHART_LOAD, CHART_PV, CHART_SOC, CHART_PRICE):
            chart = create_chart(kind)
            chart.set_result(v2_result)
            assert chart.series_point_count() == 8760, kind
        typical = create_chart(CHART_TYPICAL_DAY)
        typical.set_result(v2_result)
        assert typical.series_point_count() == 24

    def test_pv_chart_has_two_curves(self, qapp, v2_result):
        chart = create_chart(CHART_PV)
        chart.set_result(v2_result)
        assert set(chart.curve_names()) == {"load", "pv"}

    def test_typical_day_has_four_curves(self, qapp, v2_result):
        chart = create_chart(CHART_TYPICAL_DAY)
        chart.set_result(v2_result)
        assert set(chart.curve_names()) == {"load", "pv", "grid", "storage"}

    def test_time_filtering_changes_span(self, qapp, v2_result):
        """§50：时间筛选（日/月/年）必须真实改变可视区间。"""
        chart = create_chart(CHART_LOAD)
        chart.set_result(v2_result)
        chart.set_range(RANGE_YEAR)
        year_span = chart.visible_x_span_seconds()
        chart.set_range(RANGE_MONTH, month=7)
        month_span = chart.visible_x_span_seconds()
        chart.set_range(RANGE_DAY, month=7, day=15)
        day_span = chart.visible_x_span_seconds()

        assert year_span > month_span > day_span
        assert 30 < month_span / 86400 < 34, "7 月跨度应约 31 天"
        assert 23 < day_span / 3600 < 25, "单日跨度应约 24 小时"

    def test_crosshair_readout_format(self, qapp, v2_result):
        """§50：悬停读数要包含时间戳与各系列数值。"""
        chart = create_chart(CHART_LOAD)
        chart.set_result(v2_result)
        text = chart._format_readout(123)
        assert "2025-01-0" in text and "load=" in text

    def test_scenario_chart_empty_state(self, qapp, v1_result):
        """§49 第 6 张图：无方案数据时给出提示而不是报错。"""
        chart = create_chart(CHART_SCENARIO)
        assert isinstance(chart, ScenarioBarChart)
        chart.set_result(v1_result)
        assert chart.bar_count() == 0
        chart.set_result(None)
        assert chart.bar_count() == 0

    def test_charts_tolerate_missing_result(self, qapp):
        for kind in sorted(CHART_TITLES):
            chart = create_chart(kind)
            chart.set_result(None)
        chart = create_chart(CHART_LOAD)
        chart.set_range(RANGE_MONTH, month=3)  # 无数据时筛选不得抛异常

    def test_unknown_chart_kind_rejected(self, qapp):
        with pytest.raises(ValueError, match="未知的时序图类型"):
            create_chart("不存在的图")

    def test_range_selector_defaults(self, qapp):
        _box, mode, month, day = create_range_selector()
        assert mode.count() == 3
        assert [mode.itemData(i) for i in range(3)] == [RANGE_YEAR, RANGE_MONTH, RANGE_DAY]
        assert month.count() == 12 and day.count() == 31

    def test_png_export(self, qapp, v2_result, tmp_path: Path):
        """图表可导出 PNG；offscreen 下若渲染失败则跳过而不是误判。"""
        chart = create_chart(CHART_LOAD)
        chart.set_result(v2_result)
        chart.resize(800, 400)
        target = tmp_path / "chart.png"
        if not chart.export_png(target):
            pytest.skip("offscreen 平台无法渲染图表，跳过导出断言")
        assert target.exists() and target.stat().st_size > 0


# --------------------------------------------------------------------------- #
# 任务 B：结果页面（§68–§71）
# --------------------------------------------------------------------------- #
class TestTimeSeriesPage:
    def test_disabled_simulation_shows_hint(self, qapp, v1_result):
        """未启用时序仿真时必须提示且不报错，这是最常见的用户路径。"""
        assert v1_result.time_series_results is None
        page = TimeSeriesPage()
        page.show_result(Project(), v1_result)
        assert "未启用时序仿真" in page.banner.text()
        assert page.overview_values() == {}
        assert page.hourly_row_count() == 0
        for kind in CHART_TITLES:
            widget = page.chart_widget(kind)
            count = (
                widget.series_point_count()
                if hasattr(widget, "series_point_count")
                else widget.bar_count()
            )
            assert count == 0

    def test_none_result_is_safe(self, qapp):
        page = TimeSeriesPage()
        page.show_result(None, None)
        assert "未启用时序仿真" in page.banner.text()

    def test_page_fills_key_figures(self, qapp, v2_result):
        """§68：结果页首要显示的指标，且数值与 CalculationResult 一致（§105）。"""
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        values = page.overview_values()

        assert values["项目投资额"] == f"{v2_result.total_capex:,.2f} 元"
        assert values["光伏容量"] == f"{v2_result.pv_capacity_kwp:,.2f} kWp"
        assert values["项目 NPV"] == f"{v2_result.project_npv:,.2f} 元"
        assert values["LCOE"] == f"{v2_result.lcoe:.4f} 元/kWh"
        # 注意（跨会话修正）：此处曾硬编码 "20.03%"，那是**缺陷版本**的被高估 IRR
        # （build_project 未显式设置 project_type → 年度模型不计储能造价，却计入
        # 时序储能收益，V2 §105 口径不一致）。修复后项目 IRR 为 15.00%，与显式
        # PV_STORAGE 完全一致，因此改为从结果派生，保持 §105 一致性语义且不会过期。
        assert f"{v2_result.project_irr:.2%}" in values["项目 IRR"]

    def test_overview_covers_spec68_fields(self, qapp, v2_result):
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        required = {
            "项目投资额", "光伏容量", "储能容量", "首年发电量", "首年自用率", "首年自给率",
            "首年电费节省", "首年储能收益", "项目 IRR", "股东 IRR", "项目 NPV",
            "静态回收期", "动态回收期", "LCOE", "LCOS",
        }
        assert required <= set(page.overview_values())

    def test_metrics_match_result(self, qapp, v2_result):
        """§41 指标必须与 result.time_series_results.metrics 一致。"""
        metrics = v2_result.time_series_results.metrics
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        values = page.metrics_values()
        assert values["年发电量"] == f"{metrics.annual_pv_generation:,.2f} kWh"
        assert values["储能套利收益"] == f"{metrics.storage_arbitrage_revenue:,.2f} 元"
        assert values["实际等效循环次数"] == f"{metrics.equivalent_cycles:,.2f} 次"

    def test_balance_page_shows_all_flows(self, qapp, v2_result):
        """§70：九条能量流与平衡误差都要显示，且正常时判定为平衡。"""
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        values = page.balance_values()
        for name in (
            "PV 发电量", "PV → 负荷", "PV → 储能", "PV → 上网", "PV 弃光",
            "电网 → 负荷", "电网 → 储能", "储能 → 负荷", "储能 → 上网",
            "平衡误差（合计）",
        ):
            assert name in values, name
        assert values["判定结果"] == "平衡"
        assert "1e-06" in page.balance_banner.text() or "1.0e-06" in page.balance_banner.text()

    def test_storage_page(self, qapp, v2_result):
        """§71：储能页要给出容量/功率/时长/SOC/循环/充放电量/效率/收益/LCOS。"""
        page = TimeSeriesPage()
        project = build_project()
        page.show_result(project, v2_result)
        values = page.storage_values()
        for name in (
            "储能容量", "储能功率", "储能时长", "SOC 上下限（设置）", "充电效率", "放电效率",
            "往返效率", "年充电量", "年放电量", "配置循环次数", "实际等效循环次数",
            "储能套利收益", "LCOS",
        ):
            assert name in values, name
        dispatch = project.timeseries.dispatch
        assert values["充电效率"] == f"{dispatch.charge_efficiency:.4%}"
        assert values["往返效率"] == f"{dispatch.round_trip_efficiency:.4%}"

    def test_hourly_table_and_filtering(self, qapp, v2_result):
        """§69：全年/按月/按日切换必须改变明细行数与内容。"""
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        assert page.hourly_row_count() == 48  # 预览上限
        assert "8760" in page.hourly_hint.text()

        page.range_mode.setCurrentIndex(1)  # 按月
        page.range_month.setCurrentIndex(6)  # 7 月
        page._refresh_hourly()
        assert "744" in page.hourly_hint.text()

        page.range_mode.setCurrentIndex(2)  # 按日
        page.range_day.setCurrentIndex(14)  # 15 日
        page._refresh_hourly()
        assert "24" in page.hourly_hint.text()

    def test_hourly_row_carries_dispatch_reason(self, qapp, v2_result):
        """§16 在界面上的体现：明细表最后一列是「动作 / 原因」。"""
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        headers = [
            page.hourly_table.horizontalHeaderItem(c).text()
            for c in range(page.hourly_table.columnCount())
        ]
        assert headers[-1] == "动作 / 原因"

    def test_charts_bound_to_result(self, qapp, v2_result):
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        assert page.chart_widget(CHART_LOAD).series_point_count() == 8760

    def test_clear_resets_tables(self, qapp, v2_result):
        page = TimeSeriesPage()
        page.show_result(build_project(), v2_result)
        assert page.overview_values()
        page.clear()
        assert page.overview_values() == {}


# --------------------------------------------------------------------------- #
# 主窗口装配
# --------------------------------------------------------------------------- #
class TestMainWindowAssembly:
    def test_timeseries_tab_present(self, qapp, tmp_path: Path):
        """V2 §5：主窗口新增「时序仿真」标签，且位于「结果」之后。"""
        window = MainWindow(db_path=tmp_path / "gui_v2.db")
        try:
            titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
            assert "时序仿真" in titles
            assert titles.index("时序仿真") == titles.index("结果") + 1
        finally:
            window.close()

    def test_tab_populates_from_result(self, qapp, tmp_path: Path, v2_result):
        window = MainWindow(db_path=tmp_path / "gui_v2b.db")
        try:
            window.timeseries_page.show_result(build_project(), v2_result)
            assert window.timeseries_page.overview_values()["项目 NPV"] == (
                f"{v2_result.project_npv:,.2f} 元"
            )
        finally:
            window.close()


# --------------------------------------------------------------------------- #
# 界面不得计算（V2 §61、V1 §109）
# --------------------------------------------------------------------------- #
class TestUiHasNoCalculation:
    def test_forbidden_tokens_absent(self):
        offenders: list[str] = []
        for path in sorted(UI_DIR.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        assert not offenders, f"界面源码出现计算调用：{offenders}"

    def test_charts_module_has_no_economic_formula(self):
        """``charts.py`` 只能读列，不得出现任何聚合或公式。

        注意：``project_npv`` / ``static_payback`` 这类**结果字段名**是允许的
        （那是在读取 ``ScenarioResult`` 的字段供展示），禁止的是**计算行为**：
        对经济量做聚合、或引用计算层。
        """
        source = (UI_DIR / "charts.py").read_text(encoding="utf-8")
        for token in ("np.sum(", "np.mean(", "np.cumsum(", "npv(", "irr(", "lcoe(", "lcos("):
            assert token not in source, f"charts.py 出现计算调用：{token}"
        assert "calculation" not in source, "charts.py 不得引用计算层"

    def test_charts_read_only_from_result_columns(self):
        """曲线数据必须来自 ``TimeSeriesResultSet.column()``，而不是自己算。"""
        source = (UI_DIR / "charts.py").read_text(encoding="utf-8")
        assert ".column(" in source, "charts.py 应从列式结果取数"
        for forbidden in (".annual_results", ".project_cashflows", "model_dump"):
            assert forbidden not in source, f"charts.py 不应访问 {forbidden}"

    def test_new_pages_do_not_import_calculation_logic(self):
        """界面只允许导入 ``calculation.errors`` 的异常类型，不得导入计算模块。

        （``calculation.errors`` 是 V1 既有约定：界面需要捕获 ``CalculationError``。）
        """
        for name in ("charts.py", "pages.py", "sections.py", "main_window.py"):
            source = (UI_DIR / name).read_text(encoding="utf-8")
            imports = re.findall(r"from\s+\.\.calculation[.\w]*\s+import", source)
            for item in imports:
                assert "calculation.errors" in item, f"{name} 导入了计算逻辑：{item}"
