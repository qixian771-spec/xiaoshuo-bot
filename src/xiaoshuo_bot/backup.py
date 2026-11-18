from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import zipfile
import yaml

from .llm.base import redact_secrets
from .models import SHANGHAI_TZ

REDACTED_PLACEHOLDER = "[REDACTED_IN_BACKUP]"

_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_keys",
        "management_key",
        "admin_token",
        "bot_token",
        "telegram_bot_token",
        "hmac_secret",
        "secret_key",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "authorization",
        "x_api_key",
        "x-api-key",
        "x_management_key",
        "x-management-key",
    }
)

_EXCLUDED_DIR_NAMES = frozenset(
    {
        "_archive",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "auth-files",
        "chrome_profiles",
        "node_modules",
    }
)

_EXCLUDED_FILE_SUFFIXES = frozenset({".env", ".key", ".pem", ".tmp", ".lock"})


class BackupError(RuntimeError):
    """Base exception for portable site backup, restore, and git backup operations."""


class BackupManifestError(BackupError):
    """Raised when backup manifest is missing, malformed, unsafe, or fails SHA256 verification."""


@dataclass(frozen=True)
class BackupManifestEntry:
    archive_path: str
    category: str
    sha256: str
    size_bytes: int
    secrets_stripped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BackupManifestEntry:
        return cls(
            archive_path=str(data["archive_path"]),
            category=str(data.get("category", "data")),
            sha256=str(data["sha256"]),
            size_bytes=int(data["size_bytes"]),
            secrets_stripped=bool(data.get("secrets_stripped", False)),
        )


@dataclass(frozen=True)
class BackupExportResult:
    zip_path: Path
    manifest_sha256: str
    file_count: int
    total_bytes: int
    stripped_secret_files: List[str]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zip_path": str(self.zip_path),
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "stripped_secret_files": list(self.stripped_secret_files),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class BackupManifest:
    version: str
    created_at: str
    timezone: str
    file_count: int
    files: List[BackupManifestEntry]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "timezone": self.timezone,
            "file_count": self.file_count,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BackupManifest:
        if not isinstance(data, Mapping) or "files" not in data:
            raise BackupManifestError("Invalid backup_manifest.json structure: missing 'files'")
        entries = [BackupManifestEntry.from_dict(item) for item in data["files"]]
        return cls(
            version=str(data.get("version", "2.0")),
            created_at=str(data.get("created_at", "")),
            timezone=str(data.get("timezone", "Asia/Shanghai")),
            file_count=int(data.get("file_count", len(entries))),
            files=entries,
        )


@dataclass(frozen=True)
class BackupImportResult:
    target_dir: Path
    restored_count: int
    archived_count: int
    archive_dir: Optional[Path]
    manifest_created_at: str
    restored_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_dir": str(self.target_dir),
            "restored_count": self.restored_count,
            "archived_count": self.archived_count,
            "archive_dir": str(self.archive_dir) if self.archive_dir else None,
            "manifest_created_at": self.manifest_created_at,
            "restored_files": list(self.restored_files),
        }


@dataclass(frozen=True)
class GitBackupResult:
    repo_dir: Path
    committed: bool
    pushed: bool
    commit_hash: str
    branch: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_dir": str(self.repo_dir),
            "committed": self.committed,
            "pushed": self.pushed,
            "commit_hash": self.commit_hash,
            "branch": self.branch,
            "message": self.message,
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_excluded_path(rel_parts: Tuple[str, ...], file_name: str) -> bool:
    lower_parts = {p.lower() for p in rel_parts}
    if lower_parts & {d.lower() for d in _EXCLUDED_DIR_NAMES}:
        return True
    lower_name = file_name.lower()
    if lower_name == ".env" or lower_name.endswith(".env"):
        return True
    if any(lower_name.endswith(sfx) for sfx in _EXCLUDED_FILE_SUFFIXES):
        return True
    if "secret" in lower_name or lower_name.startswith("cpa_auth") or lower_name.endswith("-wal") or lower_name.endswith("-shm"):
        return True
    return False


