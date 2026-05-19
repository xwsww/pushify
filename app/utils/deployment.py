from sqlalchemy import select
from sqlalchemy.orm import contains_eager, selectinload

from models import Deployment, Project


def deployment_query_options():
    return (
        selectinload(Deployment.aliases),
        selectinload(Deployment.project),
    )


def deployment_query_options_joined_project():
    """Use with select(Deployment).join(Project) — not selectinload(project)."""
    return (
        selectinload(Deployment.aliases),
        contains_eager(Deployment.project),
    )


def team_recent_deployments_stmt(team_id: str, *, limit: int = 10):
    """Recent deployments for a team (no join — avoids SQLAlchemy loader conflicts)."""
    return (
        select(Deployment)
        .where(
            Deployment.project_id.in_(
                select(Project.id).where(
                    Project.team_id == team_id,
                    Project.status != "deleted",
                )
            )
        )
        .options(*deployment_query_options())
        .order_by(Deployment.created_at.desc())
        .limit(limit)
    )
