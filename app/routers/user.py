import logging
import os
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import asc, desc, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from starlette.responses import RedirectResponse, Response

from config import Settings, get_settings
from db import get_db
from dependencies import (
    RedirectResponseX,
    TemplateResponse,
    flash,
    get_current_user,
    get_queue,
    get_translation as _,
)
from forms.team import TeamInviteAcceptForm, TeamInviteDeclineForm, TeamLeaveForm
from forms.user import UserDeleteForm, UserEmailForm, UserGeneralForm
from models import Notification, Team, TeamInvite, TeamMember, User, UserIdentity, utc_now
from services.notification import NotificationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user")

NOTIFICATIONS_BELL_LIMIT = 8


async def _pending_invite(
    db: AsyncSession,
    invite_id: str,
    current_user: User,
) -> TeamInvite | None:
    return await db.scalar(
        select(TeamInvite)
        .options(
            joinedload(TeamInvite.team),
            joinedload(TeamInvite.inviter),
        )
        .where(
            TeamInvite.id == invite_id,
            TeamInvite.status == "pending",
            func.lower(TeamInvite.email) == current_user.email.lower(),
        )
    )


async def _load_pending_invites(
    db: AsyncSession,
    notifications: list[Notification],
    current_user: User,
) -> dict[str, TeamInvite]:
    invite_ids = [
        invite_id
        for n in notifications
        if n.type == "team_invite"
        and isinstance(n.payload, dict)
        and (invite_id := n.payload.get("invite_id"))
    ]
    if not invite_ids:
        return {}
    result = await db.scalars(
        select(TeamInvite)
        .options(
            joinedload(TeamInvite.team),
            joinedload(TeamInvite.inviter),
        )
        .where(
            TeamInvite.id.in_(invite_ids),
            TeamInvite.status == "pending",
            func.lower(TeamInvite.email) == current_user.email.lower(),
        )
    )
    return {invite.id: invite for invite in result.all()}


async def _dismiss_stale_invite_notifications(
    db: AsyncSession,
    notifications: list[Notification],
    invite_by_id: dict[str, TeamInvite],
    user_id: int,
) -> None:
    for n in notifications:
        if n.type != "team_invite" or not isinstance(n.payload, dict):
            continue
        invite_id = n.payload.get("invite_id")
        if invite_id in invite_by_id or n.read_at:
            continue
        if await NotificationService.mark_read(db, user_id, n.id):
            n.read_at = utc_now()


async def _teams_and_roles(db: AsyncSession, user_id: int):
    result = await db.execute(
        select(Team, TeamMember.role)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(TeamMember.user_id == user_id, Team.status == "active")
        .order_by(Team.name)
    )
    return result.all()


async def _auth_providers(db: AsyncSession, user_id: int):
    result = await db.execute(
        select(UserIdentity).where(UserIdentity.user_id == user_id)
    )
    identities = result.scalars().all()
    github_username = None
    google_email = None
    for identity in identities:
        if identity.provider == "github":
            github_username = (identity.provider_metadata or {}).get("login")
        elif identity.provider == "google":
            google_email = (identity.provider_metadata or {}).get("email")
    return github_username, google_email


