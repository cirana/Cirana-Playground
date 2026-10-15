"""Drag-and-drop payloads: applications (card -> workspace) and workspace entries (reorder, remove)."""
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


ENTRY_MIME = "application/x-cirana-workspace-entry"


def entry_mime(workspace_id: str, entry_id: str, name: str) -> QMimeData:
    """A workspace entry being dragged (to reorder it, or out of the workspace to remove it)."""
    data = QMimeData()
    data.setData(ENTRY_MIME, QByteArray(f"{workspace_id}|{entry_id}".encode("utf-8")))
    data.setText(name)
    return data


def entry_from(mime: QMimeData) -> tuple[str, str]:
    """(workspace id, entry id) of a dragged entry, or ("", "")."""
    if not mime.hasFormat(ENTRY_MIME):
        return "", ""
    workspace_id, _sep, entry_id = bytes(mime.data(ENTRY_MIME)).decode("utf-8", "replace").partition("|")
    return workspace_id, entry_id
