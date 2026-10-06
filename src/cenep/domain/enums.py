"""枚举类型（规范 §13、§29、§34、§52、§57、§62、§84、§92、§144）。"""

from __future__ import annotations

from enum import StrEnum


class ProjectType(StrEnum):
    """项目类型（规范 §13）。V1 只允许三种工商业项目。"""

    COMMERCIAL_PV = "COMMERCIAL_PV"
    COMMERCIAL_STORAGE = "COMMERCIAL_STORAGE"
    PV_STORAGE = "PV_STORAGE"

    @property
    def label(self) -> str:
        return {
            ProjectType.COMMERCIAL_PV: "工商业光伏",
            ProjectType.COMMERCIAL_STORAGE: "工商业储能",
            ProjectType.PV_STORAGE: "工商业光储",
        }[self]

    @property
    def has_pv(self) -> bool:
        return self in (ProjectType.COMMERCIAL_PV, ProjectType.PV_STORAGE)

    @property
    def has_storage(self) -> bool:
        return self in (ProjectType.COMMERCIAL_STORAGE, ProjectType.PV_STORAGE)


class TariffMode(StrEnum):
    """电价模式（规范 §29）。"""

    FIXED = "FIXED"
    TOU = "TOU"
    MARKET = "MARKET"
    CUSTOM = "CUSTOM"

    @property
    def label(self) -> str:
        return {
            TariffMode.FIXED: "固定电价",
            TariffMode.TOU: "峰平谷电价",
            TariffMode.MARKET: "市场电价",
            TariffMode.CUSTOM: "自定义电价",
        }[self]


class InvestmentMode(StrEnum):
    """投资模式（规范 §52）。"""

    UNIT_PRICE = "UNIT_PRICE"
    DETAILED = "DETAILED"


class DepreciationMethod(StrEnum):
    """折旧方法（规范 §57）。V1 只支持直线法。"""

    STRAIGHT_LINE = "STRAIGHT_LINE"

    @property
    def label(self) -> str:
        return {"STRAIGHT_LINE": "直线法（年限平均法）"}[self.value]


class RepaymentMethod(StrEnum):
    """还款方式（规范 §62）。"""

    EQUAL_PRINCIPAL = "EQUAL_PRINCIPAL"
    EQUAL_INSTALLMENT = "EQUAL_INSTALLMENT"

    @property
    def label(self) -> str:
        return {
            RepaymentMethod.EQUAL_PRINCIPAL: "等额本金",
            RepaymentMethod.EQUAL_INSTALLMENT: "等额本息",
        }[self]


class OpexMode(StrEnum):
    """运维费用计算模式（规范 §54）。"""

    FIXED = "FIXED"
    RATIO_OF_CAPEX = "RATIO_OF_CAPEX"


class RoofRentMode(StrEnum):
    """屋顶租金模式（规范 §56）。"""

    AREA = "AREA"
    CAPACITY = "CAPACITY"
    FIXED = "FIXED"


class SourceType(StrEnum):
    """参数来源类型（规范 §84）。"""

    USER_INPUT = "USER_INPUT"
    POLICY = "POLICY"
    CONTRACT = "CONTRACT"
    HISTORICAL = "HISTORICAL"
    EXPERIENCE = "EXPERIENCE"
    ASSUMPTION = "ASSUMPTION"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
    CALCULATED = "CALCULATED"

    @property
    def label(self) -> str:
        return {
            SourceType.USER_INPUT: "用户输入",
            SourceType.POLICY: "政策参数",
            SourceType.CONTRACT: "合同参数",
            SourceType.HISTORICAL: "历史数据",
            SourceType.EXPERIENCE: "行业经验",
            SourceType.ASSUMPTION: "假设值",
            SourceType.SYSTEM_DEFAULT: "系统默认",
            SourceType.CALCULATED: "系统计算",
        }[self]

    @property
    def ui_color(self) -> str:
        """界面配色（规范 §144）。"""
        return {
            SourceType.USER_INPUT: "blue",
            SourceType.POLICY: "gray",
            SourceType.CONTRACT: "blue",
            SourceType.HISTORICAL: "blue",
            SourceType.EXPERIENCE: "yellow",
            SourceType.ASSUMPTION: "yellow",
            SourceType.SYSTEM_DEFAULT: "yellow",
            SourceType.CALCULATED: "green",
        }[self]


class ScenarioType(StrEnum):
    """情景类型（规范 §92）。"""

    CONSERVATIVE = "CONSERVATIVE"
    BASE = "BASE"
    OPTIMISTIC = "OPTIMISTIC"

    @property
    def label(self) -> str:
        return {
            ScenarioType.CONSERVATIVE: "保守",
            ScenarioType.BASE: "基准",
            ScenarioType.OPTIMISTIC: "乐观",
        }[self]


class SensitivityVariable(StrEnum):
    """敏感性分析变量（规范 §94）。"""

    CAPEX = "CAPEX"
    ELECTRICITY_PRICE = "ELECTRICITY_PRICE"
    GENERATION = "GENERATION"
    OPEX = "OPEX"
    SELF_CONSUMPTION_RATIO = "SELF_CONSUMPTION_RATIO"
    STORAGE_CYCLES = "STORAGE_CYCLES"
    STORAGE_CAPEX = "STORAGE_CAPEX"
    INTEREST_RATE = "INTEREST_RATE"

    @property
    def label(self) -> str:
        return {
            SensitivityVariable.CAPEX: "总投资",
            SensitivityVariable.ELECTRICITY_PRICE: "电价",
            SensitivityVariable.GENERATION: "发电量",
            SensitivityVariable.OPEX: "运维成本",
            SensitivityVariable.SELF_CONSUMPTION_RATIO: "自用比例",
            SensitivityVariable.STORAGE_CYCLES: "储能循环次数",
            SensitivityVariable.STORAGE_CAPEX: "储能投资",
            SensitivityVariable.INTEREST_RATE: "贷款利率",
        }[self]


class RiskLevel(StrEnum):
    """结果颜色分级（规范 §106）。仅作提示，**不得**据此直接判定项目"可行/不可行"。"""

    EXCELLENT = "EXCELLENT"
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    RISK = "RISK"

    @property
    def label(self) -> str:
        return {
            RiskLevel.EXCELLENT: "优秀",
            RiskLevel.NORMAL: "正常",
            RiskLevel.WATCH: "关注",
            RiskLevel.RISK: "风险",
        }[self]
