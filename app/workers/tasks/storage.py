import asyncio
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import attributes

from config import get_settings
from db import AsyncSessionLocal
from models import Storage, StorageDatabaseUser, StorageProject, utc_now
from services import mariadb as mariadb_service
from services import postgres as postgres_service
from services import storage_backup as storage_backup_service

logger = logging.getLogger(__name__)


async def provision_storage(ctx, resource_id: str):
    log_prefix = f"[ProvisionStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage provisioning")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(select(Storage).where(Storage.id == resource_id))
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return

        try:
            if storage.type == "database":
                await asyncio.to_thread(_ensure_database_path, settings, storage)
            elif storage.type == "mariadb":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                db_user_payloads = [
                    {
                        "username": db_user.username,
                        "password": db_user.password,
                        "scope": db_user.scope,
                    }
                    for db_user in db_users
                ]
                await asyncio.to_thread(
                    _ensure_mariadb_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    db_user_payloads,
                )
                database = mariadb_service.get_storage_database_name(storage)
                admin_user = next((user for user in db_users if user.scope == "admin"), None)
                if admin_user is None:
                    admin_user = StorageDatabaseUser(
                        storage_id=storage.id,
                        username=mariadb_service.get_storage_admin_username(storage),
                        scope="admin",
                        created_by_user_id=storage.created_by_user_id,
                    )
                    admin_user.password = mariadb_service.generate_password()
                    db.add(admin_user)
                    db_users.append(admin_user)
                    await db.flush()
                    db_user_payloads = [
                        {
                            "username": db_user.username,
                            "password": db_user.password,
                            "scope": db_user.scope,
                        }
                        for db_user in db_users
                    ]
                    await asyncio.to_thread(
                        _ensure_mariadb_storage,
                        settings,
                        storage.name,
                        storage.id,
                        storage.config if isinstance(storage.config, dict) else {},
                        db_user_payloads,
                    )
                config = storage.config.copy() if isinstance(storage.config, dict) else {}
                config.update(
                    {
                        "engine": "mariadb",
                        "database": database,
                        "admin_username": admin_user.username,
                    }
                )
                storage.config = config
            elif storage.type == "postgres":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                db_user_payloads = [
                    {
                        "username": db_user.username,
                        "password": db_user.password,
                        "scope": db_user.scope,
                    }
                    for db_user in db_users
                ]
                await asyncio.to_thread(
                    _ensure_postgres_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    db_user_payloads,
                )
                database = postgres_service.get_storage_database_name(storage)
                admin_user = next((user for user in db_users if user.scope == "admin"), None)
                if admin_user is None:
                    admin_user = StorageDatabaseUser(
                        storage_id=storage.id,
                        username=postgres_service.get_storage_username(storage),
                        scope="admin",
                        created_by_user_id=storage.created_by_user_id,
                    )
                    admin_user.password = postgres_service.generate_password()
                    db.add(admin_user)
                    db_users.append(admin_user)
                    await db.flush()
                    db_user_payloads = [
                        {
                            "username": db_user.username,
                            "password": db_user.password,
                            "scope": db_user.scope,
                        }
                        for db_user in db_users
                    ]
                    await asyncio.to_thread(
                        _ensure_postgres_storage,
                        settings,
                        storage.name,
                        storage.id,
                        storage.config if isinstance(storage.config, dict) else {},
                        db_user_payloads,
                    )
                config = storage.config.copy() if isinstance(storage.config, dict) else {}
                config.update(
                    {
                        "engine": "postgres",
                        "database": database,
                        "username": admin_user.username,
                    }
                )
                storage.config = config
            elif storage.type == "volume":
                await asyncio.to_thread(_ensure_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            storage.status = "active"
            storage.error = None
            storage.updated_at = utc_now()
            await db.commit()
            logger.info(f"{log_prefix} Storage provisioned")
        except Exception as exc:
            storage.error = {
                "stage": f"provision_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Provisioning failed: {exc}", exc_info=True)
            raise


async def deprovision_storage(ctx, resource_id: str):
    log_prefix = f"[DeprovisionStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage deprovisioning")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(select(Storage).where(Storage.id == resource_id))
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return

        try:
            if storage.type == "database":
                await asyncio.to_thread(_remove_database_path, settings, storage)
            elif storage.type == "mariadb":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                await asyncio.to_thread(
                    _remove_mariadb_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    [db_user.username for db_user in db_users],
                )
            elif storage.type == "postgres":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                await asyncio.to_thread(
                    _remove_postgres_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    [db_user.username for db_user in db_users],
                )
            elif storage.type == "volume":
                await asyncio.to_thread(_remove_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            await asyncio.to_thread(
                storage_backup_service.remove_all_backups, settings, storage
            )

            await db.execute(
                delete(StorageProject).where(StorageProject.storage_id == storage.id)
            )
            await db.execute(
                delete(StorageDatabaseUser).where(
                    StorageDatabaseUser.storage_id == storage.id
                )
            )
            await db.execute(delete(Storage).where(Storage.id == storage.id))
            await db.commit()
            logger.info(f"{log_prefix} Storage deprovisioned")
        except Exception as exc:
            storage.error = {
                "stage": f"deprovision_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Deprovisioning failed: {exc}", exc_info=True)
            raise


async def reset_storage(ctx, resource_id: str):
    log_prefix = f"[ResetStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage reset")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(select(Storage).where(Storage.id == resource_id))
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return

        try:
            storage.status = "resetting"
            storage.error = None
            storage.updated_at = utc_now()
            await db.commit()
            if storage.type == "database":
                await asyncio.to_thread(_reset_database_path, settings, storage)
            elif storage.type == "mariadb":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                await asyncio.to_thread(
                    _reset_mariadb_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    [
                        {
                            "username": db_user.username,
                            "password": db_user.password,
                        }
                        for db_user in db_users
                    ],
                )
            elif storage.type == "postgres":
                db_users = (
                    await db.execute(
                        select(StorageDatabaseUser).where(
                            StorageDatabaseUser.storage_id == storage.id
                        )
                    )
                ).scalars().all()
                await asyncio.to_thread(
                    _reset_postgres_storage,
                    settings,
                    storage.name,
                    storage.id,
                    storage.config if isinstance(storage.config, dict) else {},
                    [
                        {
                            "username": db_user.username,
                            "password": db_user.password,
                        }
                        for db_user in db_users
                    ],
                )
            elif storage.type == "volume":
                await asyncio.to_thread(_reset_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            storage.status = "active"
            storage.error = None
            storage.updated_at = utc_now()
            await db.commit()
            logger.info(f"{log_prefix} Storage reset")
        except Exception as exc:
            storage.status = "active"
            storage.error = {
                "stage": f"reset_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Reset failed: {exc}", exc_info=True)
            raise


def _ensure_database_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir)
        / "storage"
        / storage.team_id
        / "database"
        / storage.name
    )
    db_path = base_dir / "db.sqlite"

    base_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    finally:
        conn.close()
    _apply_storage_permissions(settings, base_dir, db_path)


