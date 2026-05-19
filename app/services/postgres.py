import asyncio
import hashlib
import hmac
import json
import secrets
import re
import time
from typing import Any
from urllib.parse import quote

import psycopg2
from psycopg2.extensions import connection
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import Settings
from models import Project, Storage, StorageDatabaseUser, utc_now


DEFAULT_POSTGRES_HOST = "postgres-storage"
DEFAULT_POSTGRES_PORT = 5432
IDENTIFIER_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(value: str) -> str:
    normalized = IDENTIFIER_RE.sub("_", (value or "").strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "storage"


def _trim_identifier(value: str, max_length: int) -> str:
    return value[:max_length].rstrip("_") or value[:max_length]


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def generate_password() -> str:
    return secrets.token_hex(16)


def get_storage_database_name(storage: Storage) -> str:
    config = storage.config if isinstance(storage.config, dict) else {}
    configured = (config.get("database") or "").strip().lower()
    if configured:
        return configured
    base = _slugify(storage.name)
    suffix = storage.id[:6].lower()
    return _trim_identifier(f"db_{base}_{suffix}", 63)


def get_storage_username(storage: Storage) -> str:
    return get_storage_database_name(storage)


def get_postgres_host(settings: Settings) -> str:
    return (settings.postgres_storage_host or DEFAULT_POSTGRES_HOST).strip() or DEFAULT_POSTGRES_HOST


def get_postgres_port(settings: Settings) -> int:
    return int(settings.postgres_storage_port or DEFAULT_POSTGRES_PORT)


def build_database_url(
    settings: Settings, database: str, username: str, password: str
) -> str:
    host = get_postgres_host(settings)
    port = get_postgres_port(settings)
    return (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def build_database_url_display(
    settings: Settings, database: str, username: str
) -> str:
    host = get_postgres_host(settings)
    port = get_postgres_port(settings)
    return f"postgresql://{quote(username, safe='')}@{host}:{port}/{quote(database, safe='')}"


def connect(
    settings: Settings,
    *,
    user: str,
    password: str,
    database: str | None = None,
    autocommit: bool = True,
) -> connection:
    conn = psycopg2.connect(
        host=get_postgres_host(settings),
        port=get_postgres_port(settings),
        user=user,
        password=password,
        dbname=database or "postgres",
    )
    conn.autocommit = autocommit
    return conn


def connect_admin(settings: Settings, database: str | None = None) -> connection:
    return connect(
        settings,
        user=settings.postgres_storage_user or "postgres",
        password=settings.postgres_storage_password or "",
        database=database,
    )


def ensure_database(settings: Settings, storage: Storage) -> str:
    database = get_storage_database_name(storage)
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT 1 FROM pg_database WHERE datname = %s",
                (database,)
            )
            if not cursor.fetchone():
                cursor.execute(f"CREATE DATABASE {quote_identifier(database)}")
    finally:
        conn.close()
    return database


def ensure_user_access(
    settings: Settings,
    *,
    database: str,
    username: str,
    password: str,
) -> None:
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (username,)
            )
            if cursor.fetchone():
                cursor.execute(
                    f"ALTER USER {quote_identifier(username)} WITH PASSWORD %s",
                    (password,)
                )
            else:
                cursor.execute(
                    f"CREATE USER {quote_identifier(username)} WITH PASSWORD %s",
                    (password,)
                )
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON DATABASE {quote_identifier(database)} TO {quote_identifier(username)}"
            )
            cursor.execute(
                f"ALTER DATABASE {quote_identifier(database)} OWNER TO {quote_identifier(username)}"
            )
    finally:
        conn.close()


def revoke_user_access(settings: Settings, username: str) -> None:
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (username,)
            )
            if cursor.fetchone():
                cursor.execute(f"DROP OWNED BY {quote_identifier(username)}")
                cursor.execute(f"DROP USER {quote_identifier(username)}")
    finally:
        conn.close()


def drop_database(settings: Settings, storage: Storage) -> None:
    database = get_storage_database_name(storage)
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database,)
            )
            cursor.execute(f"DROP DATABASE IF EXISTS {quote_identifier(database)}")
    finally:
        conn.close()


