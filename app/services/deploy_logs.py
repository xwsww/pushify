import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiodocker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Deployment
from utils.log import epoch_nano_to_iso, parse_structured_log

logger = logging.getLogger(__name__)

STATUS_MESSAGES = {
    "prepare": "Preparing deployment (repository, image, container setup)…",
    "deploy": "Starting application container…",
    "finalize": "Finalizing routes and domains…",
}


def _log_entry(
    message: str,
    *,
    level: str = "INFO",
    timestamp_ns: int | None = None,
    labels: dict | None = None,
) -> dict:
    ts = str(timestamp_ns if timestamp_ns is not None else time.time_ns())
    return {
        "timestamp_iso": epoch_nano_to_iso(ts),
        "timestamp": ts,
        "message": message,
        "level": level,
        "labels": labels or {},
    }


def activity_logs(deployment: Deployment) -> list[dict]:
    if deployment.status == "completed":
        return []
    message = STATUS_MESSAGES.get(deployment.status or "")
    if not message:
        return []
    ts_ns = time.time_ns()
    if deployment.created_at:
        created = deployment.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        else:
            created = created.astimezone(timezone.utc)
        ts_ns = int(created.timestamp() * 1e9)
    return [
        _log_entry(
            message,
            timestamp_ns=ts_ns,
            labels={
                "deployment_id": deployment.id,
                "project_id": deployment.project_id,
            },
        )
    ]


def _docker_line_to_log(line: str, *, labels: dict | None = None) -> dict:
    text = line.rstrip("\n")
    parts = text.split(" ", 2)
    if len(parts) == 3 and parts[1] in ("stdout", "stderr"):
        ts_raw, stream, message = parts
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts_ns = int(dt.timestamp() * 1e9)
        except ValueError:
            ts_ns = time.time_ns()
        msg, level = parse_structured_log(message)
        if stream == "stderr" and level == "INFO":
            level = "ERROR"
        entry = _log_entry(msg, level=level, timestamp_ns=ts_ns)
    else:
        entry = _log_entry(text)
    if labels:
        entry["labels"] = {**entry.get("labels", {}), **labels}
    return entry


async def _container_labels(container) -> dict:
    data = getattr(container, "_container", None) or {}
    labels = data.get("Labels") or {}
    if not labels:
        try:
            info = await container.show()
            labels = info.get("Config", {}).get("Labels", {}) or info.get("Labels", {})
        except Exception:
            labels = {}
    return {
        "project_id": labels.get("devpush.project_id", ""),
        "deployment_id": labels.get("devpush.deployment_id", ""),
        "environment_id": labels.get("devpush.environment_id", ""),
        "branch": labels.get("devpush.branch", ""),
    }


async def discover_runner_container(docker_client, deployment: Deployment):
    if deployment.container_id:
        try:
            return await docker_client.containers.get(deployment.container_id)
        except Exception:
            pass

    runner_name = f"runner-{deployment.id[:7]}"
    try:
        return await docker_client.containers.get(runner_name)
    except Exception:
        pass

    filters = {
        "label": [
            f"devpush.deployment_id={deployment.id}",
            f"devpush.project_id={deployment.project_id}",
        ]
    }
    containers = await docker_client.containers.list(all=True, filters=filters)
    return containers[0] if containers else None


async def _logs_from_container(container, *, tail: int = 200) -> list[dict]:
    labels = await _container_labels(container)
    try:
        raw = await container.log(stdout=True, stderr=True, tail=tail, timestamps=True)
    except Exception as exc:
        logger.debug("Could not read logs for container %s: %s", container.id, exc)
        return []

    logs = []
    for line in raw:
        text = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else str(line)
        )
        if text.strip():
            logs.append(_docker_line_to_log(text, labels=labels))
    logs.sort(key=lambda x: int(x["timestamp"]))
    return logs


async def container_logs(container_id: str | None, *, tail: int = 200) -> list[dict]:
    if not container_id:
        return []
    settings = get_settings()
    try:
        async with aiodocker.Docker(url=settings.docker_host) as docker:
            container = await docker.containers.get(container_id)
            return await _logs_from_container(container, tail=tail)
    except Exception as exc:
        logger.debug("Could not read container logs for %s: %s", container_id, exc)
        return []


async def deployment_container_logs(
    deployment: Deployment, *, tail: int = 200
) -> list[dict]:
    settings = get_settings()
    try:
        async with aiodocker.Docker(url=settings.docker_host) as docker:
            container = await discover_runner_container(docker, deployment)
            if not container:
                return []
            return await _logs_from_container(container, tail=tail)
    except Exception as exc:
        logger.debug(
            "Could not read deployment logs for %s: %s", deployment.id, exc
        )
        return []


