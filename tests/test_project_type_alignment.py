"""项目类型与容量口径对齐测试（V2 §105 结果一致性；V1 §1.1 兼容性红线）。

锁定的缺陷
----------
典型 V2 项目这样配置：``project.timeseries.enabled = True`` 且
``storage.storage_energy_kwh = 1000``、``storage_power_kw = 500``，但**没有**设置
``basic_info.project_type``（取 Pydantic 默认值 ``COMMERCIAL_PV``）。
``ProjectType.COMMERCIAL_PV.has_storage == False``，于是：

* 年度模型（V1 引擎）按"无储能"计算——储能**不计造价、不计循环**；
* 时序仿真 ``economic_v2.simulate_project`` **确实**用了 1000 kWh 储能；
* ``CalculationService`` 又通过 ``year_override`` 把储能套利收益注入年度模型。

后果是"算了储能的收益、没算储能的造价"，IRR / NPV 被系统性高估，且同一份
``CalculationResult`` 里"年度模型说没有储能、时序模型说有"，违反 §105 结果一致性。

修复口径
--------
``CalculationService`` 在启用时序时，按用户**实际配置**的容量归一化项目类型
（复用 ``cenep.calculation.scenario_engine.normalize_project_type``），使两侧口径一致，
并在 ``CalculationResult.notes`` 追加中文说明；归一化在副本上进行，不修改调用方对象。
未启用时序时（V1）**不做**任何归一化，由 V1 校验器按原样给出中文报错。
"""

from __future__ import annotations

import copy

import pytest

from cenep.application.calculation_service import (
    ALIGNMENT_NOTE_KEYWORD,
    CalculationService,
    align_project_type,
)
from cenep.application.project_service import ProjectService
from cenep.calculation import economic_v2 as e2
from cenep.calculation.engine import calculation_engine
from cenep.calculation.errors import CalculationError
from cenep.domain.enums import DispatchStrategy, ProjectType
from cenep.domain.models import Project

from test_v2_integration import build_project

#: Golden Case（§85）的储能配置：1000 kWh / 500 kW
GOLDEN_STORAGE_KWH = 1000.0
GOLDEN_STORAGE_KW = 500.0
#: 储能单位投资 1000 元/kWh → 储能造价 1,000,000 元
GOLDEN_STORAGE_CAPEX = GOLDEN_STORAGE_KWH * 1000.0
#: 光伏 1000 kWp × 3000 元/kWp → 光伏造价 3,000,000 元
GOLDEN_PV_CAPEX = 3_000_000.0


def _service() -> CalculationService:
    return CalculationService(project_service=ProjectService(autosave_enabled=False))


def _forgotten_type(**kwargs) -> Project:
    """复现"用户只填容量、忘记设置 project_type"的项目。

    ``Project().basic_info.project_type`` 的默认值即 ``COMMERCIAL_PV``。
    """
    project = build_project(**kwargs)
    project.basic_info.project_type = ProjectType.COMMERCIAL_PV
    return project


def _explicit_pv_storage(**kwargs) -> Project:
    """显式设置正确类型的同一项目（期望口径的对照）。"""
    return build_project(project_type=ProjectType.PV_STORAGE, **kwargs)


