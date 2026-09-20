"""Non-delivery raw-response storage with restricted OS permissions."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import subprocess
import re
import uuid
from functools import lru_cache
from input_safety import InputError
from process_lock import ProcessFileLock


@lru_cache(maxsize=1)
def _account_sid():
    import csv
    import re
    try:
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True)
        sid = next(csv.reader(identity.stdout.splitlines()))[-1].strip()
    except (OSError,subprocess.SubprocessError,StopIteration,IndexError,csv.Error) as exc:
        raise InputError("private_artifact_identity_unavailable") from exc
    if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
        raise InputError("private_artifact_identity_unavailable")
    return sid


def _restrict(path, *, directory):
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # Replace the complete protected DACL; adding a grant alone would leave
    # pre-existing explicit grants to other accounts in place.
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    apply = security.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    flags = "OICI" if directory else ""
    sddl = f"D:P(A;{flags};FA;;;{_account_sid()})(A;{flags};FA;;;SY)"
    if not convert(sddl,1,ctypes.byref(descriptor),None):
        raise InputError("private_artifact_permissions_failed")
    try:
        if not apply(str(path),0x80000004,descriptor):
            raise InputError("private_artifact_permissions_failed")
    finally:
        kernel.LocalFree(descriptor)


def secure_directory(path):
    path = Path(path).absolute()
    if path.name != ".private-response" or path.resolve() != path:
        raise InputError("private_artifact_directory_required")
    _restrict(path, directory=True)


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _phase(callback, stage, metadata):
    event = {**metadata, "stage": stage}
    if callback is not None:
        callback(event)


def write_private_artifact(path, data, *, transaction_id=None, phase_callback=None):
    """Durably commit one content-addressed private response.

    Bytes are written and verified in a transaction-owned same-directory
    temporary file.  The content-addressed final name becomes visible only by
    an atomic rename while a cross-process lock is held.
    """
    path = Path(path).absolute()
    if path.resolve() != path or path.parent.name != ".private-response":
        raise InputError("private_artifact_path_escape")
    if not isinstance(data, bytes):
        raise InputError("private_artifact_bytes_required")
    expected_sha256 = hashlib.sha256(data).hexdigest()
    expected_size = len(data)
    raw_transaction_id = str(transaction_id or ("TX-" + uuid.uuid4().hex))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw_transaction_id):
        raise InputError("private_artifact_transaction_id_invalid")
    temporary = path.with_name(f".{path.name}.{raw_transaction_id}.tmp")
    metadata = {
        "target_name": path.name,
        "temporary_name": temporary.name,
        "transaction_id": raw_transaction_id,
        "expected_sha256": expected_sha256,
        "expected_size": expected_size,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(path):
        if path.exists():
            if path.stat().st_size != expected_size or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise InputError("private_artifact_hash_conflict")
            _restrict(path, directory=False)
            _phase(phase_callback, "reused", metadata)
            return {**metadata, "status": "reused"}
        # Establish privacy before the first byte is written, not afterwards.
        # A directory ACL failure must never expose a default-permission copy.
        try:
            secure_directory(path.parent)
            if temporary.exists():
                temporary.unlink()
            _phase(phase_callback, "before_write", metadata)
            with temporary.open("xb") as handle:
                _restrict(temporary, directory=False)
                split = max(1, expected_size // 2) if expected_size else 0
                handle.write(data[:split])
                handle.flush()
                _phase(phase_callback, "partial_written", metadata)
                handle.write(data[split:])
                _phase(phase_callback, "fully_written", metadata)
                handle.flush()
                os.fsync(handle.fileno())
            _phase(phase_callback, "synced", metadata)
            if (temporary.stat().st_size != expected_size
                    or hashlib.sha256(temporary.read_bytes()).hexdigest() != expected_sha256):
                raise InputError("private_artifact_hash_conflict")
            _restrict(temporary, directory=False)
            _phase(phase_callback, "verified", metadata)
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            _phase(phase_callback, "committed", metadata)
            return {**metadata, "status": "committed"}
        finally:
            # Never remove a committed final artifact, including callback failure.
            if temporary.exists():
                temporary.unlink()
