"""Editor-uploaded shot media validation shared by HTTP and worker boundaries."""
from __future__ import annotations

from pathlib import Path
from typing import Literal


MAX_SHOT_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SHOT_VIDEO_BYTES = 200 * 1024 * 1024

ShotMediaKind = Literal["image", "video"]

_DECLARED_FORMATS: dict[str, tuple[ShotMediaKind, str, str]] = {
    ".jpg": ("image", ".jpg", "image/jpeg"),
    ".jpeg": ("image", ".jpg", "image/jpeg"),
    ".png": ("image", ".png", "image/png"),
    ".webp": ("image", ".webp", "image/webp"),
    ".mp4": ("video", ".mp4", "video/mp4"),
    ".mov": ("video", ".mov", "video/quicktime"),
    ".webm": ("video", ".webm", "video/webm"),
}


def safe_upload_filename(value: str | None) -> str:
    normalized = str(value or "未命名素材").replace(chr(92), "/").split("/")[-1]
    cleaned = "".join(char for char in normalized if ord(char) >= 32).strip()
    return (cleaned or "未命名素材")[:255]


def declared_shot_media_format(
    filename: str | None,
    media_kind: str,
) -> tuple[ShotMediaKind, str, str]:
    """Resolve the canonical type from a browser filename before direct upload."""

    extension = Path(safe_upload_filename(filename)).suffix.lower()
    declared = _DECLARED_FORMATS.get(extension)
    if not declared or declared[0] != media_kind:
        raise ValueError("镜头素材只支持 JPG、PNG、WebP、MP4、MOV 或 WebM 格式")
    return declared


def detect_shot_media_format(path: Path) -> tuple[ShotMediaKind, str, str]:
    """Detect supported media from magic bytes; never trust filename or MIME alone."""

    with path.open("rb") as handle:
        header = handle.read(64)
    if header.startswith(bytes.fromhex("89504e470d0a1a0a")):
        return "image", ".png", "image/png"
    if header.startswith(bytes.fromhex("ffd8ff")):
        return "image", ".jpg", "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", ".webp", "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        media_type = "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
        return "video", ".mov" if media_type == "video/quicktime" else ".mp4", media_type
    if header.startswith(bytes.fromhex("1a45dfa3")):
        return "video", ".webm", "video/webm"
    raise ValueError("镜头素材只支持 JPG、PNG、WebP、MP4、MOV 或 WebM 格式")


def validate_shot_media_size(media_kind: str, size_bytes: int) -> None:
    if size_bytes <= 0:
        raise ValueError("上传素材不能为空")
    if media_kind == "image" and size_bytes > MAX_SHOT_IMAGE_BYTES:
        raise ValueError("上传图片不能超过 20 MB")
    if media_kind == "video" and size_bytes > MAX_SHOT_VIDEO_BYTES:
        raise ValueError("上传视频不能超过 200 MB")
