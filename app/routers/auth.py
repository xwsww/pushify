from typing import Annotated
import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from starlette.responses import RedirectResponse
from authlib.jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from datetime import timedelta
import secrets

from config import Settings, get_settings
from dependencies import (
    get_translation as _,
    flash,
    TemplateResponse,
    get_current_user,
    get_github_oauth_client,
    get_github_primary_email,
    decode_jwt_claims,
    get_redis_client,
    get_queue,
)
from db import get_db
from models import Allowlist, User, UserIdentity, TeamInvite, TeamMember, Team, utc_now
from utils.user import sanitize_username, get_user_by_email, get_user_by_provider
from utils.access import is_email_allowed, notify_denied

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


async def _create_user_with_team(
    request: Request,
    db: AsyncSession,
    email: str,
    name: str | None = None,
    username: str | None = None,
) -> User:
    if not username:
        username = email.split("@")[0]

    base_username = sanitize_username(username)

    user = None
    for attempt in range(5):
        try:
            if attempt == 0:
                unique_username = base_username[:50]
            else:
                random_suffix = secrets.token_hex(2)
                unique_username = f"{base_username[:45]}-{random_suffix}"

            user = User(
                email=email.strip().lower(),
                name=name,
                username=unique_username,
                email_verified=True,
            )
            db.add(user)
            await db.flush()
            break

        except IntegrityError as e:
            await db.rollback()
            if "username" not in str(e):
                raise

    if not user or not user.id:
        raise RuntimeError("Failed to create user after maximum retries")

    team = Team(name=user.name or user.username, created_by_user_id=user.id)
    db.add(team)
    await db.flush()

    user.default_team_id = team.id
    db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))

    # For superadmin accounts, we pull all runner images and display a helper message
    if user.id == 1:
        normalized_email = user.email.strip().lower()
        existing_allowlist = await db.scalar(
            select(Allowlist.id).where(
                Allowlist.type == "email",
                Allowlist.value == normalized_email,
            )
        )
        if not existing_allowlist:
            db.add(Allowlist(type="email", value=normalized_email))

        queue = get_queue(request)
        await queue.enqueue_job("pull_all_runner_images")
        flash(request, _("Pulling all enabled runner images."), "success")
        flash(
            request=request,
            title=_(
                "You can manage access, users and runners/presets in the admin panel."
            ),
            category="warning",
            action={
                "label": _("Admin"),
                "href": str(request.url_for("admin_settings")),
            },
            cancel={"label": _("Dismiss")},
            attrs={"data-duration": "-1"},
        )

    return user


def _create_session_cookie(user: User, settings: Settings) -> RedirectResponse:
    now = utc_now()
    expires_at = now + timedelta(days=settings.auth_token_ttl_days)
    jwt_token = jwt.encode(
        {"alg": "HS256"},
        {
            "sub": user.id,
            "type": "auth_token",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": settings.auth_token_issuer,
            "aud": settings.auth_token_audience,
        },
        settings.secret_key,
    )
    jwt_token_str = (
        jwt_token.decode("utf-8") if isinstance(jwt_token, bytes) else jwt_token
    )
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "auth_token",
        jwt_token_str,
        httponly=True,
        samesite="lax",
        secure=(settings.url_scheme == "https"),
        path="/",
        max_age=settings.auth_token_ttl_days * 24 * 60 * 60,
    )
    return response