# --------------------------------------------------------------------------- #
# ① 归一化核心：容量、造价、口径一致
# --------------------------------------------------------------------------- #
class TestAlignmentCore:
    def test_storage_capacity_and_capex_follow_user_config(self):
        """核心断言：配置了 1000 kWh 储能，年度模型必须认账并计入储能造价。

        修复前：``storage_energy_kwh == 0.0``、``storage_power_kw == 0.0``、
        ``capex_breakdown["储能投资"] == 0``（但时序仿真用了 1000 kWh）。
        """
        implicit = _service().calculate(_forgotten_type())
        explicit = _service().calculate(_explicit_pv_storage())

        assert implicit.storage_energy_kwh == GOLDEN_STORAGE_KWH
        assert implicit.storage_power_kw == GOLDEN_STORAGE_KW
        assert implicit.storage_duration_hours == pytest.approx(2.0)
        assert implicit.capex_breakdown["储能投资"] == pytest.approx(GOLDEN_STORAGE_CAPEX)
        assert implicit.capex_breakdown["储能投资"] > 0.0
        assert implicit.total_capex == pytest.approx(GOLDEN_PV_CAPEX + GOLDEN_STORAGE_CAPEX)

        # 与显式 PV_STORAGE 的构造方式**完全一致**
        assert implicit.project_type == explicit.project_type == ProjectType.PV_STORAGE.value
        assert implicit.total_capex == explicit.total_capex
        assert implicit.capex_breakdown == explicit.capex_breakdown
        assert implicit.pv_capacity_kwp == explicit.pv_capacity_kwp

    def test_irr_npv_payback_identical_to_explicit_pv_storage(self):
        """IRR / NPV / 回收期必须与显式 PV_STORAGE **逐位一致**（回归主断言）。"""
        a = _service().calculate(_forgotten_type())
        b = _service().calculate(_explicit_pv_storage())

        assert a.project_irr == b.project_irr
        assert a.equity_irr == b.equity_irr
        assert a.project_npv == b.project_npv
        assert a.equity_npv == b.equity_npv
        assert a.static_payback == b.static_payback
        assert a.discounted_payback == b.discounted_payback
        assert a.project_cashflows == b.project_cashflows
        assert a.roi == b.roi

    def test_overestimation_is_removed_relative_to_legacy_wiring(self):
        """证明高估被消除：修复后的 IRR 严格低于"旧接线"（时序收益 + 年度模型无储能）。

        旧接线 = 服务把时序 ``year_override`` 注入年度模型，但项目类型仍是
        ``COMMERCIAL_PV``（储能造价为 0）——即缺陷版本的行为，等价于直接调用唯一引擎。
        """
        project = _forgotten_type()
        simulation = e2.simulate_project(copy.deepcopy(project))
        legacy = calculation_engine.calculate(
            copy.deepcopy(project), year_override=simulation.overrides()
        )
        fixed = _service().calculate(project)

        # 旧接线：用了储能收益、却没有储能造价
        assert legacy.storage_energy_kwh == 0.0
        assert legacy.capex_breakdown["储能投资"] == 0.0
        assert simulation.years[0].metrics.annual_storage_discharge > 0.0

        # 修复后：储能造价进入总投资，IRR 不再被高估
        assert fixed.total_capex == pytest.approx(legacy.total_capex + GOLDEN_STORAGE_CAPEX)
        assert fixed.storage_energy_kwh == GOLDEN_STORAGE_KWH
        assert fixed.project_irr is not None and legacy.project_irr is not None
        assert fixed.project_irr < legacy.project_irr
        assert fixed.project_npv < legacy.project_npv
        assert (
            fixed.static_payback is not None
            and legacy.static_payback is not None
            and fixed.static_payback > legacy.static_payback
        )  # 储能造价抬高投资 → 回收期变长

    def test_caller_project_is_not_modified(self):
        """归一化必须发生在副本上：调用方对象（含类型）保持原样（§105）。"""
        project = _forgotten_type()
        before = copy.deepcopy(project)

        result = _service().calculate(project)

        assert project.basic_info.project_type is ProjectType.COMMERCIAL_PV
        assert project == before
        assert result.project_type == ProjectType.PV_STORAGE.value

    def test_align_project_type_returns_copy_and_metadata(self):
        """``align_project_type`` 的返回值语义（副本 + 依据 + 说明）。"""
        project = _forgotten_type()
        alignment = align_project_type(project)

        assert alignment.project is not project
        assert project.basic_info.project_type is ProjectType.COMMERCIAL_PV
        assert alignment.original_type is ProjectType.COMMERCIAL_PV
        assert alignment.aligned_type is ProjectType.PV_STORAGE
        assert alignment.changed is True
        assert alignment.has_pv is True
        assert alignment.has_storage is True
        assert alignment.pv_capacity_kwp == pytest.approx(1000.0)
        assert alignment.storage_energy_kwh == GOLDEN_STORAGE_KWH


