# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
# V1 起就依赖 PySide6.QtCharts；V2 新增 PyQtGraph 交互图（ui/charts.py）与 scipy（optimization）
hiddenimports = ['PySide6.QtCharts']
tmp_ret = collect_all('openpyxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# V2 新增依赖：
# * pyqtgraph —— `ui/charts.py` 的交互式图表（数据文件由 hooks_contrib 的 hook-pyqtgraph 收集）；
# * scipy —— `optimization/lp_optimizer.py` 在函数体内 `from scipy.optimize import linprog`。
#   函数体内的导入 modulegraph 同样能发现，但 linprog 的 HiGHS 后端（`scipy.optimize._highspy`）
#   是编译扩展 + 运行时动态加载，显式登记以杜绝"打包后 LP 寻优静默降级"。
hiddenimports += [
    'pyqtgraph',
    'scipy',
    'scipy.optimize',
    'scipy.optimize._linprog',
    'scipy.optimize._linprog_highs',
    'scipy.optimize._highspy',
    'scipy.optimize._highspy._core',
    'scipy.optimize._highspy._highs_wrapper',
    'scipy.sparse',
    'scipy.sparse._csc',
]


a = Analysis(
    ['C:/Users/Administrator/Documents/deepseek-harness/default-workspace/cenep/build/entry.py'],
    pathex=['C:/Users/Administrator/Documents/deepseek-harness/default-workspace/cenep/src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CENEP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='C:/Users/Administrator/Documents/deepseek-harness/default-workspace/cenep/build/version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CENEP',
)
