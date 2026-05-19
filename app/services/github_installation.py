from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone

from models import GithubInstallation
from services.github import GitHubService


class GitHubInstallationService:
    def __init__(self, github_service: GitHubService):
        self.github_service = github_service

    async def get_or_refresh_installation(
        self, installation_id: int, db: AsyncSession
    ) -> GithubInstallation:
        """Get installation instance, refreshing token if needed."""
        result = await db.execute(
            select(GithubInstallation).where(
                GithubInstallation.installation_id == installation_id
            )
        )
        installation = result.scalar_one_or_none()

        if not installation:
            installation = GithubInstallation(installation_id=installation_id)

        if not installation.token or (
            installation.token_expires_at
            and installation.token_expires_at
            <= datetime.now(timezone.utc).replace(tzinfo=None)
        ):
            token_data = await self.github_service.get_installation_access_token(
                str(installation_id)
            )
            installation.token = token_data["token"]
            installation.token_expires_at = datetime.fromisoformat(
                token_data["expires_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None)

        installation = await db.merge(installation)
        await db.commit()
        return installation
