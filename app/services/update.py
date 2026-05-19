from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import aiodocker
import httpx

from config import Settings

logger = logging.getLogger(__name__)

SEMVER_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
HELPER_CONTAINER_NAME = "devpush-update-helper"
HELPER_LABEL_KEY = "devpush.update-helper"
ACTIVE_UPDATE_STATES = {"starting", "running", "restarting"}


def github_repo_path(repo_url: str | None) -> str | None:
    if not repo_url:
        return None
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if not match:
        return None
    return match.group(1)


class UpdateService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status_path = Path(settings.update_status_file)

    @property
    def host_status_path(self) -> str:
        host_data_dir = self.settings.host_data_dir or self.settings.data_dir
        return os.path.join(host_data_dir, self.status_path.name)

    def build_github_headers(
        self,
        *,
        github_repo: str | None = None,
        current_ref: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": (
                f"devpush/{current_ref or 'dev'} (+https://github.com/{github_repo})"
                if github_repo
                else "devpush/dev"
            ),
        }
        token = (self.settings.github_repo_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def get_latest_tag(
        self,
        repo_url: str | None,
        current_ref: str | None,
        current_commit: str | None = None,
    ) -> tuple[dict[str, str] | None, str | None, str | None]:
        github_repo = github_repo_path(repo_url)
        release_url = repo_url.removesuffix(".git") if repo_url else None
        if not github_repo or not current_ref:
            return None, release_url, None

        update_info = None
        error = None

        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                headers=self.build_github_headers(
                    github_repo=github_repo, current_ref=current_ref
                ),
            ) as client:
                response = await client.get(
                    f"https://api.github.com/repos/{github_repo}/tags",
                    params={"per_page": 50},
                )
                if response.status_code == 403 and not self.settings.github_repo_token:
                    raise RuntimeError("GitHub API rate limit exceeded.")
                response.raise_for_status()
                if current_ref and not SEMVER_TAG_RE.match(current_ref):
                    branch_response = await client.get(
                        f"https://api.github.com/repos/{github_repo}/branches/{current_ref}"
                    )
                    if branch_response.status_code == 200:
                        branch_data = branch_response.json()
                        branch_name = branch_data.get("name") or current_ref
                        remote_commit = (
                            (branch_data.get("commit") or {}).get("sha") or ""
                        ).strip()
                        if remote_commit and remote_commit != (current_commit or "").strip():
                            update_info = {
                                "target_ref": branch_name,
                                "label": branch_name,
                                "kind": "branch",
                            }
                else:
                    data = response.json()
                    if isinstance(data, list):
                        for item in data:
                            name = item.get("name") or ""
                            if SEMVER_TAG_RE.match(name):
                                update_info = {
                                    "target_ref": name,
                                    "label": name,
                                    "kind": "tag",
                                }
                                break
        except Exception as exc:
            error = str(exc)

        if update_info and update_info.get("target_ref") == current_ref:
            if update_info.get("kind") == "tag":
                update_info = None
            elif (current_commit or "").strip():
                # Branch updates are still valid when commit changed.
                pass

        return update_info, release_url, error

    def read_status(self) -> dict[str, Any] | None:
        if not self.status_path.exists():
            return None
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read update status file %s: %s", self.status_path, exc)
            return None
        return raw if isinstance(raw, dict) else None

    def write_status(self, status: dict[str, Any]) -> dict[str, Any]:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.status_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.status_path)
        return status

    def clear_status(self) -> None:
        try:
            self.status_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to clear update status file %s: %s", self.status_path, exc)

    def is_update_active(self, status: dict[str, Any] | None = None) -> bool:
        current = status or self.read_status()
        return bool(current and current.get("state") in ACTIVE_UPDATE_STATES)

    async def _get_container_info(self, container: Any) -> dict[str, Any]:
        if isinstance(container, dict):
            return container
        return await container.show()

    async def _get_app_service_image(self, docker_client: aiodocker.Docker) -> str:
        containers = await docker_client.containers.list(all=True)
        fallback_image = None
        for container in containers:
            container_info = await self._get_container_info(container)
            labels = container_info.get("Config", {}).get("Labels") or container_info.get(
                "Labels"
            ) or {}
            if (
                labels.get("com.docker.compose.project") == "devpush"
                and labels.get("com.docker.compose.service") == "app"
            ):
                image = container_info.get("Config", {}).get("Image") or container_info.get(
                    "Image"
                )
                state = (
                    container_info.get("State", {}).get("Status")
                    or container_info.get("State")
                    or ""
                ).lower()
                if image and state == "running":
                    return image
                if image:
                    fallback_image = image
        if fallback_image:
            return fallback_image
        raise RuntimeError("Could not locate the running app image.")

    async def _has_running_helper(self, docker_client: aiodocker.Docker) -> bool:
        containers = await docker_client.containers.list(all=True)
        for container in containers:
            container_info = await self._get_container_info(container)
            labels = container_info.get("Config", {}).get("Labels") or container_info.get(
                "Labels"
            ) or {}
            state = (
                container_info.get("State", {}).get("Status")
                or container_info.get("State")
                or ""
            ).lower()
            if labels.get(HELPER_LABEL_KEY) == "1" and state in {
                "created",
                "running",
                "restarting",
            }:
                return True
        return False

    async def start_update(
        self,
        *,
        target_ref: str,
        current_ref: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.env == "development":
            raise RuntimeError("Panel updates are only available in production.")

        existing_status = self.read_status()
        if self.is_update_active(existing_status):
            raise RuntimeError("An update is already in progress.")

        host_app_dir = self.settings.host_app_dir or "/opt/devpush"
        host_data_dir = self.settings.host_data_dir or self.settings.data_dir

        self.write_status(
            {
                "state": "starting",
                "message": "Preparing the update container...",
                "current_ref": current_ref,
                "target_ref": target_ref,
            }
        )

        try:
            async with aiodocker.Docker(url=self.settings.docker_host) as docker_client:
                if await self._has_running_helper(docker_client):
                    raise RuntimeError("An update helper is already running.")

                image = await self._get_app_service_image(docker_client)
                env = [
                    "DEVPUSH_ENV=production",
                    f"DEVPUSH_APP_DIR={host_app_dir}",
                    f"DEVPUSH_DATA_DIR={host_data_dir}",
                    f"DEVPUSH_UPDATE_STATUS_FILE={self.host_status_path}",
                ]
                token = (self.settings.github_repo_token or "").strip()
                if token:
                    env.append(f"DEVPUSH_GITHUB_REPO_TOKEN={token}")

                container = await docker_client.containers.create_or_replace(
                    name=HELPER_CONTAINER_NAME,
                    config={
                        "Image": image,
                        "Cmd": [
                            "bash",
                            f"{host_app_dir}/scripts/panel-update-runner.sh",
                            "--ref",
                            target_ref,
                        ],
                        "Env": env,
                        "User": "0:0",
                        "WorkingDir": host_app_dir,
                        "Labels": {
                            HELPER_LABEL_KEY: "1",
                            "com.devpush.role": "update-helper",
                        },
                        "HostConfig": {
                            "AutoRemove": True,
                            "Binds": [
                                f"{host_app_dir}:{host_app_dir}",
                                f"{host_data_dir}:{host_data_dir}",
                                "/var/run/docker.sock:/var/run/docker.sock",
                            ],
                        },
                    },
                )
                await container.start()
        except Exception as exc:
            self.write_status(
                {
                    "state": "failed",
                    "message": str(exc),
                    "current_ref": current_ref,
                    "target_ref": target_ref,
                }
            )
            raise

        return self.write_status(
            {
                "state": "running",
                "message": "Updating the panel. This page will reconnect automatically.",
                "current_ref": current_ref,
                "target_ref": target_ref,
            }
        )
