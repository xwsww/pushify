import asyncio
import logging
import aiodocker
from sqlalchemy import select, exc, inspect
from arq.connections import ArqRedis, RedisSettings, create_pool
import httpx
from datetime import datetime, timezone
from config import get_settings

from db import AsyncSessionLocal
from models import Deployment
from services.deployment import DeploymentService
from utils.docker import is_transient_docker_error

logger = logging.getLogger(__name__)

deployment_probe_active: set[str] = set()
docker_outage_counts: dict[str, int] = {}  # deployment_id -> consecutive transient Docker errors


async def _http_probe(ip: str, port: int, timeout: float = 5) -> bool:
    """Check if the app responds to HTTP requests."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(f"http://{ip}:{port}/")
            return True
    except Exception:
        return False


def _record_docker_outage(deployment_id: str) -> int:
    count = docker_outage_counts.get(deployment_id, 0) + 1
    docker_outage_counts[deployment_id] = count
    return count


def _clear_docker_outage(deployment_id: str) -> None:
    docker_outage_counts.pop(deployment_id, None)


async def _handle_transient_docker_error(
    deployment: Deployment,
    redis_pool: ArqRedis,
    log_prefix: str,
    phase: str,
    error: Exception,
) -> bool:
    """Record transient Docker errors; fail only after the configured threshold."""
    settings = get_settings()
    count = _record_docker_outage(deployment.id)
    logger.warning(
        "%s Docker temporarily unavailable while %s (%s/%s): %s",
        log_prefix,
        phase,
        count,
        settings.docker_transient_failure_threshold,
        error,
    )
    if count >= settings.docker_transient_failure_threshold:
        await redis_pool.enqueue_job(
            "fail_deployment",
            deployment.id,
            "deploy",
            "Platform could not reach Docker while monitoring this deployment. "
            "Try deploying again; if it persists, check server resources.",
        )
        await _cleanup_deployment(deployment.id)
    return True


async def _check_status(
    deployment: Deployment,
    docker_client: aiodocker.Docker,
    redis_pool: ArqRedis,
    db,
):
    """Checks the status of a single deployment's container."""
    if deployment.status == "completed":
        return

    if deployment.id in deployment_probe_active:
        return

    log_prefix = f"[DeployMonitor:{deployment.id}]"
    settings = get_settings()
    deployment_probe_active.add(deployment.id)

    try:
        # Timeout check
        try:
            now_utc = datetime.now(timezone.utc)
            created_at = (
                deployment.created_at.replace(tzinfo=timezone.utc)
                if deployment.created_at.tzinfo is None
                else deployment.created_at
            )
            requires_http = DeploymentService.requires_http(deployment.config)
            if (
                now_utc - created_at
            ).total_seconds() > settings.deployment_timeout_seconds:
                await redis_pool.enqueue_job(
                    "fail_deployment",
                    deployment.id,
                    "deploy",
                    (
                        "Timed out waiting for app to respond on port 8000. Ensure your app starts an HTTP server on this port."
                        if requires_http
                        else "Timed out waiting for the app process to stay running."
                    ),
                )
                logger.warning(
                    f"{log_prefix} Deployment timed out; failure job enqueued."
                )
                await _cleanup_deployment(deployment.id)
                return
        except Exception:
            logger.error(f"{log_prefix} Error while evaluating timeout.", exc_info=True)

        if not deployment.container_id:
            return

        try:
            container = await docker_client.containers.get(deployment.container_id)
            logger.info(f"{log_prefix} Probing container {deployment.container_id}")
            container_info = await container.show()
        except Exception as e:
            if is_transient_docker_error(e):
                await _handle_transient_docker_error(
                    deployment, redis_pool, log_prefix, "fetching container", e
                )
                return
            await redis_pool.enqueue_job(
                "fail_deployment",
                deployment.id,
                "deploy",
                "Container stopped unexpectedly. Check the deployment logs for errors.",
            )
            await _cleanup_deployment(deployment.id)
            return

        _clear_docker_outage(deployment.id)
        status = container_info["State"]["Status"]

        if status == "exited":
            exit_code = container_info["State"].get("ExitCode", -1)
            if exit_code == 0:
                reason = "App exited unexpectedly. Ensure your app keeps running and doesn't exit on its own."
            elif exit_code == 137:
                has_build = bool((deployment.config or {}).get("build_command"))
                if has_build:
                    reason = (
                        "Build or app was killed (out of memory). "
                        "Vite/Node builds often need 4GB+ RAM — raise DEFAULT_MEMORY_MB in the server .env "
                        "and redeploy."
                    )
                else:
                    reason = (
                        "App was killed (out of memory or manually stopped). "
                        "Raise DEFAULT_MEMORY_MB in the server .env if the host has headroom."
                    )
            elif exit_code == 1:
                reason = (
                    "App crashed on startup. Check deployment logs for error details."
                )
            else:
                reason = f"App exited with code {exit_code}. Check deployment logs for error details."
            await redis_pool.enqueue_job(
                "fail_deployment",
                deployment.id,
                "deploy",
                reason,
            )
            logger.warning(
                f"{log_prefix} Deployment failed (failure job enqueued): {reason}"
            )
            await _cleanup_deployment(deployment.id)

        elif status == "running":
            requires_http = DeploymentService.requires_http(deployment.config)
            is_ready = False
            if requires_http:
                networks = container_info.get("NetworkSettings", {}).get("Networks", {})
                container_ip = networks.get("devpush_runner", {}).get("IPAddress")
                is_ready = bool(container_ip and await _http_probe(container_ip, 8000))
            else:
                is_ready = True
            if is_ready:
                await DeploymentService.update_status(
                    db,
                    deployment,
                    status="finalize",
                    redis_client=redis_pool,
                )
                await redis_pool.enqueue_job("finalize_deployment", deployment.id)
                logger.info(
                    f"{log_prefix} Deployment ready (finalization job enqueued)."
                )
                await _cleanup_deployment(deployment.id)

    except Exception as e:
        if is_transient_docker_error(e):
            await _handle_transient_docker_error(
                deployment, redis_pool, log_prefix, "probing", e
            )
            return
        logger.error(
            f"{log_prefix} Unexpected error while checking status.", exc_info=True
        )
        await redis_pool.enqueue_job(
            "fail_deployment",
            deployment.id,
            "deploy",
            f"Unexpected error while monitoring deployment: {e}",
        )
        await _cleanup_deployment(deployment.id)
    finally:
        deployment_probe_active.discard(deployment.id)


