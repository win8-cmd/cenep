"""生成示例项目：.nep 项目文件 + Excel + PDF（规范 §167 交付物）。"""
import sys, json
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\cenep\src")
from pathlib import Path
from cenep.domain.enums import ProjectType
from cenep.application.project_service import ProjectService
from cenep.application.calculation_service import CalculationService
from cenep.infrastructure.project_file import save_project
from cenep.reports.excel_exporter import ExcelExporter
from cenep.reports.pdf_exporter import PdfExporter

root = Path(r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\cenep\examples")
root.mkdir(parents=True, exist_ok=True)

svc = ProjectService(autosave_enabled=False)
calc = CalculationService(project_service=svc)

cases = {
    "示例1_工商业光伏": ProjectType.COMMERCIAL_PV,
    "示例2_工商业储能": ProjectType.COMMERCIAL_STORAGE,
    "示例3_工商业光储": ProjectType.PV_STORAGE,
}
summary = []
for name, ptype in cases.items():
    project = svc.new_project(ptype, name=name, province="湖北", city="武汉市")
    project.load.annual_load_kwh = 1_200_000.0
    project.pv.equivalent_hours = 1100.0
    project.pv.self_consumption_ratio = 0.8
    project.opex.pv_opex = 0.02
    project.opex.storage_opex = 0.02
    project.opex.insurance = 0.005
    project.opex.management_cost = 10000.0
    project.opex.other_opex = 5000.0

    nep = save_project(project, root / name)
    outcome = calc.calculate_with_outcome(project, nep)
    r = outcome.result
    ExcelExporter().export(project, r, root / f"{name}.xlsx")
    PdfExporter().export(project, r, root / f"{name}.pdf")
    summary.append({
        "项目": name,
        "总投资(元)": round(r.total_capex, 2),
        "首年发电量(kWh)": round(r.first_year_generation, 1),
        "首年收入(元)": round(r.first_year_revenue, 2),
        "项目IRR": None if r.project_irr is None else round(r.project_irr, 6),
        "资本金IRR": None if r.equity_irr is None else round(r.equity_irr, 6),
        "项目NPV(元)": round(r.project_npv, 2),
        "静态回收期(年)": None if r.static_payback is None else round(r.static_payback, 3),
        "动态回收期(年)": None if r.discounted_payback is None else round(r.discounted_payback, 3),
        "LCOE": None if r.lcoe is None else round(r.lcoe, 4),
        "LCOS": None if r.lcos is None else round(r.lcos, 4),
        "最低DSCR": None if r.min_dscr is None else round(r.min_dscr, 4),
        "ROI": None if r.roi is None else round(r.roi, 4),
        "耗时(秒)": round(outcome.elapsed_seconds, 4),
    })

print(json.dumps(summary, ensure_ascii=False, indent=2))
print()
for f in sorted(root.iterdir()):
    print(f"{f.name:40s} {f.stat().st_size:>10,} bytes")
