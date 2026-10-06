"""领域层：数据模型（规范 §11）。

本层只描述数据，不含计算；计算全部在 :mod:`cenep.calculation`。
"""

from .enums import (
    DepreciationMethod,
    InvestmentMode,
    OpexMode,
    ProjectType,
    RepaymentMethod,
    RiskLevel,
    RoofRentMode,
    ScenarioType,
    SensitivityVariable,
    SourceType,
    TariffMode,
)
from .models import (
    BasicInfo,
    FinancingConfig,
    InvestmentConfig,
    LoadConfig,
    OpexConfig,
    PolicyProfile,
    Project,
    PVConfig,
    ScenarioConfig,
    ScenarioDelta,
    SensitivityConfig,
    StorageConfig,
    TariffConfig,
    TaxConfig,
)
from .provenance import UNITS, ParameterMeta, ParameterRegistry, SOURCE_PRIORITY, unit_of
from .results import AnnualResult, CalculationResult

__all__ = [
    # enums
    "DepreciationMethod",
    "InvestmentMode",
    "OpexMode",
    "ProjectType",
    "RepaymentMethod",
    "RiskLevel",
    "RoofRentMode",
    "ScenarioType",
    "SensitivityVariable",
    "SourceType",
    "TariffMode",
    # models
    "BasicInfo",
    "FinancingConfig",
    "InvestmentConfig",
    "LoadConfig",
    "OpexConfig",
    "PolicyProfile",
    "Project",
    "PVConfig",
    "ScenarioConfig",
    "ScenarioDelta",
    "SensitivityConfig",
    "StorageConfig",
    "TariffConfig",
    "TaxConfig",
    # provenance
    "UNITS",
    "ParameterMeta",
    "ParameterRegistry",
    "SOURCE_PRIORITY",
    "unit_of",
    # results
    "AnnualResult",
    "CalculationResult",
]
