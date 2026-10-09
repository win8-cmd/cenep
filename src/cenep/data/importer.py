"""V2 时序数据导入（V2 §51、§52、§4 单位约定）。

支持 Excel（``.xlsx``，用 ``openpyxl``）与 CSV（标准库 ``csv``，``utf-8-sig`` 优先、``gbk`` 兜底），
三种模板的列名与单位严格按 ``DATA_IMPORT_SPEC.md`` 第 3 节：

===========  ==================================  ==========
模板          必填列                              单位
===========  ==================================  ==========
负荷          ``timestamp,load_kwh``              kWh
光伏          ``timestamp,pv_kwh``                kWh
电价          ``timestamp,price,export_price``    元/kWh
===========  ==================================  ==========

设计要点
--------
* **表头容错**：大小写、首尾空格、全角括号、下划线与空格差异一律忽略；
  并接受中英文别名（``时间``/``负荷``/``用电量``/``电价``/``上网电价``…）。
* **时间格式容错**：``2025-01-01 00:00:00``、``2025/1/1 0:00``、ISO ``T`` 分隔、
  Excel 日期序列号、``datetime``/``date`` 对象均可解析。
* **不做任何单位换算**（§4）：单位不符只报错或告警，绝不静默换算。
  单位合理性由 :func:`detect_unit_issues` 按 §4.3 的启发式给出提示，
  导入函数会把它写进日志；如需把提示并入统一的质量报告，
  在 :func:`cenep.data.validator.validate_series` 里会再次检出（V09）。
* **缺列 / 空文件 / 全空列** → 中文错误（``ValidationError``）。
* **V2.1 新增（增量，不改既有行为）**：:func:`list_sheets` 列出工作表名、
  :func:`read_table_from_sheet` 按指定工作表读表（V2.1 §5.3 的"选择工作表"步骤），
  并把损坏的 ``.xlsx`` 统一翻译成中文 ``ValidationError``（V2.1 §9.1、§0.2）。
  :func:`read_table` 的行为与 V2 完全一致（仍读第一张工作表）。
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..calculation.errors import ValidationError
from ..domain.enums import Resolution, SourceType
from ..domain.timeseries import LoadProfile, PVProfile, TimeSeriesPoint
from ..domain.timeseries_results import DataQualityIssue

logger = logging.getLogger(__name__)

__all__ = [
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
]

#: Excel 日期序列号的基准（1900 日期系统的第 1 天是 1900-01-01，
#: 但因历史上的 1900 非闰年 bug，实用基准取 1899-12-30）
_EXCEL_EPOCH = datetime(1899, 12, 30)

_TIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d%H%M",
)

#: 列名别名表（§3.4）：键为规范列名，值为可接受的别名（已归一化）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "时间", "时间戳", "时刻", "datetime", "date", "time"),
    "load_kwh": (
        "loadkwh", "负荷", "用电量", "电量", "load", "负荷kwh", "用电量kwh",
        "负荷电量", "实际用电量",
    ),
    "pv_kwh": (
        "pvkwh", "光伏", "发电量", "pv", "光伏发电量", "pvgenerationkwh",
        "光伏kwh", "光伏电量",
    ),
    "price": ("price", "电价", "购电电价", "电度电价", "电价元kwh", "到户电价"),
    "export_price": ("exportprice", "上网电价", "export", "上网电价元kwh", "上网"),
}


def normalize_header(name: Any) -> str:
    """归一化表头：去首尾空白、转小写、去掉空格与全角括号、下划线。

    例如 ``" Load_kWh "``、``"负荷（kWh）"``、``"电价(元/kWh)"`` 都能落到规范列名上。
    """
    text = str(name or "").strip().lower()
    for ch in (" ", "\t", "_", "-", "（", "）", "(", ")", "/", "\\", "：", ":"):
        text = text.replace(ch, "")
    return text


def _match_column(header: str, field: str) -> bool:
    normalized = normalize_header(header)
    if not normalized:
        return False
    return any(normalized == alias or normalized.startswith(alias) for alias in COLUMN_ALIASES[field])


def _resolve_columns(headers: Iterable[Any], required: tuple[str, ...], *, path: Path) -> dict[str, str]:
    """把规范列名映射到文件中的实际表头；缺列时报中文错误。"""
    header_list = [str(h) for h in headers if h is not None and str(h).strip()]
    if not header_list:
        raise ValidationError(
            f"文件「{path.name}」没有可用表头，无法识别数据列", field="data.import.header"
        )

    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field in required:
        for header in header_list:
            if header in used:
                continue
            if _match_column(header, field):
                mapping[field] = header
                used.add(header)
                break
        if field not in mapping:
            friendly = {
                "timestamp": "时间列（timestamp 或 时间）",
                "load_kwh": "负荷列（load_kwh 或 负荷）",
                "pv_kwh": "光伏发电量列（pv_kwh 或 光伏）",
                "price": "电价列（price 或 电价）",
            }[field]
            raise ValidationError(
                f"文件「{path.name}」缺少{friendly}；"
                f"实际表头：{'、'.join(header_list[:8])}",
                field=f"data.import.{field}",
            )
    return mapping


def parse_timestamp(value: Any) -> datetime | None:
    """解析时间列；无法解析返回 ``None``（由调用方按 §5.1 V01/V11 处理）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        # Excel 日期序列号：1 ≈ 1900-01-01，上限约 9999-12-31
        if 1.0 <= serial <= 2_958_465.0:
            return (_EXCEL_EPOCH + timedelta(days=serial)).replace(microsecond=0)
        return None
    text = str(value).strip()

    # CSV 中 Excel 日期序列号会以**数字字符串**出现（如 "45658.125"），必须支持：
    # 从 Excel 另存为 CSV 是最常见的交付方式。
    # 判定规则：带小数点，或纯数字且长度 ≥ 5 位（年份是 4 位，不会被误判）。
    numeric = text.replace(".", "", 1).replace("-", "", 1)
    looks_numeric = numeric.isdigit() and (("." in text) or len(numeric) >= 5)
    if looks_numeric:
        try:
            serial = float(text)
        except ValueError:
            serial = None
        if serial is not None and 1.0 <= serial <= 2_958_465.0:
            return (_EXCEL_EPOCH + timedelta(days=serial)).replace(microsecond=0)

    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError as exc:  # 换下一个编码重试
            last_error = exc
    raise ValidationError(
        f"文件「{path.name}」编码无法识别（已尝试 UTF-8 与 GBK）：{last_error}",
        field="data.import.encoding",
    )


