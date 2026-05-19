import os
import logging
from types import SimpleNamespace
from typing import Any
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, attributes
from arq.connections import ArqRedis
from utils import storage as storage_utils

from models import (
    Project,
    Deployment,
    User,
    Team,
    TeamMember,
    TeamInvite,
    Storage,
    StorageDatabaseUser,
    StorageProject,
    utc_now,
)
from dependencies import (
    get_current_user,
    get_team_by_slug,
    get_queue,
    flash,
    get_translation as _,
    TemplateResponse,
    templates,
    get_role,
    get_access,
    get_storage_by_name,
    RedirectResponseX,
)
from config import get_settings, Settings
from db import get_db
from utils.pagination import paginate
from utils.deployment import team_recent_deployments_stmt
from utils.team import get_latest_teams
from services.notification import NotificationService
from forms.team import (
    TeamDeleteForm,
    TeamGeneralForm,
    TeamCreateForm,
    TeamMemberAddForm,
    TeamMemberRemoveForm,
    TeamMemberRoleForm,
    TeamInviteRevokeForm,
)
from forms.storage import (
    StorageCreateForm,
    StorageDeleteForm,
    StorageResetForm,
    StorageProjectForm,
    StorageProjectRemoveForm,
    StorageDbUserRotateForm,
    StorageQueryForm,
    StorageBackupSettingsForm,
)
from services import mariadb as mariadb_service
from services import storage_backup as storage_backup_service

logger = logging.getLogger(__name__)

router = APIRouter()


def get_storage_db_path(storage: Storage) -> str:
    settings = get_settings()
    return f"{settings.data_dir}/storage/{storage.team_id}/database/{storage.name}/db.sqlite"


def get_storage_admin_error(storage: Storage, db_path: str) -> str | None:
    if storage.type != "database":
        return _("Only available for databases.")

    if storage.error:
        if isinstance(storage.error, dict):
            return storage.error.get("message") or _("Database is unavailable right now.")
        return str(storage.error)

    if storage.status == "pending":
        return _("This database is still being provisioned.")

    if storage.status == "resetting":
        return _("This database is still being reset.")

    if storage.status != "active":
        return _("This database is not ready yet.")

    if not os.path.isfile(db_path):
        return _("This database file is not ready yet.")

    return None


class StaticFormField:
    def __init__(self, data: Any = None, html: str = ""):
        self.data = data
        self.html = html

    def __call__(self, *args, **kwargs) -> str:
        return self.html


def get_storage_query_form_fallback(query: str = "") -> Any:
    return SimpleNamespace(
        csrf_token=StaticFormField(""),
        query=StaticFormField(query),
        write_mode=StaticFormField(False),
    )


def _storage_password_reveal_key(storage_id: str) -> str:
    return f"_mariadb_password_reveal:{storage_id}"


def set_storage_password_reveal(
    request: Request,
    storage_id: str,
    *,
    username: str,
    password: str,
    database_url: str,
) -> None:
    request.session[_storage_password_reveal_key(storage_id)] = {
        "database_url": database_url,
        "password": password,
        "username": username,
    }


def pop_storage_password_reveal(request: Request, storage_id: str) -> dict[str, str] | None:
    reveal = request.session.pop(_storage_password_reveal_key(storage_id), None)
    return reveal if isinstance(reveal, dict) else None


async def _storage_backup_context(
    request: Request,
    settings: Settings,
    storage: Storage,
    backup_form: Any | None = None,
) -> dict[str, Any]:
    cfg = storage_backup_service.get_backup_config(storage)
    schedule_choices = [
        (key, _(meta["label_key"]))
        for key, meta in storage_backup_service.BACKUP_SCHEDULES.items()
    ]
    backup_schedules = {
        key: {
            "label": _(meta["label_key"]),
            "hint": _(meta["hint_key"]),
        }
        for key, meta in storage_backup_service.BACKUP_SCHEDULES.items()
    }
    if backup_form is None:
        backup_form = await StorageBackupSettingsForm.from_formdata(
            request,
            data={
                "enabled": cfg["enabled"],
                "schedule": cfg["schedule"],
                "max_backups": str(cfg["max_backups"]),
            },
            schedule_choices=schedule_choices,
        )
    backups = [
        {
            "id": entry.id,
            "filename": entry.filename,
            "storage_type": entry.storage_type,
            "size_display": storage_backup_service.format_size(entry.size_bytes),
            "created_at": entry.created_at,
        }
        for entry in storage_backup_service.list_backups(settings, storage)
    ]
    backup_init = {
        "enabled": bool(cfg["enabled"]),
        "schedule": cfg["schedule"],
        "maxBackups": str(cfg["max_backups"]),
        "schedules": backup_schedules,
    }
    return {
        "backup_config": cfg,
        "backup_schedules": backup_schedules,
        "backup_init": backup_init,
        "backup_form": backup_form,
        "backups": backups,
    }


def should_retry_pending_mariadb_provision(storage: Storage) -> bool:
    if storage.type != "mariadb" or storage.status != "pending":
        return False
    if storage.error:
        if not isinstance(storage.error, dict):
            return False
        if storage.error.get("stage") != "provision_mariadb":
            return False
    return storage.updated_at <= utc_now() - timedelta(seconds=30)


async def get_storage_latest_teams(
    db: AsyncSession, current_user: User, team: Team, storage: Storage
) -> list[Team]:
    try:
        return await get_latest_teams(db=db, current_user=current_user, current_team=team)
    except Exception:
        logger.exception(
            "Failed to load team switcher for storage admin",
            extra={"team_id": team.id, "storage_id": storage.id},
        )
        return []


