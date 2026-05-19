import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import pymysql
from pymysql.connections import Connection
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import Settings
from models import Project, Storage, StorageDatabaseUser, utc_now


DEFAULT_MARIADB_HOST = "mariadb"
DEFAULT_MARIADB_PORT = 3306
ACCOUNT_HOST = "%"
IDENTIFIER_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(value: str) -> str:
    normalized = IDENTIFIER_RE.sub("_", (value or "").strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "storage"


def _trim_identifier(value: str, max_length: int) -> str:
    return value[:max_length].rstrip("_") or value[:max_length]


def quote_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def quote_account_part(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def quote_account(username: str, host: str = ACCOUNT_HOST) -> str:
    return f"{quote_account_part(username)}@{quote_account_part(host)}"


def generate_password() -> str:
    return secrets.token_hex(16)


def get_storage_database_name(storage: Storage) -> str:
    config = storage.config if isinstance(storage.config, dict) else {}
    configured = (config.get("database") or "").strip().lower()
    if configured:
        return configured
    base = _slugify(storage.name)
    suffix = storage.id[:6].lower()
    return _trim_identifier(f"db_{base}_{suffix}", 64)


def get_storage_admin_username(storage: Storage) -> str:
    return get_storage_database_name(storage)


def get_project_database_username(storage: Storage, project_id: str) -> str:
    base = _slugify(storage.name)
    project_suffix = project_id[:6].lower()
    storage_suffix = storage.id[:4].lower()
    return _trim_identifier(f"app_{base}_{project_suffix}_{storage_suffix}", 80)


def get_mariadb_host(settings: Settings) -> str:
    return (settings.mariadb_host or DEFAULT_MARIADB_HOST).strip() or DEFAULT_MARIADB_HOST


def get_mariadb_port(settings: Settings) -> int:
    return int(settings.mariadb_port or DEFAULT_MARIADB_PORT)


def build_database_url(
    settings: Settings, database: str, username: str, password: str, *, driver: str = "mysql"
) -> str:
    host = get_mariadb_host(settings)
    port = get_mariadb_port(settings)
    return (
        f"{driver}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def build_database_url_display(
    settings: Settings, database: str, username: str, *, driver: str = "mysql"
) -> str:
    host = get_mariadb_host(settings)
    port = get_mariadb_port(settings)
    return f"{driver}://{quote(username, safe='')}@{host}:{port}/{quote(database, safe='')}"


def build_phpmyadmin_url(settings: Settings, storage: Storage) -> str | None:
    hostname = (settings.phpmyadmin_hostname or "").strip()
    if not hostname and settings.app_hostname:
        hostname = f"db.{settings.app_hostname}"
    if not hostname:
        return None
    database = quote(get_storage_database_name(storage), safe="")
    return (
        f"{settings.url_scheme}://{hostname}/index.php"
        f"?route=/database/structure&db={database}"
    )


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def build_phpmyadmin_signon_token(
    settings: Settings,
    *,
    storage: Storage,
    username: str,
    password: str,
    expires_in_seconds: int = 60,
) -> str:
    payload = {
        "db": get_storage_database_name(storage),
        "exp": int(time.time()) + expires_in_seconds,
        "p": password,
        "u": username,
    }
    payload_part = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature_part = _b64url_encode(
        hmac.new(
            settings.secret_key.encode(),
            payload_part.encode(),
            hashlib.sha256,
        ).digest()
    )
    return f"{payload_part}.{signature_part}"


def build_phpmyadmin_login_url(
    settings: Settings,
    *,
    storage: Storage,
    username: str,
    password: str,
) -> str | None:
    hostname = (settings.phpmyadmin_hostname or "").strip()
    if not hostname and settings.app_hostname:
        hostname = f"db.{settings.app_hostname}"
    if not hostname:
        return None
    query = urlencode(
        {
            "db": get_storage_database_name(storage),
            "devpush_token": build_phpmyadmin_signon_token(
                settings,
                storage=storage,
                username=username,
                password=password,
            ),
            "route": "/database/structure",
        }
    )
    return f"{settings.url_scheme}://{hostname}/devpush-login.php?{query}"


def connect(
    settings: Settings,
    *,
    user: str,
    password: str,
    database: str | None = None,
    autocommit: bool = True,
) -> Connection:
    return pymysql.connect(
        host=get_mariadb_host(settings),
        port=get_mariadb_port(settings),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=autocommit,
    )


def connect_admin(settings: Settings, database: str | None = None) -> Connection:
    return connect(
        settings,
        user=settings.mariadb_root_user,
        password=settings.mariadb_root_password,
        database=database,
    )


def ensure_database(settings: Settings, storage: Storage) -> str:
    database = get_storage_database_name(storage)
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {quote_identifier(database)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()
    return database


def ensure_user_access(
    settings: Settings,
    *,
    database: str,
    username: str,
    password: str,
    privileges: str = "ALL PRIVILEGES",
) -> None:
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            account = quote_account(username)
            account_for_param_query = account.replace("%", "%%")
            cursor.execute(
                f"CREATE USER IF NOT EXISTS {account_for_param_query} IDENTIFIED BY %s",
                (password,),
            )
            cursor.execute(
                f"ALTER USER {account_for_param_query} IDENTIFIED BY %s", (password,)
            )
            cursor.execute(
                f"GRANT {privileges} ON {quote_identifier(database)}.* TO {account}"
            )
            cursor.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()


def revoke_user_access(settings: Settings, username: str) -> None:
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP USER IF EXISTS {quote_account(username)}")
            cursor.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()


def drop_database(settings: Settings, storage: Storage) -> None:
    database = get_storage_database_name(storage)
    conn = connect_admin(settings)
    try:
        with conn.cursor() as cursor:
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
    host = get_mariadb_host(settings)
    port = get_mariadb_port(settings)
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
        "phpmyadmin_url": build_phpmyadmin_url(settings, storage),
        "phpmyadmin_login_url": build_phpmyadmin_login_url(
            settings,
            storage=storage,
            username=username,
            password=password,
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
    username = get_storage_admin_username(storage)
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
            "engine": "mariadb",
            "database": get_storage_database_name(storage),
            "admin_username": username,
            "database_url_display": build_database_url_display(
                settings,
                database=get_storage_database_name(storage),
                username=username,
            ),
        }
    )
    phpmyadmin_url = build_phpmyadmin_url(settings, storage)
    if phpmyadmin_url:
        config["phpmyadmin_url"] = phpmyadmin_url
    storage.config = config
    return db_user


async def ensure_project_user(
    db: AsyncSession,
    settings: Settings,
    storage: Storage,
    project: Project,
    *,
    created_by_user_id: int | None = None,
    rotate_password: bool = False,
) -> StorageDatabaseUser:
    return await ensure_storage_admin_user(
        db,
        settings,
        storage,
        created_by_user_id=created_by_user_id,
        rotate_password=rotate_password,
    )


async def create_custom_user(
    db: AsyncSession,
    settings: Settings,
    storage: Storage,
    *,
    username: str,
    created_by_user_id: int | None = None,
) -> StorageDatabaseUser:
    db_user = StorageDatabaseUser(
        storage_id=storage.id,
        username=username,
        scope="custom",
        created_by_user_id=created_by_user_id,
    )
    db_user.password = generate_password()
    db.add(db_user)
    await db.flush()
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
