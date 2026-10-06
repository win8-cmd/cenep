import importlib
mods = ["pydantic","pytest","scipy","PySide6","reportlab","openpyxl","pandas","numpy","sqlite3","xlsxwriter","matplotlib","jinja2"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"{m:12} OK      {getattr(mod, '__version__', '?')}")
    except Exception as e:
        print(f"{m:12} MISSING {type(e).__name__}")
