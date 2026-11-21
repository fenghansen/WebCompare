"""工作台可以解码的常规静态图片格式。"""

IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


def image_format_from_header(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None


def is_animated_webp(header: bytes) -> bool:
    """读取扩展头的动画标志，包含只有一张画面的动画容器。"""
    return (len(header) >= 21 and header.startswith(b"RIFF")
            and header[8:16] == b"WEBPVP8X" and bool(header[20] & 0x02))
