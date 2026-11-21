"""为文本保留 gzip；图片、视频和 ZIP 已编码，直接传输原有字节。"""
from pathlib import PurePosixPath

from starlette.middleware.gzip import GZipMiddleware


class EncodedMediaGZipMiddleware(GZipMiddleware):
    """兼容旧版 Starlette：它们默认还会重复压缩 JPEG/WebP/PNG。"""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            media = (
                PurePosixPath(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".zip", ".mp4", ".mov", ".m4v", ".webm", ".mkv"}
                or path in {"/api/thumb", "/api/diff", "/api/gif", "/api/video", "/api/export-zip"}
                or (path.startswith("/api/assets/") and path.endswith(("/render", "/video")))
                or (path.startswith("/api/exports/") and path.endswith("/download"))
            )
            if media:
                return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)
