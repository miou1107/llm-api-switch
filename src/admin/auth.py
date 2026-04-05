"""Admin authentication routes — login, logout, user management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from src.db.queries import (
    admin_user_count,
    change_admin_password,
    create_admin_session,
    create_admin_user,
    delete_admin_session,
    delete_admin_user,
    list_admin_users,
    validate_admin_login,
)

router = APIRouter(prefix="/admin/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    """Return whether setup is needed (no users exist yet)."""
    db = request.app.state.db
    count = await admin_user_count(db)
    return {"needs_setup": count == 0}


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    db = request.app.state.db
    user = await validate_admin_login(db, body.username, body.password)
    if user is None:
        return Response(
            content='{"error": "Invalid username or password"}',
            status_code=401,
            media_type="application/json",
        )
    settings = getattr(request.app.state, "settings", {})
    ttl = settings.get("admin_auth", {}).get("session_ttl_hours", 168)
    token = await create_admin_session(db, user["id"], ttl)
    response = Response(
        content='{"status": "ok", "user": ' + __import__("json").dumps(user) + '}',
        media_type="application/json",
    )
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ttl * 3600,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    db = request.app.state.db
    token = request.cookies.get("admin_session")
    if token:
        await delete_admin_session(db, token)
    response = Response(
        content='{"status": "logged_out"}',
        media_type="application/json",
    )
    response.delete_cookie("admin_session", path="/")
    return response


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "admin_user", None)
    if user is None:
        return Response(
            content='{"error": "Not authenticated"}',
            status_code=401,
            media_type="application/json",
        )
    return user


@router.post("/setup")
async def setup(body: SetupRequest, request: Request, response: Response) -> Any:
    """First-time setup — create the initial admin user. Only works when no users exist."""
    db = request.app.state.db
    count = await admin_user_count(db)
    if count > 0:
        return Response(
            content='{"error": "Setup already completed. Use login instead."}',
            status_code=403,
            media_type="application/json",
        )
    user = await create_admin_user(db, body.username, body.password, body.display_name)
    settings = getattr(request.app.state, "settings", {})
    ttl = settings.get("admin_auth", {}).get("session_ttl_hours", 168)
    token = await create_admin_session(db, user["id"], ttl)
    response = Response(
        content='{"status": "ok", "user": ' + __import__("json").dumps(user) + '}',
        media_type="application/json",
    )
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ttl * 3600,
        path="/",
    )
    return response


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request) -> Any:
    """Change password for the currently logged-in user."""
    user = getattr(request.state, "admin_user", None)
    if user is None:
        return Response(
            content='{"error": "Not authenticated"}',
            status_code=401,
            media_type="application/json",
        )
    db = request.app.state.db
    # Verify old password
    check = await validate_admin_login(db, user["username"], body.old_password)
    if check is None:
        return Response(
            content='{"error": "Current password is incorrect"}',
            status_code=400,
            media_type="application/json",
        )
    await change_admin_password(db, user["id"], body.new_password)
    return {"status": "ok"}


@router.get("/users")
async def get_users(request: Request) -> list[dict[str, Any]]:
    user = getattr(request.state, "admin_user", None)
    if user is None:
        return Response(
            content='{"error": "Not authenticated"}',
            status_code=401,
            media_type="application/json",
        )
    db = request.app.state.db
    return await list_admin_users(db)


@router.post("/users")
async def create_user(body: CreateUserRequest, request: Request) -> Any:
    user = getattr(request.state, "admin_user", None)
    if user is None:
        return Response(
            content='{"error": "Not authenticated"}',
            status_code=401,
            media_type="application/json",
        )
    db = request.app.state.db
    try:
        new_user = await create_admin_user(db, body.username, body.password, body.display_name)
        return new_user
    except Exception:
        return Response(
            content='{"error": "Username already exists"}',
            status_code=409,
            media_type="application/json",
        )


@router.delete("/users/{user_id}")
async def remove_user(user_id: int, request: Request) -> Any:
    current = getattr(request.state, "admin_user", None)
    if current is None:
        return Response(
            content='{"error": "Not authenticated"}',
            status_code=401,
            media_type="application/json",
        )
    if current["id"] == user_id:
        return Response(
            content='{"error": "Cannot delete yourself"}',
            status_code=400,
            media_type="application/json",
        )
    db = request.app.state.db
    found = await delete_admin_user(db, user_id)
    if not found:
        return Response(
            content='{"error": "User not found"}',
            status_code=404,
            media_type="application/json",
        )
    return {"status": "deleted", "user_id": user_id}
