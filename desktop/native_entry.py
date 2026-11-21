"""One frozen runtime for the native window, backend and spawned media workers."""
import multiprocessing

multiprocessing.freeze_support()

import os
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _windows_stdio(*, control=False):
    # A windowed Windows executable still receives redirected parent pipe handles.
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        ctypes.windll.kernel32.GetStdHandle.restype = wintypes.HANDLE
        ctypes.windll.kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        streams = (("stdout", -11, "w"), ("stderr", -12, "w"))
        if control:
            # Windows GUI launchers may have redirected streams, a parent console, or neither.
            invalid = (None, 0, wintypes.HANDLE(-1).value)
            if any(getattr(sys, name) is None and ctypes.windll.kernel32.GetStdHandle(identifier) in invalid
                   for name, identifier, _mode in streams):
                ctypes.windll.kernel32.AttachConsole(wintypes.DWORD(-1))
        else:
            streams = (("stdin", -10, "r"), *streams)
        for name, identifier, mode in streams:
            if getattr(sys, name) is None:
                handle = ctypes.windll.kernel32.GetStdHandle(identifier)
                if control and handle in (None, 0, wintypes.HANDLE(-1).value):
                    setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))
                    continue
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == "r" else os.O_WRONLY)
                setattr(sys, name, os.fdopen(descriptor, mode, encoding="utf-8", buffering=1))


def _updated_control(arguments):
    if not getattr(sys, "frozen", False):
        return None
    import argparse

    from webcompare.native_desktop import default_user_data_dir, desktop_command
    from webcompare.updates import launch_updated_process

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("--target", choices=("desktop", "web"), default="desktop")
    parser.add_argument("--data-dir", "--user-data-dir", type=Path)
    try:
        options, _ = parser.parse_known_args(arguments)
    except argparse.ArgumentError:
        return None
    if options.target == "web":
        return None
    state = (options.data_dir or default_user_data_dir()).expanduser().resolve()
    command = desktop_command(state)
    if command == [sys.executable]:
        return None
    try:
        return launch_updated_process([*command, "--ctl", *arguments], keep_streams=True).wait()
    except OSError:
        return None


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["--ctl"]:
        _windows_stdio(control=True)
        updated = _updated_control(arguments[1:])
        if updated is not None:
            raise SystemExit(updated)
        from webcompare.control import main
        raise SystemExit(main(arguments[1:]))
    if arguments[:1] == ["--backend"]:
        _windows_stdio()
        from webcompare.desktop import main
        raise SystemExit(main(arguments[1:]))
    from webcompare.native_desktop import main
    raise SystemExit(main(arguments))