def ping(settings: Settings) -> None:
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    finally:
        conn.close()


def wait_until_ready(
    settings: Settings, *, timeout_seconds: int = 60, interval_seconds: int = 2
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            ping(settings)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(interval_seconds)

    if last_error:
        raise last_error


def build_connection_context(
    settings: Settings,
    *,
    storage: Storage,
    username: str,
    password: str,
) -> dict[str, Any]:
    database = get_storage_database_name(storage)
    host = get_postgres_host(settings)
    port = get_postgres_port(settings)
    return {
        "database": database,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "database_url": build_database_url(
            settings, database=database, username=username, password=password
        ),
        "database_url_display": build_database_url_display(
            settings, database=database, username=username
        ),
    }


async def get_storage_db_users(
    db: AsyncSession, storage_id: str
) -> list[StorageDatabaseUser]:
    result = await db.execute(
        select(StorageDatabaseUser)
        .where(StorageDatabaseUser.storage_id == storage_id)
        .options(selectinload(StorageDatabaseUser.project))
        .order_by(StorageDatabaseUser.scope.asc(), StorageDatabaseUser.username.asc())
    )
    return result.scalars().all()


async def get_storage_db_user_by_id(
    db: AsyncSession, storage_id: str, user_id: str
) -> StorageDatabaseUser | None:
    result = await db.execute(
        select(StorageDatabaseUser).where(
            StorageDatabaseUser.storage_id == storage_id,
            StorageDatabaseUser.id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_storage_admin_user(
    db: AsyncSession, storage_id: str
) -> StorageDatabaseUser | None:
    result = await db.execute(
        select(StorageDatabaseUser).where(
            StorageDatabaseUser.storage_id == storage_id,
            StorageDatabaseUser.scope == "admin",
        )
    )
    return result.scalar_one_or_none()


async def ensure_storage_admin_user(
    db: AsyncSession,
    settings: Settings,
    storage: Storage,
    *,
    created_by_user_id: int | None = None,
    rotate_password: bool = False,
) -> StorageDatabaseUser:
    db_user = await get_storage_admin_user(db, storage.id)
    old_username = db_user.username if db_user else None
    username = get_storage_username(storage)
    password = (
        generate_password()
        if db_user is None or rotate_password
        else db_user.password
    )

    if db_user is None:
        db_user = StorageDatabaseUser(
            storage_id=storage.id,
            username=username,
            scope="admin",
            created_by_user_id=created_by_user_id,
        )
        db_user.password = password
        db.add(db_user)
        await db.flush()

    db_user.username = username
    db_user.password = password
    db_user.updated_at = utc_now()

    if storage.status == "active":
        await asyncio.to_thread(
            ensure_user_access,
            settings,
            database=get_storage_database_name(storage),
            username=username,
            password=password,
        )
        if old_username and old_username != username:
            await asyncio.to_thread(revoke_user_access, settings, old_username)

    config = storage.config.copy() if isinstance(storage.config, dict) else {}
    config.update(
        {
            "engine": "postgres",
            "database": get_storage_database_name(storage),
            "username": username,
            "database_url_display": build_database_url_display(
                settings,
                database=get_storage_database_name(storage),
                username=username,
            ),
        }
    )
    storage.config = config
    return db_user


async def rotate_user_password(
    db: AsyncSession,
    settings: Settings,
    storage: Storage,
    db_user: StorageDatabaseUser,
) -> StorageDatabaseUser:
    db_user.password = generate_password()
    db_user.updated_at = utc_now()
    if storage.status == "active":
        await asyncio.to_thread(
            ensure_user_access,
            settings,
            database=get_storage_database_name(storage),
            username=db_user.username,
            password=db_user.password,
        )
    return db_user


async def delete_user(
    db: AsyncSession,
    settings: Settings,
    storage: Storage,
    db_user: StorageDatabaseUser,
) -> None:
    if storage.status == "active":
        await asyncio.to_thread(revoke_user_access, settings, db_user.username)
    await db.delete(db_user)