def _strip_secrets_from_structure(obj: Any) -> Tuple[Any, bool]:
    """Recursively strip sensitive secret keys and token strings from parsed JSON/YAML data."""
    changed = False
    if isinstance(obj, dict):
        cleaned: Dict[Any, Any] = {}
        for k, v in obj.items():
            key_norm = str(k).strip().lower()
            if key_norm in _SECRET_FIELD_NAMES:
                if v not in ("", None, [], {}):
                    changed = True
                if isinstance(v, list):
                    cleaned[k] = [REDACTED_PLACEHOLDER for _ in v]
                elif isinstance(v, dict):
                    cleaned[k] = {sk: REDACTED_PLACEHOLDER for sk in v}
                else:
                    cleaned[k] = REDACTED_PLACEHOLDER
            else:
                sub_val, sub_changed = _strip_secrets_from_structure(v)
                cleaned[k] = sub_val
                if sub_changed:
                    changed = True
        return cleaned, changed
    if isinstance(obj, list):
        out_list = []
        for item in obj:
            sub_val, sub_changed = _strip_secrets_from_structure(item)
            out_list.append(sub_val)
            if sub_changed:
                changed = True
        return out_list, changed
    if isinstance(obj, str):
        redacted = redact_secrets(obj)
        if redacted != obj:
            return redacted, True
        return obj, False
    return obj, False


def _sanitize_file_bytes(src_path: Path) -> Tuple[bytes, bool]:
    """Read file bytes safely, snapshotting SQLite DBs cleanly and stripping plaintext secrets."""
    suffix = src_path.suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        with tempfile.TemporaryDirectory(prefix="xsb_db_bak_") as tmpdir:
            dest_db = Path(tmpdir) / src_path.name
            src_conn = sqlite3.connect(str(src_path))
            try:
                dst_conn = sqlite3.connect(str(dest_db))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            return dest_db.read_bytes(), False

    raw_bytes = src_path.read_bytes()
    if suffix == ".json":
        text = raw_bytes.decode("utf-8")
        try:
            parsed = json.loads(text)
            cleaned, changed = _strip_secrets_from_structure(parsed)
            if changed:
                return json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8"), True
        except json.JSONDecodeError:
            pass
        redacted_text = redact_secrets(text)
        return redacted_text.encode("utf-8"), redacted_text != text

    if suffix in {".yaml", ".yml"}:
        text = raw_bytes.decode("utf-8")
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, (dict, list)):
                cleaned, changed = _strip_secrets_from_structure(parsed)
                if changed:
                    dumped = yaml.safe_dump(
                        cleaned,
                        allow_unicode=True,
                        sort_keys=False,
                        default_flow_style=False,
                    )
                    return dumped.encode("utf-8"), True
        except yaml.YAMLError:
            pass
        redacted_text = redact_secrets(text)
        return redacted_text.encode("utf-8"), redacted_text != text

    if suffix in {".txt", ".md", ".jsonl", ".log", ".toml", ".cfg", ".ini"}:
        text = raw_bytes.decode("utf-8")
        redacted_text = redact_secrets(text)
        return redacted_text.encode("utf-8"), redacted_text != text

    return raw_bytes, False


