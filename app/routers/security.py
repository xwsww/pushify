import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    ORJSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from db import AsyncSessionLocal, get_db
from dependencies import templates, get_translation as _
from services.security_host import security_level_for_host
from services.traefik_security import needs_browser_protection
from utils.host import is_service_host
from services.browser_challenge import (
    boot_cookie_value,
    challenge_auth_redirect,
    challenge_redirect,
    clear_boot_cookie,
    clear_boot_session,
    client_host,
    create_boot_session,
    has_browser_headers,
    is_challenge_path,
    is_static_asset_path,
    issue_challenge,
    max_solve_ms_for_difficulty,
    read_boot_session,
    safe_next_path,
    set_boot_cookie,
    set_pass_cookie,
    verify_challenge,
    verify_pass_cookie,
    wants_html_challenge,
)

CHALLENGE_STATIC_FILES = frozenset(
    {
        "challenge.css",
        "challenge-blobs.min.js",
        "challenge-solve.min.js",
    }
)
CHALLENGE_STATIC_VERSION = "23"

_FORWARD_AUTH_OK = {"X-Pushify-Verified": "1"}


def challenge_headers(*, script_nonce: str | None = None) -> dict[str, str]:
    script_src = "'self'"
    if script_nonce:
        script_src = f"'self' 'nonce-{script_nonce}'"
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors 'none'; script-src {script_src}; "
            "style-src 'self'; connect-src 'self'"
        ),
    }

logger = logging.getLogger(__name__)

router = APIRouter(tags=["security"])


async def get_redis(request: Request) -> Redis:
    return request.app.state.redis_pool


def _site_redirect(
    request: Request, host: str, settings: Settings, next_path: str | None = None
) -> RedirectResponse:
    proto = request.headers.get("x-forwarded-proto", settings.url_scheme)
    path = safe_next_path(next_path or request.query_params.get("next"))
    return RedirectResponse(f"{proto}://{host}{path}", status_code=302)


async def _redirect_if_challenge_disabled(
    request: Request,
    host: str,
    settings: Settings,
    db: AsyncSession,
) -> RedirectResponse | None:
    level = await security_level_for_host(db, host, settings)
    if needs_browser_protection(level):
        return None
    return _site_redirect(request, host, settings)


class VerifyPayload(BaseModel):
    id: str = Field(min_length=8, max_length=64)
    counter: int = Field(ge=0, le=50_000_000)
    proof: str = Field(min_length=16, max_length=512)
    fp: dict = Field(default_factory=dict)
    elapsed_ms: int = Field(ge=0, le=600_000)


@router.get("/security/forward-auth", name="security_forward_auth")
async def forward_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    try:
        host = client_host(request)
        if not host:
            return Response(status_code=401)

        if is_service_host(host, settings):
            return Response(status_code=200, headers=_FORWARD_AUTH_OK)

        ua = request.headers.get("user-agent") or ""
        if not ua or len(ua) < 10:
            return Response(status_code=403)

        ua_lower = ua.lower()
        bot_signatures = [
            "curl", "wget", "python", "go-http", "httpclient", "java",
            "scrapy", "selenium", "phantomjs", "headless", "puppeteer",
            "bot", "crawler", "spider", "scraper", "zgrab", "masscan",
            "nmap", "nikto", "sqlmap", "burp", "cloudflare-warp",
        ]
        if any(sig in ua_lower for sig in bot_signatures):
            return Response(status_code=403)

        if verify_pass_cookie(request, host, settings):
            return Response(status_code=200, headers=_FORWARD_AUTH_OK)

        uri = request.headers.get("x-forwarded-uri") or request.url.path
        uri_path = uri.split("?", 1)[0]
        if is_challenge_path(uri_path):
            return Response(status_code=200, headers=_FORWARD_AUTH_OK)
        if is_static_asset_path(uri_path):
            return Response(status_code=200, headers=_FORWARD_AUTH_OK)
        if not wants_html_challenge(request):
            return Response(status_code=401)

        async with AsyncSessionLocal() as db:
            level = await security_level_for_host(db, host, settings)
        if not needs_browser_protection(level):
            return Response(status_code=200, headers=_FORWARD_AUTH_OK)

        return challenge_auth_redirect(request, host, settings)
    except Exception:
        logger.exception("forward-auth failed")
        return Response(status_code=401)


