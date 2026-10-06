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
    "normalize_header",
    "parse_timestamp",
    "read_table",
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


def _read_xlsx(path: Path) -> list[dict[str, Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读提示
        raise ValidationError(
            "读取 .xlsx 需要 openpyxl，请先安装该依赖", field="data.import.openpyxl"
        ) from exc

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()

    if not rows:
        return []
    headers = ["" if h is None else str(h) for h in rows[0]]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        out.append({headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))})
    return out


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
    """按相邻时间差推断分辨率（用于填写 ``Profile.resolution``）。"""
    if len(points) < 2:
        return Resolution.HOURLY
    stamps = sorted(p.timestamp for p in points)
    deltas = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    median = float(np.median(np.asarray(deltas, dtype=float)))
    if median <= 60.0 * 60.0 * 0.5:
        return Resolution.QUARTER_HOURLY
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
