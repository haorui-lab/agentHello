"""图片只作为字节经中心转发。这里只做类型和大小检查，不解释图片内容。"""

from __future__ import annotations

import base64
import binascii
import struct
import zlib

from hello_center.errors import HelloError

_ALLOWED = ("image/png", "image/jpeg", "image/gif", "image/webp")


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成一张纯色 PNG，供验证脚本使用。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _magic_ok(mime: str, data: bytes) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def decode_image(image_base64: str | None, image_mime: str | None, max_bytes: int) -> tuple[bytes, str] | None:
    if not image_base64 and not image_mime:
        return None
    if not image_base64 or not image_mime:
        raise HelloError("bad_image", "图片需要同时给出 image_base64 和 image_mime")
    mime = image_mime.strip().lower()
    if mime not in _ALLOWED:
        raise HelloError("bad_image", "图片类型只接受 image/png、image/jpeg、image/gif、image/webp")
    # 先按字符长度拦住明显过大的输入，避免一次解码出超大字节。
    if len(image_base64) > (max_bytes * 4 // 3) + 8:
        raise HelloError("payload_too_large", f"图片超过 {max_bytes} 字节")
    try:
        data = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HelloError("bad_image", "image_base64 不是合法的 base64") from exc
    if len(data) > max_bytes:
        raise HelloError("payload_too_large", f"图片超过 {max_bytes} 字节")
    if not data or not _magic_ok(mime, data):
        raise HelloError("bad_image", "图片内容和声明的类型不一致")
    return data, mime
