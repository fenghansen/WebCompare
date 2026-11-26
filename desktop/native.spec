# Build on the target OS with Python 3.12; one runtime serves GUI and --backend.
import json
from importlib.metadata import distribution, packages_distributions
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
windows = sys.platform == "win32"
datas = [(str(root / "webcompare" / "static"), "webcompare/static"),
         (str(root / "desktop" / "assets" / "icon.png"), "desktop/assets")]
binaries = []
for file in distribution("pyvips-binary").files:
    if ".so" in str(file) or str(file).endswith((".dll", ".dylib", ".pyd")):
        binaries.append((str(distribution("pyvips-binary").locate_file(file)), str(file.parent)))
for name in ("_raw10.dll",) if windows else ("_raw10.so", "libwebcompare-webkit.so"):
    path = root / "webcompare" / "native" / name
    if path.is_file():
        binaries.append((str(path), "webcompare/native"))
datas += copy_metadata("pyvips-binary") + collect_data_files("webview", subdir="js")
# The optional NVDEC wheel is loaded from the owner's data directory.
excludes = ["av", "pytest", "_pytest", "PIL", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "cefpython3",
            "webview.platforms.qt", "webview.platforms.cef", "webview.platforms.cocoa", "webview.platforms.android",
            "webview.platforms.mshtml", "rich", "pygments", "jinja2", "yaml", "chardet", "pkg_resources"]
if windows:
    excludes += ["gi", "cairo", "webview.platforms.gtk"]
else:
    excludes += ["clr", "pythonnet", "webview.platforms.winforms", "webview.platforms.edgechromium",
                 "webview.platforms.mshtml"]

# Keep LGPL bindings replaceable as ordinary files beside the executable.
replaceable = {"paramiko": "py"} if windows else {"paramiko": "py", "gi": "py", "cairo": "py"}
a = Analysis([str(root / "desktop" / "native_entry.py")], pathex=[str(root)],
             binaries=binaries, datas=datas,
             hiddenimports=collect_submodules("webcompare") + ["_libvips", "uvicorn.lifespan.on",
                             "uvicorn.protocols.http.h11_impl"] + ([] if windows else ["cairo"]),
             excludes=excludes, module_collection_mode=replaceable,
             hooksconfig={"gi": {"versions": {"Gtk": "3.0", "Gdk": "3.0", "Soup": "3.0"},
                                                   "languages": [], "themes": [], "icons": []}}, noarchive=False)
if not windows:
    before = list(a.binaries)
    # GTK/WebKit, their typelibs, themes and modules must track the installed Ubuntu libraries.
    # Keep Python's ABI extensions and wheel-private libvips/NumPy libraries.
    a.exclude_system_libraries()
    a.datas = [item for item in a.datas if not item[0].startswith(("share/", "etc/", "gi_typelibs/", "gio_modules/", "lib/gdk-pixbuf/"))
               and (item[0].startswith(("gi/", "cairo/")) and item[0].endswith(".py")
                    or not item[1].startswith(("/usr/share/", "/usr/lib/", "/lib/")))]
    a.scripts = [item for item in a.scripts if item[0] not in {"pyi_rth_gi", "pyi_rth_gtk", "pyi_rth_gio", "pyi_rth_glib", "pyi_rth_gdkpixbuf"}]
    excluded = sorted({Path(item[1]).name for item in before if item not in a.binaries})
    (Path(workpath) / "system-libraries.json").write_text(json.dumps(excluded, indent=2), encoding="utf-8")
package_map = packages_distributions()
modules = {item[0].replace("/", ".").split(".")[0] for item in [*a.pure, *a.binaries]}
runtime_packages = sorted({name for module in modules for name in package_map.get(module, [])})
(Path(workpath) / "runtime-distributions.json").write_text(json.dumps(runtime_packages, indent=2), encoding="utf-8")
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="WebCompare" if windows else "webcompare-desktop",
          icon=str(root / "desktop" / "assets" / "icon.ico") if windows else None,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, hide_console="hide-early" if windows else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WebCompare")
