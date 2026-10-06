"""V2 数据层测试（V2 §51–§56；规则编号见 ``DATA_IMPORT_SPEC.md``）。

覆盖三条主线：

1. **导入**（§51）——三种模板 × CSV/XLSX、表头与时间格式容错、缺列/空文件报中文错；
2. **校验**（§52–§54）——重复、缺失、间隔、负值、单位、异常值，以及四种缺失处理策略；
3. **评分**（§55、§56）——四维度权重、来源可信度分值表、等级阈值。

全部测试数据都在 ``tmp_path`` 内生成，**不依赖任何外部文件**。
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from openpyxl import Workbook

from cenep.calculation.errors import ValidationError
from cenep.data import (
    SOURCE_CREDIBILITY,
    build_time_axis,
    check_timeline_consistency,
    detect_duplicates,
    detect_missing,
    detect_outliers,
    detect_unit_issues,
    fill_missing,
    import_load_file,
    import_pv_file,
    import_tariff_file,
    normalize_header,
    parse_timestamp,
    read_table,
    score_quality,
    source_credibility_of,
    validate_series,
)
from cenep.domain.enums import MissingDataPolicy, SourceType
from cenep.domain.timeseries import TimeSeriesPoint

YEAR = 2025
ISO = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def write_csv(path: Path, headers: list[str], rows: list[list]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def write_xlsx(path: Path, headers: list[str], rows: list[list]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return path


def hourly_stamps(count: int, start: datetime | None = None) -> list[datetime]:
    start = start or datetime(YEAR, 1, 1)
    return [start + timedelta(hours=i) for i in range(count)]


def load_points(count: int = 8760) -> list[TimeSeriesPoint]:
    return [
        TimeSeriesPoint(timestamp=ts, load_kwh=100.0 + (i % 24))
        for i, ts in enumerate(hourly_stamps(count))
    ]


def pv_points(count: int = 8760, capacity_kwp: float = 1000.0) -> list[TimeSeriesPoint]:
    """真实形状的光伏曲线：日发电等效 3.0 小时 → 年约 1095 h（落在 800–1600 h 区间）。"""
    raw = [0.0] * 24
    for h in range(6, 19):
        raw[h] = math.sin(math.pi * (h - 6) / 12.0)
    scale = 3.0 / sum(raw) * capacity_kwp
    return [
        TimeSeriesPoint(timestamp=ts, pv_generation_kwh=raw[ts.hour] * scale)
        for ts in hourly_stamps(count)
    ]


def price_points(count: int = 8760) -> list[TimeSeriesPoint]:
    def price_of(hour: int) -> float:
        return 0.35 if hour < 8 else (0.65 if hour < 18 else 1.0)

    return [
        TimeSeriesPoint(timestamp=ts, electricity_price=price_of(ts.hour), export_price=0.35)
        for ts in hourly_stamps(count)
    ]


# --------------------------------------------------------------------------- #
# V2 §51 导入
# --------------------------------------------------------------------------- #
class TestImportTemplates:
    def test_import_load_csv(self, tmp_path):
        path = write_csv(
            tmp_path / "load.csv",
            ["timestamp", "load_kwh"],
            [[ts.strftime(ISO), 100.0 + i] for i, ts in enumerate(hourly_stamps(48))],
        )
        profile = import_load_file(path)
        assert profile.point_count == 48
        assert profile.points[0].load_kwh == 100.0
        assert profile.annual_energy == pytest.approx(sum(100.0 + i for i in range(48)))
        assert profile.source == "load.csv"
        assert profile.source_type is SourceType.USER_INPUT

    def test_import_load_xlsx(self, tmp_path):
        path = write_xlsx(
            tmp_path / "load.xlsx",
            ["timestamp", "load_kwh"],
            [[ts.strftime(ISO), 200.0] for ts in hourly_stamps(24)],
        )
        profile = import_load_file(path)
        assert profile.point_count == 24
        assert profile.annual_energy == pytest.approx(200.0 * 24)

    def test_import_pv_csv(self, tmp_path):
        path = write_csv(
            tmp_path / "pv.csv",
            ["timestamp", "pv_kwh"],
            [[ts.strftime(ISO), 50.0] for ts in hourly_stamps(24)],
        )
        profile = import_pv_file(path, capacity_kwp=100.0)
        assert profile.point_count == 24
        assert profile.capacity_kwp == 100.0
        assert profile.annual_generation == pytest.approx(1200.0)
        # 模板列 pv_kwh 必须落到模型字段 pv_generation_kwh
        assert profile.points[0].pv_generation_kwh == 50.0

    def test_import_pv_xlsx(self, tmp_path):
        path = write_xlsx(
            tmp_path / "pv.xlsx",
            ["timestamp", "pv_kwh"],
            [[ts.strftime(ISO), 10.0] for ts in hourly_stamps(24)],
        )
        assert import_pv_file(path).point_count == 24

    def test_import_tariff_csv(self, tmp_path):
        path = write_csv(
            tmp_path / "price.csv",
            ["timestamp", "price", "export_price"],
            [[ts.strftime(ISO), 0.35 if ts.hour < 8 else 1.0, 0.4] for ts in hourly_stamps(24)],
        )
        points = import_tariff_file(path)
        assert len(points) == 24
        assert points[0].electricity_price == pytest.approx(0.35)
        assert points[12].electricity_price == pytest.approx(1.0)
        assert points[0].export_price == pytest.approx(0.4)

    def test_import_tariff_xlsx(self, tmp_path):
        path = write_xlsx(
            tmp_path / "price.xlsx",
            ["timestamp", "price", "export_price"],
            [[ts.strftime(ISO), 0.6, 0.4] for ts in hourly_stamps(12)],
        )
        assert len(import_tariff_file(path)) == 12

    def test_export_price_forward_filled(self, tmp_path):
        """§6.1：``export_price`` 为空属合法简写，沿用上一非空值，不算缺失。"""
        rows = [[hourly_stamps(24)[h].strftime(ISO), 0.6, 0.4 if h == 0 else ""] for h in range(24)]
        path = write_csv(tmp_path / "price.csv", ["timestamp", "price", "export_price"], rows)
        points = import_tariff_file(path)
        assert all(p.export_price == pytest.approx(0.4) for p in points)

    def test_export_price_column_optional(self, tmp_path):
        path = write_csv(
            tmp_path / "price.csv",
            ["timestamp", "price"],
            [[ts.strftime(ISO), 0.6] for ts in hourly_stamps(6)],
        )
        assert len(import_tariff_file(path)) == 6


class TestHeaderTolerance:
    @pytest.mark.parametrize(
        "header",
        ["  Timestamp ", "TIMESTAMP", "timestamp", "时间", "时间戳", "datetime", "Date"],
    )
    def test_timestamp_aliases(self, tmp_path, header):
        path = write_csv(
            tmp_path / "a.csv",
            [header, "load_kwh"],
            [[ts.strftime(ISO), 1.0] for ts in hourly_stamps(3)],
        )
        assert import_load_file(path).point_count == 3

    @pytest.mark.parametrize(
        "header",
        ["load_kwh", "Load_kWh", "负荷", "用电量", "load", "负荷（kWh）", "用电量(kWh)"],
    )
    def test_load_aliases(self, tmp_path, header):
        path = write_csv(
            tmp_path / "a.csv",
            ["timestamp", header],
            [[ts.strftime(ISO), 5.0] for ts in hourly_stamps(3)],
        )
        assert import_load_file(path).points[0].load_kwh == 5.0

    @pytest.mark.parametrize("header", ["pv_kwh", "光伏", "发电量", "pv", "光伏发电量"])
    def test_pv_aliases(self, tmp_path, header):
        path = write_csv(
            tmp_path / "a.csv",
            ["时间", header],
            [[ts.strftime(ISO), 7.0] for ts in hourly_stamps(3)],
        )
        assert import_pv_file(path).points[0].pv_generation_kwh == 7.0

    def test_price_aliases(self, tmp_path):
        path = write_csv(
            tmp_path / "a.csv",
            ["时间", "电价", "上网电价"],
            [[ts.strftime(ISO), 0.6, 0.3] for ts in hourly_stamps(3)],
        )
        points = import_tariff_file(path)
        assert points[0].electricity_price == pytest.approx(0.6)
        assert points[0].export_price == pytest.approx(0.3)

    def test_normalize_header(self):
        assert normalize_header("  Load_kWh ") == "loadkwh"
        assert normalize_header("负荷（kWh）") == "负荷kwh"
        assert normalize_header("电价(元/kWh)") == "电价元kwh"


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2025-01-01 00:00:00", datetime(2025, 1, 1, 0, 0)),
            ("2025-01-01 05:30", datetime(2025, 1, 1, 5, 30)),
            ("2025/1/1 0:00", datetime(2025, 1, 1, 0, 0)),
            ("2025/01/01 13:00", datetime(2025, 1, 1, 13, 0)),
            ("2025-01-01T08:00:00", datetime(2025, 1, 1, 8, 0)),
            ("2025.01.01 09:00", datetime(2025, 1, 1, 9, 0)),
            ("2025-01-01", datetime(2025, 1, 1, 0, 0)),
            (datetime(2025, 3, 1, 5), datetime(2025, 3, 1, 5)),
        ],
    )
    def test_formats(self, raw, expected):
        assert parse_timestamp(raw) == expected

    def test_excel_serial(self):
        assert parse_timestamp(45658) == datetime(2025, 1, 1)

    @pytest.mark.parametrize("raw", ["垃圾数据", "", None, "13:00"])
    def test_unparsable(self, raw):
        assert parse_timestamp(raw) is None

    def test_mixed_formats_in_one_file(self, tmp_path):
        path = write_csv(
            tmp_path / "a.csv",
            ["timestamp", "load_kwh"],
            [
                ["2025-01-01 00:00:00", 1.0],
                ["2025/1/1 1:00", 2.0],
                [45658.125, 3.0],  # Excel 序列号 = 2025-01-01 03:00
                ["2025-01-01T04:00:00", 4.0],
            ],
        )
        profile = import_load_file(path)
        assert profile.point_count == 4
        assert [p.load_kwh for p in profile.points] == [1.0, 2.0, 3.0, 4.0]
        assert [p.timestamp.hour for p in profile.points] == [0, 1, 3, 4]


class TestImportErrors:
    def test_missing_load_column(self, tmp_path):
        path = write_csv(tmp_path / "a.csv", ["timestamp", "foo"], [["2025-01-01 00:00:00", 1.0]])
        with pytest.raises(ValidationError, match="缺少负荷列"):
            import_load_file(path)

    def test_missing_timestamp_column(self, tmp_path):
        path = write_csv(tmp_path / "a.csv", ["load_kwh"], [[1.0]])
        with pytest.raises(ValidationError, match="缺少时间列"):
            import_load_file(path)

    def test_empty_file(self, tmp_path):
        path = write_csv(tmp_path / "a.csv", ["timestamp", "load_kwh"], [])
        with pytest.raises(ValidationError, match="没有数据行"):
            read_table(path)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(ValidationError, match="文件不存在"):
            read_table(tmp_path / "nope.csv")

    def test_unsupported_suffix(self, tmp_path):
        path = tmp_path / "a.json"
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValidationError, match="不支持的文件类型"):
            read_table(path)

    def test_all_timestamps_unparsable(self, tmp_path):
        path = write_csv(
            tmp_path / "a.csv", ["timestamp", "load_kwh"], [["abc", 1.0], ["def", 2.0]]
        )
        with pytest.raises(ValidationError, match="时间列无法解析"):
            import_load_file(path)

    def test_non_numeric_becomes_nan(self, tmp_path):
        """§5.2 V11：非数值文本按缺失处理，不得静默变成 0。"""
        path = write_csv(
            tmp_path / "a.csv",
            ["timestamp", "load_kwh"],
            [["2025-01-01 00:00:00", "abc"], ["2025-01-01 01:00:00", 5.0]],
        )
        profile = import_load_file(path)
        assert math.isnan(profile.points[0].load_kwh)
        assert profile.points[1].load_kwh == 5.0


# --------------------------------------------------------------------------- #
# V2 §52–§54 校验
# --------------------------------------------------------------------------- #
class TestTimelineValidation:
    def test_duplicate_timestamps(self):
        stamp = datetime(YEAR, 3, 15, 8)
        points = [
            TimeSeriesPoint(timestamp=stamp, load_kwh=1.0),
            TimeSeriesPoint(timestamp=stamp, load_kwh=2.0),
            TimeSeriesPoint(timestamp=stamp + timedelta(hours=1), load_kwh=3.0),
        ]
        issues = detect_duplicates(points)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.level == "ERROR"
        assert issue.category == "continuity"
        assert issue.count == 1
        assert issue.samples and "2025-03-15 08:00" in issue.samples[0]
        assert "[V02]" in issue.message

    def test_no_duplicates(self):
        assert detect_duplicates(load_points(100)) == []

    def test_missing_one_hour(self):
        axis = build_time_axis(YEAR)
        points = [p for i, p in enumerate(load_points(8760)) if i != 100]
        indices, issues = detect_missing(points, axis)
        assert indices == [100]
        assert len(issues) == 1
        assert issues[0].level == "INFO"
        assert issues[0].category == "completeness"
        assert issues[0].count == 1
        assert "[V03]" in issues[0].message
        assert "1 个时间点缺失" in issues[0].message

    def test_missing_24_hours(self):
        axis = build_time_axis(YEAR)
        points = [p for i, p in enumerate(load_points(8760)) if not (100 <= i < 124)]
        indices, issues = detect_missing(points, axis)
        assert len(indices) == 24
        assert issues[0].count == 24
        assert len(issues[0].samples) == 5  # 最多 5 条示例

    def test_interval_error(self):
        axis = build_time_axis(YEAR)
        stamps = hourly_stamps(10)
        points = [
            TimeSeriesPoint(timestamp=ts, load_kwh=1.0) for ts in stamps[:5]
        ] + [TimeSeriesPoint(timestamp=stamps[5] + timedelta(hours=1), load_kwh=1.0)]
        issues = validate_series(points, axis, kind="load")
        interval = [i for i in issues if "[V04]" in i.message]
        assert len(interval) == 1
        assert interval[0].level == "WARNING"
        assert interval[0].category == "continuity"

    def test_unsorted(self):
        axis = build_time_axis(YEAR)
        points = list(reversed(load_points(5)))
        issues = validate_series(points, axis, kind="load")
        assert any("[V05]" in i.message and i.level == "INFO" for i in issues)

    def test_cross_year(self):
        axis = build_time_axis(YEAR)
        points = [
            TimeSeriesPoint(timestamp=datetime(2024, 12, 31, 23), load_kwh=1.0),
            TimeSeriesPoint(timestamp=datetime(2025, 1, 1, 0), load_kwh=1.0),
        ]
        issues = validate_series(points, axis, kind="load")
        assert any("[V06]" in i.message and i.level == "WARNING" for i in issues)

    def test_leap_year_point_count(self):
        """§5.1 V07：闰年 2024 应为 8784 点。"""
        axis = build_time_axis(2024)
        points = [
            TimeSeriesPoint(timestamp=datetime(2024, 1, 1) + timedelta(hours=i), load_kwh=1.0)
            for i in range(8760)
        ]
        issues = validate_series(points, axis, kind="load")
        assert any("[V07]" in i.message for i in issues)

    def test_timeline_consistency(self):
        issues = check_timeline_consistency(load_points(10), pv_points(8))
        assert len(issues) == 1
        assert issues[0].level == "ERROR"
        assert "[V15]" in issues[0].message

    def test_timeline_consistent(self):
        assert check_timeline_consistency(load_points(10), pv_points(10)) == []


class TestValueValidation:
    def test_negative_load_is_error(self):
        axis = build_time_axis(YEAR)
        points = load_points(8760)
        points[1023] = TimeSeriesPoint(timestamp=points[1023].timestamp, load_kwh=-50.0)
        issues = validate_series(points, axis, kind="load")
        negative = [i for i in issues if "[V08]" in i.message]
        assert len(negative) == 1
        assert negative[0].level == "ERROR"
        assert negative[0].category == "outlier"
        assert negative[0].count == 1
        assert "第 1024 个时间点" in negative[0].message
        assert negative[0].samples

    def test_nan_reported_as_v11(self):
        axis = build_time_axis(YEAR)
        points = load_points(100)
        points[7] = TimeSeriesPoint(timestamp=points[7].timestamp, load_kwh=float("nan"))
        issues = validate_series(points, axis, kind="load")
        assert any("[V11]" in i.message and i.level == "INFO" for i in issues)

    def test_all_zero_column(self):
        axis = build_time_axis(YEAR)
        points = [TimeSeriesPoint(timestamp=ts, load_kwh=0.0) for ts in hourly_stamps(8760)]
        issues = validate_series(points, axis, kind="load")
        assert any("[V10]" in i.message and i.level == "WARNING" for i in issues)

    def test_flat_price(self):
        axis = build_time_axis(YEAR)
        points = [
            TimeSeriesPoint(timestamp=ts, electricity_price=0.65) for ts in hourly_stamps(8760)
        ]
        issues = validate_series(points, axis, kind="price")
        assert any("[V13]" in i.message for i in issues)

    def test_annual_deviation(self):
        axis = build_time_axis(YEAR)
        issues = validate_series(
            load_points(8760), axis, kind="load", annual_reference_kwh=2_000_000.0
        )
        assert any("[V14]" in i.message and i.level == "WARNING" for i in issues)

    def test_annual_deviation_within_tolerance(self):
        axis = build_time_axis(YEAR)
        actual = sum(p.load_kwh for p in load_points(8760))
        issues = validate_series(
            load_points(8760), axis, kind="load", annual_reference_kwh=actual * 1.04
        )
        assert not any("[V14]" in i.message for i in issues)


class TestOutlierDetection:
    def test_pv_night_generation(self):
        """§7 R-PV-NIGHT：夜间时段光伏出力 > 0。"""
        axis = build_time_axis(YEAR)
        points = pv_points(8760)
        for index in (22, 25, 100):  # 0/1/4 点附近属夜间时段
            points[index] = TimeSeriesPoint(
                timestamp=points[index].timestamp, pv_generation_kwh=3.2
            )
        issues = detect_outliers(points, axis, "pv", capacity_kwp=1000.0)
        night = [i for i in issues if "R-PV-NIGHT" in i.message]
        assert len(night) == 1
        assert night[0].level == "WARNING"
        assert night[0].category == "outlier"
        assert night[0].count == 3
        assert night[0].samples

    def test_no_false_night_alarm_on_clean_data(self):
        axis = build_time_axis(YEAR)
        issues = detect_outliers(pv_points(8760), axis, "pv", capacity_kwp=1000.0)
        assert not any("R-PV-NIGHT" in i.message for i in issues)

    def test_pv_over_capacity(self):
        axis = build_time_axis(YEAR)
        points = pv_points(8760, capacity_kwp=1000.0)
        points[12] = TimeSeriesPoint(timestamp=points[12].timestamp, pv_generation_kwh=2000.0)
        issues = detect_outliers(points, axis, "pv", capacity_kwp=1000.0)
        assert any("R-PV-OVER" in i.message for i in issues)

    def test_pv_equivalent_hours_out_of_range(self):
        """§7 R-PV-CAP：年等效小时 < 800 或 > 1600。"""
        axis = build_time_axis(YEAR)
        tiny = [
            TimeSeriesPoint(timestamp=p.timestamp, pv_generation_kwh=p.pv_generation_kwh * 0.2)
            for p in pv_points(8760, capacity_kwp=1000.0)
        ]
        issues = detect_outliers(tiny, axis, "pv", capacity_kwp=1000.0)
        caps = [i for i in issues if "R-PV-CAP" in i.message]
        assert len(caps) == 1
        assert "低于常见区间" in caps[0].message

    def test_load_spike(self):
        axis = build_time_axis(YEAR)
        points = load_points(8760)
        points[300] = TimeSeriesPoint(timestamp=points[300].timestamp, load_kwh=5000.0)
        issues = detect_outliers(points, axis, "load")
        assert any("R-LOAD-SPIKE" in i.message for i in issues)

    def test_flat_run(self):
        axis = build_time_axis(YEAR)
        points = [TimeSeriesPoint(timestamp=ts, load_kwh=100.0) for ts in hourly_stamps(8760)]
        issues = detect_outliers(points, axis, "load")
        assert any("R-LOAD-FLAT" in i.message for i in issues)

    def test_price_jump(self):
        """R-PRICE-JUMP：相邻小时电价变化 > 200%。"""
        axis = build_time_axis(YEAR)
        points = [
            TimeSeriesPoint(timestamp=ts, electricity_price=0.35 if ts.hour < 12 else 1.10)
            for ts in hourly_stamps(48)
        ]
        issues = detect_outliers(points, axis, "price")
        assert any("R-PRICE-JUMP" in i.message for i in issues)

    def test_price_zero_run(self):
        axis = build_time_axis(YEAR)
        stamps = hourly_stamps(48)
        points = []
        for i, ts in enumerate(stamps):
            value = 0.65 if i < 20 else 0.0
            points.append(TimeSeriesPoint(timestamp=ts, electricity_price=value))
        issues = detect_outliers(points, axis, "price")
        assert any("R-PRICE-ZERO" in i.message for i in issues)

    def test_clean_pv_has_no_outliers(self):
        axis = build_time_axis(YEAR)
        assert detect_outliers(pv_points(8760), axis, "pv", capacity_kwp=1000.0) == []


class TestUnitHeuristics:
    def test_mwh_like_electricity(self):
        values = np.full(100, 0.85)
        issues = detect_unit_issues(values, "load")
        assert len(issues) == 1
        assert issues[0].category == "unit"
        assert "[V09]" in issues[0].message
        assert "MWh" in issues[0].message

    def test_wh_like_electricity(self):
        issues = detect_unit_issues(np.full(100, 250_000.0), "load")
        assert issues and "Wh" in issues[0].message

    def test_price_in_fen(self):
        issues = detect_unit_issues(np.full(100, 65.0), "price")
        assert issues and "分/kWh" in issues[0].message

    def test_price_too_small(self):
        issues = detect_unit_issues(np.full(100, 0.001), "price")
        assert issues and "偏低" in issues[0].message

    def test_normal_load_no_issue(self):
        assert detect_unit_issues(np.full(100, 150.0), "load") == []

    def test_pv_with_many_zeros_no_false_alarm(self):
        """光伏曲线含大量夜间 0 时，不得误报"单位疑似 MWh/Wh"。"""
        values = np.concatenate([np.zeros(12), np.full(12, 800.0)])
        assert detect_unit_issues(values, "pv") == []

    def test_normal_price_no_issue(self):
        assert detect_unit_issues(np.full(100, 0.65), "price") == []


# --------------------------------------------------------------------------- #
# V2 §53 缺失数据处理四策略
# --------------------------------------------------------------------------- #
class TestMissingDataPolicies:
    def _gap_points(self, axis) -> tuple[list[TimeSeriesPoint], list[int]]:
        full = load_points(8760)
        keep = [i for i in range(8760) if not (100 <= i < 104)]
        return [full[i] for i in keep], [100, 101, 102, 103]

    def test_reject_raises(self):
        axis = build_time_axis(YEAR)
        points, _ = self._gap_points(axis)
        with pytest.raises(ValidationError, match="发现 4 个时间点缺失"):
            fill_missing(points, axis, MissingDataPolicy.REJECT, kind="load")

    def test_reject_passes_when_complete(self):
        axis = build_time_axis(YEAR)
        filled, issues = fill_missing(load_points(8760), axis, MissingDataPolicy.REJECT)
        assert len(filled) == 8760
        assert issues == []

    def test_linear_interpolation(self):
        axis = build_time_axis(YEAR)
        points, gap = self._gap_points(axis)
        filled, issues = fill_missing(
            points, axis, MissingDataPolicy.LINEAR_INTERPOLATION, kind="load"
        )
        assert len(filled) == 8760
        left = points[0].load_kwh if False else None
        del left
        before = next(p.load_kwh for p in points if p.timestamp == axis.timestamps[99])
        after = next(p.load_kwh for p in points if p.timestamp == axis.timestamps[104])
        step = (after - before) / 5.0
        for offset, index in enumerate(gap, start=1):
            assert filled[index].load_kwh == pytest.approx(before + step * offset, rel=1e-9)
        assert any("已按线性插值填充" in i.message for i in issues)

    def test_linear_interpolation_warns_on_long_gap(self):
        axis = build_time_axis(YEAR)
        points = [p for i, p in enumerate(load_points(8760)) if not (100 <= i < 110)]
        _, issues = fill_missing(
            points, axis, MissingDataPolicy.LINEAR_INTERPOLATION, kind="load"
        )
        assert any("超过线性插值推荐的 3 点" in i.message for i in issues)

    def test_forward_fill(self):
        axis = build_time_axis(YEAR)
        points, gap = self._gap_points(axis)
        filled, issues = fill_missing(points, axis, MissingDataPolicy.FORWARD_FILL, kind="load")
        last_known = next(p.load_kwh for p in points if p.timestamp == axis.timestamps[99])
        for index in gap:
            assert filled[index].load_kwh == pytest.approx(last_known)
        assert any("已按前值填充" in i.message for i in issues)

    def test_typical_day_fill_uses_same_month_weekend_hour_mean(self):
        axis = build_time_axis(YEAR)
        points, gap = self._gap_points(axis)
        filled, issues = fill_missing(
            points, axis, MissingDataPolicy.TYPICAL_DAY_FILL, kind="load"
        )
        target = axis.timestamps[gap[0]]
        same = [
            p.load_kwh
            for p in points
            if p.timestamp.month == target.month
            and (p.timestamp.weekday() >= 5) == (target.weekday() >= 5)
            and p.timestamp.hour == target.hour
        ]
        assert same, "测试数据应存在同月同类型日同小时的样本"
        assert filled[gap[0]].load_kwh == pytest.approx(float(np.mean(same)), rel=1e-9)
        assert any("合成数据" in i.message for i in issues)

    def test_all_policies_return_full_length(self):
        axis = build_time_axis(YEAR)
        points, _ = self._gap_points(axis)
        for policy in (
            MissingDataPolicy.LINEAR_INTERPOLATION,
            MissingDataPolicy.FORWARD_FILL,
            MissingDataPolicy.TYPICAL_DAY_FILL,
        ):
            filled, _ = fill_missing(points, axis, policy, kind="load")
            assert len(filled) == 8760


# --------------------------------------------------------------------------- #
# V2 §55、§56 质量评分
# --------------------------------------------------------------------------- #
class TestQualityScore:
    def test_perfect_data_with_top_source_scores_100(self):
        axis = build_time_axis(YEAR)
        score = score_quality(
            pv_points(8760), axis, kind="pv", capacity_kwp=1000.0,
            source_type=SourceType.CONTRACT,
        )
        assert score.score == pytest.approx(100.0)
        assert score.completeness == pytest.approx(40.0)
        assert score.continuity == pytest.approx(25.0)
        assert score.outlier == pytest.approx(20.0)
        assert score.source_credibility == pytest.approx(15.0)
        assert score.level_label == "优秀"
        assert score.issues == []

    def test_source_credibility_table(self):
        """§8.2 分值表逐项核对。"""
        expected = {
            SourceType.CONTRACT: 15.0,
            SourceType.POLICY: 15.0,
            SourceType.HISTORICAL: 13.0,
            SourceType.USER_INPUT: 11.0,
            SourceType.CALCULATED: 10.0,
            SourceType.ASSUMPTION: 7.0,
            SourceType.EXPERIENCE: 5.0,
            SourceType.SYSTEM_DEFAULT: 0.0,
        }
        assert SOURCE_CREDIBILITY == expected
        for source, value in expected.items():
            assert source_credibility_of(source) == value

    @pytest.mark.parametrize(
        "source,expected",
        [
            (SourceType.CONTRACT, 100.0),
            (SourceType.HISTORICAL, 98.0),
            (SourceType.USER_INPUT, 96.0),
            (SourceType.ASSUMPTION, 92.0),
            (SourceType.EXPERIENCE, 90.0),
            (SourceType.SYSTEM_DEFAULT, 85.0),
        ],
    )
    def test_source_type_changes_total(self, source, expected):
        axis = build_time_axis(YEAR)
        score = score_quality(
            pv_points(8760), axis, kind="pv", capacity_kwp=1000.0, source_type=source
        )
        assert score.score == pytest.approx(expected)

    def test_missing_one_percent(self):
        """§8.1①：完整性 = 40 × 有效/应有，按原始缺失率计分。"""
        axis = build_time_axis(YEAR)
        drop = 88  # 约 1%
        points = [p for i, p in enumerate(load_points(8760)) if i >= drop]
        score = score_quality(points, axis, kind="load", source_type=SourceType.CONTRACT)
        assert score.completeness == pytest.approx(40.0 * 8672 / 8760, abs=1e-3)
        assert score.score < 100.0
        assert score.score > 99.0

    def test_negative_value_lowers_outlier_score(self):
        """§8.1③：异常值 = 20 × max(0, 1 − 异常率/5%)。"""
        axis = build_time_axis(YEAR)
        points = load_points(8760)
        bad = 438  # 5% 的应有点数
        for i in range(bad):
            points[i] = TimeSeriesPoint(timestamp=points[i].timestamp, load_kwh=-1.0)
        score = score_quality(points, axis, kind="load", source_type=SourceType.CONTRACT)
        assert score.outlier == pytest.approx(0.0, abs=1e-6)

    def test_small_outlier_rate_partial_deduction(self):
        axis = build_time_axis(YEAR)
        points = load_points(8760)
        points[0] = TimeSeriesPoint(timestamp=points[0].timestamp, load_kwh=-1.0)
        score = score_quality(points, axis, kind="load", source_type=SourceType.CONTRACT)
        # 1 点 / 8760 / 0.05 = 0.002283 → 扣 20×0.002283（分数四舍五入到 4 位小数）
        expected = 20.0 * (1.0 - (1.0 / 8760.0) / 0.05)
        assert score.outlier == pytest.approx(expected, abs=1e-3)

    def test_issues_none_triggers_internal_validation(self):
        axis = build_time_axis(YEAR)
        points = load_points(100)
        score = score_quality(points, axis, kind="load")
        assert any("[V03]" in i.message for i in score.issues)

    def test_passed_issues_are_reused(self):
        axis = build_time_axis(YEAR)
        points = load_points(100)
        issues = validate_series(points, axis, kind="load")
        score = score_quality(points, axis, kind="load", issues=issues)
        assert score.issues == issues

    def test_level_thresholds(self):
        axis = build_time_axis(YEAR)
        points = load_points(100)
        score = score_quality(points, axis, kind="load", source_type=SourceType.SYSTEM_DEFAULT)
        assert score.level_label in {"优秀", "良好", "一般", "较差"}

    def test_sub_scores_within_documented_ranges(self):
        """§8.4：四个分项必须落在各自量程内。"""
        axis = build_time_axis(YEAR)
        score = score_quality(load_points(8760), axis, kind="load")
        assert 0.0 <= score.completeness <= 40.0
        assert 0.0 <= score.continuity <= 25.0
        assert 0.0 <= score.outlier <= 20.0
        assert 0.0 <= score.source_credibility <= 15.0
        assert 0.0 <= score.score <= 100.0


# --------------------------------------------------------------------------- #
# 端到端：8760 真实时间轴完整链路
# --------------------------------------------------------------------------- #
class TestEndToEnd:
    def test_full_pipeline_csv(self, tmp_path):
        """导入 → 校验 → 评分 的完整链路（§51 → §52 → §55）。"""
        rows = []
        raw = [0.0] * 24
        for h in range(6, 19):
            raw[h] = math.sin(math.pi * (h - 6) / 12.0)
        scale = 3.0 / sum(raw) * 1000.0
        for i, ts in enumerate(hourly_stamps(8760)):
            rows.append([ts.strftime(ISO), float(100 + i % 24), raw[ts.hour] * scale])
        path = write_csv(tmp_path / "both.csv", ["时间", "负荷", "光伏发电量"], rows)

        table = read_table(path)
        assert len(table) == 8760

        axis = build_time_axis(YEAR)
        load_path = write_csv(
            tmp_path / "load.csv",
            ["timestamp", "load_kwh"],
            [[r[0], r[1]] for r in rows],
        )
        load_profile = import_load_file(load_path)
        assert load_profile.point_count == 8760

        issues = validate_series(load_profile.points, axis, kind="load")
        assert not [i for i in issues if i.level == "ERROR"]

        score = score_quality(
            load_profile.points,
            axis,
            issues=issues,
            source_type=SourceType.HISTORICAL,
        )
        assert score.score >= 95.0
        assert score.level_label == "优秀"

    def test_xlsx_round_trip_8760(self, tmp_path):
        # 负荷必须有真实波动：常量整列会被 R-LOAD-FLAT 正确标记为疑似模拟值
        rows = [[ts.strftime(ISO), 120.0 + (i % 24)] for i, ts in enumerate(hourly_stamps(8760))]
        path = write_xlsx(tmp_path / "load.xlsx", ["timestamp", "load_kwh"], rows)
        profile = import_load_file(path)
        assert profile.point_count == 8760
        assert profile.annual_energy == pytest.approx(sum(r[1] for r in rows))
        axis = build_time_axis(YEAR)
        assert validate_series(profile.points, axis, kind="load") == []

    def test_deterministic_import(self, tmp_path):
        """§5.4：同一文件重复导入，告警的条数、顺序、文案必须一致。"""
        rows = [[ts.strftime(ISO), float(i)] for i, ts in enumerate(hourly_stamps(8760))]
        rows[100][1] = "-5.0"
        path = write_csv(tmp_path / "load.csv", ["timestamp", "load_kwh"], rows)
        axis = build_time_axis(YEAR)
        first = validate_series(import_load_file(path).points, axis, kind="load")
        second = validate_series(import_load_file(path).points, axis, kind="load")
        assert [i.message for i in first] == [i.message for i in second]
