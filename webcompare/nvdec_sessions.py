"""Short-lived NVDEC readers for the local workbench. Each pull returns one frame."""
from __future__ import annotations

import asyncio
import os
import struct
import time
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Lock

from starlette.concurrency import run_in_threadpool


@dataclass
class _Slot:
    lock: Lock = field(default_factory=Lock)
    reader: object | None = None
    source: tuple | None = None
    last_pts: int | None = None
    used: float = field(default_factory=time.monotonic)
    busy: int = 0
    discarded: bool = False

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.last_pts = None


class NVDECSessions:
    def __init__(self):
        self.slots = {}
        self.lock = Lock()
        self.reaper = None

    async def frame(self, key, path, revision, pts, previous, preview_width=None):
        if self.reaper is None:
            self.reaper = asyncio.create_task(self._reap())
        return await run_in_threadpool(self._frame, key, path, revision, pts, previous, preview_width)

    def _frame(self, key, path, revision, pts, previous, preview_width=None):
        from .nvdec import NVDECReader, NVDECError
        with self.lock:
            slot = self.slots.get(key)
            if slot is None:
                slot = self.slots[key] = _Slot()
            slot.busy += 1
        try:
            with slot.lock:
                try:
                    if slot.discarded:
                        raise NVDECError("视频解码器已关闭")
                    source = (path, revision)
                    reuse = slot.reader is not None and slot.source == source and slot.reader.accepts_preview_width(preview_width)
                    if reuse and slot.last_pts == pts:
                        frame = slot.reader.current_frame(preview_width)
                    else:
                        if not reuse or previous is None or previous != slot.last_pts:
                            slot.close()
                            slot.source = source
                            slot.reader = NVDECReader(path, pts, os.getenv('WEBCOMPARE_NVDEC_DEVICE') or None,
                                                      preview_width=preview_width)
                        frame = slot.reader.next_frame(preview_width)
                    if frame is None or struct.unpack_from('<q', frame)[0] != pts:
                        raise NVDECError('硬解输出时间戳与所选帧不一致')
                    slot.last_pts = pts
                    return frame
                except Exception:
                    slot.close()
                    raise
        finally:
            with self.lock:
                slot.used = time.monotonic()
                slot.busy -= 1

    @staticmethod
    def _release(slot):
        with slot.lock:
            slot.discarded = True
            slot.close()

    async def discard(self, key):
        with self.lock:
            slot = self.slots.pop(key, None)
        if slot is not None:
            await run_in_threadpool(self._release, slot)

    async def _reap(self):
        while True:
            await asyncio.sleep(5)
            with self.lock:
                now = time.monotonic()
                stale = [self.slots.pop(k) for k, v in list(self.slots.items()) if not v.busy and now-v.used > 15]
            for slot in stale:
                await run_in_threadpool(self._release, slot)

    async def close(self):
        if self.reaper is not None:
            self.reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self.reaper
        for key in list(self.slots):
            await self.discard(key)
