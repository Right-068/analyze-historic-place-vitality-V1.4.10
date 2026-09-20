#!/usr/bin/env python3
"""Small cross-process exclusive lock used by append-only local ledgers."""

from __future__ import annotations

import hashlib
import strict_json as json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class ProcessFileLock:
    """Serialize a critical section without polling or stale lock files."""

    def __init__(self, protected_path: Path, timeout_ms: int = 120_000) -> None:
        self.protected_path = protected_path.resolve()
        self.timeout_ms = max(0, int(timeout_ms))
        self._handle: object | None = None
        self._file = None
        self.abandoned = False
        self.wait_result: int | None = None

    def _record_abandoned(self) -> None:
        self.protected_path.parent.mkdir(parents=True, exist_ok=True)
        audit = self.protected_path.with_name(self.protected_path.name + ".abandoned-lock-events.jsonl")
        event = {
            "event": "wait_abandoned",
            "protected_resource_sha256": hashlib.sha256(
                os.path.normcase(str(self.protected_path)).encode("utf-8")
            ).hexdigest(),
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        with audit.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def __enter__(self) -> "ProcessFileLock":
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            name = "Local\\historic-vitality-" + hashlib.sha256(
                str(self.protected_path).casefold().encode("utf-8")
            ).hexdigest()
            kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.GetLastError.argtypes = ()
            kernel32.GetLastError.restype = wintypes.DWORD
            handle = kernel32.CreateMutexW(None, False, name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "cannot create ledger mutex")
            result = kernel32.WaitForSingleObject(handle, self.timeout_ms)
            self.wait_result = int(result)
            if result == 0x00000102:  # WAIT_TIMEOUT
                kernel32.CloseHandle(handle)
                raise TimeoutError("timed out waiting for protected ledger writer")
            if result == 0xFFFFFFFF:  # WAIT_FAILED
                error_code = int(kernel32.GetLastError())
                kernel32.CloseHandle(handle)
                raise OSError(error_code, "failed while waiting for protected ledger mutex")
            if result not in {0x00000000, 0x00000080}:
                kernel32.CloseHandle(handle)
                raise OSError(int(result), "unexpected protected ledger mutex wait result")
            self._handle = (kernel32, handle)
            if result == 0x00000080:  # WAIT_ABANDONED
                self.abandoned = True
                self._record_abandoned()
            return self

        import fcntl

        lock_path = self.protected_path.with_name(self.protected_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = lock_path.open("a+b")
        deadline = time.monotonic() + (self.timeout_ms / 1000.0)
        try:
            while True:
                try:
                    fcntl.flock(
                        self._file.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    return self
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "timed out waiting for protected ledger writer"
                        ) from exc
                    time.sleep(min(0.01, remaining))
        except BaseException:
            self._file.close()
            self._file = None
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if os.name == "nt":
            if self._handle is not None:
                kernel32, handle = self._handle
                try:
                    if not kernel32.ReleaseMutex(handle):
                        raise OSError("cannot release protected ledger mutex")
                finally:
                    kernel32.CloseHandle(handle)
            self._handle = None
            return
        if self._file is not None:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