@router.api_route("/settings", methods=["GET", "POST"], name="user_settings")
async def user_settings(
    request: Request,
    fragment: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    queue: ArqRedis = Depends(get_queue),
):
    delete_form: Any = await UserDeleteForm.from_formdata(
        request, data={"email": current_user.email}
    )

    if fragment == "danger":
        if await delete_form.validate_on_submit():
            await queue.enqueue_job("delete_user", current_user.id)
            flash(
                request,
                _("Your account is being deleted. You will be signed out shortly."),
                "success",
            )
            return RedirectResponse("/auth/logout", status_code=303)

    general_form: Any = await UserGeneralForm.from_formdata(
        request,
        db=db,
        user=current_user,
        data={
            "name": current_user.name or "",
            "username": current_user.username,
        },
    )

    if fragment == "general":
        if request.method == "POST" and await general_form.validate_on_submit():
            current_user.name = general_form.name.data or None
            current_user.username = general_form.username.data

            avatar_file = general_form.avatar.data
            if (
                avatar_file
                and hasattr(avatar_file, "filename")
                and avatar_file.filename
            ):
                try:
                    from PIL import Image

                    avatar_dir = os.path.join(settings.upload_dir, "avatars")
                    os.makedirs(avatar_dir, exist_ok=True)
                    target_filepath = os.path.join(
                        avatar_dir, f"user_{current_user.id}.webp"
                    )
                    await avatar_file.seek(0)
                    img = Image.open(avatar_file.file)
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                    img.thumbnail((512, 512))
                    img.save(target_filepath, "WEBP", quality=85)
                    current_user.has_avatar = True
                    current_user.updated_at = utc_now()
                except Exception as e:
                    logger.error("Error processing user avatar: %s", e)
                    flash(request, _("Avatar could not be updated."), "error")

            if general_form.delete_avatar.data:
                try:
                    filepath = os.path.join(
                        settings.upload_dir,
                        "avatars",
                        f"user_{current_user.id}.webp",
                    )
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    current_user.has_avatar = False
                    current_user.updated_at = utc_now()
                except Exception as e:
                    logger.error("Error deleting user avatar: %s", e)
                    flash(request, _("Avatar could not be removed."), "error")

            await db.commit()
            flash(request, _("Profile updated."), "success")
            new_url = request.url_for("user_settings")
            if request.headers.get("HX-Request"):
                return Response(
                    status_code=200, headers={"HX-Redirect": str(new_url)}
                )
            return RedirectResponse(new_url, status_code=303)

        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="user/partials/_settings-general.html",
                context={
                    "current_user": current_user,
                    "general_form": general_form,
                },
            )

    email_form: Any = await UserEmailForm.from_formdata(
        request,
        db=db,
        user=current_user,
        data={"email": current_user.email},
    )

    if fragment == "email":
        if request.method == "POST" and await email_form.validate_on_submit():
            new_email = email_form.email.data.strip().lower()
            if new_email != current_user.email.lower():
                current_user.email = new_email
                current_user.updated_at = utc_now()
                await db.commit()
                flash(request, _("Email address updated."), "success")
            else:
                flash(request, _("No changes to save."), "info")

        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="user/partials/_settings-email.html",
                context={
                    "current_user": current_user,
                    "email_form": email_form,
                },
            )

    leave_team_form: Any = await TeamLeaveForm.from_formdata(request)

    if fragment == "teams":
        if request.method == "POST" and await leave_team_form.validate_on_submit():
            team_id = leave_team_form.team_id.data
            if team_id == current_user.default_team_id:
                flash(request, _("You cannot leave your default team."), "error")
            else:
                member = await db.scalar(
                    select(TeamMember).where(
                        TeamMember.team_id == team_id,
                        TeamMember.user_id == current_user.id,
                    )
                )
                if member:
                    if member.role == "owner":
                        other_owners = await db.scalar(
                            select(func.count(TeamMember.id)).where(
                                TeamMember.team_id == team_id,
                                TeamMember.role == "owner",
                                TeamMember.user_id != current_user.id,
                            )
                        )
                        if not other_owners:
                            flash(
                                request,
                                _("Transfer ownership before leaving this team."),
                                "error",
                            )
                        else:
                            await db.delete(member)
                            await db.commit()
                            flash(request, _("You left the team."), "success")
                    else:
                        await db.delete(member)
                        await db.commit()
                        flash(request, _("You left the team."), "success")

        if request.headers.get("HX-Request"):
            teams_and_roles = await _teams_and_roles(db, current_user.id)
            return TemplateResponse(
                request=request,
                name="user/partials/_settings-teams.html",
                context={
                    "current_user": current_user,
                    "teams_and_roles": teams_and_roles,
                    "leave_team_form": leave_team_form,
                },
            )

    teams_and_roles = await _teams_and_roles(db, current_user.id)
    github_username, google_email = await _auth_providers(db, current_user.id)

    return TemplateResponse(
        request=request,
        name="user/pages/settings.html",
        context={
            "current_user": current_user,
            "delete_form": delete_form,
            "general_form": general_form,
            "email_form": email_form,
            "github_username": github_username,
            "google_email": google_email,
            "teams_and_roles": teams_and_roles,
            "leave_team_form": leave_team_form,
        },
    )


