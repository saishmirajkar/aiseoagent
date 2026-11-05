"""Image format + intrinsic dimensions from the first bytes of a file.

Only a prefix is ever read (streamed); full images are never downloaded into memory.
Supports PNG, GIF, JPEG, WebP (VP8/VP8L/VP8X), AVIF/HEIF (ispe box), SVG (format only).
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from app.crawler.fetcher import Fetcher

HEADER_BYTES = 131_072
MAX_COUNT_BYTES = 25_000_000  # beyond this we stop counting and report ">"


@dataclass
class ImageInfo:
    format: str | None = None
    width: int | None = None
    height: int | None = None


def sniff(data: bytes) -> ImageInfo:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return ImageInfo("png", w, h)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return ImageInfo("gif", w, h)
    if data[:2] == b"\xff\xd8":
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis") or b"avif" in data[8:32]:
            return _ispe(data, "avif")
        if brand in (b"heic", b"heix", b"mif1"):
            return _ispe(data, "heic")
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return ImageInfo("svg")
    if data[:4] == b"\x00\x00\x01\x00":
        return ImageInfo("ico")
    if data[:2] == b"BM" and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return ImageInfo("bmp", w, abs(h))
    return ImageInfo()


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(data: bytes) -> ImageInfo:
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in _SOF and i + 9 <= n:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return ImageInfo("jpeg", w, h)
        i += 2 + seg_len
    return ImageInfo("jpeg")


def _webp(data: bytes) -> ImageInfo:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w, h = struct.unpack("<HH", data[26:30])
        return ImageInfo("webp", w & 0x3FFF, h & 0x3FFF)
    if chunk == b"VP8L" and len(data) >= 25:
        b = data[21:25]
        w = 1 + (b[0] | ((b[1] & 0x3F) << 8))
        h = 1 + ((b[1] >> 6) | (b[2] << 2) | ((b[3] & 0x0F) << 10))
        return ImageInfo("webp", w, h)
    if chunk == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return ImageInfo("webp", w, h)
    return ImageInfo("webp")


def _ispe(data: bytes, fmt: str) -> ImageInfo:
    best: tuple[int, int] | None = None
    for m in re.finditer(b"ispe", data):
        j = m.end() + 4  # skip version/flags
        if j + 8 <= len(data):
            w, h = struct.unpack(">II", data[j:j + 8])
            if best is None or w * h > best[0] * best[1]:
                best = (w, h)
    return ImageInfo(fmt, *(best or (None, None)))


@dataclass
class ImageAssetResult:
    src: str
    status: int | None
    final_url: str | None
    content_type: str | None
    bytes: int | None
    bytes_exact: bool
    format: str | None
    width: int | None
    height: int | None
    error: str | None = None


async def probe_image(fetcher: Fetcher, src: str) -> ImageAssetResult:
    """Stream the image: parse the header from the first bytes, count the rest without storing it."""
    head = bytearray()
    size = 0
    total: int | None = None
    exact = False
    async with fetcher.stream(src, headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}) as (res, body):
        out = ImageAssetResult(src=src, status=res.status, final_url=res.final_url,
                               content_type=res.headers.get("content-type"), bytes=None,
                               bytes_exact=False, format=None, width=None, height=None, error=res.error)
        if body is None or res.status != 200:
            if res.status and res.status != 200 and not out.error:
                out.error = f"HTTP {res.status}"
            return out
        cl = res.headers.get("content-length")
        if cl and cl.isdigit() and "gzip" not in res.headers.get("content-encoding", ""):
            total = int(cl)
            exact = True
        try:
            async for chunk in body:
                size += len(chunk)
                if len(head) < HEADER_BYTES:
                    head.extend(chunk[:HEADER_BYTES - len(head)])
                info = sniff(bytes(head))
                header_done = info.width is not None or info.format == "svg" or len(head) >= HEADER_BYTES
                if header_done and total is not None:
                    break  # size known from Content-Length; no need to read the rest
                if size >= MAX_COUNT_BYTES:
                    break
            else:
                if total is None:
                    total, exact = size, True
        except Exception as e:  # network error mid-stream
            out.error = f"{type(e).__name__} while reading image"
    info = sniff(bytes(head))
    out.format = info.format
    out.width, out.height = info.width, info.height
    out.bytes = total if total is not None else size
    out.bytes_exact = exact
    return out
