"""报告层：Excel / PDF 导出（规范 §108–§111）。

**铁律**：本层只把 :class:`cenep.domain.results.CalculationResult` 渲染成文件，
**不得进行任何计算**（规范 §8、§109、§154）。
"""

from .excel_exporter import SHEET_NAMES, ExcelExporter, excel_exporter
from .pdf_exporter import REPORT_SECTIONS, DISCLAIMER, PdfExporter, pdf_exporter

__all__ = [
    "ExcelExporter",
    "excel_exporter",
    "SHEET_NAMES",
    "PdfExporter",
    "pdf_exporter",
    "REPORT_SECTIONS",
    "DISCLAIMER",
]
