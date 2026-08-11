"""Standalone FastAPI entry point for the Qijia AI video workbench."""
from __future__ import annotations

import html
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from qijia_video import (
    MODULE_VERSION,
    account_api,
    api as qijia_video_api,
    cost_api,
)
from qijia_video import topic_api as qijia_topic_api
from qijia_video import auth, run_service
from qijia_video.database import (
    close_database,
    database_configured,
    init_database,
)
from qijia_video.settings import settings


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "qijia_video" / "web"
mimetypes.add_type("image/webp", ".webp")


def ok(data=None, message: str = "") -> dict:
    return {"code": 0, "data": data, "message": message}


@asynccontextmanager
async def lifespan(_: FastAPI):
    configuration_errors = settings.standalone_configuration_errors()
    if configuration_errors and os.getenv("RAILWAY_ENVIRONMENT_ID"):
        raise RuntimeError(
            "独立服务缺少必要配置：" + "、".join(configuration_errors)
        )
    await init_database()
    await run_service.recover_interrupted_tasks()
    yield
    await close_database()


app = FastAPI(
    title="齐家 AI 家庭教育内容工作台",
    version=MODULE_VERSION,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception):
    logger.exception("Unhandled standalone video error")
    return JSONResponse(
        status_code=500,
        content={"code": 500, "data": None, "message": type(exc).__name__},
    )


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    public_paths = {"/health", "/login", "/favicon.ico"}
    if request.url.path in public_paths:
        return await call_next(request)
    user = await auth.user_from_request(request)
    if not user:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"code": 401, "data": None, "message": "请先登录"},
            )
        target = quote(
            request.url.path
            + (f"?{request.url.query}" if request.url.query else ""),
            safe="/?=&",
        )
        return RedirectResponse(url=f"/login?next={target}", status_code=303)
    request.state.user = user
    tokens = run_service.set_task_context(user)
    try:
        return await call_next(request)
    finally:
        run_service.reset_task_context(tokens)


def _safe_next(value: str) -> str:
    candidate = str(value or "").strip()
    if (
        candidate == "/qijia-video"
        or candidate.startswith("/qijia-video?")
        or candidate == "/qijia-video/accounts"
        or candidate.startswith("/qijia-video/accounts?")
        or candidate == "/qijia-video/costs"
        or candidate.startswith("/qijia-video/costs?")
    ) and "\\" not in candidate:
        return candidate
    return "/qijia-video"


def _login_html(*, next_path: str, failed: bool = False) -> str:
    template = (WEB_DIR / "login.html").read_text(encoding="utf-8")
    error = (
        '<p class="error" role="alert">账号或密码不正确。</p>'
        if failed
        else ""
    )
    return template.replace("{{ERROR}}", error).replace(
        "{{NEXT}}", html.escape(_safe_next(next_path), quote=True)
    )


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/qijia-video"):
    if await auth.user_from_request(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(
        _login_html(next_path=next),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/login", include_in_schema=False)
async def login(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/qijia-video"),
):
    user = await auth.authenticate(username, password)
    if not user:
        return HTMLResponse(
            _login_html(next_path=next, failed=True),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        auth.AUTH_COOKIE,
        auth.create_session_token(user),
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="lax",
    )
    return response


@app.post("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.AUTH_COOKIE)
    return response


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/qijia-video", status_code=307)


@app.get("/health")
async def health():
    return ok({
        "status": "ok",
        "version": MODULE_VERSION,
        "database_configured": database_configured(),
        "auth_configured": auth.configured(),
    })


@app.get("/api/tasks/{task_id}")
async def task_status(task_id: str, request: Request):
    user = getattr(request.state, "user", None)
    task = await run_service.get_task_async(task_id)
    if not task or not run_service.can_read_task(user or {}, task):
        raise HTTPException(status_code=404, detail="任务不存在")
    return ok(run_service.public_task(task, viewer=user))


app.include_router(qijia_video_api.api_router)
app.include_router(qijia_topic_api.topic_api_router)
app.include_router(account_api.account_api_router)
app.include_router(cost_api.cost_api_router)
app.include_router(qijia_video_api.page_router)
app.include_router(account_api.account_page_router)
app.include_router(cost_api.cost_page_router)
app.mount(
    "/qijia-video/assets",
    StaticFiles(directory=WEB_DIR),
    name="qijia_video_assets",
)
