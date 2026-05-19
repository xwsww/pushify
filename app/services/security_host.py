from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from config import Settings
from models import Alias, Deployment, Domain
from services.browser_challenge import normalize_host
from services.deployment import DeploymentService
from utils.host import is_service_host

_LEVEL_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL_ATTACK_SEC = 30.0
_CACHE_TTL_OTHER_SEC = 120.0


def _cache_get(host: str) -> str | None:
    entry = _LEVEL_CACHE.get(host)
    if not entry:
        return None
    level, expires = entry
    if time.monotonic() >= expires:
        _LEVEL_CACHE.pop(host, None)
        return None
    return level


def _cache_set(host: str, level: str) -> None:
    if len(_LEVEL_CACHE) > 4096:
        _LEVEL_CACHE.clear()
    ttl = (
        _CACHE_TTL_ATTACK_SEC
        if level == "under_attack"
        else _CACHE_TTL_OTHER_SEC
    )
    _LEVEL_CACHE[host] = (level, time.monotonic() + ttl)


async def _level_from_db(
    db: AsyncSession, host: str, settings: Settings
) -> str:
    domain = await db.scalar(
        select(Domain)
        .options(joinedload(Domain.project))
        .where(
            Domain.hostname == host,
            Domain.status == "active",
            Domain.type == "route",
        )
    )
    if domain and domain.project:
        return DeploymentService.security_level(domain.project.config)

    suffix = f".{settings.deploy_domain.lower()}"
    if host.endswith(suffix):
        subdomain = host[: -len(suffix)]
        alias = await db.scalar(
            select(Alias)
            .options(
                joinedload(Alias.deployment).joinedload(Deployment.project),
            )
            .where(Alias.subdomain == subdomain)
        )
        if alias and alias.deployment and alias.deployment.project:
            return DeploymentService.security_level(alias.deployment.project.config)

    return "off"


async def security_level_for_host(
    db: AsyncSession, host: str, settings: Settings
) -> str:
    host = normalize_host(host)
    if not host or is_service_host(host, settings):
        return "off"

    cached = _cache_get(host)
    if cached is not None:
        return cached

    level = await _level_from_db(db, host, settings)
    _cache_set(host, level)
    return level