async def list_runner_containers(
    docker_client,
    project_id: str,
    *,
    deployment_id: str | None = None,
    environment_id: str | None = None,
    branch: str | None = None,
):
    label_filters = [f"devpush.project_id={project_id}"]
    if deployment_id:
        label_filters.append(f"devpush.deployment_id={deployment_id}")
    if environment_id:
        label_filters.append(f"devpush.environment_id={environment_id}")
    if branch:
        label_filters.append(f"devpush.branch={branch}")
    return await docker_client.containers.list(
        all=True, filters={"label": label_filters}
    )


def merge_deployment_logs(*sources: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for source in sources:
        for log in source:
            key = (log["timestamp"], log["message"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(log)
    merged.sort(key=lambda x: int(x["timestamp"]))
    return merged


def _filter_logs_by_time(
    logs: list[dict],
    *,
    start_timestamp: int | None,
    end_timestamp: int | None,
    keyword: str | None,
) -> list[dict]:
    filtered: list[dict] = []
    for log in logs:
        ts = int(log["timestamp"])
        if start_timestamp is not None and ts < start_timestamp:
            continue
        if end_timestamp is not None and ts > end_timestamp:
            continue
        if keyword and keyword.lower() not in log["message"].lower():
            continue
        filtered.append(log)
    return filtered


async def supplement_logs(
    deployment: Deployment, logs: list[dict], *, tail: int = 200
) -> list[dict]:
    parts: list[list[dict]] = []
    if logs:
        parts.append(logs)
    if deployment.status != "completed" or deployment.container_id:
        status = activity_logs(deployment)
        if status:
            parts.append(status)
    docker_logs = await deployment_container_logs(deployment, tail=tail)
    if docker_logs:
        parts.append(docker_logs)
    if not parts:
        return []
    return merge_deployment_logs(*parts)


async def resolve_deployment_logs(
    deployment: Deployment,
    loki_service: Any,
    *,
    limit: int = 50,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    logs: list[dict] = []
    try:
        logs = await loki_service.get_logs(
            limit=limit,
            project_id=deployment.project_id,
            deployment_id=deployment.id,
            end_timestamp=end_timestamp,
            start_timestamp=start_timestamp,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(
            "Loki unavailable for deployment %s: %s", deployment.id, exc
        )
    return await supplement_logs(deployment, logs, tail=max(limit, 500))


async def resolve_project_logs(
    db: AsyncSession,
    project_id: str,
    loki_service: Any,
    *,
    limit: int = 50,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    deployment_id: str | None = None,
    environment_id: str | None = None,
    branch: str | None = None,
    keyword: str | None = None,
    timeout: float = 10.0,
    apply_time_filter: bool = True,
) -> list[dict]:
    logs: list[dict] = []
    try:
        logs = await loki_service.get_logs(
            project_id=project_id,
            limit=limit,
            start_timestamp=str(start_timestamp) if start_timestamp else None,
            end_timestamp=str(end_timestamp) if end_timestamp else None,
            deployment_id=deployment_id,
            environment_id=environment_id,
            branch=branch,
            keyword=keyword,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Loki unavailable for project %s: %s", project_id, exc)

    if logs:
        return logs

    settings = get_settings()
    merged: list[dict] = []
    per_container_tail = max(limit, 200)

    try:
        async with aiodocker.Docker(url=settings.docker_host) as docker:
            containers = await list_runner_containers(
                docker,
                project_id,
                deployment_id=deployment_id,
                environment_id=environment_id,
                branch=branch,
            )
            for container in containers[:20]:
                merged.extend(
                    await _logs_from_container(container, tail=per_container_tail)
                )
    except Exception as exc:
        logger.warning(
            "Docker log discovery failed for project %s: %s", project_id, exc
        )

    if not merged:
        query = select(Deployment).where(Deployment.project_id == project_id)
        if deployment_id:
            query = query.where(Deployment.id == deployment_id)
        if environment_id:
            query = query.where(Deployment.environment_id == environment_id)
        if branch:
            query = query.where(Deployment.branch == branch)
        query = query.order_by(Deployment.created_at.desc()).limit(20)
        deployments = (await db.execute(query)).scalars().all()

        for deployment in deployments:
            merged.extend(
                await deployment_container_logs(deployment, tail=per_container_tail)
            )

    if keyword or (apply_time_filter and (start_timestamp or end_timestamp)):
        merged = _filter_logs_by_time(
            merged,
            start_timestamp=start_timestamp if apply_time_filter else None,
            end_timestamp=end_timestamp if apply_time_filter else None,
            keyword=keyword,
        )

    merged.sort(key=lambda x: int(x["timestamp"]))
    if len(merged) > limit:
        merged = merged[-limit:]
    return merged
