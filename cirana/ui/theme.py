"""CIRANA design tokens and stylesheet loading.

Graphite neutrals with a single chartreuse accent. Every colour the UI paints comes from COLORS; Python code
uses the named constants below instead of hex literals, so the palette can change in one place.
(assets/icons/check.svg and close.svg carry the on_accent and text2 values as literals.)
"""
from __future__ import annotations

import re

from PySide6.QtGui import QColor, QFont

from cirana.paths import asset_path

COLORS: dict[str, str] = {
    "bg": "#0E0F11",
    "bg2": "#141519",
    "panel": "#1A1C21",
    "panel_hover": "#212329",
    "border": "#2A2D34",
    "text": "#EDEEF0",
    "text2": "#8F949D",
    "accent": "#C3E85B",
    "accent_hover": "#D2F07B",
    "on_accent": "#12160A",  # text and icons on an accent fill (contrast 13:1)
    "good": "#34D399",
    "warn": "#E3B341",
    "bad": "#F87171",
}


def _rgba(hex_color: str, alpha: float) -> str:
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


COLORS.update({
    "accent_soft": _rgba(COLORS["accent"], 0.14),
    "accent_line": _rgba(COLORS["accent"], 0.45),
    "warn_soft": _rgba(COLORS["warn"], 0.12),
    "bad_soft": _rgba(COLORS["bad"], 0.12),
    "scrim": _rgba(COLORS["bg"], 0.72),
})

TEXT = COLORS["text"]
TEXT2 = COLORS["text2"]
ACCENT = COLORS["accent"]
ON_ACCENT = COLORS["on_accent"]
BAD = COLORS["bad"]
WARN = COLORS["warn"]


def qcolor(token: str) -> QColor:
    return QColor(COLORS[token])


def colorref(token: str) -> int:
    """A token as a Win32 COLORREF (0x00BBGGRR), e.g. for DWM title-bar tinting."""
    color = QColor(COLORS[token])
    return color.red() | (color.green() << 8) | (color.blue() << 16)


def build_stylesheet() -> str:
    raw = asset_path("themes", "dark.qss").read_text(encoding="utf-8")
    values = {
        **COLORS,
        "check_icon": asset_path("icons", "check.svg").as_posix(),
        "close_icon": asset_path("icons", "close.svg").as_posix(),
    }
    # Longest token first so that @accent_soft is not clobbered by @accent.
    for key in sorted(values, key=len, reverse=True):
        raw = re.sub(rf"@{key}\b", values[key], raw)
    return raw


def app_font() -> QFont:
    font = QFont("Segoe UI Variable Text")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSizeF(9.5)
    return font
