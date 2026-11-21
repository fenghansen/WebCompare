"""Freeze one Python runtime and create native Linux/Windows desktop packages."""
from __future__ import annotations

import argparse
import ctypes.util
import hashlib
from importlib.metadata import distribution
from importlib.util import find_spec
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
RELEASES = "https://github.com/fenghansen/WebCompare/releases"
VC_REDIST = "https://aka.ms/vc14/vc_redist.x64.exe"
VC_MINIMUM = "14.44.35211"
SYSTEM_CRT = re.compile(
    r"(?P<name>(?:vcruntime|msvcp|msvcr|vccorlib|concrt|vcomp)[0-9]+(?:_[a-z0-9_]+)?"
    r"|msvcrt|msvcp_win|ucrtbase|api-ms-win-crt-[a-z0-9-]+)(?:-[a-f0-9]{32})?\.dll",
    re.I,
)
# Ubuntu 24.04 ABI baseline. GTK/WebKit and their typelibs stay system-managed.
LINUX_DEPENDS = (
    "libc6 (>= 2.39)", "libstdc++6", "libgcc-s1", "libgirepository-1.0-1",
    "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1", "libgtk-3-0t64", "libwebkit2gtk-4.1-0",
    "libglib2.0-0t64", "libffi8", "libssl3t64", "libsqlite3-0", "libbz2-1.0",
    "liblzma5", "libexpat1", "zlib1g", "libreadline8t64", "libtinfo6",
)
SOURCE_PATTERNS = (
    "pyproject.toml", "requirements.lock", "LICENSE", "NOTICE",
    "desktop/*.py", "desktop/*.spec", "desktop/*.c", "desktop/requirements-native*.txt",
    "desktop/assets/*", "desktop/installers/*", "desktop/third-party/**/*",
    "webcompare/*.py", "webcompare/native/*.c",
    "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
    "frontend/tsconfig.json", "frontend/src/**/*", "frontend/public/**/*",
)


def run(command, **kwargs):
    return subprocess.run([str(value) for value in command], check=True, **kwargs)


def source_provenance():
    """Identify the exact source inputs without retaining local checkout paths."""
    source = {"git_commit": None, "git_tree": None}
    root = ROOT.resolve()
    paths = {path.relative_to(ROOT).as_posix(): path for pattern in SOURCE_PATTERNS
             for path in ROOT.glob(pattern) if path.is_file() and not any(
                 part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts)}
    try:
        top = run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                  capture_output=True, text=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        top = ""
    if top and Path(top).resolve() == root:
        if run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT,
               capture_output=True, text=True).stdout:
            raise SystemExit("Build from a clean source checkout; commit or remove local changes first.")
        for key, revision in (("git_commit", "HEAD"), ("git_tree", "HEAD^{tree}")):
            source[key] = run(["git", "rev-parse", revision], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
        tracked = set(run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                          text=True).stdout.split("\0"))
        paths = {name: path for name, path in paths.items() if name in tracked}
    source["files"] = {}
    for name, path in sorted(paths.items()):
        if not path.resolve().is_relative_to(root):
            raise SystemExit(f"Build input symlink escapes its directory: {name}")
        source["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    source["sha256"] = hashlib.sha256(json.dumps(source["files"], sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
    source["frontend_files"] = {
        path.relative_to(ROOT / "frontend/dist").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "frontend/dist").rglob("*"))
        if path.is_file() and path.name != ".webcompare-build.json"}
    return source


def compile_webkit(sysroot: Path | None):
    """Compile before freezing; release machines never need a compiler or SDK."""
    env = os.environ.copy()
    if sysroot:
        sysroot = sysroot.resolve()
        env["PKG_CONFIG_SYSROOT_DIR"] = str(sysroot)
        env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(str(path) for path in (
            *sysroot.glob("usr/lib/*/pkgconfig"), sysroot / "usr/lib/pkgconfig", sysroot / "usr/share/pkgconfig", Path("/usr/share/pkgconfig")))
    try:
        flags = run(["pkg-config", "--cflags", "--libs", "webkit2gtk-web-extension-4.1"],
                    env=env, capture_output=True, text=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        print(getattr(error, "stderr", ""), file=sys.stderr)
        raise SystemExit("WebKit extension SDK is missing. Install libwebkit2gtk-4.1-dev for builds, "
                         "or pass --webkit-sysroot with unpacked Ubuntu development packages.") from error
    link_flags = shlex.split(flags)
    if sysroot:
        # Unpacked -dev .so symlinks can point at runtime files outside the sysroot.
        # Resolve against the installed SONAMEs without copying those libraries.
        for index, flag in enumerate(link_flags):
            if flag.startswith("-l") and not flag.startswith("-l:"):
                library = ctypes.util.find_library(flag[2:])
                if not library:
                    raise SystemExit(f"Build host runtime library is missing: {flag[2:]}")
                link_flags[index] = f"-l:{library}"
    target = ROOT / "webcompare/native/libwebcompare-webkit.so"
    run([os.environ.get("CC", "cc"), "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
         ROOT / "desktop/native_webkit.c", "-o", target, *link_flags], env=env)
    return target


def runtime_distributions():
    names = []
    for file in (ROOT / "requirements.lock", ROOT / "desktop/requirements-native.txt"):
        for line in file.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_-]+)==", line)
            if match and ("sys_platform" not in line or sys.platform == "win32"):
                names.append(match[1])
    return sorted(set(names))