@router.api_route("/new-team", methods=["GET", "POST"], name="new_team")
async def new_team(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form: Any = await TeamCreateForm.from_formdata(request)

    if request.method == "POST" and await form.validate_on_submit():
        team = Team(name=form.name.data, created_by_user_id=current_user.id)
        db.add(team)
        await db.flush()
        db.add(TeamMember(team_id=team.id, user_id=current_user.id, role="owner"))
        await db.commit()
        return Response(
            status_code=200,
            headers={
                "HX-Redirect": str(request.url_for("team_index", team_slug=team.slug))
            },
        )

    return TemplateResponse(
        request=request,
        name="team/partials/_dialog-new-team-form.html",
        context={"form": form},
    )


@router.get("/{team_slug}", name="team_index")
async def team_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    role: str = Depends(get_role),
):
    team, membership = team_and_membership

    projects_result = await db.execute(
        select(Project)
        .where(Project.team_id == team.id, Project.status != "deleted")
        .order_by(Project.updated_at.desc())
        .limit(6)
    )
    projects = projects_result.scalars().all()

    deployments_result = await db.execute(team_recent_deployments_stmt(team.id))
    deployments = deployments_result.scalars().all()

    latest_teams = await get_latest_teams(
        db=db, current_user=current_user, current_team=team
    )

    return TemplateResponse(
        request=request,
        name="team/pages/index.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "projects": projects,
            "deployments": deployments,
            "latest_teams": latest_teams,
        },
    )


@router.get("/{team_slug}/projects", name="team_projects")
async def team_projects(
    request: Request,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    db: AsyncSession = Depends(get_db),
):
    team, membership = team_and_membership

    latest_teams = await get_latest_teams(
        db=db, current_user=current_user, current_team=team
    )

    per_page = 25

    query = (
        select(Project)
        .where(Project.team_id == team.id, Project.status != "deleted")
        .order_by(Project.updated_at.desc())
    )

    pagination = await paginate(db, query, page, per_page)

    return TemplateResponse(
        request=request,
        name="team/pages/projects.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "latest_teams": latest_teams,
            "projects": pagination.get("items"),
            "pagination": pagination,
        },
    )


@router.api_route("/{team_slug}/storage", methods=["GET", "POST"], name="team_storage")
async def team_storage(
    request: Request,
    page: int = Query(1, ge=1),
    storage_search: str | None = Query(None),
    storage_type: str | None = Query(None),
    fragment: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    queue: ArqRedis = Depends(get_queue),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    team, membership = team_and_membership

    form: Any = await StorageCreateForm.from_formdata(request, db=db, team=team)

    if request.method == "POST":
        if await form.validate_on_submit():
            storage = Storage(
                name=form.name.data,
                type=form.type.data,
                status="pending",
                team_id=team.id,
                created_by_user_id=current_user.id,
            )
            db.add(storage)
            await db.flush()
            if storage.type == "mariadb":
                mariadb_admin_user = await mariadb_service.ensure_storage_admin_user(
                    db,
                    settings,
                    storage,
                    created_by_user_id=current_user.id,
                )
            await db.commit()
            try:
                await queue.enqueue_job("provision_storage", storage.id)
            except Exception as exc:
                logger.error(
                    "Failed to enqueue provisioning for storage %s: %s",
                    storage.id,
                    exc,
                )
            flash(request, _("Storage created."), "success")

            if storage.type == "mariadb":
                set_storage_password_reveal(
                    request,
                    storage.id,
                    username=mariadb_admin_user.username,
                    password=mariadb_admin_user.password,
                    database_url=mariadb_service.build_connection_context(
                        settings,
                        storage=storage,
                        username=mariadb_admin_user.username,
                        password=mariadb_admin_user.password,
                    )["database_url"],
                )
                return RedirectResponseX(
                    request.url_for(
                        "team_storage_settings",
                        team_slug=team.slug,
                        storage_name=storage.name,
                    ),
                    status_code=200,
                    request=request,
                )

            return RedirectResponseX(
                request.url_for("team_storage", team_slug=team.slug),
                status_code=200,
                request=request,
            )

        return TemplateResponse(
            request=request,
            name="team/partials/_dialog-new-storage-form.html",
            context={
                "team": team,
                "form": form,
            },
        )

    latest_teams = await get_latest_teams(
        db=db, current_user=current_user, current_team=team
    )

    per_page = 25

    allowed_types = {"database", "mariadb", "volume", "kv", "queue"}
    storage_type = storage_type if storage_type in allowed_types else None

    query = select(Storage).where(
        Storage.team_id == team.id,
        Storage.status != "deleted",
    )
    if not get_access(role, "admin"):
        query = query.where(Storage.created_by_user_id == current_user.id)
    if storage_type:
        query = query.where(Storage.type == storage_type)
    if storage_search:
        query = query.where(Storage.name.ilike(f"%{storage_search}%"))

    query = query.options(
        selectinload(Storage.project_links).selectinload(StorageProject.project)
    ).order_by(Storage.updated_at.desc())

    pagination = await paginate(db, query, page, per_page)

    projects = await db.execute(
        select(Project)
        .where(Project.team_id == team.id, Project.status != "deleted")
        .order_by(Project.name.asc())
    )
    projects = projects.scalars().all()

    storage_count_query = select(func.count(Storage.id)).where(
        Storage.team_id == team.id,
        Storage.status != "deleted",
    )
    if not get_access(role, "admin"):
        storage_count_query = storage_count_query.where(
            Storage.created_by_user_id == current_user.id
        )
    storage_count_result = await db.execute(storage_count_query)
    storage_count = storage_count_result.scalar_one() or 0

    if request.headers.get("HX-Request") and fragment == "storage-content":
        return TemplateResponse(
            request=request,
            name="team/partials/_storage-list.html",
            context={
                "current_user": current_user,
                "team": team,
                "role": role,
                "projects": projects,
                "form": form,
                "pagination": pagination,
                "storages": pagination.get("items"),
                "storage_search": storage_search,
                "storage_type": storage_type,
                "storage_count": storage_count,
            },
        )

    return TemplateResponse(
        request=request,
        name="team/pages/storage.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "latest_teams": latest_teams,
            "projects": projects,
            "form": form,
            "pagination": pagination,
            "storages": pagination.get("items"),
            "storage_search": storage_search,
            "storage_type": storage_type,
            "storage_count": storage_count,
        },
    )


