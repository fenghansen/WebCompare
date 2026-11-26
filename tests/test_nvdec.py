from __future__ import annotations

import struct
import sys
from fractions import Fraction
from types import SimpleNamespace

import pytest

from webcompare import nvdec


class Plane(bytearray):
    def __init__(self, data: bytes, stride: int):
        super().__init__(data)
        self.line_size = stride


def frame(pts: int, **overrides):
    # 3×3 Y，每行 3 字节。交错 UV 是两行，每行 4 字节。两平面都有填充。
    args = dict(pts=pts, time_base=Fraction(1, 1000), width=3, height=3,
                rotation=0, side_data={}, format=SimpleNamespace(name="nv12"),
                color_range=0, colorspace=2, color_primaries=2, color_trc=2,
                planes=(Plane(b"abc.dfg.hij.", 4), Plane(b"klmn....opqr....", 8)))
    return SimpleNamespace(**(args | overrides))


class Container:
    def __init__(self, frames, *, hw=True, ratio=Fraction(1)):
        codec = SimpleNamespace(is_hwaccel=hw, width=3, height=3, coded_width=4, coded_height=4)
        stream = SimpleNamespace(codec_context=codec, time_base=Fraction(1, 1000),
                                 sample_aspect_ratio=ratio, start_time=0)
        self.streams = SimpleNamespace(video=[stream])
        self.frames = frames
        self.seek_calls = []
        self.reads = 0
        self.closes = 0

    def seek(self, offset, *, backward, any_frame, stream):
        self.seek_calls.append((offset, backward, any_frame, stream))

    def decode(self, stream):
        assert stream is self.streams.video[0]
        for decoded in self.frames:
            self.reads += 1
            yield decoded

    def close(self):
        self.closes += 1


@pytest.fixture
def connect(monkeypatch, tmp_path):
    monkeypatch.setattr(nvdec, "available_memory_bytes", lambda: None)

    def reader(container, start_us=0, *, preview_width=None):
        def open_container(path, device):
            assert path == tmp_path / "fixture.mp4" and device == "1"
            return container

        monkeypatch.setattr(nvdec, "_open_nvdec", open_container)
        return nvdec.NVDECReader(tmp_path / "fixture.mp4", start_us, "1", preview_width=preview_width)

    return reader


def test_odd_nv12_converts_to_tight_independent_i420_frames(connect):
    changed = (Plane(b"ABC.DFG.HIJ.", 4), Plane(b"KLMN....OPQR....", 8))
    source = Container([frame(10), frame(11, planes=changed)])
    reader = connect(source)
    one = reader.next_frame()
    two = reader.next_frame()
    assert one.readonly and two.readonly
    assert nvdec.FRAME_HEADER.size == 16
    assert bytes(one) == struct.pack("<qHHI", 10_000, 3, 3, 17) + b"abcdfghijkmoqlnpr"
    assert bytes(two) == struct.pack("<qHHI", 11_000, 3, 3, 17) + b"ABCDFGHIJKMOQLNPR"
    assert reader.next_frame() is None and source.closes == 1
    reader.close()
    assert source.closes == 1


def test_preview_can_repack_current_pts_and_restore_full_size(connect):
    calls = []

    def reformat(**options):
        calls.append(options)
        return frame(10, width=2, height=2, planes=(Plane(b"ab..cd..", 4), Plane(b"x...", 4), Plane(b"y...", 4)))

    source = Container([frame(10, color_range=2, reformat=reformat), frame(11, color_range=2)])
    reader = connect(source)
    assert reader.current_frame() is None
    assert reader.accepts_preview_width(None) and not reader.accepts_preview_width(2)
    preview = reader.next_frame(2)
    exact = reader.current_frame()
    assert source.reads == 1
    assert calls == [{"width": 2, "height": 2, "format": "yuv420p", "interpolation": "AREA",
                      "src_color_range": 2, "dst_color_range": 2, "threads": 1}]
    assert bytes(preview) == struct.pack("<qHHI", 10_000, 2, 2, 6) + b"abcdxy"
    assert bytes(exact) == struct.pack("<qHHI", 10_000, 3, 3, 17) + b"abcdfghijkmoqlnpr"
    assert nvdec.FRAME_HEADER.unpack_from(reader.next_frame(640))[:3] == (11_000, 3, 3)
    assert source.reads == 2
    reader.close()
    assert reader.current_frame() is None


def test_hardware_preview_corrects_odd_size_and_only_accepts_same_width(connect, monkeypatch):
    def correct_size(**options):
        assert (options["width"], options["height"]) == (1, 1)
        return frame(10, width=1, height=1, planes=(Plane(b"a", 1), Plane(b"u", 1), Plane(b"v", 1)))

    scaled = Container([frame(10, width=2, height=2, reformat=correct_size)])
    monkeypatch.setattr(nvdec, "_open_resized_nvdec", lambda path, device, width: (
        scaled, scaled.streams.video[0].codec_context, (2, 2),
    ))
    monkeypatch.setattr(nvdec, "_decode_resized", lambda container, stream, codec: container.decode(stream))
    reader = connect(Container([]), preview_width=1)
    try:
        assert reader.accepts_preview_width(1)
        assert not reader.accepts_preview_width(2) and not reader.accepts_preview_width(None)
        one = reader.next_frame(1)
        assert scaled.reads == 1
        assert bytes(one) == bytes(reader.current_frame(1)) == struct.pack("<qHHI", 10_000, 1, 1, 3) + b"auv"
        with pytest.raises(nvdec.NVDECError, match="重新打开"):
            reader.current_frame()
    finally:
        reader.close()
    assert scaled.closes == 1


