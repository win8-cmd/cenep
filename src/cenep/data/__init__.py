"""CENEP V2 数据层：导入 / 校验 / 质量评分（V2 §51–§56）。

对外公开三个模块的能力：

* :mod:`cenep.data.importer`  —— Excel / CSV 导入（三种模板，§51）
* :mod:`cenep.data.validator` —— 时间轴与数值校验、缺失处理（§52–§54）
* :mod:`cenep.data.quality`   —— ``DataQualityScore`` 评分（§55、§56）

典型用法::

    from cenep.data import import_load_file, build_time_axis, validate_series, score_quality

    profile = import_load_file("负荷.xlsx")
    axis = build_time_axis(2025)
    issues = validate_series(profile.points, axis, kind="load")
    quality = score_quality(profile.points, axis, issues=issues,
                            source_type=profile.source_type)
"""

from __future__ import annotations

from ..calculation.timeseries_engine import TimeAxis, build_time_axis, points_per_year
from .importer import (
    COLUMN_ALIASES,
    detect_unit_issues,
    import_load_file,
    import_pv_file,
    import_tariff_file,
    normalize_header,
    parse_timestamp,
    read_table,
)
from .quality import (
    SOURCE_CREDIBILITY,
    WEIGHT_COMPLETENESS,
    WEIGHT_CONTINUITY,
    WEIGHT_OUTLIER,
    WEIGHT_SOURCE,
    level_of,
    score_quality,
    source_credibility_of,
)
from .validator import (
    check_timeline_consistency,
    detect_duplicates,
    detect_missing,
    detect_outliers,
    fill_missing,
    validate_series,
)

__all__ = [
    # 导入（§51）
    "COLUMN_ALIASES",
    "detect_unit_issues",
    "import_load_file",
    "import_pv_file",
    "import_tariff_file",
    "normalize_header",
    "parse_timestamp",
    "read_table",
    # 校验（§52–§54）
    "check_timeline_consistency",
    "detect_duplicates",
    "detect_missing",
    "detect_outliers",
    "fill_missing",
    "validate_series",
    # 质量评分（§55、§56）
    "SOURCE_CREDIBILITY",
    "WEIGHT_COMPLETENESS",
    "WEIGHT_CONTINUITY",
    "WEIGHT_OUTLIER",
    "WEIGHT_SOURCE",
    "level_of",
    "score_quality",
    "source_credibility_of",
    # 时间轴（便于调用方一次导入）
    "TimeAxis",
    "build_time_axis",
    "points_per_year",
]
