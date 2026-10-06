"""电价与收益模块单元测试（规范 §27、§28、§32、§43、§44）。"""

from __future__ import annotations

import pytest

from cenep.calculation.errors import ValidationError
from cenep.calculation.revenue import (
    average_tou_price,
    export_revenue,
    resolve_tariff,
    self_use_revenue,
    storage_arbitrage_revenue,
    storage_total_revenue,
)


class TestTouPrice:
    def test_average_formula(self):
        """§32：AverageTOUPrice = 峰×峰比例 + 平×平比例 + 谷×谷比例。"""
        value = average_tou_price(1.0, 0.7, 0.4, 0.3, 0.4, 0.3)
        assert value == pytest.approx(0.70)

    def test_ratios_must_sum_to_one(self):
        """§31：三个比例之和必须为 1。"""
        with pytest.raises(ValidationError) as exc:
            average_tou_price(1.0, 0.7, 0.4, 0.5, 0.4, 0.3)
        assert "比例之和必须等于 1" in str(exc.value)
        assert exc.value.field == "tou_ratios"


class TestResolveTariff:
    def test_fixed_mode(self):
        r = resolve_tariff("FIXED", average_price=0.65, export_price=0.3)
        assert r.avoided_electricity_price == pytest.approx(0.65)
        assert r.charge_price == pytest.approx(0.65)
        assert r.export_price == pytest.approx(0.3)

    def test_tou_mode_uses_average_and_valley(self):
        """§31、§32：替代价取综合电价，充电价取谷价。"""
        r = resolve_tariff(
            "TOU",
            peak_price=1.0,
            flat_price=0.7,
            valley_price=0.4,
            peak_ratio=0.3,
            flat_ratio=0.4,
            valley_ratio=0.3,
        )
        assert r.avoided_electricity_price == pytest.approx(0.70)
        assert r.charge_price == pytest.approx(0.40)
        assert r.average_tou_price == pytest.approx(0.70)

    def test_market_mode(self):
        r = resolve_tariff("MARKET", market_price=0.42)
        assert r.avoided_electricity_price == pytest.approx(0.42)
        assert r.charge_price == pytest.approx(0.42)

    def test_custom_mode_requires_both(self):
        with pytest.raises(ValidationError):
            resolve_tariff("CUSTOM", custom_avoided_price=0.7)
        r = resolve_tariff("CUSTOM", custom_avoided_price=0.7, custom_charge_price=0.3)
        assert r.avoided_electricity_price == pytest.approx(0.7)
        assert r.charge_price == pytest.approx(0.3)

    def test_override_wins(self):
        """§85：用户明确修改的值优先。"""
        r = resolve_tariff("FIXED", average_price=0.65, avoided_price_override=0.9)
        assert r.avoided_electricity_price == pytest.approx(0.9)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValidationError) as exc:
            resolve_tariff("WHATEVER")
        assert exc.value.field == "tariff_mode"


class TestRevenue:
    def test_self_use_revenue(self):
        """§27：SelfUseRevenue = PVSelfUse × AvoidedElectricityPrice。"""
        assert self_use_revenue(880_000.0, 0.70) == pytest.approx(616_000.0)

    def test_export_revenue(self):
        """§28：ExportRevenue = PVExport × ExportPrice。"""
        assert export_revenue(220_000.0, 0.35) == pytest.approx(77_000.0)

    def test_storage_arbitrage(self):
        """§43：Edis × 放电替代价 - Echg × 充电价。"""
        value = storage_arbitrage_revenue(278_610.696142, 0.70, 316_603.063798, 0.40)
        assert value == pytest.approx(68_386.26178, rel=1e-9)

    def test_storage_total_is_sum_of_four_parts(self):
        """§44：四类收益必须分开记录。"""
        assert storage_total_revenue(68_386.26, 10_000.0, 5_000.0, 1_000.0) == pytest.approx(84_386.26)