def _open_workbook(path: Path):
    """打开 ``.xlsx`` 工作簿；损坏文件转成**中文** ``ValidationError``（V2.1 §9.1）。

    在 V2.1 之前，非 zip / 损坏的 ``.xlsx`` 会让 ``openpyxl`` 抛出
    ``BadZipFile`` 这类裸异常直接冒到界面上（V2.1 §0.2 明令禁止）。
    这里统一翻译成中文提示，并保留原始异常作为 ``__cause__`` 供日志排查。
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读提示
        raise ValidationError(
            "读取 .xlsx 需要 openpyxl，请先安装该依赖", field="data.import.openpyxl"
        ) from exc

    try:
        return load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 统一翻译为中文提示（BadZipFile / InvalidFileException / OSError）
        raise ValidationError(
            f"文件「{path.name}」不是有效的 Excel 工作簿（可能已损坏、被占用或不是 .xlsx 格式）：{exc}",
            field="data.import.corrupt",
        ) from exc


def _rows_to_dicts(
    rows: list[tuple],
    *,
    path: Path,
    sheet_name: str,
    first_row: int = 1,
    with_row_numbers: bool = False,
) -> list[dict[str, Any]] | list[tuple[int, dict[str, Any]]]:
    """把"首行为表头"的二维表转成 ``list[dict]``，跳过全空行。

    ``with_row_numbers=True`` 时返回 ``[(文件行号, 行字典), …]``，
    供导入预览给出**与源文件一致的行号**（跳过空行后行号仍不漂移，V2.1 §5.5）。
    """
    if not rows:
        raise ValidationError(
            f"工作表「{sheet_name}」没有可用的表头行（文件「{path.name}」为空）",
            field="data.import.header",
        )
    headers = ["" if h is None else str(h) for h in rows[0]]
    out: list[dict[str, Any]] = []
    numbered: list[tuple[int, dict[str, Any]]] = []
    for offset, row in enumerate(rows[1:], start=1):
        if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        record = {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
        out.append(record)
        numbered.append((first_row + offset, record))
    return numbered if with_row_numbers else out


def list_sheets(path: str | Path) -> list[str]:
    """列出文件包含的工作表名（V2.1 §5.3：导入时要让用户选择工作表）。

    ``.csv`` / ``.txt`` 只有一个"表"，返回文件名本身，便于界面统一处理。

    :raises ValidationError: 文件不存在、扩展名不支持、工作簿损坏
    """
    target = Path(path)
    if not target.exists():
        raise ValidationError(f"文件不存在：{target}", field="data.import.path")
    suffix = target.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        workbook = _open_workbook(target)
        try:
            return list(workbook.sheetnames)
        finally:
            workbook.close()
    if suffix in (".csv", ".txt"):
        return [target.name]
    raise ValidationError(
        f"不支持的文件类型「{suffix}」，请使用 .xlsx 或 .csv", field="data.import.suffix"
    )


def read_table_from_sheet(
    path: str | Path,
    sheet: str | None = None,
    *,
    header_row: int = 1,
    with_row_numbers: bool = False,
) -> list[dict[str, Any]] | list[tuple[int, dict[str, Any]]]:
    """按**指定工作表**读表（V2.1 §5.3；V2 的 :func:`read_table` 只读第一个表）。

    与 :func:`read_table` 的差异仅在"选哪张表"：``sheet=None`` 时仍取第一张工作表，
    因此既有调用方的行为完全不变。

    :param sheet: 工作表名；``None`` = 第一张工作表。名称不存在时报中文错误并列出可用工作表。
    :param header_row: 表头所在行（从 1 开始）。
    :param with_row_numbers: ``True`` 时返回 ``[(文件行号, 行字典), …]``，
        供导入预览定位到源文件行号（V2.1 §5.5）。``.csv`` 不支持 ``header_row``，
        行号一律按物理行计算。
    :raises ValidationError: 文件不存在、扩展名不支持、工作表不存在、空表、工作簿损坏
    """
    target = Path(path)
    if not target.exists():
        raise ValidationError(f"文件不存在：{target}", field="data.import.path")
    suffix = target.suffix.lower()
    start_row = max(int(header_row), 1)

    if suffix in (".csv", ".txt"):
        records = _read_csv(target)
        if not records:
            raise ValidationError(
                f"文件「{target.name}」没有数据行（只有表头或完全为空）", field="data.import.empty"
            )
        if with_row_numbers:
            # DictReader 已消费表头，第 1 条数据对应物理行 2；完全空白行被 DictReader 跳过，
            # 因此行号在含大量空行的 CSV 上可能有偏差（Excel 导入不受影响）
            return [(index + 2, record) for index, record in enumerate(records)]
        return records

    if suffix not in (".xlsx", ".xlsm"):
        raise ValidationError(
            f"不支持的文件类型「{suffix}」，请使用 .xlsx 或 .csv", field="data.import.suffix"
        )

    workbook = _open_workbook(target)
    try:
        names = list(workbook.sheetnames)
        if sheet is None:
            sheet_name = names[0]
        elif sheet in names:
            sheet_name = sheet
        else:
            raise ValidationError(
                f"工作表「{sheet}」不存在；文件「{target.name}」包含的工作表："
                f"{'、'.join(names) if names else '（无）'}",
                field="data.import.sheet",
            )
        worksheet = workbook[sheet_name]
        rows = list(worksheet.iter_rows(min_row=start_row, values_only=True))
    finally:
        workbook.close()

    if not rows:
        raise ValidationError(
            f"工作表「{sheet_name}」没有数据行（只有表头或完全为空）", field="data.import.empty"
        )
    table = _rows_to_dicts(
        rows,
        path=target,
        sheet_name=sheet_name,
        first_row=start_row,
        with_row_numbers=with_row_numbers,
    )
    if not table:
        raise ValidationError(
            f"工作表「{sheet_name}」没有数据行（只有表头或完全为空）", field="data.import.empty"
        )
    return table


def _read_xlsx(path: Path) -> list[dict[str, Any]]:
    """读取 ``.xlsx`` 的第一张工作表（兼容 V2 既有行为）。"""
    workbook = _open_workbook(path)
    try:
        sheet_name = workbook.sheetnames[0]
        rows = list(workbook[sheet_name].iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return []
    return _rows_to_dicts(rows, path=path, sheet_name=sheet_name)


def read_table(path: str | Path) -> list[dict[str, Any]]:
    """统一读表入口（§51）：``.xlsx`` 用 openpyxl，``.csv`` 用标准库。

    :raises ValidationError: 文件不存在、扩展名不支持、空文件
    """
    target = Path(path)
    if not target.exists():
        raise ValidationError(f"文件不存在：{target}", field="data.import.path")
    suffix = target.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        rows = _read_xlsx(target)
    elif suffix in (".csv", ".txt"):
        rows = _read_csv(target)
    else:
        raise ValidationError(
            f"不支持的文件类型「{suffix}」，请使用 .xlsx 或 .csv", field="data.import.suffix"
        )
    if not rows:
        raise ValidationError(
            f"文件「{target.name}」没有数据行（只有表头或完全为空）", field="data.import.empty"
        )
    return rows


def _finite_values(rows: list[dict[str, Any]], column: str) -> np.ndarray:
    values: list[float] = []
    for row in rows:
        raw = row.get(column)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return np.asarray(values, dtype=float)


def detect_unit_issues(values: np.ndarray, kind: str, *, column: str = "") -> list[DataQualityIssue]:
    """按 §4.3 的启发式检测**疑似单位不匹配**，只提示不换算。

    ==========  ================================================
    类型        判定
    ==========  ================================================
    电量        中位数 < 1.0 → 疑似 MWh/Wh；中位数 > 100000 → 疑似 Wh
    电价        中位数 > 5.0 → 疑似 分/kWh 或 元/MWh；中位数 < 0.05 → 疑似单位错误
    ==========  ================================================
    """
    issues: list[DataQualityIssue] = []
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return issues
    # 电量类曲线（尤其光伏）天然含大量 0（夜间不出力、非生产时段无负荷），
    # 用全体中位数会把"正常含零"误判成"数值普遍偏小"。因此取**非零值**中位数；
    # 若全为 0（整列无有效数据）则退回全体中位数。
    positive = finite[finite > 0.0]
    median = float(np.median(positive)) if positive.size else float(np.median(finite))

    if kind in ("load", "pv"):
        if positive.size and median < 1.0:
            issues.append(
                DataQualityIssue(
                    level="WARNING",
                    category="unit",
                    message=(
                        f"[V09] {column or kind} 非零数值普遍偏小（中位数 {median:.4f}），"
                        f"疑似单位为 MWh 或 Wh；本软件只接受 kWh，不做自动换算"
                    ),
                    count=int(positive.size),
                    samples=[],
                )
            )
        elif positive.size and median > 100_000.0:
            issues.append(
                DataQualityIssue(
                    level="WARNING",
                    category="unit",
                    message=(
                        f"[V09] {column or kind} 数值普遍偏大（中位数 {median:,.1f}），"
                        f"疑似单位为 Wh；本软件只接受 kWh，不做自动换算"
                    ),
                    count=int(positive.size),
                    samples=[],
                )
            )
    elif kind == "price":
        if median > 5.0:
            issues.append(
                DataQualityIssue(
                    level="WARNING",
                    category="unit",
                    message=(
                        f"[V09] 电价中位数 {median:.4f} 明显偏高，疑似单位为 分/kWh 或 元/MWh；"
                        f"电价应在 0.1~2.0 元/kWh 区间"
                    ),
                    count=int(finite.size),
                    samples=[],
                )
            )
        elif median < 0.05:
            issues.append(
                DataQualityIssue(
                    level="WARNING",
                    category="unit",
                    message=(
                        f"[V09] 电价中位数 {median:.5f} 明显偏低，疑似单位填写错误；"
                        f"电价应在 0.1~2.0 元/kWh 区间"
                    ),
                    count=int(finite.size),
                    samples=[],
                )
            )
    return issues


#: 模板列名 → :class:`TimeSeriesPoint` 模型字段名。
#: 模板用业务列名（``pv_kwh``、``price``），模型用语义字段名
#: （``pv_generation_kwh``、``electricity_price``）。
#: **键必须与 :func:`_resolve_columns` 的 required 名称一致**，否则取值会落空。
TEMPLATE_TO_FIELD: dict[str, str] = {
    "load_kwh": "load_kwh",
    "pv_kwh": "pv_generation_kwh",
    "price": "electricity_price",
    "export_price": "export_price",
}


def _find_optional_column(headers: Iterable[Any], field: str) -> str | None:
    """在表头中查找**可选列**（如电价模板的 ``export_price``），找不到返回 ``None``。"""
    for header in headers:
        if header is None or not str(header).strip():
            continue
        if _match_column(str(header), field):
            return str(header)
    return None


def _build_points(
    rows: list[dict[str, Any]],
    mapping: dict[str, str],
    *,
    path: Path,
    fields: tuple[str, ...],
) -> list[TimeSeriesPoint]:
    """按模板把表格行转成 :class:`TimeSeriesPoint` 列表。"""
    points: list[TimeSeriesPoint] = []
    unparsed = 0
    first_bad: str | None = None

    for row in rows:
        stamp = parse_timestamp(row.get(mapping["timestamp"]))
        if stamp is None:
            raw = row.get(mapping["timestamp"])
            if raw is not None and str(raw).strip():
                unparsed += 1
                first_bad = first_bad or str(raw)
            continue
        kwargs: dict[str, Any] = {"timestamp": stamp}
        for field in fields:
            column = mapping.get(field)
            raw = row.get(column) if column else None
            model_field = TEMPLATE_TO_FIELD.get(field, field)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                # 空值显式记为 NaN，**不能**留默认 0.0：
                # 否则"缺失"与"真值为 0"无法区分，V11 与缺失统计都会失真（§5.2、§6.1）
                kwargs[model_field] = float("nan")
                continue
            try:
                kwargs[model_field] = float(raw)
            except (TypeError, ValueError):
                # 非数值文本同样按 NaN 处理，由校验器按 V11 报告（§5.2）
                kwargs[model_field] = float("nan")
        points.append(TimeSeriesPoint(**kwargs))

    if unparsed:
        logger.warning("文件 %s 有 %d 行时间无法解析，示例：%s", path.name, unparsed, first_bad)
    if not points:
        raise ValidationError(
            f"文件「{path.name}」的时间列无法解析：请使用 YYYY-MM-DD HH:MM 格式",
            field="data.import.timestamp",
        )
    return points


def _infer_resolution(points: list[TimeSeriesPoint]) -> Resolution:
    """按相邻时间差推断分辨率（用于填写 ``Profile.resolution``）。

    V2.2 阶段 3 增量：新增 ``HALF_HOURLY``（30 分钟）判定。
    在此之前，30 分钟数据会被推断成 ``QUARTER_HOURLY``（Δt 0.25 h 而非 0.5 h），
    任何基于 Δt 的功率/电量换算都会差 2 倍，属**修正**而非行为变更：
    15 分钟与 1 小时及以上数据的判定边界与结果完全不变。
    """
    if len(points) < 2:
        return Resolution.HOURLY
    stamps = sorted(p.timestamp for p in points)
    deltas = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    median = float(np.median(np.asarray(deltas, dtype=float)))
    if median <= 60.0 * 60.0 * 0.5:
        # 30 分钟（1800 秒）落在 15 分钟与 1 小时之间：超过 22.5 分钟即判为 30 分钟
        return (
            Resolution.HALF_HOURLY
            if median > 15.0 * 60.0 * 1.5
            else Resolution.QUARTER_HOURLY
        )
    if median <= 24.0 * 3600.0 * 0.5:
        return Resolution.HOURLY
    if median <= 31.0 * 24.0 * 3600.0 * 0.5:
        return Resolution.DAILY
    return Resolution.MONTHLY


def _log_unit_hints(values: np.ndarray, kind: str, column: str, path: Path) -> None:
    for issue in detect_unit_issues(values, kind, column=column):
        logger.warning("单位疑点（%s）：%s", path.name, issue.message)


def import_load_file(path: str | Path) -> LoadProfile:
    """导入负荷曲线（模板 ``timestamp,load_kwh``，单位 kWh）。

    :raises ValidationError: 缺列、空文件、时间列无法解析
    """
    target = Path(path)
    rows = read_table(target)
    mapping = _resolve_columns(rows[0].keys(), ("timestamp", "load_kwh"), path=target)
    points = _build_points(rows, mapping, path=target, fields=("load_kwh",))

    values = _finite_values(rows, mapping["load_kwh"])
    _log_unit_hints(values, "load", mapping["load_kwh"], target)

    points.sort(key=lambda p: p.timestamp)
    return LoadProfile(
        profile_id=target.stem,
        name=target.stem,
        resolution=_infer_resolution(points),
        source=target.name,
        source_date=date.fromtimestamp(target.stat().st_mtime),
        source_type=SourceType.USER_INPUT,
        points=points,
        annual_energy=float(np.nansum([p.load_kwh for p in points])),
    )


def import_pv_file(path: str | Path, *, capacity_kwp: float = 0.0) -> PVProfile:
    """导入光伏出力曲线（模板 ``timestamp,pv_kwh``，单位 kWh）。

    :param capacity_kwp: 该曲线对应的装机容量，用于后续超容量校验（R-PV-OVER）
    """
    target = Path(path)
    rows = read_table(target)
    mapping = _resolve_columns(rows[0].keys(), ("timestamp", "pv_kwh"), path=target)
    points = _build_points(rows, mapping, path=target, fields=("pv_kwh",))

    values = _finite_values(rows, mapping["pv_kwh"])
    _log_unit_hints(values, "pv", mapping["pv_kwh"], target)

    points.sort(key=lambda p: p.timestamp)
    return PVProfile(
        profile_id=target.stem,
        name=target.stem,
        resolution=_infer_resolution(points),
        source=target.name,
        source_date=date.fromtimestamp(target.stat().st_mtime),
        source_type=SourceType.USER_INPUT,
        points=points,
        capacity_kwp=max(capacity_kwp, 0.0),
        annual_generation=float(np.nansum([p.pv_generation_kwh for p in points])),
    )


def import_tariff_file(path: str | Path) -> list[TimeSeriesPoint]:
    """导入电价曲线（模板 ``timestamp,price,export_price``，单位 元/kWh）。

    ``export_price`` 允许为空——按模板定义**沿用上一非空值**（§6.1 表），
    这属于合法简写而非缺失。
    """
    target = Path(path)
    rows = read_table(target)
    mapping = _resolve_columns(rows[0].keys(), ("timestamp", "price"), path=target)
    export_header = _find_optional_column(rows[0].keys(), "export_price")
    if export_header:
        mapping["export_price"] = export_header
    fields = ("price", "export_price") if export_header else ("price",)
    points = _build_points(rows, mapping, path=target, fields=fields)

    values = _finite_values(rows, mapping["price"])
    _log_unit_hints(values, "price", mapping["price"], target)

    points.sort(key=lambda p: p.timestamp)
    # 上网电价沿用上一非空值（合法简写，不计缺失）
    last_export = 0.0
    for point in points:
        if point.export_price > 0.0:
            last_export = point.export_price
        elif last_export > 0.0:
            point.export_price = last_export
    return points