# --------------------------------------------------------------------------- #
# ② 退化情形：纯储能 / 纯光伏
# --------------------------------------------------------------------------- #
class TestDegenerateAlignments:
    def test_storage_only_project_aligns_to_commercial_storage(self):
        """光伏容量 0、储能 > 0 → 自动判为 ``COMMERCIAL_STORAGE`` 并算通。"""
        project = _forgotten_type(pv_kwp=0.0, pv_hours=0.0)
        alignment = align_project_type(project)
        assert alignment.aligned_type is ProjectType.COMMERCIAL_STORAGE

        result = _service().calculate(project)
        assert result.project_type == ProjectType.COMMERCIAL_STORAGE.value
        assert result.pv_capacity_kwp == 0.0
        assert result.storage_energy_kwh == GOLDEN_STORAGE_KWH
        assert result.storage_power_kw == GOLDEN_STORAGE_KW
        assert result.capex_breakdown["光伏投资"] == 0.0
        assert result.total_capex == pytest.approx(GOLDEN_STORAGE_CAPEX)
        assert any(ALIGNMENT_NOTE_KEYWORD in note for note in result.notes)

    def test_pv_only_project_aligns_to_commercial_pv(self):
        """储能容量 0、光伏 > 0 → 自动判为 ``COMMERCIAL_PV``（不得造出储能造价）。"""
        project = _forgotten_type(storage_kw=0.0, storage_kwh=0.0)
        alignment = align_project_type(project)
        assert alignment.aligned_type is ProjectType.COMMERCIAL_PV

        result = _service().calculate(project)
        assert result.project_type == ProjectType.COMMERCIAL_PV.value
        assert result.storage_energy_kwh == 0.0
        assert result.storage_power_kw == 0.0
        assert result.capex_breakdown["储能投资"] == 0.0
        assert result.total_capex == pytest.approx(GOLDEN_PV_CAPEX)

    def test_no_alignment_note_when_type_already_matches(self):
        """类型本就正确时不追加说明（避免噪声）。"""
        result = _service().calculate(_explicit_pv_storage())
        assert not any(ALIGNMENT_NOTE_KEYWORD in note for note in result.notes)


# --------------------------------------------------------------------------- #
# ③ notes 说明与"无储能但配置要求储能"的告警
# --------------------------------------------------------------------------- #
class TestNotes:
    def test_alignment_note_explains_target_and_basis(self):
        """归一化必须写入中文说明：判定结果 + 依据（哪些容量非零）+ 原类型。"""
        result = _service().calculate(_forgotten_type())
        hits = [note for note in result.notes if ALIGNMENT_NOTE_KEYWORD in note]
        assert len(hits) == 1
        note = hits[0]
        assert "项目类型已按容量自动判定为「工商业光储」（PV_STORAGE）" in note
        assert "光伏容量 1000 kWp > 0" in note
        assert "储能容量 1000 kWh > 0" in note
        assert "COMMERCIAL_PV" in note  # 说明原配置，便于追溯（§161）

    def test_zero_storage_with_storage_power_warns_in_chinese(self):
        """储能容量为 0 但仍配置了储能功率/套利策略 → 必须给出中文告警，不得静默。"""
        project = _forgotten_type(storage_kwh=0.0, storage_kw=0.0)
        project.storage.storage_power_kw = 500.0  # 只填了功率，漏填容量

        result = _service().calculate(project)
        warnings = [note for note in result.notes if "储能容量为 0" in note]
        assert warnings, "储能容量为 0 且存在储能配置时必须给出告警说明"
        assert "储能功率 500 kW > 0" in warnings[0]
        assert "峰谷套利" in warnings[0]  # 调度策略本身也以储能充放电为前提
        # 告警不等于失败：无储能的口径是一致的，计算仍然完成
        assert result.storage_energy_kwh == 0.0

    def test_no_warning_when_no_storage_is_configured_at_all(self):
        """彻底没有储能配置（含默认调度策略）时不得给出告警。"""
        project = _forgotten_type(storage_kw=0.0, storage_kwh=0.0)
        project.timeseries.dispatch.strategy = DispatchStrategy.PV_SELF_CONSUMPTION

        result = _service().calculate(project)
        assert not any("储能容量为 0" in note for note in result.notes)

    def test_roof_area_pv_caliber_gap_warns_in_chinese(self):
        """只填屋顶面积时：V1 口径能算出光伏、时序口径为 0 → 必须告警，不得静默。

        逐时仿真的光伏容量口径是 ``timeseries.pv.capacity_kwp → pv.pv_capacity_kwp → 0``，
        不含"按屋顶面积换算"（V1 §19 有该回退）。两侧口径撕裂时必须提示用户补填显式容量。
        """
        project = build_project(storage_kw=0.0, storage_kwh=0.0)
        project.basic_info.project_type = ProjectType.COMMERCIAL_PV
        project.pv.pv_capacity_kwp = None  # 只给屋顶面积
        project.pv.usable_roof_area_m2 = 6500.0
        project.pv.area_per_kwp = 6.0
        project.timeseries.pv.capacity_kwp = None
        project.timeseries.dispatch.strategy = DispatchStrategy.PV_SELF_CONSUMPTION

        result = _service().calculate(project)

        warnings = [note for note in result.notes if "光伏容量口径不一致" in note]
        assert warnings, "屋顶面积口径与时序口径撕裂时必须给出告警说明"
        assert "1083.33" in warnings[0]  # 6500 ÷ 6 = 1083.33 kWp（V1 §19 口径）
        # 口径撕裂的后果：年度模型仍按 V1 §19 计入屋顶面积对应的光伏容量与造价，
        # 但逐时口径为 0 → 首年发电量为 0（偏保守：只计光伏造价、不计光伏收益），
        # 因此必须告警提示用户补填显式容量，而不是让用户拿到一个莫名亏损的结果。
        assert result.pv_capacity_kwp == pytest.approx(6500.0 / 6.0, rel=1e-9)
        assert result.first_year_generation == 0.0


