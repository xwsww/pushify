import logging
from datetime import datetime

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Notification, TeamInvite, TeamMember, User, utc_now

logger = logging.getLogger(__name__)


class NotificationService:
    @staticmethod
    async def unread_count(db: AsyncSession, user_id: int) -> int:
        return (
            await db.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id,
                    Notification.read_at.is_(None),
                )
            )
            or 0
        )

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        user_id: int,
        type: str,
        title: str,
        body: str | None = None,
        action_url: str | None = None,
        action_label: str | None = None,
        payload: dict | None = None,
        dedupe_key: str | None = None,
    ) -> Notification | None:
        values = {
            "user_id": user_id,
            "type": type,
            "title": title[:120],
            "body": body[:500] if body else None,
            "action_url": action_url,
            "action_label": action_label,
            "payload": payload,
            "dedupe_key": dedupe_key,
            "created_at": utc_now(),
        }
        if dedupe_key:
            existing = await db.scalar(
                select(Notification).where(
                    Notification.user_id == user_id,
                    Notification.dedupe_key == dedupe_key,
                )
            )
            if existing:
                existing.title = values["title"]
                existing.body = values["body"]
                existing.action_url = values["action_url"]
                existing.action_label = values["action_label"]
                existing.payload = values["payload"]
                existing.read_at = None
                existing.created_at = values["created_at"]
                await db.flush()
                return existing

        notification = Notification(**values)
        db.add(notification)
        await db.flush()
        return notification

    @staticmethod
    async def create_for_team(
        db: AsyncSession,
        *,
        team_id: str,
        type: str,
        title: str,
        body: str | None = None,
        action_url: str | None = None,
        action_label: str | None = None,
        payload: dict | None = None,
        dedupe_key: str | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> None:
        query = select(TeamMember.user_id).where(TeamMember.team_id == team_id)
        if roles:
            query = query.where(TeamMember.role.in_(roles))
        result = await db.execute(query)
        user_ids = result.scalars().all()
        for user_id in user_ids:
            key = f"{dedupe_key}:{user_id}" if dedupe_key else None
            await NotificationService.create(
                db,
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                action_url=action_url,
                action_label=action_label,
                payload=payload,
                dedupe_key=key,
            )

    @staticmethod
    async def sync_team_invite(
        db: AsyncSession,
        invite: TeamInvite,
        *,
        team_name: str,
        inviter_name: str,
    ) -> None:
        user = await db.scalar(select(User).where(User.email == invite.email))
        if not user:
            return
        await NotificationService.create(
            db,
            user_id=user.id,
            type="team_invite",
            title=team_name,
            body=f"{inviter_name} invited you to join this team.",
            action_url=None,
            action_label=None,
            payload={"invite_id": invite.id, "team_id": invite.team_id},
            dedupe_key=f"team_invite:{invite.id}",
        )

    @staticmethod
    async def dismiss_team_invite(db: AsyncSession, invite_id: str) -> None:
        await db.execute(
            update(Notification)
            .where(Notification.dedupe_key == f"team_invite:{invite_id}")
            .values(read_at=utc_now())
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    async def mark_read(db: AsyncSession, user_id: int, notification_id: str) -> bool:
        result = await db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount > 0

    @staticmethod
    async def mark_all_read(db: AsyncSession, user_id: int) -> None:
        await db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
            .execution_options(synchronize_session=False)
        )