@router.get("/.pushify", name="security_pushify_root")
@router.get("/.pushify/", name="security_pushify_root_slash")
async def pushify_root(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    host = client_host(request)
    if verify_pass_cookie(request, host, settings):
        return _site_redirect(request, host, settings, "/")
    if redirect := await _redirect_if_challenge_disabled(request, host, settings, db):
        return redirect
    return challenge_redirect(request, host, settings, next_path="/")


@router.get(
    "/.pushify/challenge/static/{asset_name}",
    name="security_challenge_static",
)
async def challenge_static(asset_name: str):
    """Serve challenge assets on /.pushify (deploy hosts route only /.pushify to the panel app)."""
    if asset_name not in CHALLENGE_STATIC_FILES:
        raise HTTPException(status_code=404)
    path = f"assets/security/{asset_name}"
    media = "text/css" if asset_name.endswith(".css") else "application/javascript"
    headers = challenge_headers()
    headers["Cache-Control"] = "public, max-age=86400, immutable"
    return FileResponse(path, media_type=media, headers=headers)


@router.get("/.pushify/challenge", name="security_challenge_page")
async def challenge_page(
    request: Request,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    host = client_host(request)
    if not host:
        return HTMLResponse("Invalid host", status_code=400, headers=challenge_headers())
    if not has_browser_headers(request):
        return HTMLResponse("Invalid client", status_code=400, headers=challenge_headers())
    if verify_pass_cookie(request, host, settings):
        next_path = safe_next_path(request.query_params.get("next"))
        return _site_redirect(request, host, settings, next_path)
    if redirect := await _redirect_if_challenge_disabled(request, host, settings, db):
        return redirect
    level = await security_level_for_host(db, host, settings)
    next_path = safe_next_path(request.query_params.get("next"))
    challenge = await issue_challenge(redis, host, settings, request, level=level)
    difficulty = int(challenge["difficulty"])
    min_ms = int(settings.security_pow_min_seconds * 1000)
    max_ms = max_solve_ms_for_difficulty(difficulty)
    boot_token = await create_boot_session(
        redis,
        host=host,
        next_path=next_path,
        issue=challenge,
        min_ms=min_ms,
        max_ms=max_ms,
        settings=settings,
    )
    template = templates.get_template("security/challenge.html")
    html = template.render(
        request=request,
        challenge_id=challenge["id"],
        static_version=CHALLENGE_STATIC_VERSION,
        _=_,
    )
    response = HTMLResponse(html, headers=challenge_headers())
    set_boot_cookie(response, boot_token, settings, request)
    return response


@router.get("/.pushify/challenge/boot", name="security_challenge_boot")
async def challenge_boot(
    request: Request,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    host = client_host(request)
    if not host:
        return JSONResponse({"error": "invalid_host"}, status_code=400)
    if not has_browser_headers(request):
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if verify_pass_cookie(request, host, settings):
        next_path = safe_next_path(request.query_params.get("next"))
        proto = request.headers.get("x-forwarded-proto", settings.url_scheme)
        return ORJSONResponse(
            {"ok": True, "redirect": f"{proto}://{host}{next_path}"},
            headers=challenge_headers(),
        )
    if redirect := await _redirect_if_challenge_disabled(request, host, settings, db):
        return ORJSONResponse(
            {"ok": True, "redirect": str(redirect.headers["location"])},
            headers=challenge_headers(),
        )
    level = await security_level_for_host(db, host, settings)
    if not needs_browser_protection(level):
        return ORJSONResponse({"ok": True, "redirect": "/"}, headers=challenge_headers())
    token = boot_cookie_value(request)
    if not token:
        return JSONResponse({"error": "session_required"}, status_code=401)
    session = await read_boot_session(redis, token, host)
    if not session:
        return JSONResponse({"error": "session_expired"}, status_code=401)
    return ORJSONResponse(
        {
            "ok": True,
            "next": session.get("next") or "/",
            "issue": session.get("issue"),
            "minMs": session.get("min_ms"),
            "maxMs": session.get("max_ms"),
        },
        headers=challenge_headers(),
    )


@router.post("/.pushify/challenge/verify", name="security_challenge_verify")
async def challenge_verify(
    request: Request,
    body: VerifyPayload,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    host = client_host(request)
    if not host:
        return JSONResponse({"error": "invalid_host"}, status_code=400)
    level = await security_level_for_host(db, host, settings)
    if not needs_browser_protection(level):
        return ORJSONResponse({"ok": True, "redirect": "/"}, headers=challenge_headers())
    ok, reason = await verify_challenge(
        redis,
        challenge_id=body.id,
        counter=body.counter,
        host=host,
        proof=body.proof,
        fp=body.fp,
        elapsed_ms=body.elapsed_ms,
        request=request,
        settings=settings,
    )
    if not ok:
        logger.warning(
            "Challenge verify failed host=%s id=%s reason=%s ip=%s ua=%r",
            host,
            body.id,
            reason,
            request.headers.get("x-forwarded-for"),
            request.headers.get("user-agent"),
        )
        return ORJSONResponse(
            {"ok": False, "reason": reason},
            status_code=403,
            headers=challenge_headers(),
        )
    next_path = safe_next_path(request.query_params.get("next"))
    proto = request.headers.get("x-forwarded-proto", settings.url_scheme)
    redirect_url = f"{proto}://{host}{next_path}"
    response = ORJSONResponse(
        {"ok": True, "redirect": redirect_url}, headers=challenge_headers()
    )
    set_pass_cookie(response, host, settings, request)
    boot_token = boot_cookie_value(request)
    await clear_boot_session(redis, boot_token)
    clear_boot_cookie(response)
    return response