def ubuntu_python_sources():
    bindings = {"python3.12-minimal": sys.executable, "python3-gi": find_spec("gi").origin,
                "python3-cairo": find_spec("cairo").origin}
    for package, filename in bindings.items():
        path = Path(filename).resolve()
        owner = run(["dpkg-query", "-S", path], capture_output=True, text=True).stdout
        if owner.split(":", 1)[0] != package:
            raise SystemExit(f"Expected to build with the Ubuntu package {package}, found {path}")
    result = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n",
                  *bindings, "python3.12", "libpython3.12-stdlib", "libpython3.12t64"],
                 capture_output=True, text=True)
    return [{"package": name, "version": version, "source": source, "source_version": source_version}
            for name, version, source, source_version in (line.split("\t") for line in result.stdout.splitlines())]


def copy_licenses(app: Path, runtime_packages=()):
    """Retain wheel notices and Python/bootloader licenses without copying build environments."""
    project_license = ROOT / "LICENSE"
    if project_license.is_file():
        target = app / "licenses/WebCompare"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project_license, target / "LICENSE")
        if (ROOT / "NOTICE").is_file():
            shutil.copy2(ROOT / "NOTICE", target / "NOTICE")
    packages = {}
    names = {re.sub(r"[_.-]+", "-", item).lower()
             for item in runtime_distributions() + list(runtime_packages) + ["pyinstaller"]}
    for name in sorted(names - {"webcompare"}):
        dist = distribution(name)
        packages[name] = dist.version
        target = app / "licenses" / name
        for file in dist.files or []:
            parts = Path(str(file)).parts
            if ".." not in parts and "__pycache__" not in parts and Path(str(file)).suffix not in {".py", ".pyc", ".pyo", ".pyi"} and any(re.match(r"^(licen[cs]e|copying|copyright|notice|authors)", part, re.I) for part in parts):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = target / Path(*parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        target.mkdir(parents=True, exist_ok=True)
        (target / "METADATA.txt").write_text(dist.read_text("METADATA") or "", encoding="utf-8")
    if sys.platform == "linux":
        for package in ("python3.12", "python3-gi", "python3-cairo"):
            source = Path("/usr/share/doc") / package / "copyright"
            if not source.is_file():
                raise SystemExit(f"Required license is missing: {source}")
            target = app / "licenses" / package
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / "copyright")
    else:
        source = Path(sys.base_prefix) / "LICENSE.txt"
        if not source.is_file():
            raise SystemExit("Python LICENSE.txt is missing from the build interpreter")
        shutil.copy2(source, app / "licenses/PYTHON-LICENSE.txt")
    source = ROOT / "desktop/third-party/libvips"
    target = app / "licenses/libvips-runtime"
    target.mkdir(parents=True, exist_ok=True)
    for file in source.iterdir():
        if file.is_file():
            shutil.copy2(file, target / file.name)
    shutil.copytree(source / ("linux-x64" if sys.platform == "linux" else "win-x64"), target / "components")
    for name in ("SOURCE.txt", "LGPL-2.1"):
        shutil.copy2(ROOT / "desktop/third-party" / name, app / "licenses" / name)
    if sys.platform == "win32":
        shutil.copytree(ROOT / "desktop/third-party/webview2-sdk", app / "licenses/webview2-sdk")
    return packages


