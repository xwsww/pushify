import os
import logging
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "/pushify/"
    app_description: str = (
        "An open-source platform to build and deploy any app from GitHub."
    )
    url_scheme: str = "https"
    app_hostname: str = ""
    deploy_domain: str = ""
    github_repo_token: str = ""
    github_app_id: str = ""
    github_app_name: str = ""
    github_app_private_key: str = ""
    github_app_webhook_secret: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    secret_key: str = ""
    encryption_key: str = ""
    postgres_db: str = "devpush"
    postgres_user: str = "devpush-app"
    postgres_password: str = ""
    mariadb_host: str = "mariadb"
    mariadb_port: int = 3306
    mariadb_root_user: str = "root"
    mariadb_root_password: str = ""
    phpmyadmin_hostname: str = ""
    postgres_storage_host: str = "postgres-storage"
    postgres_storage_port: int = 5432
    postgres_storage_user: str = "postgres"
    postgres_storage_password: str = ""
    redis_url: str = "redis://redis:6379"
    docker_host: str = "tcp://docker-proxy:2375"
    data_dir: str = "/data"
    host_data_dir: str | None = None
    app_dir: str = "/app"
    host_app_dir: str | None = None
    registry_catalog_url: str = "https://raw.githubusercontent.com/xwsww/pushify/refs/heads/main/registry/catalog.json"
    upload_dir: str = ""
    traefik_dir: str = ""
    env_file: str = ""
    version_file: str = ""
    update_status_file: str = ""
    default_cpus: float | None = None
    max_cpus: float | None = None
    default_memory_mb: int | None = None
    max_memory_mb: int | None = None
    runner_fallback_cpus: float = 2.0
    runner_fallback_memory_mb: int = 0
    runner_oom_score_adj: int = 500
    docker_transient_failure_threshold: int = 15
    default_db_size_limit_bytes: int | None = 5 * 1024 * 1024 * 1024
    presets: list[dict] = []
    runners: list[dict] = []
    job_timeout_seconds: int = 320
    job_completion_wait_seconds: int = 300
    deployment_timeout_seconds: int = 300
    container_delete_grace_seconds: int = 15
    log_stream_grace_seconds: int = 5
    service_uid: int = 1000
    service_gid: int = 1000
    db_echo: bool = False
    log_level: str = "WARNING"
    env: str = "production"
    access_denied_message: str = "Sign-in not allowed for this email."
    access_denied_webhook: str = ""
    login_header: str = ""
    toaster_header: str = ""
    magic_link_ttl_seconds: int = 900
    auth_token_ttl_days: int = 30
    auth_token_refresh_threshold_days: int = 1
    auth_token_issuer: str = "devpush-app"
    auth_token_audience: str = "devpush-web"
    job_max_tries: int = 3
    server_ip: str = "127.0.0.1"
    security_pass_ttl_seconds: int = 3600
    security_challenge_ttl_seconds: int = 600
    security_pow_difficulty: int = 4
    security_pow_min_seconds: float = 1.0
    security_verify_rate_limit: int = 12
    security_pow_load_elevated: int = 50
    security_pow_load_high: int = 120
    enable_https: bool = Field(
        default=True,
        validation_alias=AliasChoices("DEVPUSH_ENABLE_HTTPS", "ENABLE_HTTPS"),
    )
    behind_cloudflare: bool = Field(
        default=False,
        validation_alias=AliasChoices("DEVPUSH_BEHIND_CLOUDFLARE", "BEHIND_CLOUDFLARE"),
    )
    trusted_proxy_cidrs: str = Field(
        default="",
        validation_alias=AliasChoices(
            "DEVPUSH_TRUSTED_PROXY_CIDRS", "TRUSTED_PROXY_CIDRS"
        ),
    )

    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("enable_https", "behind_cloudflare", mode="before")
    @classmethod
    def parse_env_bool(cls, value: object) -> object:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("0", "false", "no", "off"):
                return False
            if v in ("1", "true", "yes", "on"):
                return True
        return value

    @field_validator("github_app_private_key", mode="before")
    @classmethod
    def normalize_github_app_private_key(cls, value: object) -> object:
        if not isinstance(value, str) or not value:
            return value
        if "\\n" in value:
            value = value.replace("\\n", "\n")
        return value.strip()

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        if not value:
            return value
        try:
            Fernet(value.encode())
        except Exception as exc:
            raise ValueError("ENCRYPTION_KEY is not a valid Fernet key") from exc
        return value

    @property
    def allow_custom_cpu(self) -> bool:
        return self.default_cpus is not None and self.max_cpus is not None

    @property
    def allow_custom_memory(self) -> bool:
        return self.default_memory_mb is not None and self.max_memory_mb is not None


