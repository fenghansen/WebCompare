#!/usr/bin/env python3
"""Create deterministic, synthetic image sequences for WebCompare demos."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


MAX_PIXELS = 24_000_000


def _chart(frame: int) -> Image.Image:
    """A single optical test chart shared by every method at this frame."""
    paper = "#f6f2df"
    ink = "#172d32"
    image = Image.new("RGB", (800, 480), paper)
    draw = ImageDraw.Draw(image)
    small = ImageFont.load_default(size=14)
    title = ImageFont.load_default(size=26)
    draw.rectangle((0, 0, 799, 59), fill=ink)
    draw.text((22, 13), "OPTICAL TEST CHART", fill=paper, font=title)
    draw.text((648, 23), f"FRAME   {frame:04d}", fill="#93dfc8", font=small)

    # A Siemens star exposes blur at different spatial frequencies and angles.
    draw.rectangle((17, 73, 359, 417), fill="#fbf9ee", outline=ink)
    box = (39, 95, 337, 393)
    phase = (frame - 1) % 10
    for angle in range(0, 360, 10):
        draw.pieslice(box, angle + phase, angle + phase + 5, fill=ink)
    draw.ellipse(box, outline=ink, width=2)
    draw.ellipse((180, 236, 196, 252), fill="#ff663b")
    draw.text((18, 431), "01   RADIAL RESOLUTION", fill=ink, font=small)

    # Saturated patches, fine line pairs and a neutral ramp reveal different losses.
    colors = ("#d8513d", "#fa8835", "#f8c449", "#e6df83", "#80b866", "#399b76",
              "#186e87", "#447fbd", "#7376bf", "#b571a4", "#d59282", "#af7146")
    for index, color in enumerate(colors):
        x, y = 380 + (index % 6) * 67, 73 + (index // 6) * 51
        draw.rectangle((x, y, x + 62, y + 46), fill=color)
    draw.text((380, 177), "02   CHROMA", fill=ink, font=small)

    for i, spacing in enumerate((15, 10, 6, 4, 3, 2)):
        left = 380 + i * 67
        draw.rectangle((left, 211, left + 62, 298), fill="#fbf9ee", outline=ink)
        for x in range(left + 1, left + 62, 2 * spacing):
            draw.rectangle((x, 212, min(x + spacing - 1, left + 61), 297), fill=ink)
    draw.text((380, 305), "03   LINE PAIRS", fill=ink, font=small)

    for i in range(12):
        level = round(18 + 221 * i / 11)
        x = 380 + i * 33
        draw.rectangle((x, 341, x + 32, 417), fill=(level, level, level), outline=ink)
    draw.text((380, 431), "04   TONAL RESPONSE", fill=ink, font=small)
    return image


def _image(width: int, height: int, frame: int, kind: str) -> Image.Image:
    chart = _chart(frame)
    if kind == "baseline":
        # Reconstruct a coarse image on the original grid; geometry and crop stay fixed.
        chart = chart.resize((80, 48), Image.Resampling.BOX).resize(chart.size, Image.Resampling.BILINEAR)
        chart = ImageEnhance.Color(chart).enhance(0.30)
    elif kind == "candidate":
        chart = chart.resize((400, 240), Image.Resampling.LANCZOS).resize(chart.size, Image.Resampling.BICUBIC)
    elif kind == "variant":
        chart = ImageEnhance.Contrast(chart.convert("L").convert("RGB")).enhance(0.64)
    return chart.resize((width, height), Image.Resampling.LANCZOS)


def _noisy(chart: Image.Image) -> Image.Image:
    # Correlated luminance grain survives thumbnail scaling and compresses losslessly.
    width, height = chart.width // 4, chart.height // 4
    rng = random.Random(42)
    samples = bytes(
        max(0, min(255, 128 + 5 * round(rng.gauss(0, 5))))
        for _ in range(width * height)
    )
    grain = Image.frombytes("L", (width, height), samples)
    grain = grain.resize(chart.size, Image.Resampling.NEAREST).convert("RGB")
    return ImageChops.add(chart, grain, offset=-128)


def _prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to write into non-empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)


def make_calibration(output: Path) -> list[Path]:
    """Write six comparable views of frame one as lossless WebP files."""
    _prepare_output(output)
    reference = _chart(1)
    mosaic = reference.resize((80, 48), Image.Resampling.BOX)
    images = (
        ("reference", reference),
        ("noise", _noisy(reference)),
        ("blur", reference.filter(ImageFilter.GaussianBlur(3))),
        ("mosaic", mosaic.resize(reference.size, Image.Resampling.NEAREST)),
        ("faded", reference.convert("L").convert("RGB")),
        ("low-light", ImageEnhance.Brightness(reference).enhance(0.76)),
    )
    written: list[Path] = []
    for index, (name, image) in enumerate(images, start=1):
        path = output / f"{index:02d}-{name}.webp"
        image.save(path, format="WEBP", lossless=True, method=6)
        written.append(path)
    return written


def make_demo(
    output: Path,
    *,
    width: int = 800,
    height: int = 480,
    frames: int = 8,
    sources: int = 3,
    image_format: str = "png",
    missing_frame: int | None = None,
    missing_in: str = "candidate",
) -> list[Path]:
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ValueError("width and height must be positive and at most 24 MP")
    if frames <= 0:
        raise ValueError("frames must be positive")
    if sources not in (3, 4):
        raise ValueError("sources must be 3 or 4")
    if image_format not in ("png", "jpg", "webp"):
        raise ValueError("format must be png, jpg or webp")
    if missing_frame is not None and not 1 <= missing_frame <= frames:
        raise ValueError("missing-frame must be between 1 and frames")
    if missing_in not in {"baseline", "candidate", "reference", "variant"}:
        raise ValueError("missing-in must name a generated source")
    _prepare_output(output)

    names = ["baseline", "candidate", "reference"]
    if sources == 4:
        names.append("variant")
    written: list[Path] = []
    for name in names:
        directory = output / name
        directory.mkdir(exist_ok=True)
        for frame in range(1, frames + 1):
            if name == missing_in and missing_frame == frame:
                continue
            image = _image(width, height, frame, name)
            path = directory / f"frame_{frame:04d}.{image_format}"
            if image_format == "jpg":
                image.save(path, format="JPEG", quality=84, optimize=True)
            elif image_format == "webp":
                image.save(path, format="WEBP", lossless=True, method=6)
            else:
                image.save(path, format="PNG")
            written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration", action="store_true", help="write one 800×480 lossless WebP set instead of a sequence")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--sources", type=int, choices=(3, 4), default=3)
    parser.add_argument("--format", dest="image_format", choices=("png", "jpg", "webp"), default="png")
    parser.add_argument("--missing-frame", type=int)
    parser.add_argument("--missing-in", choices=("baseline", "candidate", "reference", "variant"), default="candidate")
    args = parser.parse_args()
    if args.calibration:
        make_calibration(args.output)
        return 0
    make_demo(
        args.output,
        width=args.width,
        height=args.height,
        frames=args.frames,
        sources=args.sources,
        image_format=args.image_format,
        missing_frame=args.missing_frame,
        missing_in=args.missing_in,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
