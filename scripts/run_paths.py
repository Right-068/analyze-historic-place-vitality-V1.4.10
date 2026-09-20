#!/usr/bin/env python3
"""Portable run-root references with compatibility for older absolute paths."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import Mapping


class RunPathError(ValueError):
    """A declared run-local path is unsafe or cannot be migrated safely."""


def _comparison_path(path: Path) -> Path:
    """Normalize equivalent Windows spellings only after resolving links."""

    resolved = path.resolve()
    text = str(resolved)
    if os.name == "nt":
        # A concurrent replace can make non-strict realpath retain its extended
        # prefix.  The drive/UNC prefix is a spelling, not a different root.
        # Do not normalize other device namespaces or use unresolved paths.
        if text[:8].upper() == "\\\\?\\UNC\\":
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:", text[4:]) and text[6:7] == os.sep:
            text = text[4:]
    return Path(text)


def _within_root(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(str(_comparison_path(candidate)))
    root_text = os.path.normcase(str(_comparison_path(root)))
    try:
        return os.path.commonpath([candidate_text, root_text]) == root_text
    except ValueError:
        return False


def safe_run_relative_path(value: object, run_root: Path) -> Path:
    """Resolve one logical path and fail closed on traversal or link escape."""

    raw = str(value or "").strip()
    if not raw:
        raise RunPathError("运行相对路径不能为空")
    windows = PureWindowsPath(raw)
    native = Path(raw)
    if (
        native.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or raw.startswith(("\\\\", "//"))
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise RunPathError("run_relative 路径不得为绝对路径、盘符路径或 UNC 路径")
    if any(part == ".." for part in windows.parts) or any(part == ".." for part in native.parts):
        raise RunPathError("run_relative 路径不得包含父目录逃逸")
    root = run_root.resolve()
    candidate = (root / native).resolve()
    if not _within_root(candidate, root):
        raise RunPathError("run_relative 路径解析后逃逸运行根目录")
    return candidate


def infer_run_root(anchor: Path, state: Mapping[str, object] | None = None) -> Path:
    """Use the current state location as the authoritative movable run root."""

    anchor = anchor.resolve()
    if anchor.is_file() or anchor.suffix:
        return anchor.parent
    return anchor


def logical_path(path: Path, run_root: Path) -> str:
    root = _comparison_path(run_root)
    # On Windows, another process may materialize an otherwise safe missing
    # parent while Path.resolve() is walking it.  Require two identical,
    # in-root resolutions instead of accepting one observation or failing on
    # one transient observation.  A stable escape remains rejected.
    last_relative = ""
    for _ in range(3):
        resolved = _comparison_path(path)
        try:
            if not _within_root(resolved, root):
                last_relative = ""
                continue
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            last_relative = ""
            continue
        if relative and relative == last_relative:
            return relative
        last_relative = relative
    return ""


def path_descriptor(path: Path, run_root: Path, role: str, sha256: str) -> dict[str, str]:
    relative = logical_path(path, run_root)
    if relative:
        return {
            "path_kind": "run_relative",
            "logical_path": relative,
            "role": role,
            "sha256": sha256,
        }
    return {
        "path_kind": "external_named",
        "logical_path": path.name,
        "role": role,
        "sha256": sha256,
    }


def resolve_run_path(
    reference: object,
    run_root: Path,
    *,
    legacy_absolute: object = "",
    allow_legacy_migration: bool = True,
) -> Path:
    """Resolve inside the current run root, preferring the current copy.

    A legacy absolute path is never returned directly.  If the current copy is
    absent, controlled migration copies one verified legacy file into the run
    root before returning it.
    """

    root = run_root.resolve()
    if isinstance(reference, Mapping):
        logical = str(reference.get("logical_path", "")).strip()
        kind = str(reference.get("path_kind", "run_relative"))
        if logical and kind == "run_relative":
            return safe_run_relative_path(logical, root)
        if kind not in {"run_relative", "legacy_absolute", "external_named"}:
            raise RunPathError(f"未知路径描述类型：{kind}")
        reference = logical
    raw = str(reference or "").strip()
    path = Path(raw) if raw else Path(str(legacy_absolute or ""))
    if raw and not path.is_absolute():
        return safe_run_relative_path(raw, root)
    # A moved run must use its own copy whenever one exists, even while the old
    # absolute path is still reachable.
    name = path.name if str(path) else ""
    if name and root.is_dir():
        matches = [candidate for candidate in root.rglob(name) if candidate.is_file()]
        if len(matches) == 1:
            resolved_match = matches[0].resolve()
            if not _within_root(resolved_match, root):
                raise RunPathError("当前副本中的符号链接逃逸运行根目录")
            return resolved_match
        if len(matches) > 1:
            raise RunPathError("当前运行目录存在多个同名迁移候选，拒绝猜测")
    if str(path) and path.is_absolute() and path.is_file() and allow_legacy_migration:
        migration_root = safe_run_relative_path("migration-imports", root)
        migration_root.mkdir(parents=True, exist_ok=True)
        destination = safe_run_relative_path(f"migration-imports/{path.name}", root)
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise RunPathError("历史路径与当前迁移副本内容冲突")
        if not destination.exists():
            shutil.copy2(path, destination)
        return destination.resolve()
    if str(path) and path.is_absolute():
        raise RunPathError("历史绝对路径未完成受控迁移")
    return safe_run_relative_path("__missing_reference__", root)


def rebind_state_paths(state: dict[str, object], state_path: Path) -> dict[str, object]:
    root = infer_run_root(state_path, state)
    references = state.get("path_references")
    if not isinstance(references, dict):
        references = {}
    for field in ("release_manifest", "approved_writer_ledger", "detailed_log"):
        descriptor = references.get(field)
        if descriptor:
            state[field] = str(resolve_run_path(descriptor, root, legacy_absolute=state.get(field, "")))
    artifacts = state.get("artifacts")
    artifact_refs = references.get("artifacts")
    if isinstance(artifacts, dict) and isinstance(artifact_refs, dict):
        rebound = dict(artifacts)
        for role, descriptor in artifact_refs.items():
            rebound[str(role)] = str(
                resolve_run_path(descriptor, root, legacy_absolute=artifacts.get(role, ""))
            )
        state["artifacts"] = rebound
    state["active_run_root"] = str(root)
    return state


def portable_state_references(state: Mapping[str, object], state_path: Path) -> dict[str, object]:
    root = infer_run_root(state_path, state)
    result: dict[str, object] = {}
    for field in ("release_manifest", "approved_writer_ledger", "detailed_log"):
        path = Path(str(state.get(field, "")))
        relative = logical_path(path, root)
        if relative:
            result[field] = {"path_kind": "run_relative", "logical_path": relative}
    artifacts = state.get("artifacts")
    artifact_refs: dict[str, object] = {}
    if isinstance(artifacts, dict):
        for role, raw in artifacts.items():
            path = Path(str(raw))
            relative = logical_path(path, root)
            if relative:
                artifact_refs[str(role)] = {
                    "path_kind": "run_relative",
                    "logical_path": relative,
                }
    result["artifacts"] = artifact_refs
    return result
