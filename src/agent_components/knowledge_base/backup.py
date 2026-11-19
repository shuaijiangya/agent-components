"""将 SQLite 快照和文件产物打包为可独立恢复的归档。"""

import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from pydantic import Field

from agent_components.core.contracts import Contract
from agent_components.knowledge_base.artifacts import (
    _SHA256,
    ArtifactRef,
    ArtifactStore,
    _check_artifact,
)

_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "database.sqlite3"


class _BackupManifest(Contract):
    version: int = Field(strict=True, ge=1, le=1)
    database_sha256: _SHA256
    database_size: int = Field(ge=0, strict=True)
    artifacts: tuple[ArtifactRef, ...]


def _artifact_name(ref: ArtifactRef) -> str:
    return f"artifacts/{ref.kind}-{ref.sha256}"


def _copy_with_digest(source: IO[bytes], target: IO[bytes]) -> tuple[str, int]:
    digest = sha256()
    size = 0
    while block := source.read(1024 * 1024):
        target.write(block)
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _check_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            result = db.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError("backup contains an invalid SQLite snapshot") from exc
    if result != [("ok",)]:
        raise ValueError("backup contains an invalid SQLite snapshot")


def write_backup(
    database_snapshot: Path,
    refs: Sequence[ArtifactRef],
    artifacts: ArtifactStore,
    destination: Path,
) -> None:
    """原子保存一致的 SQLite 快照和引用产物，保留路径上的既有文件。"""
    if os.path.lexists(destination):
        raise FileExistsError(f"backup path already exists: {destination}")
    unique_refs = tuple(dict.fromkeys(ArtifactRef.model_validate(ref) for ref in refs))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as work_dir:
        pending = Path(work_dir) / "backup.zip"
        pending.touch(mode=0o600)
        with pending.open("r+b") as output:
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                with (
                    database_snapshot.open("rb") as source,
                    archive.open(_DATABASE_NAME, "w", force_zip64=True) as target,
                ):
                    digest, size = _copy_with_digest(source, target)
                for ref in unique_refs:
                    data = artifacts.read(ref)
                    _check_artifact(ref, data)
                    archive.writestr(_artifact_name(ref), data)
                manifest = _BackupManifest(
                    version=1, database_sha256=digest, database_size=size, artifacts=unique_refs
                )
                archive.writestr(_MANIFEST_NAME, manifest.model_dump_json())
            output.flush()
            os.fsync(output.fileno())
        os.link(pending, destination)


def _stage_backup(archive: ZipFile, staging: Path) -> tuple[ArtifactRef, ...]:
    manifest = _BackupManifest.model_validate_json(archive.read(_MANIFEST_NAME))
    expected = [_MANIFEST_NAME, _DATABASE_NAME, *map(_artifact_name, manifest.artifacts)]
    if len(set(expected)) != len(expected) or sorted(archive.namelist()) != sorted(expected):
        raise ValueError("backup entries do not match the manifest")

    pending = staging / _DATABASE_NAME
    pending.touch(mode=0o600)
    with archive.open(_DATABASE_NAME) as source, pending.open("wb") as target:
        digest, size = _copy_with_digest(source, target)
        if (digest, size) != (manifest.database_sha256, manifest.database_size):
            raise ValueError("backup database does not match its size or SHA-256 checksum")
        target.flush()
        os.fsync(target.fileno())
    _check_database(pending)

    for index, ref in enumerate(manifest.artifacts):
        data = archive.read(_artifact_name(ref))
        _check_artifact(ref, data)
        (staging / str(index)).write_bytes(data)
    return manifest.artifacts


def restore_backup(archive: Path, database: Path, artifacts: ArtifactStore) -> None:
    """校验归档成员、恢复产物，再原子安装数据库到未占用的路径。"""
    if os.path.lexists(database):
        raise FileExistsError(f"restore database path already exists: {database}")
    database.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=database.parent) as work_dir:
        staging = Path(work_dir)
        try:
            with ZipFile(archive) as bundle:
                refs = _stage_backup(bundle, staging)
        except (BadZipFile, KeyError) as exc:
            raise ValueError("invalid knowledge base backup archive") from exc

        for index, ref in enumerate(refs):
            restored = artifacts.put(ref.kind, (staging / str(index)).read_bytes())
            if restored != ref:
                raise ValueError("restored artifact does not match the reference in the manifest")
        os.link(staging / _DATABASE_NAME, database)
