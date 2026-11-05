"""Estimate the rendered width of a SERP title (Arial 20px, Google desktop).

Uses Helvetica/Arial advance widths (units per 1000 em). An estimate: Google's exact
truncation also depends on the device and on bolded query terms.
"""
from __future__ import annotations

import unicodedata

_W = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191, "(": 333, ")": 333,
    "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584,
    ">": 584, "?": 556, "@": 1015, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "{": 334, "|": 260, "}": 334, "~": 584, "–": 556, "—": 1000, "’": 222, "‘": 222, "“": 333, "”": 333,
    "•": 350, "…": 1000, "·": 278,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556, "i": 222, "j": 222,
    "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278,
    "u": 556, "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722, "I": 278, "J": 500,
    "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611,
    "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
}
for _d in "0123456789":
    _W[_d] = 556


def title_px(text: str, font_px: float = 20.0) -> int:
    total = 0
    for ch in text:
        if ch in _W:
            total += _W[ch]
            continue
        base = unicodedata.normalize("NFD", ch)[0]
        if base in _W:
            total += _W[base]
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            total += 1000
        else:
            total += 556
    return round(total * font_px / 1000)