def _ensure_volume_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir) / "storage" / storage.team_id / "volume" / storage.name
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    _apply_storage_permissions(settings, base_dir)


def _build_storage_stub(storage_name: str, storage_id: str, config: dict) -> Storage:
    storage = Storage(name=storage_name, type="mariadb", team_id="", config=config or {})
    storage.id = storage_id
    return storage


def _ensure_mariadb_storage(
    settings,
    storage_name: str,
    storage_id: str,
    config: dict,
    db_users: list[dict[str, str]],
) -> None:
    mariadb_service.wait_until_ready(settings)
    storage = _build_storage_stub(storage_name, storage_id, config)
    database = mariadb_service.ensure_database(settings, storage)
    for db_user in db_users:
        mariadb_service.ensure_user_access(
            settings,
            database=database,
            username=db_user["username"],
            password=db_user["password"],
        )


def _remove_database_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir)
        / "storage"
        / storage.team_id
        / "database"
        / storage.name
    )
    if base_dir.exists():
        shutil.rmtree(base_dir)


def _remove_mariadb_storage(
    settings, storage_name: str, storage_id: str, config: dict, usernames: list[str]
) -> None:
    mariadb_service.wait_until_ready(settings)
    storage = _build_storage_stub(storage_name, storage_id, config)
    mariadb_service.drop_database(settings, storage)
    for username in usernames:
        mariadb_service.revoke_user_access(settings, username)


def _build_postgres_storage_stub(storage_name: str, storage_id: str, config: dict) -> Storage:
    storage = Storage(name=storage_name, type="postgres", team_id="", config=config or {})
    storage.id = storage_id
    return storage


def _ensure_postgres_storage(
    settings,
    storage_name: str,
    storage_id: str,
    config: dict,
    db_users: list[dict[str, str]],
) -> None:
    postgres_service.wait_until_ready(settings)
    storage = _build_postgres_storage_stub(storage_name, storage_id, config)
    database = postgres_service.ensure_database(settings, storage)
    for db_user in db_users:
        postgres_service.ensure_user_access(
            settings,
            database=database,
            username=db_user["username"],
            password=db_user["password"],
        )


def _remove_postgres_storage(
    settings, storage_name: str, storage_id: str, config: dict, usernames: list[str]
) -> None:
    postgres_service.wait_until_ready(settings)
    storage = _build_postgres_storage_stub(storage_name, storage_id, config)
    postgres_service.drop_database(settings, storage)
    for username in usernames:
        postgres_service.revoke_user_access(settings, username)


