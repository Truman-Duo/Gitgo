"""User-scoped encrypted secret storage.

Provider metadata is intentionally kept separate from credentials. Windows
uses DPAPI for the current OS user. macOS stores each value in the login
Keychain, while Linux uses the desktop Secret Service through ``secret-tool``.
Only opaque native-store references are written to disk. No backend creates
portable credential data or places plaintext in Gitgo state files.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager


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


class _MacOSSecurityFramework:
    """Minimal ctypes bridge to the native macOS Keychain Services API."""

    _SUCCESS = 0
    _ITEM_NOT_FOUND = -25300

    def __init__(self) -> None:
        security_path = "/System/Library/Frameworks/Security.framework/Security"
        core_foundation_path = (
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        try:
            self._security = ctypes.CDLL(security_path)
            self._core_foundation = ctypes.CDLL(core_foundation_path)
        except OSError as exc:
            raise SecretStoreError("macOS Keychain framework is unavailable") from exc

        self._security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _encoded(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def _raise(action: str, status: int) -> None:
        raise SecretStoreError(
            f"macOS Keychain {action} failed (OSStatus {status})"
        )

    def add(self, service: str, account: str, plaintext: str) -> None:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        password_data = self._encoded(plaintext)
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainAddGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            len(password_data), password_data,
            ctypes.byref(item_ref),
        )
        try:
            if status != self._SUCCESS:
                self._raise("write", status)
        finally:
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)

    def read(self, service: str, account: str) -> str:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            ctypes.byref(password_length), ctypes.byref(password_data),
            ctypes.byref(item_ref),
        )
        try:
            if status != self._SUCCESS:
                self._raise("read", status)
            try:
                raw = ctypes.string_at(password_data, password_length.value)
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecretStoreError(
                    "macOS Keychain credential is not UTF-8"
                ) from exc
        finally:
            if password_data.value:
                self._security.SecKeychainItemFreeContent(None, password_data)
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)

    def delete(self, service: str, account: str) -> None:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            None, None, ctypes.byref(item_ref),
        )
        if status == self._ITEM_NOT_FOUND:
            return
        if status != self._SUCCESS:
            self._raise("lookup for deletion", status)
        try:
            delete_status = self._security.SecKeychainItemDelete(item_ref)
            if delete_status not in (self._SUCCESS, self._ITEM_NOT_FOUND):
                self._raise("delete", delete_status)
        finally:
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)


class MacOSKeychainProtector:
    """Store credentials in Keychain and expose only opaque references."""

    SERVICE = "io.github.truman-duo.gitgo.provider-credentials"
    REFERENCE_PREFIX = "macos-keychain:"

    def __init__(self, keychain=None) -> None:
        if sys.platform != "darwin" and keychain is None:
            raise SecretStoreError("macOS Keychain is unavailable on this platform")
        self._keychain = keychain or _MacOSSecurityFramework()

    def _account(self, reference: str) -> str:
        if not reference.startswith(self.REFERENCE_PREFIX):
            raise SecretStoreError("Credential is not a macOS Keychain reference")
        account = reference[len(self.REFERENCE_PREFIX):]
        try:
            parsed = uuid.UUID(hex=account)
        except (ValueError, AttributeError) as exc:
            raise SecretStoreError("macOS Keychain reference is invalid") from exc
        if parsed.hex != account:
            raise SecretStoreError("macOS Keychain reference is invalid")
        return account

    def protect(self, plaintext: str) -> str:
        reference = self.allocate_reference()
        self.protect_into(reference, plaintext)
        return reference

    def allocate_reference(self) -> str:
        return self.REFERENCE_PREFIX + uuid.uuid4().hex

    def protect_into(self, reference: str, plaintext: str) -> None:
        self._keychain.add(self.SERVICE, self._account(reference), plaintext)

    def unprotect(self, reference: str) -> str:
        return self._keychain.read(self.SERVICE, self._account(reference))

    def delete(self, reference: str) -> None:
        self._keychain.delete(self.SERVICE, self._account(reference))


class _LinuxSecretToolBackend:
    """Small adapter for the freedesktop.org Secret Service command client."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("secret-tool") or ""
        if not self.executable:
            raise SecretStoreError(
                "Linux Secret Service client is unavailable; install libsecret-tools"
            )

    def _run(
        self, action: str, service: str, account: str, *, plaintext: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [self.executable, action]
        if action == "store":
            arguments.append("--label=Gitgo provider credential")
        arguments.extend(["service", service, "account", account])
        try:
            return subprocess.run(
                arguments,
                input=(plaintext if plaintext is not None else None),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # TimeoutExpired may contain captured credential output. Never
            # surface subprocess stdout/stderr or input in public diagnostics.
            raise SecretStoreError(f"Linux Secret Service {action} unavailable or timed out") from exc

    @staticmethod
    def _failure(action: str, result: subprocess.CompletedProcess[str]) -> SecretStoreError:
        return SecretStoreError(f"Linux Secret Service {action} failed (exit {result.returncode})")

    def add(self, service: str, account: str, plaintext: str) -> None:
        result = self._run("store", service, account, plaintext=plaintext)
        if result.returncode != 0:
            raise self._failure("write", result)

    def read(self, service: str, account: str) -> str:
        result = self._run("lookup", service, account)
        if result.returncode != 0:
            raise self._failure("read", result)
        return result.stdout.removesuffix("\n")

    def delete(self, service: str, account: str) -> None:
        result = self._run("clear", service, account)
        if result.returncode == 0 and not result.stderr:
            return
        # clear's exit 1 can mean a service failure, not just no matching item.
        # Confirm absence through a successful search, without --unlock.
        if result.returncode == 1 and not result.stderr:
            absent = self._run("search", service, account)
            if absent.returncode == 0 and not absent.stdout and not absent.stderr:
                return
        raise self._failure("delete", result)


class LinuxSecretServiceProtector:
    """Store Linux credentials in Secret Service behind opaque references."""

    SERVICE = "io.github.truman-duo.gitgo.provider-credentials"
    REFERENCE_PREFIX = "linux-secret-service:"

    def __init__(self, backend=None) -> None:
        if not sys.platform.startswith("linux") and backend is None:
            raise SecretStoreError("Linux Secret Service is unavailable on this platform")
        self._backend = backend or _LinuxSecretToolBackend()

    def _account(self, reference: str) -> str:
        if not reference.startswith(self.REFERENCE_PREFIX):
            raise SecretStoreError("Credential is not a Linux Secret Service reference")
        account = reference[len(self.REFERENCE_PREFIX):]
        try:
            parsed = uuid.UUID(hex=account)
        except (ValueError, AttributeError) as exc:
            raise SecretStoreError("Linux Secret Service reference is invalid") from exc
        if parsed.hex != account:
            raise SecretStoreError("Linux Secret Service reference is invalid")
        return account

    def protect(self, plaintext: str) -> str:
        reference = self.allocate_reference()
        self.protect_into(reference, plaintext)
        return reference

    def allocate_reference(self) -> str:
        return self.REFERENCE_PREFIX + uuid.uuid4().hex

    def protect_into(self, reference: str, plaintext: str) -> None:
        self._backend.add(self.SERVICE, self._account(reference), plaintext)

    def unprotect(self, reference: str) -> str:
        return self._backend.read(self.SERVICE, self._account(reference))

    def delete(self, reference: str) -> None:
        self._backend.delete(self.SERVICE, self._account(reference))


def default_secret_protector():
    """Select the current OS credential backend without an insecure fallback."""
    if os.name == "nt":
        return WindowsDPAPIProtector()
    if sys.platform == "darwin":
        return MacOSKeychainProtector()
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceProtector()
    raise SecretStoreError(
        f"Secure credential storage is unavailable on platform {sys.platform!r}"
    )


class EncryptedSecretStore:
    """Atomic encrypted key/value store.

    The injected protector seam keeps persistence tests deterministic without
    weakening the production backend.
    """

    VERSION = 2

    def __init__(self, path: Path, protector=None) -> None:
        self.path = Path(path)
        self.protector = protector or default_secret_protector()
        self._lock = threading.RLock()

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return  # Windows uses the existing file/replace persistence contract.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_parent(self) -> None:
        missing = []
        directory = self.path.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for directory in missing:
            self._sync_directory(directory.parent)

    @contextmanager
    def _exclusive(self):
        """Serialize native effects and their retry index across Host processes."""
        with self._lock:
            self._ensure_parent()
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            with open(lock_path, "a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b"0"); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def pending_count(cls, path: Path) -> int:
        """Read retry status without opening an OS keyring or returning refs."""
        return len(cls._read_payload(Path(path))[1])

    @classmethod
    def _read_payload(cls, path: Path) -> tuple[dict[str, str], list[str]]:
        if not path.exists():
            return {}, []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError("Cannot read encrypted credential index") from exc
        if not isinstance(payload, dict) or payload.get("version") not in (1, cls.VERSION):
            raise SecretStoreError("Unsupported credential store version")
        values = payload.get("secrets", {})
        pending = payload.get("pending_cleanup", [])
        if (not isinstance(values, dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()) or
                not isinstance(pending, list) or any(not isinstance(v, str) for v in pending)):
            raise SecretStoreError("Invalid encrypted credential index")
        return dict(values), list(dict.fromkeys(pending))

    def _read_ciphertexts(self) -> dict[str, str]:
        return self._read_payload(self.path)[0]

    def _retry_cleanup(self) -> bool:
        encrypted, pending = self._read_payload(self.path)
        if pending:
            try:
                # Reconfirm the durable journal before deleting native effects.
                self._write_ciphertexts(encrypted, pending)
            except OSError:
                return False
        delete = getattr(self.protector, "delete", None)
        remaining = []
        for reference in pending:
            # A journaled creation can have become the active credential.
            if reference in encrypted.values():
                continue
            try:
                if not callable(delete):
                    raise SecretStoreError("Native deletion backend unavailable")
                delete(reference)
            except (SecretStoreError, OSError):
                remaining.append(reference)
        if remaining != pending:
            try:
                self._write_ciphertexts(encrypted, remaining)
            except OSError:
                # Deletion was attempted, but the durable retry record remains.
                return False
        return not remaining

    def retry_cleanup(self) -> bool:
        with self._exclusive():
            return self._retry_cleanup()

    def read_all(self) -> dict[str, str]:
        with self._exclusive():
            return {
                key: self.protector.unprotect(value)
                for key, value in self._read_ciphertexts().items()
            }

    def upsert(self, values: dict[str, str]) -> None:
        if not values:
            return
        with self._exclusive():
            encrypted, pending = self._read_payload(self.path)
            replacements: dict[str, tuple[str | None, str]] = {}
            try:
                for key, value in values.items():
                    normalized_key = str(key)
                    if callable(getattr(self.protector, "delete", None)):
                        allocate = getattr(self.protector, "allocate_reference", None)
                        write = getattr(self.protector, "protect_into", None)
                        if not callable(allocate) or not callable(write):
                            raise SecretStoreError("Native protector must support journaled creation")
                        protected = allocate()
                        pending.append(protected)
                        # Persist intent BEFORE creating any native credential.
                        self._write_ciphertexts(self._read_ciphertexts(), pending)
                        write(protected, str(value))
                    else:
                        protected = self.protector.protect(str(value))
                    replacements[normalized_key] = (
                        encrypted.get(normalized_key), protected,
                    )
                    encrypted[normalized_key] = protected
                pending.extend(old for old, new in replacements.values() if old and old != new
                               and callable(getattr(self.protector, "delete", None)))
                pending = [ref for ref in dict.fromkeys(pending) if ref not in encrypted.values()]
                self._write_ciphertexts(encrypted, pending)
            except Exception:
                # Durable creation intents remain available even if cleanup or
                # the native write failed. Never erase an unconfirmed deletion.
                raise
            self._retry_cleanup()

    def retain_only(self, keys: set[str]) -> None:
        with self._exclusive():
            encrypted, pending = self._read_payload(self.path)
            retained = {key: value for key, value in encrypted.items() if key in keys}
            if retained != encrypted:
                if callable(getattr(self.protector, "delete", None)):
                    pending.extend(value for key, value in encrypted.items() if key not in retained)
                self._write_ciphertexts(retained, list(dict.fromkeys(pending)))
            self._retry_cleanup()

    def clear(self) -> None:
        """Remove every native credential, then durably empty the reference file.

        Like retryable orphan cleanup, uninstall must fail closed: if the OS
        credential backend refuses a deletion, keep the reference index so the
        operation can be retried instead of silently stranding a secret.
        """
        with self._exclusive():
            encrypted, pending = self._read_payload(self.path)
            if callable(getattr(self.protector, "delete", None)):
                pending.extend(encrypted.values())
            if encrypted or pending or self.path.exists():
                self._write_ciphertexts({}, list(dict.fromkeys(pending)))
                if not self._retry_cleanup():
                    raise SecretStoreError("Credential deletion incomplete; retry index preserved")

    def _write_ciphertexts(self, values: dict[str, str], pending: list[str] | None = None) -> None:
        if pending is None:
            pending = self._read_payload(self.path)[1]
        self._ensure_parent()
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": self.VERSION, "secrets": values,
                           "pending_cleanup": pending}, handle,
                          ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._sync_directory(self.path.parent)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
