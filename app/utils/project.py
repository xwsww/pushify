from sqlalchemy import select

from models import Project, Deployment
from utils.deployment import deployment_query_options


async def get_latest_projects(db, team, current_project=None, limit=4):
    query = (
        select(Project)
        .where(Project.status != "deleted", Project.team_id == team.id)
        .order_by(Project.updated_at.desc())
    )

    if limit:
        query = query.limit(limit)

    if current_project:
        query = query.where(Project.id != current_project.id)

    result = await db.execute(query)
    return result.scalars().all()


async def get_latest_deployments(db, project, current_deployment=None, limit=4):
    query = (
        select(Deployment)
        .options(*deployment_query_options())
        .where(Deployment.project_id == project.id)
        .order_by(Deployment.created_at.desc())
    )

    if limit:
        query = query.limit(limit)

    if current_deployment:
        query = query.where(Deployment.id != current_deployment.id)

    result = await db.execute(query)
    return result.scalars().all()