def test_failed_hardware_resize_resumes_area_at_next_pts(connect, monkeypatch):
    def resize(**_options):
        return frame(11, width=2, height=2, planes=(Plane(b"abcd", 2), Plane(b"u", 1), Plane(b"v", 1)))

    scaled = Container([
        frame(10, width=2, height=2, planes=(Plane(b"abcd", 2), Plane(b"uv", 2))),
        frame(11, width=3, height=3),
    ])
    normal = Container([frame(10), frame(11, reformat=resize)])
    monkeypatch.setattr(nvdec, "_open_resized_nvdec", lambda path, device, width: (
        scaled, scaled.streams.video[0].codec_context, (2, 2),
    ))
    monkeypatch.setattr(nvdec, "_decode_resized", lambda container, stream, codec: container.decode(stream))
    reader = connect(normal, preview_width=2)
    try:
        assert nvdec.FRAME_HEADER.unpack_from(reader.next_frame(2)) == (10_000, 2, 2, 6)
        assert nvdec.FRAME_HEADER.unpack_from(reader.next_frame(2)) == (11_000, 2, 2, 6)
        assert scaled.closes == 1 and normal.reads == 2
        assert normal.seek_calls == [(10, True, False, normal.streams.video[0])]
        assert reader.accepts_preview_width(None)
        assert nvdec.FRAME_HEADER.unpack_from(reader.current_frame()) == (11_000, 3, 3, 17)
    finally:
        reader.close()


def test_open_requires_hardware_and_only_authorizes_video_demuxers(monkeypatch, tmp_path):
    calls = []

    def hwaccel(*args, **kwargs):
        calls.append((args, kwargs))
        return "cuda-context"

    def open_container(*args, **kwargs):
        calls.append((args, kwargs))
        return "container"

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=open_container, FFmpegError=RuntimeError))
    monkeypatch.setitem(sys.modules, "av.codec.hwaccel", SimpleNamespace(HWAccel=hwaccel))
    path = tmp_path / "fixture.mp4"
    assert nvdec._open_nvdec(path, "1") == "container"
    assert calls == [(('cuda',), {"device": "1", "allow_software_fallback": False,
                                 "options": {"primary_ctx": "1"}}),
                     ((str(path),), {"mode": "r", "options": {"protocol_whitelist": "file",
                                                              "format_whitelist": "mov,matroska,webm"},
                                     "hwaccel": "cuda-context"})]


def test_primary_cuda_context_failure_keeps_nvdec_available(monkeypatch, tmp_path):
    class AVError(Exception):
        pass

    contexts = []

    def open_input(path, hwaccel):
        contexts.append(hwaccel)
        if hwaccel == "primary":
            raise AVError("incompatible primary context")
        return "isolated-container"

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(FFmpegError=AVError))
    monkeypatch.setattr(nvdec, "_cuda", lambda device, *, primary=True: "primary" if primary else "isolated")
    monkeypatch.setattr(nvdec, "_open_input", open_input)
    assert nvdec._open_nvdec(tmp_path / "fixture.mp4", "1") == "isolated-container"
    assert contexts == ["primary", "isolated"]


def test_seek_uses_container_time_base_and_skips_frames_before_target(connect):
    source = Container([frame(99), frame(100), frame(101)])
    reader = connect(source, start_us=100_001)
    try:
        assert source.seek_calls == [(100, True, False, source.streams.video[0])]
        packet = reader.next_frame()
        assert nvdec.FRAME_HEADER.unpack_from(packet)[0] == 101_000
        assert source.reads == 3
    finally:
        reader.close()


def test_presentation_timestamps_use_browser_rounding_and_reject_duplicates(connect):
    tick = Fraction(1, 2_000_000)
    source = Container([frame(3, time_base=tick), frame(4, time_base=tick)])
    reader = connect(source)
    assert nvdec.FRAME_HEADER.unpack_from(reader.next_frame())[0] == 2
    with pytest.raises(nvdec.NVDECError, match="重复或逆序"):
        reader.next_frame()
    assert source.closes == 1


@pytest.mark.parametrize("overrides,message", [
    ({"rotation": 90}, "显示变换"),
    ({"side_data": {"DISPLAYMATRIX": struct.pack("=9i", -(1 << 16), 0, 0, 0, 1 << 16, 0, 0, 0, 1 << 30)}}, "显示变换"),
    ({"format": SimpleNamespace(name="p010le")}, "8-bit NV12"),
])
def test_unusable_hardware_output_closes_decoder(connect, overrides, message):
    source = Container([frame(0, **overrides)])
    reader = connect(source)
    with pytest.raises(nvdec.NVDECError, match=message):
        reader.next_frame()
    assert source.closes == 1


@pytest.mark.parametrize("options,message", [
    ({"hw": False}, "未启用 NVIDIA 硬件解码"),
    ({"ratio": Fraction(4, 3)}, "非方形像素"),
])
def test_incompatible_stream_closes_failed_initialization(connect, options, message):
    source = Container([], **options)
    with pytest.raises(nvdec.NVDECError, match=message):
        connect(source)
    assert source.closes == 1


def test_memory_block_precedes_decode_and_allows_retry(monkeypatch, connect):
    source = Container([frame(0)])
    reader = connect(source)
    try:
        total = 512 * 1024**2 + reader._allocation
        monkeypatch.setattr(nvdec, "available_memory_bytes", lambda: total - 1)
        with pytest.raises(nvdec.NVDECMemoryError, match="512 MiB"):
            reader.next_frame()
        assert source.reads == 0
        monkeypatch.setattr(nvdec, "available_memory_bytes", lambda: total)
        assert nvdec.FRAME_HEADER.unpack_from(reader.next_frame())[:3] == (0, 3, 3)
    finally:
        reader.close()
