"""CENEP V2.5 —— 工商业新能源项目经济评价软件。

铁律：
* 所有公式集中在 :mod:`cenep.calculation`；
* 所有数据结构集中在 :mod:`cenep.domain`；
* GUI / Excel / PDF 一律只展示 :class:`cenep.domain.results.CalculationResult`，不得自行计算。

V2 在 V1 的年度模型之上新增 8760 时序仿真，但**年度评价模式与 V1 计算逻辑
逐位不变**：``timeseries.enabled`` 默认为 ``False``，此时不加载任何 V2 模块。

V2.5 主题：**账单 PDF 全量解析 + 账单数据分类口径**
--------------------------------------------------
落实需求方确认的"哪些数据进模型"口径（详见
:mod:`cenep.data.bill_pdf_importer` 与 :mod:`cenep.domain.bill_models` 的模块文档）：

* ① 进模型并直接参与计算：账期、分时电量（以电量明细为准）、**24 小时电量电价表**
  （消纳率电价的首选输入）、电压等级/计费方式、计费需量 + 需量电价、总购电量 + 总电费；
* ② 进模型但只用于校验与追溯：六项费用分项、功率因数三件套、户号/表号/户名、
  ``meter_groups``（逐电能表分组明细）；
* ③ 不进模型：示数、倍率、抄见电量、变损、线损、加减、正向无功电量、第 1 页"峰谷比例"、
  展示性文字、增值税专用发票金额；
* ④ **市场化运营费用（第 5 页 B/C 类）只留档，绝不进入电价或费用的计算口径**——
  这些市场化交易结算项已包含在总电费里，重复计入即重复计算；
* ⑤ **逐时电价优先**：市场化直购客户用逐时交易价格，**不得**套用湖北政府峰谷系数
  （尖峰 200% / 高峰 150% / 低谷 45%），该系数只适用于代理购电客户。
  取价优先级见 :func:`cenep.calculation.bill_price_source.resolve_bill_energy_price`。

**冻结接口（规格书 §0.2）**：``schema_version`` / ``CALCULATION_ENGINE_VERSION`` /
``BILL_SECTION_SCHEMA_VERSION`` 等**不随交付版本号变化**，本次升级未改动。
"""

#: 软件**交付版本号**（安装包 / EXE 产物口径）。
#:
#: 与项目文件 ``schema_version``、``CALCULATION_ENGINE_VERSION`` **不是一回事**：
#: 后两者是公开接口与项目文件语义（规格书 §0.2 冻结条款），不得因交付号变化而改动。
__version__ = "2.5.0"

#: 应用名。**唯一来源** —— 窗口标题、PDF 作者、日志都引用它，
#: 不得在别处再写字面量（曾因 ``main_window.py`` 与 ``pdf_exporter.py``
#: 各自硬编码 "CENEP V1"，导致升级版本后窗口标题仍显示 V1）。
APP_NAME = "CENEP V2"