def linux_dependency_report():
    """Simulate current-host and empty-status dependency costs; never install packages."""
    try:
        import apt
    except ImportError:
        return {"depends": list(LINUX_DEPENDS), "cost_unavailable": "python3-apt is unavailable"}
    names = [item.split()[0] for item in LINUX_DEPENDS]
    cache = apt.Cache()
    report = {"depends": list(LINUX_DEPENDS), "cost_note": (
        "empty_status is the complete dependency closure with an empty dpkg status, not the "
        "incremental cost of an existing Ubuntu Desktop. A real machine normally shares many libraries. "
        "Download and installed sizes are package metadata estimates, excluding the application and caches.")}
    with tempfile.TemporaryDirectory(prefix="webcompare-dependency-cost-") as temp:
        status = Path(temp) / "status"
        status.touch()
        for label, extra in (("current_host", []), ("empty_status", ["-o", f"Dir::State::status={status}"])):
            result = run(["apt-get", "--simulate", "--no-install-recommends", "-o", "Debug::NoLocking=1",
                          *extra, "install", *names], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"})
            selected = sorted(set(re.findall(r"^Inst ([^ ]+)", result.stdout, re.M)))
            packages = [{"package": name, "version": cache[name].candidate.version,
                         "download_bytes": cache[name].candidate.size,
                         "installed_bytes": cache[name].candidate.installed_size} for name in selected]
            report[label] = {"package_count": len(packages),
                             "download_bytes": sum(item["download_bytes"] for item in packages),
                             "installed_bytes": sum(item["installed_bytes"] for item in packages), "packages": packages}
    return report


def desktop_entry(executable: str):
    return ("[Desktop Entry]\nType=Application\nName=WebCompare\nComment=Local image and video comparison\n"
            f"Exec={executable}\nIcon=webcompare-desktop\nTerminal=false\nCategories=Graphics;Photography;\n"
            "StartupWMClass=WebCompare\n")


def write_linux_launcher(path: Path, command: str):
    path.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


def write_control_launcher(app: Path):
    if sys.platform == "win32":
        (app / "webcompare-ctl.cmd").write_text(
            '@echo off\nsetlocal DisableDelayedExpansion\n"%~dp0WebCompare.exe" --ctl %*\nexit /b %ERRORLEVEL%\n',
            encoding="ascii", newline="\r\n")
    else:
        write_linux_launcher(app / "webcompare-ctl", '"$(dirname -- "$0")/webcompare-desktop" --ctl')


def create_deb(app: Path, output: Path, version: str, arch: str):
    with tempfile.TemporaryDirectory(prefix="webcompare-deb-") as temp:
        stage = Path(temp)
        shutil.copytree(app, stage / "opt/webcompare-desktop", symlinks=True)
        (stage / "opt/webcompare-desktop/.webcompare-install-kind").write_text("linux-deb\n", encoding="ascii")
        for directory in ("DEBIAN", "usr/bin", "usr/share/applications", "usr/share/icons/hicolor/256x256/apps"):
            (stage / directory).mkdir(parents=True, exist_ok=True)
        write_linux_launcher(stage / "usr/bin/webcompare-desktop", "/opt/webcompare-desktop/webcompare-desktop")
        write_linux_launcher(stage / "usr/bin/webcompare-ctl", "/opt/webcompare-desktop/webcompare-ctl")
        (stage / "usr/share/applications/webcompare-desktop.desktop").write_text(
            desktop_entry("/opt/webcompare-desktop/webcompare-desktop"), encoding="utf-8")
        shutil.copy2(ROOT / "desktop/assets/icon.png", stage / "usr/share/icons/hicolor/256x256/apps/webcompare-desktop.png")
        installed_kib = (sum(path.lstat().st_size for path in app.rglob("*") if path.is_file()) + 1023) // 1024
        (stage / "DEBIAN/control").write_text(
            f"Package: webcompare-desktop\nVersion: {version}\nArchitecture: {arch}\n"
            "Maintainer: fenghansen\nSection: graphics\nPriority: optional\n"
            f"Installed-Size: {installed_kib}\nDepends: {', '.join(LINUX_DEPENDS)}\n"
            "Description: Local image and video comparison desktop\n"
            " Includes Python and image libraries. Uses the system GTK and WebKitGTK engine.\n", encoding="utf-8")
        target = output / f"webcompare-desktop_{version}_{arch}.deb"
        run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", stage, target])
        return target


def prepare_webview2(app: Path):
    """Bundle the signed Evergreen bootstrapper; execution belongs to setup, not builds."""
    url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    cached = ROOT / ".runtime/native-prerequisites/MicrosoftEdgeWebview2Setup.exe"
    cached.parent.mkdir(parents=True, exist_ok=True)
    if not cached.is_file():
        pending = cached.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=60) as source, pending.open("wb") as target:
            shutil.copyfileobj(source, target)
        pending.replace(cached)
    verification = r'''$signature = Get-AuthenticodeSignature $env:WEBCOMPARE_WEBVIEW2_BOOTSTRAPPER
[pscustomobject]@{status=$signature.Status.ToString(); publisher=$signature.SignerCertificate.Subject; message=$signature.StatusMessage} | ConvertTo-Json -Compress
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Invalid Microsoft WebView2 bootstrapper signature' }
'''
    try:
        result = run([shutil.which("pwsh") or "powershell", "-NoProfile", "-NonInteractive", "-Command", verification],
                     env={**os.environ, "WEBCOMPARE_WEBVIEW2_BOOTSTRAPPER": str(cached)}, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        print("WebView2 bootstrapper signature verification failed:\n" + (error.stdout or "") + (error.stderr or ""), file=sys.stderr)
        raise
    signature = json.loads(result.stdout)
    target = app / "prerequisites/MicrosoftEdgeWebview2Setup.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached, target)
    return {"url": url, "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "signature": signature, "purpose": "Online per-user Evergreen installation when the shared Runtime is absent; the full offline Runtime is not bundled."}


def create_windows_installer(app: Path, output: Path, version: str):
    compiler = shutil.which("ISCC")
    if not compiler:
        candidate = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
        if candidate.is_file():
            compiler = str(candidate)
    if not compiler:
        raise SystemExit("Inno Setup 6 is required for the Windows installer; use --skip-installer for ZIP only")
    marker = app / ".webcompare-install-kind"
    marker.write_text("windows-setup\n", encoding="ascii")
    try:
        run([compiler, f"/DAppVersion={version}", f"/DAppSource={app}", f"/DArtifactDir={output}",
             ROOT / "desktop/installers/windows.iss"])
    finally:
        marker.unlink()
    return output / f"webcompare-desktop-{version}-windows-x64-setup.exe"


def use_system_crt(app: Path):
    """Remove collected Microsoft CRT files and undo wheel-specific CRT import names."""
    import pefile

    def imports(executable):
        executable.parse_data_directories(directories=[1, 13])
        for entry in getattr(executable, "DIRECTORY_ENTRY_IMPORT", ()):
            yield entry.dll, entry.struct.Name
        for entry in getattr(executable, "DIRECTORY_ENTRY_DELAY_IMPORT", ()):
            yield entry.dll, entry.struct.szName

    changes = {}
    removed = []
    for path in sorted(app.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".dll", ".pyd", ".exe"}:
            continue
        name = path.relative_to(app).as_posix()
        if SYSTEM_CRT.fullmatch(path.name):
            path.unlink()
            removed.append(name)
            continue
        original = path.read_bytes()
        pe = pefile.PE(data=original, fast_load=True)
        renamed = {}
        for dll, rva in imports(pe):
            match = SYSTEM_CRT.fullmatch(dll.decode("ascii"))
            if match and match[0].lower() != f"{match['name'].lower()}.dll":
                renamed[pe.get_offset_from_rva(rva)] = (dll, f"{match['name'].lower()}.dll".encode("ascii"))
        if not renamed:
            continue
        if any(pe.OPTIONAL_HEADER.DATA_DIRECTORY[index].Size for index in (4, 11)):
            raise SystemExit(f"Unexpected signed or bound PE with a private CRT import: {name}")
        changed = bytearray(original)
        for offset, (old, new) in renamed.items():
            if original[offset:offset + len(old) + 1] != old + b"\0" or len(new) >= len(old):
                raise SystemExit(f"Could not locate the CRT import string in {name}")
            changed[offset:offset + len(old)] = new.ljust(len(old), b"\0")
        verify = pefile.PE(data=bytes(changed), fast_load=True)
        actual = {dll for dll, _ in imports(verify)}
        if any(old in actual or new not in actual for old, new in renamed.values()):
            raise SystemExit(f"Could not use standard system CRT imports in {name}")
        if pe.OPTIONAL_HEADER.CheckSum:
            struct.pack_into("<I", changed, pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum"),
                             verify.generate_checksum())
        path.write_bytes(changed)
        changes[name] = {old.decode("ascii"): new.decode("ascii") for old, new in renamed.values()}
    return {"removed_app_local_files": removed, "restored_standard_imports": changes}


def audit_bundle(app: Path):
    forbidden = {".worklog", ".runtime", ".git", "node_modules", "state", "__pycache__"}
    for path in app.rglob("*"):
        parts = path.relative_to(app).parts
        if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
            raise SystemExit(f"Bundle symlink escapes its directory: {path.relative_to(app)}")
        if forbidden.intersection(parts) or path.name in {".env", "one-api.db"}:
            raise SystemExit(f"Private/build-only path found in bundle: {path.relative_to(app)}")
        if sys.platform == "linux" and re.match(r"lib(webkit|javascriptcore|gtk-3|Qt[56]|cef)", path.name, re.I):
            raise SystemExit(f"Unexpected bundled browser/widget engine: {path.name}")
        if sys.platform == "win32" and SYSTEM_CRT.fullmatch(path.name):
            raise SystemExit(f"Unexpected app-local Microsoft VC++ runtime: {path.relative_to(app)}")
    python_runtimes = list(app.rglob("python312.dll")) if sys.platform == "win32" else list(app.rglob("libpython3.12.so.1.0"))
    if len(python_runtimes) != 1:
        raise SystemExit(f"Expected exactly one Python runtime, found {len(python_runtimes)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/native")
    parser.add_argument("--webkit-sysroot", type=Path, default=os.environ.get("WEBCOMPARE_WEBKIT_SYSROOT"))
    parser.add_argument("--skip-installer", action="store_true", help="Windows: only create the portable ZIP")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or sys.platform not in {"linux", "win32"}:
        parser.error("Build on Linux or Windows with Python 3.12")
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("The current desktop packages target x86_64")
    if not (ROOT / "frontend/dist/index.html").is_file():
        parser.error("frontend/dist is missing; run npm ci and npm run build in frontend first")
    source = source_provenance()
    ubuntu_sources = []
    if sys.platform == "linux":
        ubuntu_sources = ubuntu_python_sources()
        compile_webkit(args.webkit_sysroot)
    static = ROOT / "webcompare/static"
    if static.exists():
        shutil.rmtree(static)
    shutil.copytree(ROOT / "frontend/dist", static, ignore=shutil.ignore_patterns(".webcompare-build.json"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = ROOT / ".runtime/native-build"
    env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(ROOT / ".runtime/native-pyinstaller-cache")}
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", output / "unpacked",
         "--workpath", work, ROOT / "desktop/native.spec"], cwd=ROOT, env=env)
    app = output / "unpacked/WebCompare"
    runtime_packages = json.loads((work / "native/runtime-distributions.json").read_text(encoding="utf-8"))
    if sys.platform == "linux":
        runtime_packages += ["pygobject", "pycairo"]
    packages = copy_licenses(app, runtime_packages)
    replaceable = ["paramiko"] if sys.platform == "win32" else ["paramiko", "gi", "cairo"]
    for name in replaceable:
        if not (app / "_internal" / name / "__init__.py").is_file():
            raise SystemExit(f"Expected editable Python sources in the bundle: {name}")
    shutil.copy2(ROOT / "desktop/assets/icon.png", app / "icon.png")
    write_control_launcher(app)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    corresponding_source = f"webcompare-desktop-{version}-corresponding-source.tar.gz"
    source_note = f"Licenses and library replacement are described in licenses/SOURCE.txt.\nCorresponding source is available alongside this release as {corresponding_source}.\n{RELEASES}\n"
    crt = use_system_crt(app) if sys.platform == "win32" else None
    bootstrapper = prepare_webview2(app) if sys.platform == "win32" else None
    system_deps = linux_dependency_report() if sys.platform == "linux" else {
        "required": ["Windows 10/11 x64", ".NET Framework >= 4.6.2", "Microsoft Edge WebView2 Evergreen Runtime",
                     f"Microsoft Visual C++ v14 Redistributable (x64) >= {VC_MINIMUM}"],
        "visual_cpp_runtime": {"download": VC_REDIST, "minimum_version": VC_MINIMUM, **crt},
        "cost_note": "WebView2 is shared and installed separately when absent. Its full engine is not included in the app ZIP/setup.",
        "download": "https://developer.microsoft.com/microsoft-edge/webview2/", "bootstrapper": bootstrapper}
    if sys.platform == "linux":
        shutil.copy2(ROOT / "desktop/installers/linux-install-menu.sh", app / "install-menu.sh")
        (app / "install-menu.sh").chmod(0o755)
        excluded_path = work / "native/system-libraries.json"
        system_deps["excluded_system_libraries"] = json.loads(excluded_path.read_text(encoding="utf-8"))
        (app / "README.txt").write_text(
            "WebCompare desktop - Ubuntu 24.04 x86_64\n\n"
            "Run ./webcompare-desktop. No Python or Node.js installation is needed.\n"
            "On a graphical Linux desktop with GTK and WebKitGTK available, unpack and run without sudo.\n"
            "On a headless server, use the Python application with SSH port forwarding.\n"
            "With administrator access, apt install ./webcompare-desktop_*.deb also installs missing system libraries.\n"
            "For automation, run ./webcompare-ctl --help (or webcompare-ctl --help after .deb installation).\n"
            "See system-dependencies.json for the portable runtime requirements.\n"
            "Run ./install-menu.sh to add this folder to the current user's application menu.\n"
            "Keep this folder in place afterwards. Remove its menu entry manually to uninstall.\n"
            "System WebKit/GTK must be updated through Ubuntu security updates.\n\n"
            + source_note, encoding="utf-8")
    else:
        (app / "README.txt").write_text(
            "WebCompare desktop - Windows 10/11 x64\n\n"
            "Extract the entire ZIP to a new folder.\n"
            f"Install the current Microsoft Visual C++ v14 Redistributable (x64), version {VC_MINIMUM} or newer.\n"
            f"Download the installer directly from Microsoft at {VC_REDIST}\n"
            "Run WebCompare.exe. No Python or Node.js installation is needed.\n"
            "For automation, run .\\webcompare-ctl.cmd --help from this folder.\n"
            "Setup also installs the command in %LOCALAPPDATA%\\Programs\\WebCompare by default.\n"
            "Requires .NET Framework 4.6.2+ and Microsoft Edge WebView2 Evergreen Runtime.\n"
            "If absent, install the Runtime from https://developer.microsoft.com/microsoft-edge/webview2/\n"
            "The setup installer installs WebView2 online per user when missing and creates shortcuts.\n"
            "The included prerequisites/MicrosoftEdgeWebview2Setup.exe is only an online bootstrapper.\n"
            "Offline machines need the separate Microsoft Evergreen Standalone Runtime installer.\n"
            "The app uses WebView2.\n\n"
            + source_note, encoding="utf-8")
    (app / "system-dependencies.json").write_text(json.dumps(system_deps, indent=2), encoding="utf-8")
    audit_bundle(app)
    manifest = {"version": version, "platform": sys.platform, "architecture": "x64", "python": platform.python_version(),
                "source": source,
                "corresponding_source": {"asset": corresponding_source, "releases": RELEASES},
                "replaceable_python_packages": replaceable,
                "ubuntu_python_sources": ubuntu_sources,
                "project_license": "licenses/WebCompare/LICENSE" if (ROOT / "LICENSE").is_file() else None,
                "packages": packages, "single_python_runtime": True,
                "files": {path.relative_to(app).as_posix(): {"bytes": path.lstat().st_size, "symlink": path.readlink().as_posix() if path.is_symlink() else None,
                          "sha256": None if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in sorted(app.rglob("*")) if path.is_file()}}
    (app / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    artifacts = []
    if sys.platform == "linux":
        archive = output / f"webcompare-desktop-{version}-linux-x64.tar.gz"
        with tarfile.open(archive, "w:gz", compresslevel=9) as target:
            target.add(app, arcname=f"webcompare-desktop-{version}")
        artifacts.extend((archive, create_deb(app, output, version, "amd64")))
    else:
        archive = output / f"webcompare-desktop-{version}-windows-x64.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    target.write(path, Path(f"webcompare-desktop-{version}") / path.relative_to(app))
        artifacts.append(archive)
        if not args.skip_installer:
            artifacts.append(create_windows_installer(app, output, version))
    summary = {"unpacked_bytes": sum(path.lstat().st_size for path in app.rglob("*") if path.is_file()),
               "artifacts": {path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in artifacts}, "system_dependencies": system_deps}
    (output / "build-report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "SHA256SUMS").write_text("".join(f"{item['sha256']}  {name}\n" for name, item in summary["artifacts"].items()), encoding="utf-8")
    print(json.dumps({"output": str(output), "unpacked_bytes": summary["unpacked_bytes"], "artifacts": summary["artifacts"]}, indent=2))


if __name__ == "__main__":
    main()
