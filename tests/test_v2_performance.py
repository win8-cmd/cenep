"""V2 性能与复用正确性测试（V2 §86、§87）。

规范 §86 的性能目标：

* 「单项目：8760 小时 —— 计算目标 < 2 秒」
* 「100 个方案扫描 —— 尽量 < 30 秒」

口径说明：8760 指的是**一个完整年度的逐时仿真**（V2 §7 的时间模型），
因此本文件把 2 秒预算断在单年仿真上；25 年全周期是 25 次仿真，另设上限。
方案扫描按单年评估（见 ``OPTIMIZATION.md`` 的两阶段设计），故 100 候选用单年耗时的 100 倍设界。

性能断言留了充足余量以免在不同机器上抖动，同时仍能抓住数量级退化
（例如有人把向量化改回逐点 NumPy 调用，耗时通常立刻翻数倍）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from cenep.calculation import economic_v2 as e2
from cenep.domain.enums import DispatchStrategy

from test_v2_integration import axis_for, build_project

#: 单年 8760 仿真预算（规范 §86）
SINGLE_YEAR_BUDGET_S = 2.0
#: 100 个候选的扫描预算（规范 §86「尽量 < 30 秒」）
SCAN_BUDGET_S = 30.0
#: 25 年全周期上限（25 次单年仿真，留松一些）
FULL_PERIOD_BUDGET_S = 12.0


def _time(fn) -> tuple[float, object]:
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


class TestPerformanceBudget:
    def test_single_year_under_two_seconds(self):
        """§86：单年 8760 仿真 < 2 秒。"""
        proj = build_project()
        axis = axis_for()
        elapsed, sim = _time(lambda: e2.simulate_year(proj, axis, 1))
        assert sim.axis.point_count == 8760
        assert elapsed < SINGLE_YEAR_BUDGET_S, f"单年仿真 {elapsed:.3f}s 超出 §86 预算"

    def test_axis_build_does_not_dominate(self):
        """时间轴构造不应成为主要开销（否则逐年仿真会线性变慢）。"""
        elapsed, axis = _time(lambda: e2.build_time_axis(2025, e2.Resolution.HOURLY, ()))
        assert axis.point_count == 8760
        assert elapsed < 0.5, f"时间轴构造 {elapsed:.3f}s 过慢"

    def test_hundred_candidates_within_scan_budget(self):
        """§86：100 个候选 < 30 秒（按单年评估推算）。"""
        proj = build_project()
        axis = axis_for()
        # 先量 5 次取平均，避免单次抖动影响判定
        total = 0.0
        for _ in range(5):
            elapsed, _ = _time(lambda: e2.simulate_year(proj, axis, 1))
            total += elapsed
        per_candidate = total / 5
        projected = per_candidate * 100
        assert projected < SCAN_BUDGET_S, (
            f"100 候选预计 {projected:.1f}s（单个 {per_candidate * 1000:.0f} ms），"
            f"超出 §86 的 {SCAN_BUDGET_S:.0f}s 预算"
        )

    def test_full_period_completes(self):
        """25 年全周期必须能在数秒内跑完。"""
        proj = build_project()
        elapsed, sim = _time(lambda: e2.simulate_project(proj))
        assert len(sim.years) == 25
        assert elapsed < FULL_PERIOD_BUDGET_S, f"25 年仿真 {elapsed:.2f}s 过慢"


class TestStaticShapeReuse:
    """曲线逐年等比例时复用首年结果——这是 25 年能在预算内的关键。"""

    def _static_project(self):
        proj = build_project()
        proj.pv.annual_degradation_rate = 0.0
        proj.storage.annual_degradation_rate = 0.0
        proj.timeseries.load.annual_growth_rate = 0.0
        proj.timeseries.tariff.annual_growth_rate = 0.0
        return proj

    def test_reuse_is_activated(self):
        sim = e2.simulate_project(self._static_project())
        assert sim.reused_first_year == 24, "静态形状下应复用首年 24 次"

    def test_reuse_much_faster_than_full_simulation(self):
        static = self._static_project()

        # 构造真正"动态"的项目：光伏衰减 + 储能衰减 + 负荷增长 + 电价增长，
        # 四项任一非零就会关闭复用路径。
        dynamic = build_project()
        dynamic.pv.annual_degradation_rate = 0.005
        dynamic.storage.annual_degradation_rate = 0.02
        dynamic.timeseries.load.annual_growth_rate = 0.02
        dynamic.timeseries.tariff.annual_growth_rate = 0.01

        t_static, sim_static = _time(lambda: e2.simulate_project(static))
        t_dynamic, sim_dynamic = _time(lambda: e2.simulate_project(dynamic))

        assert sim_static.reused_first_year == 24
        assert sim_dynamic.reused_first_year == 0
        assert t_static < 1.0, f"复用后仍耗时 {t_static:.2f}s"
        assert t_static < t_dynamic / 3.0, (
            f"复用应显著快于逐年仿真（{t_static:.2f}s vs {t_dynamic:.2f}s）"
        )

    def test_reuse_results_are_equivalent(self):
        """复用不得改变数值：与逐年仿真逐位一致。"""
        static = self._static_project()
        reused = e2.simulate_project(static)

        # 手工逐年跑，禁用复用路径
        axis = e2.build_axis(static)
        manual = [e2.simulate_year(static, axis, y) for y in range(1, 26)]

        for got, want in zip(reused.years, manual):
            assert got.year_index == want.year_index
            assert got.metrics.electricity_cost_saving == pytest.approx(
                want.metrics.electricity_cost_saving, rel=1e-12
            )
            assert got.metrics.annual_grid_purchase == pytest.approx(
                want.metrics.annual_grid_purchase, rel=1e-12
            )
            assert np.array_equal(got.outcome.storage.soc_end, want.outcome.storage.soc_end)

    def test_degradation_disables_reuse(self):
        """有光伏衰减时不得复用（各年确实不同）。"""
        proj = build_project()
        proj.pv.annual_degradation_rate = 0.005
        sim = e2.simulate_project(proj)
        assert sim.reused_first_year == 0

    def test_varying_strategy_still_deterministic(self):
        """换策略也要保持确定性（同一输入两次结果相同）。"""
        for strategy in DispatchStrategy:
            proj = build_project(strategy=strategy)
            axis = axis_for()
            a = e2.simulate_year(proj, axis, 1)
            b = e2.simulate_year(proj, axis, 1)
            assert a.metrics.electricity_cost_saving == b.metrics.electricity_cost_saving
            assert a.metrics.equivalent_cycles == b.metrics.equivalent_cycles


class TestMemoryFootprint:
    """§87：不得为每小时创建大量 Python 对象。"""

    def test_year_simulation_keeps_only_arrays(self):
        """YearSimulation 不得持有 8760 个 Pydantic 对象。"""
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert not hasattr(sim, "decisions") or not isinstance(
            getattr(sim, "decisions"), list
        ), "YearSimulation 不应预先物化 8760 个 DispatchDecision"
        # 逐时数据以列式数组存在
        assert sim.outcome.storage.soc_end.shape == (8760,)
        assert len(sim.outcome.action_codes) == 8760

    def test_decisions_materialised_lazily_and_capped(self):
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        sample = sim.decisions()
        assert len(sample) == 168, "默认只物化首周，避免 8760 个对象"
        assert sample[0].timestamp == sim.axis.timestamps[0]
        assert all(d.reason for d in sample), "每条决策都必须有可解释原因（§16）"

    def test_reason_summary_covers_all_hours(self):
        sim = e2.simulate_year(build_project(), axis_for(), 1)
        assert sum(sim.reason_summary().values()) == 8760
