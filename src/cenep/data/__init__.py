"""CENEP V2 数据层：导入 / 校验 / 质量评分（V2 §51–§56）。

对外公开三个模块的能力：

* :mod:`cenep.data.importer`  —— Excel / CSV 导入（三种模板，§51）
* :mod:`cenep.data.validator` —— 时间轴与数值校验、缺失处理（§52–§54）
* :mod:`cenep.data.quality`   —— ``DataQualityScore`` 评分（§55、§56）

V2.1 增量接入（§5.3、§5.5）：新增账单模板 / 列映射 / 导入预览与账单质量评分，
既有函数签名与行为完全不变。

典型用法::

    from cenep.data import import_load_file, build_time_axis, validate_series, score_quality

    profile = import_load_file("负荷.xlsx")
    axis = build_time_axis(2025)
    issues = validate_series(profile.points, axis, kind="load")
    quality = score_quality(profile.points, axis, issues=issues,
                            source_type=profile.source_type)

账单导入（V2.1）::

    from cenep.data import build_bill_template, preview_bill_import, apply_bill_import

    build_bill_template("账单模板.xlsx")
    preview = preview_bill_import("账单.xlsx", project_id="某项目")
    result = apply_bill_import(preview, strategy=DuplicateStrategy.SKIP)

高频负荷导入（V2.2 阶段 3）::

    from cenep.data import preview_load_import, apply_load_import

    preview = preview_load_import("15分钟负荷.xlsx", value_kind=None)  # 口径按表头判定
    dataset = apply_load_import(preview, project_id="某项目")
    print(dataset.provenance_text, dataset.quality_summary_text())
"""

from __future__ import annotations

from ..calculation.timeseries_engine import TimeAxis, build_time_axis, points_per_year
from .bill_importer import (
    DEFAULT_SHEET,
    SHEET_BILLS,
    SHEET_DICT,
    SHEET_HELP,
    SHEET_TARIFF,
    TEMPLATE_FILE_NAME,
    BillImportPreview,
    BillImportResult,
    BillImportRow,
    apply_bill_import,
    bill_template_bytes,
    build_bill_template,
    preview_bill_import,
    resolve_bill_columns,
)
from .importer import (
    COLUMN_ALIASES,
    detect_unit_issues,
    import_load_file,
    import_pv_file,
    import_tariff_file,
    list_sheets,
    normalize_header,
    parse_timestamp,
    read_table,
    read_table_from_sheet,
)
from .load_profile_importer import (
    COVERAGE_WARNING_RATIO,
    LOAD_COLUMNS,
    LOAD_COLUMN_ALIASES,
    AlignmentResult,
    IntervalDetection,
    LoadColumn,
    LoadImportPreview,
    LoadImportRow,
    TimeBlock,
    align_timestamps,
    apply_load_import,
    build_points_from_block,
    check_load_quality,
    detect_interval,
    detect_value_kind_from_header,
    expected_year_from_file_name,
    load_block_catalog,
    preview_block_import,
    preview_load_import,
    read_time_blocks,
    resolve_load_columns,
    resolve_value_kind,
    scan_timezone_issues,
)
from .quality import (
    SOURCE_CREDIBILITY,
    WEIGHT_COMPLETENESS,
    WEIGHT_CONTINUITY,
    WEIGHT_OUTLIER,
    WEIGHT_SOURCE,
    level_of,
    score_bill_quality,
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
    "list_sheets",
    "normalize_header",
    "parse_timestamp",
    "read_table",
    "read_table_from_sheet",
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
    "score_bill_quality",
    "score_quality",
    "source_credibility_of",
    # 时间轴（便于调用方一次导入）
    "TimeAxis",
    "build_time_axis",
    "points_per_year",
    # V2.1 账单模板 / 导入预览（§5.3、§5.5）
    "DEFAULT_SHEET",
    "SHEET_BILLS",
    "SHEET_DICT",
    "SHEET_HELP",
    "SHEET_TARIFF",
    "TEMPLATE_FILE_NAME",
    "BillImportPreview",
    "BillImportResult",
    "BillImportRow",
    "apply_bill_import",
    "bill_template_bytes",
    "build_bill_template",
    "preview_bill_import",
    "resolve_bill_columns",
    # V2.2 阶段 3 高频负荷导入 / 间隔识别 / 质量检查
    "COVERAGE_WARNING_RATIO",
    "LOAD_COLUMNS",
    "LOAD_COLUMN_ALIASES",
    "AlignmentResult",
    "IntervalDetection",
    "LoadColumn",
    "LoadImportPreview",
    "LoadImportRow",
    "TimeBlock",
    "align_timestamps",
    "apply_load_import",
    "build_points_from_block",
    "check_load_quality",
    "detect_interval",
    "detect_value_kind_from_header",
    "expected_year_from_file_name",
    "load_block_catalog",
    "preview_block_import",
    "preview_load_import",
    "read_time_blocks",
    "resolve_load_columns",
    "resolve_value_kind",
    "scan_timezone_issues",
]