async def _cleanup_deployment(deployment_id: str):
    """Cleans up monitor state for a deployment."""
    deployment_probe_active.discard(deployment_id)
    _clear_docker_outage(deployment_id)


async def monitor():
    """Monitors the status of deployments."""
    logger.info("Deployment monitor started")
    settings = get_settings()
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    redis_pool = await create_pool(redis_settings)

    async with AsyncSessionLocal() as db:
        schema_ready = False
        while True:
            try:
                if not schema_ready:
                    schema_ready = await db.run_sync(
                        lambda sync_session: inspect(
                            sync_session.connection()
                        ).has_table("alembic_version")
                    )
                    if not schema_ready:
                        logger.warning(
                            "Database schema not ready (no alembic_version); waiting for migrations..."
                        )
                        await asyncio.sleep(5)
                        continue

                result = await db.execute(
                    select(Deployment).where(
                        Deployment.status == "deploy",
                        Deployment.container_status == "running",
                    )
                )
                deployments_to_check = result.scalars().all()

                if deployments_to_check:
                    try:
                        async with aiodocker.Docker(
                            url=settings.docker_host
                        ) as docker_client:
                            tasks = [
                                _check_status(
                                    deployment, docker_client, redis_pool, db
                                )
                                for deployment in deployments_to_check
                            ]
                            await asyncio.gather(*tasks)
                    except Exception as e:
                        if is_transient_docker_error(e):
                            logger.warning(
                                "Docker temporarily unavailable in monitor loop: %s",
                                e,
                            )
                        else:
                            logger.error(
                                "Error connecting to Docker in monitor loop",
                                exc_info=True,
                            )

            except exc.SQLAlchemyError as e:
                logger.error(f"Database error in monitor loop: {e}. Reconnecting.")
                await db.close()
                db = AsyncSessionLocal()
            except Exception:
                logger.error("Critical error in monitor main loop", exc_info=True)

            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(monitor())
