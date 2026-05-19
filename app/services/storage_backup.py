from __future__ import annotations

import gzip
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings
from models import Storage, StorageDatabaseUser, utc_now
from services import mariadb as mariadb_service
from services import postgres as postgres_service

logger = logging.getLogger(__name__)

BACKUP_FILENAME_RE = re.compile(
    r"^(\d{8}T\d{6}Z)_[a-f0-9]{8}\.(sql|sqlite)\.gz$"
)

BACKUP_SCHEDULES: dict[str, dict[str, Any]] = {
    "every_5h": {
        "interval_hours": 5,
        "label_key": "Every 5 hours",
        "hint_key": "Runs every 5 hours (minimum interval).",
    },
    "every_12h": {
        "interval_hours": 12,
        "label_key": "Every 12 hours",
        "hint_key": "Runs twice per day.",
    },
    "daily_morning": {
        "interval_hours": 24,
        "run_hour": 6,
        "label_key": "Daily at 6:00",
        "hint_key": "Runs once per day at 06:00 UTC.",
    },
    "daily_night": {
        "interval_hours": 24,
        "run_hour": 2,
        "label_key": "Daily at 2:00",
        "hint_key": "Runs once per day at 02:00 UTC.",
    },
    "weekly": {
        "interval_hours": 168,
        "run_weekday": 6,
        "run_hour": 3,
        "label_key": "Weekly (Sunday 3:00)",
        "hint_key": "Runs every Sunday at 03:00 UTC.",
    },
}

MIN_INTERVAL_HOURS = 5
MAX_STORED_BACKUPS = 20
DEFAULT_MAX_BACKUPS = 20
BACKUP_COUNT_CHOICES = (5, 10, 15, 20)


@dataclass
class BackupEntry:
    id: str
    filename: str
    size_bytes: int
    format: str
    storage_type: str
    created_at: datetime


def schedule_choices() -> list[tuple[str, str]]:
    return [(key, meta["label_key"]) for key, meta in BACKUP_SCHEDULES.items()]


def get_backup_config(storage: Storage) -> dict[str, Any]:
    config = storage.config if isinstance(storage.config, dict) else {}
    backup = config.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    schedule = backup.get("schedule") or "every_12h"
    if schedule not in BACKUP_SCHEDULES:
        schedule = "every_12h"
    preset = BACKUP_SCHEDULES[schedule]
    max_backups = int(backup.get("max_backups") or DEFAULT_MAX_BACKUPS)
    if max_backups not in BACKUP_COUNT_CHOICES:
        max_backups = DEFAULT_MAX_BACKUPS
    return {
        "enabled": bool(backup.get("enabled")),
        "schedule": schedule,
        "max_backups": max_backups,
        "last_backup_at": backup.get("last_backup_at"),
        "last_backup_status": backup.get("last_backup_status"),
        "last_backup_error": backup.get("last_backup_error"),
    }


def set_backup_config(
    storage: Storage, *, enabled: bool, schedule: str, max_backups: int
) -> None:
    if schedule not in BACKUP_SCHEDULES:
        raise ValueError("Invalid backup schedule")
    count = int(max_backups)
    if count not in BACKUP_COUNT_CHOICES:
        count = DEFAULT_MAX_BACKUPS
    config = storage.config.copy() if isinstance(storage.config, dict) else {}
    backup = config.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    was_enabled = bool(backup.get("enabled"))
    backup.update(
        {
            "enabled": enabled,
            "schedule": schedule,
            "max_backups": count,
        }
    )
    if enabled and not was_enabled:
        backup["enabled_at"] = datetime.now(timezone.utc).isoformat()
    if not enabled:
        backup.pop("enabled_at", None)
    config["backup"] = backup
    storage.config = config


def backup_root(settings: Settings, storage: Storage) -> Path:
    return (
        Path(settings.data_dir)
        / "backups"
        / "storage"
        / storage.team_id
        / storage.id
    )