def _validate_archive_member_path(archive_path: str) -> PurePosixPath:
    if not archive_path or "\\" in archive_path or archive_path.startswith("/"):
        raise BackupManifestError(f"Unsafe archive member path: {archive_path!r}")
    pure = PurePosixPath(archive_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BackupManifestError(f"Illegal path traversal in archive member: {archive_path!r}")
    return pure


def _collect_tree_files(root_dir: Path, prefix: str, category: str) -> List[Tuple[Path, str, str]]:
    if not root_dir.exists():
        raise BackupError(f"Backup source directory does not exist: '{root_dir}'")
    if not root_dir.is_dir():
        raise BackupError(f"Backup source path is not a directory: '{root_dir}'")
    collected: List[Tuple[Path, str, str]] = []
    for p in sorted(root_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root_dir)
        if _is_excluded_path(rel.parts[:-1], p.name):
            continue
        arc_path = f"{prefix}/{rel.as_posix()}" if prefix else rel.as_posix()
        collected.append((p, arc_path, category))
    return collected


def export_backup(
    *,
    output_zip: str | Path,
    projects_dir: Optional[str | Path] = None,
    data_dir: Optional[str | Path] = None,
    motif_store_path: Optional[str | Path] = None,
    ledger_path: Optional[str | Path] = None,
    extra_files: Optional[Sequence[str | Path]] = None,
) -> BackupExportResult:
    """Create a portable ZIP backup with plaintext secrets stripped and a SHA256 manifest."""
    sources: List[Tuple[Path, str, str]] = []

    if projects_dir is not None:
        sources.extend(_collect_tree_files(Path(projects_dir).resolve(), "projects", "projects"))
    if data_dir is not None:
        sources.extend(_collect_tree_files(Path(data_dir).resolve(), "data", "data"))

    seen_arc: set[str] = {arc for _, arc, _ in sources}

    def _add_single(path_val: str | Path, prefix_dir: str, category: str) -> None:
        p = Path(path_val).resolve()
        if not p.is_file():
            raise BackupError(f"Backup file does not exist: '{p}'")
        if _is_excluded_path((), p.name):
            raise BackupError(f"Refusing to pack excluded secret/temp file: '{p}'")
        arc = f"{prefix_dir}/{p.name}" if prefix_dir else p.name
        if arc in seen_arc:
            # Avoid duplicate entries if already collected under data_dir
            return
        seen_arc.add(arc)
        sources.append((p, arc, category))

    if motif_store_path is not None:
        _add_single(motif_store_path, "evolution", "evolution")
    if ledger_path is not None:
        _add_single(ledger_path, "ledgers", "ledgers")
    if extra_files:
        for ef in extra_files:
            _add_single(ef, "extra", "extra")

    if not sources:
        raise BackupError(
            "No backup sources supplied or no eligible files found to export."
        )

    out_zip = Path(output_zip).resolve()
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = out_zip.with_name(f"{out_zip.name}.tmp")

    created_at = datetime.now(SHANGHAI_TZ).isoformat()
    manifest_entries: List[BackupManifestEntry] = []
    stripped_files: List[str] = []
    total_bytes = 0

    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src_path, arc_path, category in sorted(sources, key=lambda x: x[1]):
            _validate_archive_member_path(arc_path)
            payload_bytes, was_stripped = _sanitize_file_bytes(src_path)
            digest = _sha256_bytes(payload_bytes)
            zf.writestr(arc_path, payload_bytes)
            if was_stripped:
                stripped_files.append(arc_path)
            total_bytes += len(payload_bytes)
            manifest_entries.append(
                BackupManifestEntry(
                    archive_path=arc_path,
                    category=category,
                    sha256=digest,
                    size_bytes=len(payload_bytes),
                    secrets_stripped=was_stripped,
                )
            )

        manifest = BackupManifest(
            version="2.0",
            created_at=created_at,
            timezone="Asia/Shanghai",
            file_count=len(manifest_entries),
            files=manifest_entries,
        )
        manifest_bytes = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        manifest_sha256 = _sha256_bytes(manifest_bytes)
        zf.writestr("backup_manifest.json", manifest_bytes)

    os.replace(tmp_zip, out_zip)
    return BackupExportResult(
        zip_path=out_zip,
        manifest_sha256=manifest_sha256,
        file_count=len(manifest_entries),
        total_bytes=total_bytes,
        stripped_secret_files=stripped_files,
        created_at=created_at,
    )


def verify_backup(zip_path: str | Path) -> BackupManifest:
    """Verify backup ZIP integrity: safe member paths, zero extra files, and 100% SHA256 match."""
    zp = Path(zip_path).resolve()
    if not zp.is_file():
        raise BackupManifestError(f"Backup archive does not exist: '{zp}'")

    with zipfile.ZipFile(zp, "r") as zf:
        names = zf.namelist()
        if "backup_manifest.json" not in names:
            raise BackupManifestError(
                f"Backup archive '{zp}' is missing 'backup_manifest.json'"
            )
        try:
            manifest_raw = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupManifestError(
                f"Corrupt backup_manifest.json in '{zp}': {exc}"
            ) from exc

        manifest = BackupManifest.from_dict(manifest_raw)
        if manifest.file_count != len(manifest.files):
            raise BackupManifestError(
                f"Manifest file_count mismatch: declared {manifest.file_count}, actual {len(manifest.files)}"
            )

        expected_map: Dict[str, BackupManifestEntry] = {}
        for entry in manifest.files:
            _validate_archive_member_path(entry.archive_path)
            if entry.archive_path in expected_map:
                raise BackupManifestError(
                    f"Duplicate archive_path in manifest: {entry.archive_path!r}"
                )
            expected_map[entry.archive_path] = entry

        actual_members = {n for n in names if n != "backup_manifest.json" and not n.endswith("/")}
        missing = sorted(set(expected_map.keys()) - actual_members)
        if missing:
            raise BackupManifestError(
                f"Backup archive missing manifest-declared file(s): {missing[:5]}"
            )
        extra = sorted(actual_members - set(expected_map.keys()))
        if extra:
            raise BackupManifestError(
                f"Backup archive contains unexpected undeclared file(s): {extra[:5]}"
            )

        for arc_path, entry in expected_map.items():
            raw = zf.read(arc_path)
            if len(raw) != entry.size_bytes:
                raise BackupManifestError(
                    f"Backup size mismatch for '{arc_path}': expected {entry.size_bytes}, got {len(raw)}"
                )
            actual_sha = _sha256_bytes(raw)
            if actual_sha != entry.sha256:
                raise BackupManifestError(
                    f"Backup SHA256 mismatch for '{arc_path}': expected {entry.sha256}, got {actual_sha}"
                )

    return manifest


def import_backup(
    zip_path: str | Path,
    target_dir: str | Path,
    *,
    overwrite: bool = True,
) -> BackupImportResult:
    """Verify SHA256 manifest first, then atomically restore backup into target_dir (archiving displaced files)."""
    manifest = verify_backup(zip_path)
    zp = Path(zip_path).resolve()
    dest_root = Path(target_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
    archive_root = dest_root / "_archive" / f"backup_import_{stamp}"

    restored_files: List[str] = []
    archived_count = 0

    with zipfile.ZipFile(zp, "r") as zf:
        for entry in manifest.files:
            pure = _validate_archive_member_path(entry.archive_path)
            dest_file = (dest_root / Path(*pure.parts)).resolve()
            # Extra defense against escaping dest_root
            if dest_root != dest_file and dest_root not in dest_file.parents:
                raise BackupManifestError(
                    f"Refusing path escape outside target_dir: '{entry.archive_path}'"
                )

            if dest_file.exists():
                if not overwrite:
                    raise BackupError(
                        f"Destination file already exists and overwrite=False: '{dest_file}'"
                    )
                archived_target = archive_root / Path(*pure.parts)
                archived_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest_file), str(archived_target))
                archived_count += 1

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = dest_file.with_name(f"{dest_file.name}.tmp")
            tmp_file.write_bytes(zf.read(entry.archive_path))
            os.replace(tmp_file, dest_file)
            restored_files.append(str(dest_file))

    return BackupImportResult(
        target_dir=dest_root,
        restored_count=len(restored_files),
        archived_count=archived_count,
        archive_dir=archive_root if archived_count > 0 else None,
        manifest_created_at=manifest.created_at,
        restored_files=restored_files,
    )