@router.api_route(
    "/{team_slug}/storage/{storage_name}",
    methods=["GET", "POST"],
    name="team_storage_settings",
)
async def team_storage_settings(
    request: Request,
    fragment: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    storage: Storage = Depends(get_storage_by_name),
    queue: ArqRedis = Depends(get_queue),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    team, membership = team_and_membership

    is_admin = get_access(role, "admin")
    is_storage_creator = storage.created_by_user_id == current_user.id
    if not is_admin and not is_storage_creator:
        raise HTTPException(status_code=404, detail="Storage not found")

    storage_db_path = get_storage_db_path(storage) if storage.type == "database" else None
    storage_admin_error = (
        get_storage_admin_error(storage, storage_db_path)
        if storage_db_path
        else None
    )
    storage_admin_ready = storage_admin_error is None if storage_db_path else False

    delete_form: Any = await StorageDeleteForm.from_formdata(request)
    reset_form: Any = await StorageResetForm.from_formdata(request)
    mariadb_admin_user: StorageDatabaseUser | None = None
    mariadb_connection: dict[str, Any] | None = None
    mariadb_database_name: str | None = None
    mariadb_usage_dsn: str | None = None
    mariadb_phpmyadmin_url: str | None = None
    mariadb_password_reveal: dict[str, str] | None = None
    mariadb_open_url: str | None = None
    mariadb_rotate_user_form: Any = None

    if storage.type == "mariadb":
        mariadb_password_reveal = pop_storage_password_reveal(request, storage.id)
        if request.method == "GET" and should_retry_pending_mariadb_provision(storage):
            try:
                storage.error = None
                await queue.enqueue_job("provision_storage", storage.id)
                storage.updated_at = utc_now()
                await db.commit()
            except Exception as exc:
                logger.error(
                    "Failed to retry provisioning for MariaDB storage %s: %s",
                    storage.id,
                    exc,
                )
                await db.rollback()
        mariadb_database_name = mariadb_service.get_storage_database_name(storage)
        mariadb_admin_user = await mariadb_service.get_storage_admin_user(db, storage.id)
        if (
            mariadb_admin_user is None
            or mariadb_admin_user.username
            != mariadb_service.get_storage_admin_username(storage)
        ):
            mariadb_admin_user = await mariadb_service.ensure_storage_admin_user(
                db,
                settings,
                storage,
                created_by_user_id=storage.created_by_user_id,
            )
            await db.commit()
        mariadb_phpmyadmin_url = mariadb_service.build_phpmyadmin_url(settings, storage)
        mariadb_open_url = str(
            request.url_for(
                "team_storage_phpmyadmin",
                team_slug=team.slug,
                storage_name=storage.name,
            )
        )
        mariadb_rotate_user_form = await StorageDbUserRotateForm.from_formdata(request)
        if mariadb_admin_user:
            mariadb_connection = mariadb_service.build_connection_context(
                settings,
                storage=storage,
                username=mariadb_admin_user.username,
                password=mariadb_admin_user.password,
            )
            mariadb_usage_dsn = mariadb_connection["database_url_display"]

    if request.method == "POST" and fragment == "danger":
        form_data = await request.form()
        if not get_access(role, "admin"):
            flash(
                request,
                _("You don't have permission to delete storage."),
                "warning",
            )
        elif "reset_storage" in form_data and await reset_form.validate_on_submit():
            if storage.type in ("database", "mariadb", "volume"):
                storage.status = "resetting"
                storage.error = None
                await db.commit()
                try:
                    await queue.enqueue_job("reset_storage", storage.id)
                except Exception as exc:
                    logger.error(
                        "Failed to enqueue reset for storage %s: %s",
                        storage.id,
                        exc,
                    )
                    storage.status = "active"
                    await db.commit()
                    flash(request, _("Failed to reset storage."), "error")
                else:
                    flash(request, _("Storage reset queued."), "success")
        elif "delete_storage" in form_data and await delete_form.validate_on_submit():
            storage.status = "deleted"
            await db.commit()
            if storage.type in ("database", "mariadb", "volume"):
                try:
                    await queue.enqueue_job("deprovision_storage", storage.id)
                except Exception as exc:
                    logger.error(
                        "Failed to enqueue deprovisioning for storage %s: %s",
                        storage.id,
                        exc,
                    )
            flash(request, _("Storage deleted."), "success")
            return RedirectResponse(
                url=str(request.url_for("team_storage", team_slug=team.slug)),
                status_code=303,
            )

    projects_query = (
        select(Project)
        .where(Project.team_id == team.id, Project.status != "deleted")
        .order_by(Project.name.asc())
    )
    if not is_admin:
        projects_query = projects_query.where(
            Project.created_by_user_id == current_user.id
        )
    projects_result = await db.execute(projects_query)
    projects = projects_result.scalars().all()

    associations_query = (
        select(StorageProject)
        .join(Project)
        .where(
            StorageProject.storage_id == storage.id,
            Project.team_id == team.id,
            Project.status != "deleted",
        )
        .options(selectinload(StorageProject.project))
        .order_by(Project.name.asc())
    )
    if not is_admin:
        associations_query = associations_query.where(
            Project.created_by_user_id == current_user.id
        )
    associations_result = await db.execute(associations_query)
    associations = associations_result.scalars().all()
    available_projects = [
        project
        for project in projects
        if project.id not in {association.project_id for association in associations}
    ]
    default_project = available_projects[0] if available_projects else None

    association_form: Any = await StorageProjectForm.from_formdata(
        request, storage=storage, projects=projects, associations=associations
    )
    remove_association_form: Any = await StorageProjectRemoveForm.from_formdata(
        request, associations=associations
    )

    if request.method == "GET" and fragment == "environment_select":
        project_id = request.query_params.get("project_id")
        selected_project = next(
            (project for project in projects if project.id == project_id), None
        )
        if not selected_project:
            flash(
                request,
                _("You don't have permission to update storage associations."),
                "warning",
            )
            return Response(status_code=403)
        association_form.project_id.data = project_id
        association_form.storage_id.data = storage.id
        return TemplateResponse(
            request=request,
            name="team/partials/_storage-select-environments.html",
            context={
                "current_user": current_user,
                "team": team,
                "role": role,
                "storage": storage,
                "associations": associations,
                "association_form": association_form,
                "selected_project": selected_project,
                "is_active": False,
            },
        )

    if request.method == "POST" and fragment == "association":
        if not is_admin and not is_storage_creator:
            flash(
                request,
                _("You don't have permission to update storage associations."),
                "warning",
            )
        elif await association_form.validate_on_submit():
            association_id = association_form.association_id.data
            association_by_id = {
                str(association.id): association for association in associations
            }
            if association_id:
                association = association_by_id.get(str(association_id))
                if association:
                    association.environment_ids = (
                        association_form.environment_ids.data or []
                    )
                    flash(request, _("Association updated."), "success")
                    await db.commit()
                else:
                    flash(request, _("Association not found."), "error")
                    await db.rollback()
            elif association_form.association:
                association_form.association.environment_ids = (
                    association_form.environment_ids.data or []
                )
                flash(request, _("Association updated."), "success")
                await db.commit()
            else:
                existing_result = await db.execute(
                    select(StorageProject).where(
                        StorageProject.project_id == association_form.project_id.data,
                        StorageProject.storage_id == storage.id,
                    )
                )
                existing_association = existing_result.scalar_one_or_none()
                if existing_association:
                    existing_association.environment_ids = (
                        association_form.environment_ids.data or []
                    )
                    flash(request, _("Association updated."), "success")
                else:
                    association = StorageProject(
                        project_id=association_form.project_id.data,
                        storage_id=storage.id,
                        environment_ids=association_form.environment_ids.data or [],
                    )
                    db.add(association)
                    flash(request, _("Project linked to storage."), "success")
                await db.commit()
            associations_result = await db.execute(associations_query)
            associations = associations_result.scalars().all()
            available_projects = [
                project
                for project in projects
                if project.id
                not in {association.project_id for association in associations}
            ]
            default_project = available_projects[0] if available_projects else None
            association_form = await StorageProjectForm.from_formdata(
                request,
                storage=storage,
                projects=projects,
                associations=associations,
            )
            remove_association_form = await StorageProjectRemoveForm.from_formdata(
                request,
                associations=associations,
            )
            if request.headers.get("HX-Request"):
                return TemplateResponse(
                    request=request,
                    name="team/partials/_storage-settings-associations.html",
                    context={
                        "current_user": current_user,
                        "team": team,
                        "role": role,
                        "storage": storage,
                        "projects": projects,
                        "associations": associations,
                        "association_form": association_form,
                        "remove_association_form": remove_association_form,
                        "available_projects": available_projects,
                        "default_project": default_project,
                    },
                )
            return RedirectResponse(
                url=str(
                    request.url_for(
                        "team_storage_settings",
                        team_slug=team.slug,
                        storage_name=storage.name,
                    )
                ),
                status_code=303,
            )
        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="team/partials/_storage-settings-associations.html",
                context={
                    "current_user": current_user,
                    "team": team,
                    "role": role,
                    "storage": storage,
                    "projects": projects,
                    "associations": associations,
                    "association_form": association_form,
                    "remove_association_form": remove_association_form,
                    "available_projects": available_projects,
                    "default_project": default_project,
                },
            )

    if request.method == "POST" and fragment == "delete_association":
        if not is_admin and not is_storage_creator:
            flash(
                request,
                _("You don't have permission to update storage associations."),
                "warning",
            )
        elif await remove_association_form.validate_on_submit():
            association = remove_association_form.association
            await db.delete(association)
            await db.commit()
            associations_result = await db.execute(associations_query)
            associations = associations_result.scalars().all()
            available_projects = [
                project
                for project in projects
                if project.id
                not in {association.project_id for association in associations}
            ]
            default_project = available_projects[0] if available_projects else None
            association_form = await StorageProjectForm.from_formdata(
                request,
                storage=storage,
                projects=projects,
                associations=associations,
            )
            remove_association_form = await StorageProjectRemoveForm.from_formdata(
                request,
                associations=associations,
            )
            flash(request, _("Association removed."), "success")
            if request.headers.get("HX-Request"):
                return TemplateResponse(
                    request=request,
                    name="team/partials/_storage-settings-associations.html",
                    context={
                        "current_user": current_user,
                        "team": team,
                        "role": role,
                        "storage": storage,
                        "projects": projects,
                        "associations": associations,
                        "association_form": association_form,
                        "remove_association_form": remove_association_form,
                        "available_projects": available_projects,
                        "default_project": default_project,
                    },
                )
            return RedirectResponse(
                url=str(
                    request.url_for(
                        "team_storage_settings",
                        team_slug=team.slug,
                        storage_name=storage.name,
                    )
                ),
                status_code=303,
            )
        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="team/partials/_storage-settings-associations.html",
                context={
                    "current_user": current_user,
                    "team": team,
                    "role": role,
                    "storage": storage,
                    "projects": projects,
                    "associations": associations,
                    "association_form": association_form,
                    "remove_association_form": remove_association_form,
                    "available_projects": available_projects,
                    "default_project": default_project,
                },
            )

    if request.method == "POST" and fragment == "mariadb" and storage.type == "mariadb":
        if not is_admin and not is_storage_creator:
            flash(
                request,
                _("You don't have permission to manage MariaDB access."),
                "warning",
            )
        else:
            form_data = await request.form()
            if "rotate_db_user" in form_data and await mariadb_rotate_user_form.validate_on_submit():
                db_user = await mariadb_service.get_storage_admin_user(db, storage.id)
                if not db_user or str(db_user.id) != str(mariadb_rotate_user_form.user_id.data):
                    flash(request, _("Database user not found."), "error")
                else:
                    await mariadb_service.rotate_user_password(
                        db, settings, storage, db_user
                    )
                    await db.commit()
                    mariadb_admin_user = db_user
                    mariadb_connection = mariadb_service.build_connection_context(
                        settings,
                        storage=storage,
                        username=db_user.username,
                        password=db_user.password,
                    )
                    mariadb_password_reveal = {
                        "database_url": mariadb_connection["database_url"],
                        "password": db_user.password,
                        "username": db_user.username,
                    }
                    flash(request, _("Password rotated."), "success")

        mariadb_admin_user = await mariadb_service.get_storage_admin_user(db, storage.id)
        mariadb_rotate_user_form = await StorageDbUserRotateForm.from_formdata(request)
        mariadb_database_name = mariadb_service.get_storage_database_name(storage)
        if mariadb_admin_user:
            mariadb_connection = mariadb_service.build_connection_context(
                settings,
                storage=storage,
                username=mariadb_admin_user.username,
                password=mariadb_admin_user.password,
            )
            mariadb_usage_dsn = mariadb_connection["database_url_display"]
        else:
            mariadb_connection = None
            mariadb_usage_dsn = None
        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="team/partials/_storage-settings-mariadb.html",
                context={
                    "current_user": current_user,
                    "team": team,
                    "role": role,
                    "storage": storage,
                    "mariadb_admin_user": mariadb_admin_user,
                    "mariadb_connection": mariadb_connection,
                    "mariadb_database_name": mariadb_database_name,
                    "mariadb_usage_dsn": mariadb_usage_dsn,
                    "mariadb_phpmyadmin_url": mariadb_phpmyadmin_url,
                    "mariadb_password_reveal": mariadb_password_reveal,
                    "mariadb_open_url": mariadb_open_url,
                    "mariadb_rotate_user_form": mariadb_rotate_user_form,
                },
            )
        if mariadb_password_reveal and mariadb_admin_user:
            set_storage_password_reveal(
                request,
                storage.id,
                username=mariadb_admin_user.username,
                password=mariadb_password_reveal["password"],
                database_url=mariadb_password_reveal["database_url"],
            )
        return RedirectResponse(
            url=str(
                request.url_for(
                    "team_storage_settings",
                    team_slug=team.slug,
                    storage_name=storage.name,
                )
            ),
            status_code=303,
        )

    backup_context: dict[str, Any] = {}
    if storage.type in ("database", "mariadb") and is_admin:
        backup_ctx = await _storage_backup_context(request, settings, storage)
        backup_form = backup_ctx["backup_form"]

        if fragment == "backup" and request.method == "POST":
            if await backup_form.validate_on_submit():
                storage_backup_service.set_backup_config(
                    storage,
                    enabled=bool(backup_form.enabled.data),
                    schedule=backup_form.schedule.data,
                    max_backups=int(backup_form.max_backups.data),
                )
                attributes.flag_modified(storage, "config")
                storage.updated_at = utc_now()
                await db.commit()
                flash(request, _("Backup settings saved."), "success")
                backup_ctx = await _storage_backup_context(request, settings, storage)

        if fragment == "backup_run" and request.method == "POST":
            if storage.status != "active":
                flash(request, _("Storage must be active to create a backup."), "error")
            else:
                try:
                    await queue.enqueue_job("backup_storage", storage.id)
                    flash(request, _("Backup queued."), "success")
                except Exception as exc:
                    logger.error("Failed to enqueue backup for %s: %s", storage.id, exc)
                    flash(request, _("Failed to queue backup."), "error")
            backup_ctx = await _storage_backup_context(request, settings, storage)

        if fragment in ("backup", "backup_run") and request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="team/partials/_storage-settings-backup.html",
                context={
                    "current_user": current_user,
                    "team": team,
                    "role": role,
                    "storage": storage,
                    **backup_ctx,
                },
            )

        backup_context = backup_ctx

    latest_teams = await get_latest_teams(
        db=db, current_user=current_user, current_team=team
    )

    return TemplateResponse(
        request=request,
        name="team/pages/storage-settings.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "storage": storage,
            "delete_form": delete_form,
            "reset_form": reset_form,
            "associations": associations,
            "association_form": association_form,
            "remove_association_form": remove_association_form,
            "projects": projects,
            "available_projects": available_projects,
            "default_project": default_project,
            "storage_admin_ready": storage_admin_ready,
            "storage_admin_error": storage_admin_error,
            "mariadb_admin_user": mariadb_admin_user,
            "mariadb_connection": mariadb_connection,
            "mariadb_database_name": mariadb_database_name,
            "mariadb_usage_dsn": mariadb_usage_dsn,
            "mariadb_phpmyadmin_url": mariadb_phpmyadmin_url,
            "mariadb_password_reveal": mariadb_password_reveal,
            "mariadb_open_url": mariadb_open_url,
            "mariadb_rotate_user_form": mariadb_rotate_user_form,
            "latest_teams": latest_teams,
            **backup_context,
        },
    )