def _parse_backup_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def should_run_backup(storage: Storage, now: datetime | None = None) -> bool:
    cfg = get_backup_config(storage)
    if not cfg["enabled"] or storage.status != "active":
        return False
    if storage.type not in ("database", "mariadb", "postgres"):
        return False

    now = now or datetime.now(timezone.utc)
    preset = BACKUP_SCHEDULES[cfg["schedule"]]
    last = _parse_backup_time(cfg["last_backup_at"])
    interval = timedelta(hours=int(preset["interval_hours"]))

    if last is None:
        backup_cfg = (
            storage.config.get("backup")
            if isinstance(storage.config, dict)
            else None
        )
        enabled_at = _parse_backup_time(
            backup_cfg.get("enabled_at") if isinstance(backup_cfg, dict) else None
        )
        if enabled_at is None:
            return False
        return now - enabled_at >= interval

    if now - last < interval:
        return False

    run_hour = preset.get("run_hour")
    if run_hour is not None:
        if now.hour != int(run_hour):
            return False
        run_weekday = preset.get("run_weekday")
        if run_weekday is not None and now.weekday() != int(run_weekday):
            return False
        if now - last < timedelta(hours=20):
            return False

    return True


def list_backups(settings: Settings, storage: Storage) -> list[BackupEntry]:
    root = backup_root(settings, storage)
    if not root.is_dir():
        return []

    entries: list[BackupEntry] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        match = BACKUP_FILENAME_RE.match(path.name)
        if not match:
            continue
        stamp, ext = match.groups()
        try:
            created = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if ext == "sql":
            storage_type = "mariadb" if "mariadb" in path.name.lower() else "postgres"
        else:
            storage_type = "sqlite"
        entries.append(
            BackupEntry(
                id=path.name,
                filename=path.name,
                size_bytes=path.stat().st_size,
                format="gzip",
                storage_type=storage_type,
                created_at=created,
            )
        )
    entries.sort(key=lambda item: item.created_at, reverse=True)
    return entries


def resolve_backup_path(
    settings: Settings, storage: Storage, backup_id: str
) -> Path | None:
    if not re.fullmatch(r"[\w.-]+", backup_id or ""):
        return None
    root = backup_root(settings, storage).resolve()
    path = (root / backup_id).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        legacy = (root / f"{backup_id}.gz").resolve()
        if str(legacy).startswith(str(root)) and legacy.is_file():
            path = legacy
        else:
            return None
    if not BACKUP_FILENAME_RE.match(path.name):
        return None
    return path