def _run_git(repo_dir: Path, args: Sequence[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise BackupError(f"git {' '.join(args)} failed (rc={proc.returncode}): {err}")
    return proc.stdout.strip()


def git_incremental_backup(
    repo_dir: str | Path,
    *,
    paths: Optional[Sequence[str]] = None,
    remote: str = "origin",
    branch: Optional[str] = None,
    commit_message: Optional[str] = None,
    push: bool = False,
) -> GitBackupResult:
    """Perform an explicit-path incremental Git commit (and optional push) without 'git add -A'."""
    root = Path(repo_dir).resolve()
    if not (root / ".git").exists():
        raise BackupError(f"Not a git repository: '{root}'")

    target_paths = list(paths) if paths else ["projects"]
    existing_paths: List[str] = []
    for p in target_paths:
        cand = (root / p).resolve()
        if not cand.exists():
            raise BackupError(f"Backup path does not exist in repo: '{p}'")
        if _is_excluded_path((), cand.name):
            raise BackupError(f"Refusing to stage excluded secret path: '{p}'")
        existing_paths.append(p)

    _run_git(root, ["add", "--", *existing_paths])
    staged = _run_git(root, ["diff", "--cached", "--name-only"])
    current_branch = branch or _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])

    if not staged:
        head_hash = _run_git(root, ["rev-parse", "HEAD"])
        return GitBackupResult(
            repo_dir=root,
            committed=False,
            pushed=False,
            commit_hash=head_hash,
            branch=current_branch,
            message="No changes staged in backup paths.",
        )

    stamp = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    msg = commit_message or f"backup: incremental snapshot at {stamp}"
    _run_git(root, ["commit", "-m", msg])
    head_hash = _run_git(root, ["rev-parse", "HEAD"])

    pushed = False
    if push:
        _run_git(root, ["push", remote, current_branch])
        pushed = True

    return GitBackupResult(
        repo_dir=root,
        committed=True,
        pushed=pushed,
        commit_hash=head_hash,
        branch=current_branch,
        message=msg,
    )


__all__ = [
    "BackupError",
    "BackupExportResult",
    "BackupImportResult",
    "BackupManifest",
    "BackupManifestEntry",
    "BackupManifestError",
    "GitBackupResult",
    "REDACTED_PLACEHOLDER",
    "export_backup",
    "git_incremental_backup",
    "import_backup",
    "verify_backup",
]