@router.get(
    "/{team_slug}/storage/{storage_name}/backups/{backup_id}/download",
    name="team_storage_backup_download",
)
async def team_storage_backup_download(
    backup_id: str,
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    storage: Storage = Depends(get_storage_by_name),
    settings: Settings = Depends(get_settings),
):
    team, _membership = team_and_membership
    is_admin = get_access(role, "admin")
    is_storage_creator = storage.created_by_user_id == current_user.id
    if not is_admin and not is_storage_creator:
        raise HTTPException(status_code=404, detail="Storage not found")

    path = storage_backup_service.resolve_backup_path(settings, storage, backup_id)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
    )


@router.get(
    "/{team_slug}/storage/{storage_name}/phpmyadmin",
    name="team_storage_phpmyadmin",
)
async def team_storage_phpmyadmin(
    request: Request,
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    storage: Storage = Depends(get_storage_by_name),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    team, membership = team_and_membership
    is_admin = get_access(role, "admin")
    is_storage_creator = storage.created_by_user_id == current_user.id
    if not is_admin and not is_storage_creator:
        raise HTTPException(status_code=404, detail="Storage not found")
    if storage.type != "mariadb":
        raise HTTPException(status_code=400, detail="Only available for MariaDB storages")

    db_user = await mariadb_service.get_storage_admin_user(db, storage.id)
    if db_user is None or db_user.username != mariadb_service.get_storage_admin_username(storage):
        db_user = await mariadb_service.ensure_storage_admin_user(
            db,
            settings,
            storage,
            created_by_user_id=storage.created_by_user_id,
        )
        await db.commit()

    login_url = mariadb_service.build_phpmyadmin_login_url(
        settings,
        storage=storage,
        username=db_user.username,
        password=db_user.password,
    )
    if not login_url:
        raise HTTPException(status_code=404, detail="phpMyAdmin is not configured")
    return RedirectResponse(login_url, status_code=302)


@router.get(
    "/{team_slug}/storage/{storage_name}/data",
    name="team_storage_data",
)
async def team_storage_data(
    request: Request,
    table: str | None = Query(None),
    page: int = Query(1),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    storage: Storage = Depends(get_storage_by_name),
    db: AsyncSession = Depends(get_db),
):
    team, membership = team_and_membership
    if not get_access(role, "creator"):
        raise HTTPException(status_code=403, detail="Forbidden")
    if storage.type != "database":
        raise HTTPException(status_code=400, detail="Only available for databases")
    tables = []
    query_result = None
    query_error = None
    table_structure = None
    db_path = get_storage_db_path(storage)
    storage_admin_ready = False

    try:
        query_error = get_storage_admin_error(storage, db_path)
        storage_admin_ready = query_error is None

        if not query_error:
            try:
                tables, _schemas = storage_utils.get_tables(db_path)
            except Exception as e:
                query_error = str(e)

        if not table and tables:
            return RedirectResponse(
                url=str(
                    request.url_for(
                        "team_storage_data",
                        team_slug=team.slug,
                        storage_name=storage.name,
                    ).include_query_params(table=tables[0])
                ),
                status_code=302,
            )

        if table and table not in tables:
            raise HTTPException(status_code=404, detail="Table not found")

        if table and not query_error:
            try:
                query_result = storage_utils.read_table(db_path, table, page)
                table_structure = storage_utils.get_table_structure(db_path, table)
            except Exception as e:
                query_error = str(e)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to open storage data admin",
            extra={"team_id": team.id, "storage_id": storage.id},
        )
        query_error = str(exc) or _("Unexpected error while opening the database browser.")
        storage_admin_ready = False

    latest_teams = await get_storage_latest_teams(
        db=db, current_user=current_user, team=team, storage=storage
    )

    return TemplateResponse(
        request=request,
        name="team/pages/storage-data.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "storage": storage,
            "tables": tables,
            "storage_admin_ready": storage_admin_ready,
            "query_result": query_result,
            "query_error": query_error,
            "table_structure": table_structure,
            "current_storage_view": "data",
            "current_storage_table": table,
            "latest_teams": latest_teams,
        },
    )


