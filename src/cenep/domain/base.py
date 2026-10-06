"""领域模型共享基类与字段模板（V1 规范 §112；V2 §6）。

原 ``_Model`` / ``RATIO`` / ``NON_NEG`` 定义在 :mod:`cenep.domain.models`，
V2 新增 :mod:`cenep.domain.timeseries` 与 :mod:`cenep.domain.timeseries_results`
也需要同一基类，为避免循环导入而抽出到本模块。
``models.py`` 仍重新导出这三个名字，既有 ``from cenep.domain.models import _Model`` 不受影响。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: 比例字段模板（内部统一用小数，0~1）
RATIO = Field(default=0.0, ge=0.0, le=1.0, description="比例，内部统一用小数")
#: 非负数值字段模板
NON_NEG = Field(default=0.0, ge=0.0, description="非负数值")


class _Model(BaseModel):
    """统一基类：禁止多余字段，保证模型与文档严格一致。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


__all__ = ["NON_NEG", "RATIO", "_Model"]
