"""Versioned, atomic, self-recovering JSON persistence."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

Validator = Callable[[Any], bool]


class JsonStore:
    """Stores ``{"version": N, "data": ...}`` documents.

    * Writes go to a temp file in the same directory, then ``os.replace``.
    * The previous good file is kept as ``<name>.bak``.
    * Corrupt/invalid files are moved to ``<name>.corrupt-<ts>`` and the
      loader falls back to ``.bak`` and finally to the default value.
    """

    def __init__(
        self,
        path: Path,
        default_factory: Callable[[], Any],
        version: int = 1,
        validator: Validator | None = None,
        migrate: Callable[[int, Any], Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self._default_factory = default_factory
        self.version = version
        self._validator = validator
        self._migrate = migrate
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def default(self) -> Any:
        return self._default_factory()

    def load(self) -> Any:
        with self._lock:
            if not self.path.exists():
                data = self.default()
                self._write(data)
                return data
            data = self._try_read(self.path)
            if data is not None:
                return data
            self._quarantine()
            data = self._try_read(self.backup_path)
            if data is not None:
                log.warning("Recovered %s from backup", self.path.name)
                self._write(data)
                return data
            log.warning("Resetting %s to defaults", self.path.name)
            data = self.default()
            self._write(data)
            return data

    def save(self, data: Any) -> None:
        with self._lock:
            if self._validator and not self._validator(data):
                raise ValueError(f"Refusing to save invalid data to {self.path.name}")
            self._write(data)

    # -- internals ---------------------------------------------------------
    def _try_read(self, path: Path) -> Any | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if path.exists():
                log.warning("Cannot read %s: %s", path.name, exc)
            return None
        if not isinstance(raw, dict) or "version" not in raw or "data" not in raw:
            return None
        version, data = raw["version"], raw["data"]
        if not isinstance(version, int) or version > self.version:
            log.warning("%s has unsupported version %r", path.name, version)
            return None
        if version < self.version:
            if self._migrate is None:
                return None
            try:
                data = self._migrate(version, data)
            except Exception:  # noqa: BLE001 - any migration failure means "unusable"
                log.exception("Migration of %s failed", path.name)
                return None
        if self._validator and not self._validator(data):
            log.warning("%s failed validation", path.name)
            return None
        return data

    def _quarantine(self) -> None:
        target = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            shutil.move(str(self.path), str(target))
            log.warning("Moved corrupt file to %s", target.name)
        except OSError:
            log.exception("Could not quarantine %s", self.path)

    def _write(self, data: Any) -> None:
        payload = json.dumps({"version": self.version, "data": data}, indent=2, ensure_ascii=False)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if self.path.exists() and self._try_read(self.path) is not None:
            try:
                shutil.copyfile(self.path, self.backup_path)
            except OSError:
                log.debug("Could not refresh backup for %s", self.path.name)
        self._replace_with_retry(tmp, self.path)

    @staticmethod
    def _replace_with_retry(src: Path, dst: Path, attempts: int = 8) -> None:
        # Antivirus / the search indexer can briefly lock files on Windows.
        for attempt in range(attempts):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))


def deep_default(value: Any) -> Callable[[], Any]:
    return lambda: copy.deepcopy(value)