@router.api_route(
    "/{team_slug}/storage/{storage_name}/sql",
    methods=["GET", "POST"],
    name="team_storage_sql",
)
async def team_storage_sql(
    request: Request,
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    storage: Storage = Depends(get_storage_by_name),
    db: AsyncSession = Depends(get_db),
):
    team, membership = team_and_membership
    if not get_access(role, "creator"):
        raise HTTPException(status_code=403, detail="Forbidden")
    if storage.type != "database":
        raise HTTPException(status_code=400, detail="Only available for databases")
    tables = []
    query_form: Any = get_storage_query_form_fallback()
    query_result = None
    query_error = None
    can_write = get_access(role, "admin")
    db_path = get_storage_db_path(storage)
    storage_admin_ready = False

    try:
        query_form = await StorageQueryForm.from_formdata(request)
        query_error = get_storage_admin_error(storage, db_path)
        storage_admin_ready = query_error is None

        if not query_error:
            try:
                tables, _schemas = storage_utils.get_tables(db_path)
            except Exception as e:
                query_error = str(e)

        if request.method == "POST" and not query_error:
            if await query_form.validate_on_submit():
                sql = query_form.query.data.strip()
                write_mode = query_form.write_mode.data
                if write_mode and not can_write:
                    query_error = _("You don't have permission to run write queries.")
                elif not write_mode and storage_utils.is_write_query(sql):
                    query_error = _(
                        "Write mode is disabled. Enable it to run this query."
                    )
                else:
                    try:
                        query_result = storage_utils.execute_query(
                            db_path, sql, write_mode
                        )
                    except Exception as e:
                        query_error = str(e)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to open storage SQL admin",
            extra={"team_id": team.id, "storage_id": storage.id},
        )
        query_error = str(exc) or _("Unexpected error while opening the SQL console.")
        storage_admin_ready = False

    latest_teams = await get_storage_latest_teams(
        db=db, current_user=current_user, team=team, storage=storage
    )

    return TemplateResponse(
        request=request,
        name="team/pages/storage-sql.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "storage": storage,
            "tables": tables,
            "query_form": query_form,
            "query_result": query_result,
            "query_error": query_error,
            "storage_admin_ready": storage_admin_ready,
            "can_write": can_write,
            "current_storage_view": "sql",
            "current_storage_table": None,
            "latest_teams": latest_teams,
        },
    )