def _reset_postgres_storage(
    settings,
    storage_name: str,
    storage_id: str,
    config: dict,
    db_users: list[dict[str, str]],
) -> None:
    postgres_service.wait_until_ready(settings)
    storage = _build_postgres_storage_stub(storage_name, storage_id, config)
    database = postgres_service.get_storage_database_name(storage)
    postgres_service.drop_database(settings, storage)
    postgres_service.ensure_database(settings, storage)
    for db_user in db_users:
        postgres_service.ensure_user_access(
            settings,
            database=database,
            username=db_user["username"],
            password=db_user["password"],
        )


def _remove_volume_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir) / "storage" / storage.team_id / "volume" / storage.name
    )
    if base_dir.exists():
        shutil.rmtree(base_dir)


def _reset_database_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir)
        / "storage"
        / storage.team_id
        / "database"
        / storage.name
    )
    if base_dir.exists():
        shutil.rmtree(base_dir)
    _ensure_database_path(settings, storage)


def _reset_mariadb_storage(
    settings,
    storage_name: str,
    storage_id: str,
    config: dict,
    db_users: list[dict[str, str]],
) -> None:
    mariadb_service.wait_until_ready(settings)
    storage = _build_storage_stub(storage_name, storage_id, config)
    database = mariadb_service.get_storage_database_name(storage)
    mariadb_service.drop_database(settings, storage)
    mariadb_service.ensure_database(settings, storage)
    for db_user in db_users:
        mariadb_service.ensure_user_access(
            settings,
            database=database,
            username=db_user["username"],
            password=db_user["password"],
        )


def _reset_volume_path(settings, storage: Storage) -> None:
    base_dir = (
        Path(settings.data_dir) / "storage" / storage.team_id / "volume" / storage.name
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    for entry in base_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    _apply_storage_permissions(settings, base_dir)


def _apply_storage_permissions(
    settings, base_dir: Path, db_path: Path | None = None
) -> None:
    uid = int(settings.service_uid)
    gid = int(settings.service_gid)
    try:
        os.chown(base_dir, uid, gid)
        os.chmod(base_dir, 0o777)
        if db_path and db_path.exists():
            os.chown(db_path, uid, gid)
            os.chmod(db_path, 0o666)
    except Exception as exc:
        logger.warning("Failed to set storage permissions: %s", exc)


async def backup_storage(ctx, storage_id: str):
    log_prefix = f"[BackupStorage:{storage_id}]"
    logger.info("%s Starting storage backup", log_prefix)
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(select(Storage).where(Storage.id == storage_id))
        ).scalar_one_or_none()
        if not storage:
            logger.error("%s Storage not found", log_prefix)
            return

        admin_user = None
        if storage.type == "mariadb":
            admin_user = await mariadb_service.get_storage_admin_user(db, storage.id)
            if admin_user is None:
                storage_backup_service.record_backup_result(
                    storage,
                    status="failed",
                    error="MariaDB admin user not found",
                )
                attributes.flag_modified(storage, "config")
                storage.updated_at = utc_now()
                await db.commit()
                return
        elif storage.type == "postgres":
            admin_user = await postgres_service.get_storage_admin_user(db, storage.id)
            if admin_user is None:
                storage_backup_service.record_backup_result(
                    storage,
                    status="failed",
                    error="PostgreSQL admin user not found",
                )
                attributes.flag_modified(storage, "config")
                storage.updated_at = utc_now()
                await db.commit()
                return

        try:
            await asyncio.to_thread(
                storage_backup_service.create_backup,
                settings,
                storage,
                admin_user,
            )
            storage_backup_service.record_backup_result(storage, status="ok", error=None)
            attributes.flag_modified(storage, "config")
            storage.updated_at = utc_now()
            await db.commit()
            logger.info("%s Backup completed", log_prefix)
        except Exception as exc:
            storage_backup_service.record_backup_result(
                storage, status="failed", error=str(exc)
            )
            attributes.flag_modified(storage, "config")
            storage.updated_at = utc_now()
            await db.commit()
            logger.error("%s Backup failed: %s", log_prefix, exc, exc_info=True)
            raise


async def storage_backup_scheduler(ctx):
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Storage).where(
                Storage.status == "active",
                Storage.type.in_(["database", "mariadb", "postgres"]),
            )
        )
        storages = result.scalars().all()

        queue = ctx["redis"]
        for storage in storages:
            if not storage_backup_service.should_run_backup(storage, now):
                continue
            job = await queue.enqueue_job("backup_storage", storage.id)
            logger.info(
                "[BackupScheduler] Enqueued backup for %s (job %s)",
                storage.id,
                job.job_id,
            )
