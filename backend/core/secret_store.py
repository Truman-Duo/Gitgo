"""User-scoped encrypted secret storage.

Provider metadata is intentionally kept separate from credentials.  On
Windows, values are protected with DPAPI for the current OS user before they
are written to disk.  The file contains only opaque ciphertext and may safely
be backed up together with other Gitgo user state (it is not portable to a
different Windows account).
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading
import uuid


class SecretStoreError(RuntimeError):
    """Credential storage is unavailable or corrupted."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class WindowsDPAPIProtector:
    """Small stdlib-only wrapper around current-user Windows DPAPI."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is unavailable on this platform")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, plaintext: str) -> str:
        raw = plaintext.encode("utf-8")
        source, keepalive = _blob(raw)
        output = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(source), "Gitgo provider credential", None, None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(f"DPAPI protect failed ({ctypes.get_last_error()})")
        del keepalive
        try:
            encrypted = ctypes.string_at(output.pbData, output.cbData)
            return base64.b64encode(encrypted).decode("ascii")
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, ciphertext: str) -> str:
        try:
            raw = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise SecretStoreError("Credential ciphertext is not valid base64") from exc
        source, keepalive = _blob(raw)
        output = _DataBlob()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(f"DPAPI unprotect failed ({ctypes.get_last_error()})")
        del keepalive
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretStoreError("Decrypted credential is not UTF-8") from exc
        finally:
            self._kernel32.LocalFree(output.pbData)


class EncryptedSecretStore:
    """Atomic encrypted key/value store.

    The injected protector seam keeps persistence tests deterministic without
    weakening the production backend.
    """

    VERSION = 1

    def __init__(self, path: Path, protector=None) -> None:
        self.path = Path(path)
        self.protector = protector or WindowsDPAPIProtector()
        self._lock = threading.RLock()

    def _read_ciphertexts(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"Cannot read encrypted credential store: {exc}") from exc
        if int(payload.get("version", 0) or 0) != self.VERSION:
            raise SecretStoreError("Unsupported credential store version")
        values = payload.get("secrets", {})
        if not isinstance(values, dict):
            raise SecretStoreError("Credential store has an invalid secrets map")
        return {str(key): str(value) for key, value in values.items()}

    def read_all(self) -> dict[str, str]:
        with self._lock:
            return {
                key: self.protector.unprotect(value)
                for key, value in self._read_ciphertexts().items()
            }

    def upsert(self, values: dict[str, str]) -> None:
        if not values:
            return
        with self._lock:
            encrypted = self._read_ciphertexts()
            for key, value in values.items():
                encrypted[str(key)] = self.protector.protect(str(value))
            self._write_ciphertexts(encrypted)

    def retain_only(self, keys: set[str]) -> None:
        with self._lock:
            encrypted = self._read_ciphertexts()
            retained = {key: value for key, value in encrypted.items() if key in keys}
            if retained != encrypted:
                self._write_ciphertexts(retained)

    def _write_ciphertexts(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": self.VERSION, "secrets": values}, handle,
                          ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
