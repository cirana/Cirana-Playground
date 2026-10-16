# PyInstaller spec for a single-file portable build:  pyinstaller cirana-portable.spec --noconfirm
# Output: dist\CIRANA.exe  (see cirana.spec for the one-folder build)
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["run_cirana.py"],
    pathex=["."],
    datas=[("cirana/assets", "cirana/assets")],
    hiddenimports=["win32timezone", "win32com.shell.shell", "win32com.shell.shellcon", "pythoncom"]
    + collect_submodules("cirana"),
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtPdf", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="CIRANA",
    icon="cirana/assets/icons/main_icon.ico",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
