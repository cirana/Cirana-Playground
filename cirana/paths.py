"""Resolution of data and bundled-asset locations."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """User data directory. Overridable with CIRANA_HOME (used by tests)."""
    override = os.environ.get("CIRANA_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CIRANA"
    base.mkdir(parents=True, exist_ok=True)
    return base


def asset_path(*parts: str) -> Path:
    """Locate a bundled asset in a source checkout or a PyInstaller bundle."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass, "cirana", "assets").joinpath(*parts)
    return Path(__file__).resolve().parent.joinpath("assets", *parts)
