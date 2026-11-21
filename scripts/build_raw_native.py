"""Build the optional package-local RAW10 kernel; no Python/NumPy SDK required."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.getenv("CC", ""))
    args = parser.parse_args()
    cc = shlex.split(args.cc) if args.cc else [shutil.which("cc") or shutil.which("gcc") or shutil.which("clang") or ""]
    if not cc[0]:
        raise SystemExit("No C compiler; NumPy fallback remains available. Install a C compiler to enable acceleration.")
    root = Path(__file__).resolve().parents[1]
    folder = root / "webcompare/native"
    suffix = ".dll" if os.name == "nt" else ".dylib" if sys.platform == "darwin" else ".so"
    output = folder / ("_raw10" + suffix)
    temporary = output.with_name(output.name + ".building")
    command = cc + ["-O3", "-std=c99", "-shared"]
    if os.name != "nt":
        command += ["-fPIC"]
    command += [str(folder / "raw10.c"), "-o", str(temporary)]
    try:
        subprocess.run(command, check=True)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(output)


if __name__ == "__main__":
    main()