@router.get("/notifications/open", name="user_notification_open")
async def user_notification_open(
    request: Request,
    id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == id,
            Notification.user_id == current_user.id,
        )
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    await NotificationService.mark_read(db, current_user.id, notification.id)
    await db.commit()

    if notification.action_url:
        return RedirectResponse(notification.action_url, status_code=303)
    return RedirectResponse(
        str(request.url_for("user_settings")), status_code=303
    )


@router.api_route("/notifications/menu", methods=["GET", "POST"], name="user_notifications")
async def user_notifications(
    request: Request,
    fragment: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    accept_invite_form: Any = await TeamInviteAcceptForm.from_formdata(request)
    decline_invite_form: Any = await TeamInviteDeclineForm.from_formdata(request)

    if request.method == "POST" and fragment == "decline_invite":
        if await decline_invite_form.validate_on_submit():
            invite = await _pending_invite(
                db, decline_invite_form.invite_id.data, current_user
            )
            if not invite or invite.expires_at < utc_now():
                flash(request, _("Invalid or expired invitation."), "error")
            else:
                invite.status = "revoked"
                await NotificationService.dismiss_team_invite(db, invite.id)
                await db.commit()
                flash(request, _("Invitation declined."), "success")
            return RedirectResponse(
                str(request.url_for("user_settings")),
                status_code=303,
            )

    if request.method == "POST" and fragment == "accept_invite":
        if await accept_invite_form.validate_on_submit():
            try:
                invite = await _pending_invite(
                    db, accept_invite_form.invite_id.data, current_user
                )
                if not invite or invite.expires_at < utc_now():
                    flash(request, _("Invalid or expired invitation."), "error")
                    return TemplateResponse(
                        request=request,
                        name="layouts/fragment.html",
                        context={"content": ""},
                        status_code=200,
                    )

                invite.status = "accepted"
                await db.execute(
                    insert(TeamMember).values(
                        team_id=invite.team_id,
                        user_id=current_user.id,
                        role=invite.role,
                    )
                )
                await NotificationService.dismiss_team_invite(db, invite.id)
                await db.commit()

                flash(
                    request,
                    _(
                        'You have accepted the invitation to join "%(team_name)s".',
                        team_name=invite.team.name,
                    ),
                    "success",
                )
                return RedirectResponseX(
                    str(request.url_for("team_index", team_slug=invite.team.slug)),
                    status_code=303,
                    request=request,
                )
            except Exception as e:
                logger.error("Error accepting invitation: %s", e)
                flash(
                    request,
                    _("An error occurred while accepting the invitation."),
                    "error",
                )
                await db.rollback()

    if request.method == "POST" and fragment == "mark_all_read":
        await NotificationService.mark_all_read(db, current_user.id)
        await db.commit()
        return TemplateResponse(
            request=request,
            name="layouts/fragment.html",
            context={"content": ""},
            status_code=200,
        )

    try:
        result = await db.execute(
            select(Notification)
            .where(Notification.user_id == current_user.id)
            .order_by(
                desc(Notification.read_at.is_(None)),
                desc(Notification.created_at),
            )
            .limit(NOTIFICATIONS_BELL_LIMIT)
        )
        notifications = result.scalars().all()
        unread_count = await NotificationService.unread_count(db, current_user.id)

        invite_by_id = await _load_pending_invites(db, notifications, current_user)
        await _dismiss_stale_invite_notifications(
            db, notifications, invite_by_id, current_user.id
        )
        await db.commit()
    except SQLAlchemyError as exc:
        logger.exception("Failed to load notifications menu: %s", exc)
        await db.rollback()
        notifications = []
        invite_by_id = {}
        unread_count = 0
    except Exception as exc:
        logger.exception("Failed to load notifications menu: %s", exc)
        await db.rollback()
        notifications = []
        invite_by_id = {}
        unread_count = 0

    return TemplateResponse(
        request=request,
        name="user/partials/_notifications.html",
        context={
            "current_user": current_user,
            "notifications": notifications,
            "invite_by_id": invite_by_id,
            "pending_invites": list(invite_by_id.values()),
            "unread_count": unread_count,
            "accept_invite_form": accept_invite_form,
            "decline_invite_form": decline_invite_form,
        },
    )
