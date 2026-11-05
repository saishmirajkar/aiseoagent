"""Content fingerprints: exact hash + 64-bit simhash for near-duplicate detection."""
from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def content_hash(text: str) -> str:
    norm = " ".join(tokens(text))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(text: str, shingle: int = 3) -> int:
    """64-bit simhash over word shingles, returned as a signed int64 (fits Postgres BIGINT)."""
    words = tokens(text)
    if not words:
        return 0
    feats = [" ".join(words[i:i + shingle]) for i in range(max(1, len(words) - shingle + 1))]
    v = [0] * 64
    for f in feats:
        h = _h64(f)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")