@lru_cache
def get_settings():
    settings = Settings()

    # Set URL scheme (panel cookies, redirects, deploy URLs)
    if settings.env == "development":
        settings.url_scheme = "http"
    elif not settings.enable_https:
        settings.url_scheme = "http"
    else:
        settings.url_scheme = "https"

    # CPU default/max normalization
    if settings.default_cpus is not None and settings.default_cpus <= 0:
        logger.warning("DEFAULT_CPUS must be > 0; ignoring and treating as unlimited.")
        settings.default_cpus = None
    if settings.max_cpus is not None and settings.max_cpus <= 0:
        logger.warning("MAX_CPUS must be > 0; ignoring.")
        settings.max_cpus = None
    if settings.default_cpus is None and settings.max_cpus is not None:
        logger.warning("MAX_CPUS is set but DEFAULT_CPUS is not; ignoring MAX_CPUS.")
        settings.max_cpus = None
    if settings.allow_custom_cpu:
        default_cpus = settings.default_cpus
        max_cpus = settings.max_cpus
        if (
            default_cpus is not None
            and max_cpus is not None
            and default_cpus > max_cpus
        ):
            logger.warning(
                "DEFAULT_CPUS is greater than MAX_CPUS; clamping default to max."
            )
            settings.default_cpus = max_cpus

    # Memory default/max normalization
    if settings.default_memory_mb is not None and settings.default_memory_mb <= 0:
        logger.debug(
            "DEFAULT_MEMORY_MB is 0 or unset; deployment memory is unlimited."
        )
        settings.default_memory_mb = None
    if settings.max_memory_mb is not None and settings.max_memory_mb <= 0:
        logger.warning("MAX_MEMORY_MB must be > 0; ignoring.")
        settings.max_memory_mb = None
    if settings.default_memory_mb is None and settings.max_memory_mb is not None:
        logger.warning(
            "MAX_MEMORY_MB is set but DEFAULT_MEMORY_MB is not; ignoring MAX_MEMORY_MB."
        )
        settings.max_memory_mb = None
    if settings.allow_custom_memory:
        default_memory_mb = settings.default_memory_mb
        max_memory_mb = settings.max_memory_mb
        if (
            default_memory_mb is not None
            and max_memory_mb is not None
            and default_memory_mb > max_memory_mb
        ):
            logger.warning(
                "DEFAULT_MEMORY_MB is greater than MAX_MEMORY_MB; clamping default to max."
            )
            settings.default_memory_mb = max_memory_mb

    if settings.runner_fallback_cpus <= 0:
        settings.runner_fallback_cpus = 0
    if settings.runner_fallback_memory_mb <= 0:
        settings.runner_fallback_memory_mb = 0
    if settings.docker_transient_failure_threshold < 1:
        settings.docker_transient_failure_threshold = 1

    # Directories/files normalization
    if not settings.upload_dir:
        settings.upload_dir = os.path.join(settings.data_dir, "upload")
    if not settings.traefik_dir:
        settings.traefik_dir = os.path.join(settings.data_dir, "traefik")
    if not settings.env_file:
        settings.env_file = os.path.join(settings.data_dir, ".env")
    if not settings.version_file:
        settings.version_file = os.path.join(settings.data_dir, "version.json")
    if not settings.update_status_file:
        settings.update_status_file = os.path.join(settings.data_dir, "update-status.json")
    if not settings.host_data_dir:
        settings.host_data_dir = settings.data_dir
    if not settings.host_app_dir:
        settings.host_app_dir = (
            settings.app_dir if settings.env == "development" else "/opt/devpush"
        )

    return settings
