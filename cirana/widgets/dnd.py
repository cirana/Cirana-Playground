"""Drag-and-drop payload for applications (card -> workspace)."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData

APP_MIME = "application/x-cirana-app-id"


def app_mime(app_id: str, name: str) -> QMimeData:
    data = QMimeData()
    data.setData(APP_MIME, QByteArray(app_id.encode("utf-8")))
    data.setText(name)
    return data


def app_id_from(mime: QMimeData) -> str:
    if not mime.hasFormat(APP_MIME):
        return ""
    return bytes(mime.data(APP_MIME)).decode("utf-8", "replace")
