"""FastAPI 应用装配：组装服务、注册路由、托管 SPA、启动后台协程。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, Tunables
from ..db import Account, make_session_factory
from ..scheduler.queue import TaskQueue
from ..services.accounts import AccountService, AccountUnavailable
from ..services.autoreply import AutoReplyService
from ..services.settings import SettingsStore
from ..services.stats import StatsService
from ..services.peers import PeerService
from ..xianyu import MtopError
from ..xianyu.transport import Transport
from . import api

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("shopmgr")


class Container:
    def __init__(self, settings: Settings, transport_factory: Callable[[Account], Transport] | None = None,
                 tunables_override: Tunables | None = None):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.Session = make_session_factory(settings.db_url)
        self.store = SettingsStore(self.Session)
        self.tunables = (lambda: tunables_override) if tunables_override else self.store.get
        self.accounts = AccountService(settings, self.Session, self.tunables, transport_factory)
        self.peers = PeerService(self.Session)
        self.queue = TaskQueue(self.Session, self.accounts, self.tunables)
        self.autoreply = AutoReplyService(self.Session, self.accounts, self.queue, self.tunables)
        self.stats = StatsService(self.Session, self.accounts)


def create_app(settings: Settings | None = None, *, transport_factory=None, tunables_override=None) -> FastAPI:
    settings = settings or Settings()
    c = Container(settings, transport_factory, tunables_override)

    async def _resume_sessions():
        """启动时依次拉起上次处于登录状态的账号窗口（复用各自 profile 中的登录态），供队列、自动回复使用。"""
        for acc in c.accounts.list():
            if acc.status in ("ok", "restricted"):
                try:
                    await c.accounts.check(acc.id)
                except Exception:  # noqa: BLE001 - 单个账号启动失败不影响其它账号
                    log.exception("resume account %s failed", acc.id)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs = []
        if settings.workers:
            jobs = [asyncio.create_task(c.queue.run_forever()),
                    asyncio.create_task(c.accounts.health_loop()),
                    asyncio.create_task(c.autoreply.run_forever()),
                    asyncio.create_task(_resume_sessions())]
        try:
            yield
        finally:
            c.queue.stop()
            for j in jobs:
                j.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await c.accounts.close_all()

    app = FastAPI(title="闲鱼多店铺管理", version="1.0.0", lifespan=lifespan)
    app.state.c = c

    @app.exception_handler(AccountUnavailable)
    async def _unavailable(_: Request, e: AccountUnavailable):
        return JSONResponse(status_code=409, content={
            "detail": str(e), "status": e.status,
            "resume_at": e.resume_at.isoformat(timespec="seconds") if e.resume_at else None})

    @app.exception_handler(MtopError)
    async def _mtop(_: Request, e: MtopError):
        return JSONResponse(status_code=502, content={
            "detail": e.ret[0] if e.ret else str(e), "code": e.code, "kind": e.kind.value, "api": e.api})

    @app.exception_handler(KeyError)
    async def _missing(_: Request, e: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(e).strip("'")})

    @app.exception_handler(ValueError)
    async def _bad(_: Request, e: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(e)})

    app.include_router(api.router, prefix="/api")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