@router.api_route("/login", methods=["GET", "POST"], name="auth_login")
async def auth_login(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    try:
        current_user = await get_current_user(request, db, settings)
        if current_user:
            return RedirectResponse("/", status_code=303)
    except HTTPException:
        pass

    if request.method == "POST":
        return RedirectResponse(request.url_for("auth_github_login"), status_code=303)

    return TemplateResponse(
        request=request,
        name="auth/pages/login.html",
        context={
            "login_header": settings.login_header,
        },
    )


@router.get("/email/verify", name="auth_email_verify")
async def auth_email_verify(
    request: Request,
    token: str,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis_client),
):
    token_type = None
    try:
        current_user = None
        try:
            current_user = await get_current_user(request, db, settings)
        except HTTPException:
            pass

        payload = decode_jwt_claims(token, settings)
        token_type = payload.get("type")

        if token_type == "email_login":
            flash(
                request,
                _("Email login is disabled. Please continue with GitHub."),
                "warning",
            )
            return RedirectResponse("/auth/login", status_code=303)

        elif token_type == "email_change":
            flash(
                request,
                _("Email verification links are no longer used. Update your email in account settings."),
                "warning",
            )
            return RedirectResponse("/user/settings#email", status_code=303)

        elif token_type == "team_invite":
            flash(
                request,
                _("Team invitations are accepted from notifications while signed in with GitHub."),
                "info",
            )
            if current_user:
                return RedirectResponse("/", status_code=303)
            return RedirectResponse(
                str(request.url_for("auth_login")), status_code=303
            )

        else:
            raise HTTPException(status_code=400, detail="Invalid token type")

    except Exception:
        logger.error(
            "Auth email verify failed",
            extra={"token_type": token_type},
            exc_info=True,
        )
        flash(request, _("Invalid or expired invitation."), "error")
        return RedirectResponse("/", status_code=303)


@router.get("/github", name="auth_github_login")
async def auth_github_login(
    request: Request,
    oauth_client=Depends(get_github_oauth_client),
):
    if not oauth_client.github:
        raise HTTPException(
            status_code=500, detail="GitHub OAuth client not configured"
        )
    return await oauth_client.github.authorize_redirect(
        request, request.url_for("auth_github_callback")
    )


@router.get("/github/callback", name="auth_github_callback")
async def auth_github_callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: AsyncSession = Depends(get_db),
    oauth_client=Depends(get_github_oauth_client),
):
    if not oauth_client.github:
        raise HTTPException(
            status_code=500, detail="GitHub OAuth client not configured"
        )

    token = await oauth_client.github.authorize_access_token(request)
    response = await oauth_client.github.get("user", token=token)
    gh_user = response.json()

    user = await get_user_by_provider(db, "github", str(gh_user["id"]))

    if user:
        result = await db.execute(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id, UserIdentity.provider == "github"
            )
        )
        github_identity = result.scalar_one_or_none()
        if github_identity:
            github_identity.access_token = token["access_token"]
            github_identity.provider_metadata = {
                "login": gh_user["login"],
                "name": gh_user.get("name"),
            }
    else:
        email = await get_github_primary_email(oauth_client, token)
        if email:
            user = await get_user_by_email(db, email)

        if not user:
            if email and not await is_email_allowed(email, db):
                await notify_denied(
                    email,
                    "github",
                    request,
                    settings.access_denied_webhook,
                )
                flash(request, _(settings.access_denied_message), "error")
                return RedirectResponse("/auth/login", status_code=303)
            user = await _create_user_with_team(
                request,
                db,
                email=email or f"{gh_user['login']}@github.local",
                name=gh_user.get("name"),
                username=gh_user["login"],
            )

        github_identity = UserIdentity(
            user_id=user.id,
            provider="github",
            provider_user_id=str(gh_user["id"]),
            access_token=token["access_token"],
            provider_metadata={
                "login": gh_user["login"],
                "name": gh_user.get("name"),
            },
        )
        db.add(github_identity)

    await db.commit()
    await db.refresh(user)
    return _create_session_cookie(user, settings)


@router.get("/google", name="auth_google_login")
async def auth_google_login(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    flash(
        request,
        _("Google login is disabled. Please continue with GitHub."),
        "warning",
    )
    return RedirectResponse("/auth/login", status_code=303)


@router.get("/google/callback", name="auth_google_callback")
async def auth_google_callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: AsyncSession = Depends(get_db),
):
    flash(
        request,
        _("Google login is disabled. Please continue with GitHub."),
        "warning",
    )
    return RedirectResponse("/auth/login", status_code=303)


@router.get("/logout", name="auth_logout")
async def auth_logout():
    response = RedirectResponse("/auth/login")
    response.delete_cookie("auth_token")
    return response
