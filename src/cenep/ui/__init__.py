"""界面层（PySide6）。

**铁律（规范 §8、§148、§161）**：本层只做两件事——

1. 把 :class:`cenep.domain.models.Project` 的字段绑定到控件（双向）；
2. 调用 :class:`cenep.application.calculation_service.CalculationService` 拿到
   :class:`cenep.domain.results.CalculationResult` 并展示。

界面代码中**不得出现任何公式**；所有数值一律来自 ``CalculationResult``。
"""

from .field_spec import FieldRow, FieldSpec, SectionForm

__all__ = ["FieldSpec", "FieldRow", "SectionForm"]
