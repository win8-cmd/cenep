"""CENEP V2 —— 工商业新能源项目经济评价软件。

铁律：
* 所有公式集中在 :mod:`cenep.calculation`；
* 所有数据结构集中在 :mod:`cenep.domain`；
* GUI / Excel / PDF 一律只展示 :class:`cenep.domain.results.CalculationResult`，不得自行计算。

V2 在 V1 的年度模型之上新增 8760 时序仿真，但**年度评价模式与 V1 计算逻辑
逐位不变**：``timeseries.enabled`` 默认为 ``False``，此时不加载任何 V2 模块。
"""

__version__ = "2.0.0"

#: 应用名。**唯一来源** —— 窗口标题、PDF 作者、日志都引用它，
#: 不得在别处再写字面量（曾因 ``main_window.py`` 与 ``pdf_exporter.py``
#: 各自硬编码 "CENEP V1"，导致升级版本后窗口标题仍显示 V1）。
APP_NAME = "CENEP V2"
