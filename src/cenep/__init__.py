"""CENEP V2 —— 工商业新能源项目经济评价软件。

铁律：
* 所有公式集中在 :mod:`cenep.calculation`；
* 所有数据结构集中在 :mod:`cenep.domain`；
* GUI / Excel / PDF 一律只展示 :class:`cenep.domain.results.CalculationResult`，不得自行计算。

V2 在 V1 的年度模型之上新增 8760 时序仿真，但**年度评价模式与 V1 计算逻辑
逐位不变**：``timeseries.enabled`` 默认为 ``False``，此时不加载任何 V2 模块。
"""

__version__ = "2.0.0"
