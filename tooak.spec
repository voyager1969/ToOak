# -*- mode: python ; coding: utf-8 -*-

# 打包配置（`pyinstaller tooak.spec` 或按 README「打包发布」那一节跑都行）。
#
# **datas 必须留着**：程序运行时靠 `tooak/resource.py` 从 `sys._MEIPASS` 里取
# 这些资源 —— 少了它们，界面里的「说明」打不开（PDF 没了）、图标是空的、
# Web 审阅页 404。PyInstaller 从 .py 生成 spec 时会把 datas 清空，
# 所以每回重新生成之后都要把下面两行补回来（README 里的打包命令也带了 --add-data）。

import os

REPO = os.path.abspath(os.path.dirname(SPEC))


a = Analysis(
    [os.path.join(REPO, 'run.py')],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(REPO, 'tooak', 'desktop', 'assets'), 'tooak/desktop/assets'),
        (os.path.join(REPO, 'tooak', 'core', 'api', 'review.html'), 'tooak/core/api'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ToOak',
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
    icon=['D:/Program/2_PythonProject/工程项目/ToOak/tooak/desktop/assets/tooak.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ToOak',
)
