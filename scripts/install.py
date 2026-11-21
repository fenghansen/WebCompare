"""用独立环境安装 WebCompare，并注册当前用户的命令与桌面快捷方式。"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parent.parent


def write_launcher(directory: Path, python: Path, *, windows: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for command, module in (("webcompare", "webcompare.cli"), ("webcompare-ctl", "webcompare.control")):
        if windows:
            path = directory / f"{command}.cmd"
            path.write_text(f'@echo off\nchcp 65001 >nul\n"{str(python).replace("%", "%%")}" -m {module} %*\n', encoding="utf-8")
        else:
            path = directory / command
            path.write_text(f'#!/bin/sh\nexec {shlex.quote(str(python))} -m {module} "$@"\n', encoding="utf-8")
            path.chmod(0o755)
    return directory / ("webcompare.cmd" if windows else "webcompare")


def register_path(directory: Path) -> None:
    """仅更新当前用户设置；已打开的终端继续使用原 PATH。"""
    if os.name == "nt":
        import ctypes
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
            existing = {os.path.normcase(os.path.expandvars(part)).rstrip("\\/") for part in value.split(";")}
            if os.path.normcase(str(directory)).rstrip("\\/") not in existing:
                winreg.SetValueEx(key, "Path", 0, kind, value.rstrip(";") + (";" if value else "") + str(directory))
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"), 2, 2000, ctypes.byref(result))
        return
    if str(directory) in os.environ.get("PATH", "").split(os.pathsep):
        return
    line = f"export PATH={shlex.quote(str(directory))}:\"$PATH\""
    profiles = [Path.home() / ".profile"]
    shell = Path(os.getenv("SHELL", "/bin/bash")).name
    if shell in {"bash", "zsh"}:
        profiles.append(Path.home() / (".bashrc" if shell == "bash" else ".zshrc"))
    for profile in profiles:
        content = profile.read_text(encoding="utf-8") if profile.exists() else ""
        if line not in content.splitlines():
            with profile.open("a", encoding="utf-8") as stream:
                stream.write(f"\n# WebCompare command\n{line}\n")


def desktop_shortcut(python: Path, directory: Path | None = None) -> Path:
    if os.name == "nt":
        # 经 Python/cmd 启动 Windows PowerShell 时，不继承 PowerShell 7 的模块路径。
        env = {name: value for name, value in os.environ.items() if name.casefold() != "psmodulepath"}
        env.update(WEBCOMPARE_SHORTCUT_PYTHON=str(python),
                   WEBCOMPARE_SHORTCUT_DIR=str(directory) if directory else "")
        # WScript.Shell.CreateShortcut 会按系统 ANSI 代码页转换文件名；直接使用 Unicode COM 接口。
        script = """[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class WebCompareShortcut
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    // IShellLinkW vtable order from ShObjIdl_core.h; HRESULT failures become COMException.
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr findData, uint flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath, int size, out int iconIndex);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconIndex);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public static void Save(string path, string target)
    {
        object shortcut = new ShellLink();
        try
        {
            IShellLinkW link = (IShellLinkW)shortcut;
            link.SetPath(target);
            link.SetArguments("-m webcompare.cli");
            link.SetDescription("WebCompare");
            ((IPersistFile)shortcut).Save(path, true);
        }
        finally { Marshal.FinalReleaseComObject(shortcut); }
    }
}
'@
$desktop = $env:WEBCOMPARE_SHORTCUT_DIR
if (!$desktop) { $desktop = [Environment]::GetFolderPath('Desktop') }
$desktop = [IO.Directory]::CreateDirectory($desktop).FullName
$path = [IO.Path]::Combine($desktop, 'WebCompare.lnk')
[WebCompareShortcut]::Save($path, $env:WEBCOMPARE_SHORTCUT_PYTHON)
[Console]::WriteLine($path)
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        output = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
                                env=env, check=True, text=True, encoding="utf-8", errors="replace", capture_output=True)
        return Path(output.stdout.strip())
    if directory is None:
        xdg = shutil.which("xdg-user-dir")
        if xdg:
            result = subprocess.run([xdg, "DESKTOP"], capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip():
                directory = Path(result.stdout.strip())
        directory = directory or Path.home() / "Desktop"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "WebCompare.desktop"
    # Desktop Entry 的 Exec 有独立转义规则，不能使用 shell 单引号。
    executable = '"' + ''.join('\\' + char if char in '\\"`$' else '%%' if char == '%' else char for char in str(python)) + '"'
    executable = executable.replace("\\", "\\\\")
    path.write_text(f'[Desktop Entry]\nType=Application\nName=WebCompare\nComment=Image and video comparison\nExec={executable} -m webcompare.cli\nTerminal=true\nIcon=applications-graphics\nCategories=Graphics;\n', encoding="utf-8")
    path.chmod(0o755)
    gio = shutil.which("gio")
    if gio:
        subprocess.run([gio, "set", str(path), "metadata::trusted", "true"], capture_output=True, check=False)
    return path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-dir", type=Path, help="命令目录；默认当前用户的 bin")
    parser.add_argument("--desktop-dir", type=Path, help="桌面快捷方式目录")
    parser.add_argument("--no-path", action="store_true", help="不登记用户 PATH")
    parser.add_argument("--no-desktop", action="store_true", help="不创建桌面快捷方式（适合服务器）")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("需要 Python 3.12 或更新版本；可用 WEBCOMPARE_PYTHON 指定解释器")
    environment = ROOT / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if not python.is_file():
            if environment.exists() and any(environment.iterdir()):
                raise RuntimeError(".venv 已存在但不是可用环境，请检查该目录后重试")
            print("正在创建项目独立 Python 环境……", flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        subprocess.run([str(python), "-c", "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ required'"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-c", str(ROOT / "requirements.lock"), "-e", str(ROOT)], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        directory = args.command_dir or (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "WebCompare/bin"
            if os.name == "nt" else Path.home() / ".local/bin"
        )
        directory = directory.expanduser().resolve()
        launcher = write_launcher(directory, python, windows=os.name == "nt")
        if not args.no_path:
            register_path(directory)
        shortcut = None
        if not args.no_desktop:
            try:
                shortcut = desktop_shortcut(python, args.desktop_dir)
            except (OSError, subprocess.CalledProcessError) as error:
                detail = (error.stderr or error.stdout or "").strip() if isinstance(error, subprocess.CalledProcessError) else ""
                print(f"桌面快捷方式未创建：{detail or error}；可直接运行 start 脚本。", file=sys.stderr)
        print(f"安装完成。命令入口：{launcher}")
        if not args.no_path:
            print("重新打开终端后可使用 webcompare 和 webcompare-ctl。")
        print(f"也可直接运行：{ROOT / ('start.bat' if os.name == 'nt' else 'start.sh')}")
        if shortcut:
            print(f"桌面快捷方式：{shortcut}")
        print("默认状态目录沿用 ~/.local/share/webcompare；自定义实例继续设置原 WEBCOMPARE_DATA_DIR。")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"安装失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
