"""计算层：唯一公式所在地（规范 §8、§145、§146）。

本包内每个模块职责单一，全部为纯函数或纯计算类：

======================  ==========================================
模块                     职责
======================  ==========================================
``pv``                   光伏容量、发电量、衰减、自用、上网
``storage``              储能容量、效率、循环、衰减、充放电
``revenue``              电价与光伏/储能收益
``investment``           CAPEX
``opex``                 OPEX
``tax``                  折旧与税
``financing``            贷款、利息、还本
``cashflow``             项目现金流、资本金现金流
``financial_metrics``    IRR / NPV / Payback / LCOE / LCOS / ROI / DSCR
``scenario``             情景分析
``sensitivity``          敏感性分析
``validator``            输入校验与守恒校验
``engine``               统一入口 ``calculation_engine.calculate(project)``
``bill_calculator``      账单勾稽（ΔE / ΔC / 平均综合电价）与口径披露
``bill_recalculator``    基准账单复算与差异分析
``bill_price_source``    **账单电价取值来源与优先级**（逐时电价优先；政府峰谷系数仅代理购电）
======================  ==========================================

**禁止**在 ``ui`` / ``reports`` 中复制本包任何公式。
"""
