import aiodocker


def is_transient_docker_error(exc: Exception) -> bool:
    """True when Docker is temporarily unreachable (not a deployment-specific failure)."""
    if isinstance(exc, aiodocker.DockerError):
        if exc.status in (500, 502, 503, 504, 900):
            return True
    msg = str(exc).lower()
    markers = (
        "cannot connect",
        "connection refused",
        "connection reset",
        "temporary failure",
        "name resolution",
        "timed out",
        "timeout",
        "broken pipe",
        "server disconnected",
        "session is closed",
        "connector is closed",
    )
    return any(marker in msg for marker in markers)


def resolve_runner_resources(settings) -> tuple[float | None, int | None]:
    """Resolve CPU and memory limits for a deployment runner container."""
    cpus: float | None = settings.default_cpus
    memory_mb: int | None = settings.default_memory_mb

    if cpus is None and settings.runner_fallback_cpus > 0:
        cpus = settings.runner_fallback_cpus
    if memory_mb is None and settings.runner_fallback_memory_mb > 0:
        memory_mb = settings.runner_fallback_memory_mb

    return cpus, memory_mb


def runner_host_config(
    *,
    cpus: float | None,
    memory_mb: int | None,
    mounts: list[str] | None,
    oom_score_adj: int,
) -> dict:
    """Build Docker HostConfig for deployment runner containers."""
    host_config: dict = {
        "SecurityOpt": ["no-new-privileges:true"],
        "LogConfig": {
            "Type": "json-file",
            "Config": {"max-size": "10m", "max-file": "5"},
        },
    }
    if oom_score_adj:
        host_config["OomScoreAdj"] = oom_score_adj
    if cpus is not None and cpus > 0:
        host_config["CpuQuota"] = int(cpus * 100000)
        host_config["CpuPeriod"] = 100000
    if memory_mb is not None and memory_mb > 0:
        memory_bytes = memory_mb * 1024 * 1024
        host_config["Memory"] = memory_bytes
        host_config["MemorySwap"] = memory_bytes
    if mounts:
        host_config["Binds"] = mounts
    return host_config
