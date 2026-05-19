from sqlalchemy import select

from models import Team, TeamMember


async def get_latest_teams(db, current_user, current_team=None, limit=4):
    query = (
        select(Team)
        .join(TeamMember, Team.id == TeamMember.team_id)
        .where(Team.status != "deleted", TeamMember.user_id == current_user.id)
        .order_by(Team.updated_at.desc())
    )

    if limit:
        query = query.limit(limit)

    if current_team:
        query = query.where(Team.id != current_team.id)

    result = await db.execute(query)

    return result.scalars().all()