@router.get(
    "/{team_slug}/storage/{storage_id}/status",
    name="team_storage_status",
)
async def team_storage_status(
    request: Request,
    storage_id: str,
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    db: AsyncSession = Depends(get_db),
):
    team, membership = team_and_membership
    is_admin = get_access(role, "admin")

    query = select(Storage).where(
        Storage.id == storage_id,
        Storage.team_id == team.id,
        Storage.status != "deleted",
    )
    if not is_admin:
        query = query.where(Storage.created_by_user_id == current_user.id)

    result = await db.execute(query)
    storage = result.scalar_one_or_none()
    if not storage:
        raise HTTPException(status_code=404, detail="Storage not found")

    return TemplateResponse(
        request=request,
        name="team/partials/_storage-status.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "storage": storage,
        },
    )


@router.api_route(
    "/{team_slug}/settings", methods=["GET", "POST"], name="team_settings"
)
async def team_settings(
    request: Request,
    fragment: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_role),
    team_and_membership: tuple[Team, TeamMember] = Depends(get_team_by_slug),
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_queue),
    settings: Settings = Depends(get_settings),
):
    team, membership = team_and_membership

    if not get_access(role, "admin"):
        flash(
            request,
            _("You don't have permission to access team settings."),
            "warning",
        )
        return RedirectResponse(
            url=str(request.url_for("team_index", team_slug=team.slug)),
            status_code=302,
        )

    # Delete
    delete_team_form = None
    if get_access(role, "owner"):
        # Prevent deleting default teams
        result = await db.execute(select(User).where(User.default_team_id == team.id))
        is_default_team = result.scalar_one_or_none()
        if not is_default_team:
            delete_team_form: Any = await TeamDeleteForm.from_formdata(
                request, team=team
            )
            if request.method == "POST" and fragment == "danger":
                if await delete_team_form.validate_on_submit():
                    try:
                        delete_team_form.status = "deleted"
                        await db.commit()

                        # Team is marked as deleted, actual cleanup is delegated to a job
                        await queue.enqueue_job("delete_team", team.id)

                        flash(
                            request,
                            _('Team "%(name)s" has been marked for deletion.')
                            % {"name": team.name},
                            "success",
                        )
                        return RedirectResponse("/", status_code=303)
                    except Exception as e:
                        await db.rollback()
                        logger.error(
                            f'Error marking team "{team.name}" as deleted: {str(e)}'
                        )
                        flash(
                            request,
                            _("An error occurred while marking the team for deletion."),
                            "error",
                        )

    # General
    general_form: Any = await TeamGeneralForm.from_formdata(
        request,
        data={
            "name": team.name,
            "slug": team.slug,
        },
        db=db,
        team=team,
    )

    if fragment == "general":
        if request.method == "POST" and await general_form.validate_on_submit():
            # Name
            team.name = general_form.name.data or ""

            # Slug
            old_slug = team.slug
            team.slug = general_form.slug.data or ""

            # Avatar upload
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

                    target_filename = f"team_{team.id}.webp"
                    target_filepath = os.path.join(avatar_dir, target_filename)

                    await avatar_file.seek(0)
                    img = Image.open(avatar_file.file)

                    if img.mode != "RGBA":
                        img = img.convert("RGBA")

                    max_size = (512, 512)
                    img.thumbnail(max_size)

                    img.save(target_filepath, "WEBP", quality=85)

                    team.has_avatar = True
                    team.updated_at = utc_now()
                except Exception as e:
                    logger.error(f"Error processing avatar: {str(e)}")
                    flash(request, _("Avatar could not be updated."), "error")

            # Avatar deletion
            if general_form.delete_avatar.data:
                try:
                    avatar_dir = os.path.join(settings.upload_dir, "avatars")
                    filename = f"team_{team.id}.webp"
                    filepath = os.path.join(avatar_dir, filename)

                    if os.path.exists(filepath):
                        os.remove(filepath)

                    team.has_avatar = False
                    team.updated_at = utc_now()
                except Exception as e:
                    logger.error(f"Error deleting avatar: {str(e)}")
                    flash(request, _("Avatar could not be removed."), "error")

            await db.commit()
            flash(request, _("General settings updated."), "success")

            # Redirect if the name has changed
            if old_slug != team.slug:
                new_url = request.url_for("team_settings", team_slug=team.slug)

                if request.headers.get("HX-Request"):
                    return Response(
                        status_code=200, headers={"HX-Redirect": str(new_url)}
                    )
                else:
                    return RedirectResponse(new_url, status_code=303)

        if request.headers.get("HX-Request"):
            return TemplateResponse(
                request=request,
                name="team/partials/_settings-general.html",
                context={
                    "current_user": current_user,
                    "general_form": general_form,
                    "team": team,
                },
            )

    # Members
    add_member_form: Any = await TeamMemberAddForm.from_formdata(
        request, db=db, team=team
    )

    if fragment == "add_member":
        if await add_member_form.validate_on_submit():
            invite = TeamInvite(
                team_id=team.id,
                email=add_member_form.email.data.strip().lower(),
                role=add_member_form.role.data,
                inviter_id=current_user.id,
            )
            db.add(invite)
            await db.commit()
            await db.refresh(invite)
            await _after_member_invite(request, invite, team, settings, db)

    remove_member_form: Any = await TeamMemberRemoveForm.from_formdata(request)

    if fragment == "delete_member":
        if await remove_member_form.validate_on_submit():
            try:
                user = await db.scalar(
                    select(User).where(User.email == remove_member_form.email.data)
                )
                if not user:
                    flash(request, _("User not found."), "error")
                else:
                    member = await db.scalar(
                        select(TeamMember).where(
                            TeamMember.team_id == team.id,
                            TeamMember.user_id
                            == user.id,  # Compare with user.id, not email
                        )
                    )
                    if member:
                        await db.delete(member)
                        await db.commit()
                        flash(
                            request,
                            _(
                                'Member "%(name)s" removed.',
                                name=user.name or user.username,
                            ),
                            "success",
                        )
                    else:
                        flash(request, _("Member not found."), "error")
            except ValueError as e:
                flash(request, str(e), "error")

    member_role_form: Any = await TeamMemberRoleForm.from_formdata(
        request, db=db, team=team
    )

    if fragment == "member_role":
        if await member_role_form.validate_on_submit():
            member = await db.scalar(
                select(TeamMember).where(
                    TeamMember.team_id == team.id,
                    TeamMember.user_id == int(member_role_form.user_id.data),  # type: ignore
                )
            )
            if member:
                member.role = member_role_form.role.data
                await db.commit()
                flash(request, _("Member role updated."), "success")
            else:
                flash(request, _("Member not found."), "error")

    revoke_invite_form: Any = await TeamInviteRevokeForm.from_formdata(request)

    if fragment == "revoke_member_invite" and request.method == "POST":
        if await revoke_invite_form.validate_on_submit():
            invite = await db.scalar(
                select(TeamInvite).where(
                    TeamInvite.id == revoke_invite_form.invite_id.data,
                    TeamInvite.team_id == team.id,
                    TeamInvite.status == "pending",
                )
            )
            if not invite:
                flash(request, _("Invite not found."), "error")
            else:
                email = invite.email
                invite.status = "revoked"
                await NotificationService.dismiss_team_invite(db, invite.id)
                await db.delete(invite)
                await db.commit()
                joined = await db.scalar(
                    select(TeamMember)
                    .join(User)
                    .where(
                        TeamMember.team_id == team.id,
                        func.lower(User.email) == email.lower(),
                    )
                )
                if joined:
                    flash(
                        request,
                        _(
                            "Invite to %(email)s revoked. They had already joined the team — remove them from members if needed.",
                            email=email,
                        ),
                        "warning",
                    )
                else:
                    flash(
                        request,
                        _("Invite to %(email)s revoked.", email=email),
                        "success",
                    )

    members = await db.execute(
        select(TeamMember)
        .where(TeamMember.team_id == team.id)
        .options(selectinload(TeamMember.user))
    )
    members = members.scalars().all()

    member_invites = await db.execute(
        select(TeamInvite).where(
            TeamInvite.team_id == team.id,
            TeamInvite.expires_at > utc_now(),
            TeamInvite.status == "pending",
        )
    )
    member_invites = member_invites.scalars().all()

    owner_count = await db.scalar(
        select(func.count(TeamMember.id)).where(
            TeamMember.team_id == team.id,
            TeamMember.role == "owner",
        )
    )

    if fragment in (
        "add_member",
        "delete_member",
        "revoke_member_invite",
        "member_role",
    ) and request.headers.get("HX-Request"):
        return TemplateResponse(
            request=request,
            name="team/partials/_settings-members.html",
            context={
                "current_user": current_user,
                "team": team,
                "members": members,
                "member_invites": member_invites,
                "revoke_invite_form": revoke_invite_form,
                "add_member_form": add_member_form,
                "remove_member_form": remove_member_form,
                "member_role_form": member_role_form,
                "owner_count": owner_count,
            },
        )

    latest_teams = await get_latest_teams(
        db=db, current_user=current_user, current_team=team
    )

    return TemplateResponse(
        request=request,
        name="team/pages/settings.html",
        context={
            "current_user": current_user,
            "team": team,
            "role": role,
            "delete_team_form": delete_team_form,
            "general_form": general_form,
            "members": members,
            "add_member_form": add_member_form,
            "remove_member_form": remove_member_form,
            "member_role_form": member_role_form,
            "member_invites": member_invites,
            "revoke_invite_form": revoke_invite_form,
            "owner_count": owner_count,
            "latest_teams": latest_teams,
        },
    )


async def _after_member_invite(
    request: Request,
    invite: TeamInvite,
    team: Team,
    settings: Settings,
    db: AsyncSession,
):
    inviter = await db.scalar(select(User).where(User.id == invite.inviter_id))
    inviter_name = (inviter.name or inviter.username) if inviter else _("Someone")
    await NotificationService.sync_team_invite(
        db, invite, team_name=team.name, inviter_name=inviter_name
    )
    await db.commit()
    flash(
        request,
        _(
            "Invitation sent to %(email)s. They can accept it from notifications after signing in with GitHub.",
            email=invite.email,
        ),
        "success",
    )