def _new_backup_filename(storage: Storage) -> tuple[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(4)
    if storage.type == "mariadb":
        ext = "sql"
    elif storage.type == "postgres":
        ext = "sql"
    else:
        ext = "sqlite"
    filename = f"{stamp}_{suffix}.{ext}.gz"
    return filename, filename


def _gzip_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, gzip.open(destination, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)


def _backup_sqlite(settings: Settings, storage: Storage, out_path: Path) -> None:
    db_path = (
        Path(settings.data_dir)
        / "storage"
        / storage.team_id
        / "database"
        / storage.name
        / "db.sqlite"
    )
    if not db_path.is_file():
        raise FileNotFoundError("SQLite database file not found")

    tmp_path = out_path.with_suffix("")
    src = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(tmp_path))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    try:
        _gzip_file(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _backup_mariadb(
    settings: Settings,
    storage: Storage,
    admin_user: StorageDatabaseUser,
    out_path: Path,
) -> None:
    database = mariadb_service.get_storage_database_name(storage)
    host = mariadb_service.get_mariadb_host(settings)
    port = mariadb_service.get_mariadb_port(settings)
    env = os.environ.copy()
    env["MYSQL_PWD"] = admin_user.password

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump_cmd = [
        "mariadb-dump",
        f"--host={host}",
        f"--port={port}",
        f"--user={admin_user.username}",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--default-character-set=utf8mb4",
        database,
    ]
    try:
        with gzip.open(out_path, "wb", compresslevel=9) as gz:
            subprocess.run(
                dump_cmd,
                env=env,
                stdout=gz,
                stderr=subprocess.PIPE,
                check=True,
            )
    except FileNotFoundError:
        dump_cmd[0] = "mysqldump"
        with gzip.open(out_path, "wb", compresslevel=9) as gz:
            subprocess.run(
                dump_cmd,
                env=env,
                stdout=gz,
                stderr=subprocess.PIPE,
                check=True,
            )


def prune_backups(settings: Settings, storage: Storage, max_backups: int) -> None:
    entries = list_backups(settings, storage)
    if len(entries) <= max_backups:
        return
    root = backup_root(settings, storage)
    for entry in entries[max_backups:]:
        path = root / entry.filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove old backup %s: %s", path, exc)


def _backup_postgres(
    settings: Settings,
    storage: Storage,
    admin_user: StorageDatabaseUser,
    out_path: Path,
) -> None:
    database = postgres_service.get_storage_database_name(storage)
    host = postgres_service.get_postgres_host(settings)
    port = postgres_service.get_postgres_port(settings)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump_cmd = [
        "pg_dump",
        f"--host={host}",
        f"--port={port}",
        f"--username={admin_user.username}",
        "--format=custom",
        "--verbose",
        database,
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = admin_user.password
    with gzip.open(out_path, "wb", compresslevel=9) as gz:
        subprocess.run(
            dump_cmd,
            env=env,
            stdout=gz,
            stderr=subprocess.PIPE,
            check=True,
        )


def create_backup(
    settings: Settings,
    storage: Storage,
    admin_user: StorageDatabaseUser | None = None,
) -> BackupEntry:
    if storage.type not in ("database", "mariadb", "postgres"):
        raise ValueError("Backups are only supported for SQLite, MariaDB and PostgreSQL storage")
    if storage.status != "active":
        raise ValueError("Storage must be active")

    backup_id, filename = _new_backup_filename(storage)
    out_path = backup_root(settings, storage) / filename

    if storage.type == "database":
        _backup_sqlite(settings, storage, out_path)
    elif storage.type == "mariadb":
        if admin_user is None:
            raise ValueError("MariaDB admin user is required")
        _backup_mariadb(settings, storage, admin_user, out_path)
    elif storage.type == "postgres":
        if admin_user is None:
            raise ValueError("PostgreSQL admin user is required")
        _backup_postgres(settings, storage, admin_user, out_path)

    _apply_permissions(settings, out_path.parent)

    cfg = get_backup_config(storage)
    max_backups = min(int(cfg.get("max_backups") or DEFAULT_MAX_BACKUPS), MAX_STORED_BACKUPS)
    prune_backups(settings, storage, max_backups)

    entry = list_backups(settings, storage)[0]
    return entry


def _apply_permissions(settings: Settings, directory: Path) -> None:
    uid = int(settings.service_uid)
    gid = int(settings.service_gid)
    try:
        os.chown(directory, uid, gid)
        os.chmod(directory, 0o750)
        for path in directory.iterdir():
            os.chown(path, uid, gid)
            os.chmod(path, 0o640)
    except OSError as exc:
        logger.warning("Failed to set backup permissions: %s", exc)


def record_backup_result(
    storage: Storage, *, status: str, error: str | None = None
) -> None:
    config = storage.config.copy() if isinstance(storage.config, dict) else {}
    backup = config.get("backup")
    if not isinstance(backup, dict):
        backup = {}
    backup["last_backup_at"] = datetime.now(timezone.utc).isoformat()
    backup["last_backup_status"] = status
    backup["last_backup_error"] = error
    config["backup"] = backup
    storage.config = config


def remove_all_backups(settings: Settings, storage: Storage) -> None:
    root = backup_root(settings, storage)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
