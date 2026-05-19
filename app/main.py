import asyncio
import logging
import os
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request as StarletteRequest
from starlette_wtf import CSRFProtectMiddleware

from config import get_settings, Settings
from services.traefik_security import ensure_security_middlewares_file
from db import get_db, AsyncSessionLocal
from dependencies import get_current_user, TemplateResponse, templates
from models import User, Team, Deployment, Project
from utils.host import is_deploy_host, is_panel_host
from routers import auth, project, github, google, team, user, event, admin, security
from services.loki import LokiService

settings = get_settings()


class PushifyCSRFMiddleware(CSRFProtectMiddleware):
    def _csrf_exempt(self, request: StarletteRequest) -> bool:
        path = request.url.path
        return (
            path == "/.pushify"
            or path.startswith("/.pushify/")
            or path.startswith("/security/")
        )

    async def dispatch(self, request, call_next):
        if self._csrf_exempt(request):
            return await call_next(request)
        return await super().dispatch(request, call_next)


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
logging.basicConfig(level=log_level)
if log_level > logging.DEBUG:
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_security_middlewares_file(settings.traefik_dir)
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    last_error: Exception | None = None
    for attempt in range(60):
        try:
            app.state.redis_pool = await create_pool(redis_settings)
            break
        except Exception as exc:
            last_error = exc
            if attempt == 59:
                raise RuntimeError(
                    f"Redis unavailable at {settings.redis_url!r} after 60 attempts"
                ) from last_error
            await asyncio.sleep(1)
    app.state.loki_service = LokiService()
    try:
        yield
    finally:
        try:
            await app.state.loki_service.client.aclose()
            await app.state.redis_pool.close()
        except Exception:
            pass


app = FastAPI(
    lifespan=lifespan,
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=settings.secret_key,
            https_only=(settings.url_scheme == "https"),
            same_site="lax",
            max_age=settings.auth_token_ttl_days * 24 * 60 * 60,
        ),
        Middleware(PushifyCSRFMiddleware, csrf_secret=settings.secret_key),
    ],
)
app.mount("/assets", CachedStaticFiles(directory="assets"), name="assets")
os.makedirs(settings.upload_dir, exist_ok=True)
app.mount("/upload", StaticFiles(directory=settings.upload_dir), name="upload")


@app.middleware("http")
async def refresh_auth_cookie(request: Request, call_next):
    """Sets auth cookie if dependency flagged refresh."""
    response = await call_next(request)
    refresh = getattr(request.state, "auth_cookie_refresh", None)
    if refresh:
        response.set_cookie(
            "auth_token",
            refresh["value"],
            httponly=True,
            samesite="lax",
            secure=(settings.url_scheme == "https"),
            path="/",
            max_age=refresh["max_age"],
        )
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].lower()


async def _error_page_context(
    request: Request, settings: Settings, db: AsyncSession | None = None
) -> dict:
    host = _request_host(request)
    panel = is_panel_host(host, settings)
    deploy = False
    if db is not None:
        deploy = await is_deploy_host(host, settings, db)
    return {"is_panel_host": panel, "is_deploy_host": deploy}


_DEPLOY_ERROR_COPY = {
    502: ("Bad gateway", "The app is temporarily unreachable (502)."),
    503: ("Service unavailable", "The app is not available right now (503)."),
    504: ("Gateway timeout", "The app took too long to respond (504)."),
}


@app.get("/errors/{status_code:int}", name="public_error")
async def public_error_page(
    request: Request,
    status_code: int,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _error_page_context(request, settings, db)
    if ctx.get("is_deploy_host"):
        title, message = _DEPLOY_ERROR_COPY.get(status_code, _DEPLOY_ERROR_COPY[502])
        html = templates.get_template("error/standalone.html").render(
            title=title,
            message=message,
        )
        return HTMLResponse(content=html, status_code=status_code)

    templates_map = {502: "error/502.html", 504: "error/504.html", 503: "error/502.html"}
    name = templates_map.get(status_code, "error/502.html")
    return TemplateResponse(
        request=request,
        name=name,
        status_code=status_code,
        context=ctx,
    )


@app.get("/deployment-not-found/{host}")
async def catch_all_missing_container(
    request: Request,
    host: str,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    deploy_host = host.split("/", 1)[0].lower()
    if deploy_host.endswith(f".{settings.deploy_domain.lower()}") or await is_deploy_host(
        deploy_host, settings, db
    ):
        proto = request.headers.get("x-forwarded-proto", settings.url_scheme)
        return RedirectResponse(f"{proto}://{deploy_host}/", status_code=302)

    current_user = await get_current_user(
        request=request,
        db=db,
        settings=settings,
        redirect_on_fail=False,
    )

    if current_user and host.endswith(settings.deploy_domain):
        import re

        subdomain = host.removesuffix(f".{settings.deploy_domain}")

        match = re.match(
            r"^(?P<project_slug>.+)-id-(?P<short_id>[a-f0-9]{7})$", subdomain
        )
        if match:
            project_slug = match.group("project_slug")
            short_id = match.group("short_id")
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Deployment, Project, Team)
                    .join(Project, Deployment.project_id == Project.id)
                    .join(Team, Project.team_id == Team.id)
                    .where(
                        Project.slug == project_slug,
                        Deployment.id.startswith(short_id),
                    )
                )
                deployment, project, team = result.first() or (None, None, None)
                if deployment:
                    return TemplateResponse(
                        request=request,
                        name="error/deployment-not-found.html",
                        status_code=503,
                        context={
                            "deployment_url": request.url_for(
                                "project_deployment",
                                team_slug=team.slug,
                                project_name=project.name,
                                deployment_id=deployment.id,
                            ).include_query_params(action="redeploy"),
                        },
                    )

        return TemplateResponse(
            request=request,
            name="error/deployment-not-found.html",
            status_code=503,
            context={},
        )

    return TemplateResponse(
        request=request,
        name="error/deployment-not-found.html",
        status_code=503,
        context={},
    )


@app.get("/", name="root")
async def root(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Team.slug).where(Team.id == current_user.default_team_id)
    )
    team_slug = result.scalar_one_or_none()
    if team_slug:
        return RedirectResponse(f"/{team_slug}", status_code=302)


app.include_router(security.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(user.router)
app.include_router(project.router)
app.include_router(github.router)
app.include_router(google.router)
app.include_router(team.router)
app.include_router(event.router)


@app.exception_handler(404)
async def handle_404(request: Request, exc: HTTPException):
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        ctx = await _error_page_context(request, settings, db)
    return TemplateResponse(
        request=request, name="error/404.html", status_code=404, context=ctx
    )


@app.exception_handler(500)
async def handle_500(request: Request, exc: HTTPException):
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        ctx = await _error_page_context(request, settings, db)
    return TemplateResponse(
        request=request, name="error/500.html", status_code=500, context=ctx
    )


@app.exception_handler(Exception)
async def handle_unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logging.getLogger(__name__).exception(
        "Unhandled error on %s %s", request.method, request.url.path
    )
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        ctx = await _error_page_context(request, settings, db)
    return TemplateResponse(
        request=request, name="error/500.html", status_code=500, context=ctx
    )
