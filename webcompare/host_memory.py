"""读取系统当前可供应用使用的物理内存。"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path


MEMORY_RESERVE_BYTES = 512 * 1024**2


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def available_memory_bytes() -> int | None:
    try:
        if sys.platform == "linux":
            with Path("/proc/meminfo").open(encoding="ascii") as meminfo:
                for line in meminfo:
                    if line.startswith("MemAvailable:"):
                        _, value, unit = line.split()
                        kib = int(value)
                        return kib * 1024 if unit == "kB" and kib >= 0 else None
        elif sys.platform == "win32":
            status = _MemoryStatusEx(dwLength=ctypes.sizeof(_MemoryStatusEx))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullAvailPhys
    except (AttributeError, OSError, ValueError):
        return None
    return None