# --------------------------------------------------------------------------- #
# ④ 反向保护：内部不一致必须显式失败（不得静默继续）
# --------------------------------------------------------------------------- #
class TestReverseProtection:
    def test_internal_inconsistency_raises_chinese_calculation_error(self, monkeypatch):
        """归一化后年度模型仍判定"无储能"→ 抛中文 ``CalculationError``。

        用 monkeypatch 模拟"归一化失效"（例如规则被改坏），验证这道防线真实生效。
        """
        from cenep.calculation import scenario_engine

        monkeypatch.setattr(
            scenario_engine,
            "normalize_project_type",
            lambda project: ProjectType.COMMERCIAL_PV,  # 故意不调整类型
        )

        with pytest.raises(CalculationError) as excinfo:
            _service().calculate(_forgotten_type())

        message = str(excinfo.value)
        assert "储能" in message
        assert "不一致" in message
        assert "高估" in message
        assert excinfo.value.field == "basic_info.project_type"

    def test_inconsistency_error_is_not_swallowed_by_generic_wrapper(self, monkeypatch):
        """反向保护的报错必须是计算错误（中文可读），而不是被兜底包装成"未预期错误"。"""
        from cenep.calculation import scenario_engine

        monkeypatch.setattr(
            scenario_engine,
            "normalize_project_type",
            lambda project: ProjectType.COMMERCIAL_PV,
        )
        with pytest.raises(CalculationError) as excinfo:
            _service().calculate(_forgotten_type())
        assert "未预期错误" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# ⑤ V1 行为不变（§1.1）：未启用时序时严禁归一化
# --------------------------------------------------------------------------- #
class TestV1Untouched:
    def test_disabled_timeseries_keeps_original_type_and_caliber(self):
        """§1.1：``timeseries.enabled=False`` 时按 V1 原样计算，类型不被改写。"""
        project = _forgotten_type()
        project.timeseries.enabled = False

        result = _service().calculate(project)

        assert result.project_type == ProjectType.COMMERCIAL_PV.value
        assert result.storage_energy_kwh == 0.0  # V1 口径：该类型不含储能
        assert result.capex_breakdown["储能投资"] == 0.0
        assert result.total_capex == pytest.approx(GOLDEN_PV_CAPEX)
        assert not any(ALIGNMENT_NOTE_KEYWORD in note for note in result.notes)
        assert project.basic_info.project_type is ProjectType.COMMERCIAL_PV

    def test_disabled_timeseries_type_mismatch_reports_v1_chinese_error(self):
        """§1.1：V1 的类型/容量不匹配仍由 V1 校验器报中文错，不得被归一化绕过。"""
        project = build_project(project_type=ProjectType.COMMERCIAL_STORAGE)
        project.timeseries.enabled = False
        project.storage.storage_energy_kwh = 0.0  # 与"储能项目"矛盾
        project.storage.storage_power_kw = 0.0

        with pytest.raises(CalculationError) as excinfo:
            _service().calculate(project)

        assert "储能容量必须大于 0" in str(excinfo.value)
        assert excinfo.value.field == "storage.storage_energy_kwh"
        # V1 校验失败时也不得改写调用方项目
        assert project.basic_info.project_type is ProjectType.COMMERCIAL_STORAGE

    def test_v1_result_identical_with_and_without_service(self):
        """§1.1：未启用时序时，经由服务与直接调用引擎的结果逐位一致。"""
        project = _forgotten_type()
        project.timeseries.enabled = False

        direct = calculation_engine.calculate(copy.deepcopy(project))
        via_service = _service().calculate(project)

        assert direct.project_irr == via_service.project_irr
        assert direct.project_npv == via_service.project_npv
        assert direct.project_cashflows == via_service.project_cashflows
        assert direct.storage_energy_kwh == via_service.storage_energy_kwh == 0.0
