"""政策层（规范 §34–§36、§89、§90）。

设计要点
--------
1. **模板与实例分离**：模板层 :class:`PolicyTemplate` 允许字段"未填写"（``None``），
   用户填写完成后才能转成可用于计算的 :class:`PolicyProfile`；
   **禁止把 ``None`` 直接塞进 ``PolicyProfile``**（那会造成"看起来有政策、其实是空值"的假象）。
2. **不预填任何具体数值**：内置模板的所有数值字段一律为 ``None``，
   ``to_profile()`` 会在未填写时抛出中文错误（规范 §159）。
3. **版本化**：同一 ``policy_id`` 可有多个 ``policy_version``，旧版本只新增不覆盖。
"""

from .template import PolicyTemplate, PolicyTemplateError
from .store import PolicyStore
from .hubei import HUBEI_TEMPLATE, describe_startup_notice

__all__ = [
    "PolicyTemplate",
    "PolicyTemplateError",
    "PolicyStore",
    "HUBEI_TEMPLATE",
    "describe_startup_notice",
]
