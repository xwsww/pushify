from config import Settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Domain


def _host_only(host: str) -> str:
    return host.split(":")[0].lower()


def is_panel_host(host: str, settings: Settings) -> bool:
    return _host_only(host) == settings.app_hostname.lower()


def phpmyadmin_hostname(settings: Settings) -> str:
    raw = (settings.phpmyadmin_hostname or f"db.{settings.app_hostname}").strip()
    return _host_only(raw)


def is_phpmyadmin_host(host: str, settings: Settings) -> bool:
    return _host_only(host) == phpmyadmin_hostname(settings)


def is_service_host(host: str, settings: Settings) -> bool:
    """Panel and phpMyAdmin — never deploy DDoS / forward-auth."""
    h = _host_only(host)
    return h == settings.app_hostname.lower() or h == phpmyadmin_hostname(settings)


async def is_deploy_host(
    host: str, settings: Settings, db: AsyncSession | None = None
) -> bool:
    host = host.split(":")[0].lower()
    if is_panel_host(host, settings):
        return False
    if host.endswith(f".{settings.deploy_domain.lower()}"):
        return True
    if db is not None:
        found = await db.scalar(
            select(Domain.id).where(
                Domain.hostname == host,
                Domain.status == "active",
            )
        )
        return found is not None
    return False
