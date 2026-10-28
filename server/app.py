# -*- coding: utf-8 -*-
"""FastAPI 应用：REST + SSE + 静态托管 + 同源校验（严格按 api-contract.md）。"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from . import persist
from .core import SniperEngine

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ============================ 事件广播 ============================
class Broadcaster:
    """每个订阅者一个有界队列（500），满则丢弃该事件，绝不阻塞广播方。"""

    def __init__(self, maxsize: int = 500) -> None:
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._subs: set = set()

    def subscribe(self) -> "queue.Queue[Any]":
        q: "queue.Queue[Any]" = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Any]") -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        """引擎线程调用；put_nowait 失败即丢弃。"""
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
            except Exception:
                pass

    def count(self) -> int:
        with self._lock:
            return len(self._subs)


# ============================ 同源校验 ============================
def _hostname(value: str) -> str:
    """从 Host / Origin 头里取出主机名（小写，去端口/协议/路径）。"""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):  # IPv6 字面量
        return value[1:].split("]", 1)[0].lower()
    return value.split(":")[0].strip().lower()


async def _send_forbidden(send, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1"))],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


class SameOriginMiddleware:
    """纯 ASGI 中间件（不经 BaseHTTPMiddleware，避免破坏 SSE 流式输出）。

    - Host 必须是 localhost / 127.0.0.1；
    - 写方法（POST/PUT/PATCH/DELETE）若带 Origin 头，其 host 必须同源。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {(k or b"").lower(): (v or b"").decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        if _hostname(headers.get(b"host", "")) not in ALLOWED_HOSTS:
            await _send_forbidden(send, "Host 不被允许（仅限 localhost/127.0.0.1）")
            return
        if scope.get("method", "").upper() in WRITE_METHODS:
            origin = headers.get(b"origin")
            if origin and _hostname(origin) not in ALLOWED_HOSTS:
                await _send_forbidden(send, "跨站请求被拒绝（Origin 非同源）")
                return
        await self.app(scope, receive, send)


# ============================ 静态目录 ============================
class _OptionalStaticFiles(StaticFiles):
    """web/ 目录可能尚未生成（另一条 lane 并行创建）：缺失时不报错，按 404 处理。"""

    async def check_config(self) -> None:  # noqa: D401 - 有意跳过目录存在性检查
        return


# ============================ 请求模型 ============================
class ConfigPatch(BaseModel):
    """PUT /api/config：接受任意子集，返回全量。未知字段/越界值 → 422。"""

    model_config = ConfigDict(extra="forbid")

    base: Optional[str] = None
    poll_interval: Optional[float] = Field(default=None, ge=0.2, le=3600)
    task_types: Optional[list[str]] = None
    platform_account_id: Optional[int] = Field(default=None, ge=1)
    rate_limit_max: Optional[int] = Field(default=None, ge=1, le=10000)
    rate_limit_window: Optional[float] = Field(default=None, ge=0.1, le=3600)
    max_pages: Optional[int] = Field(default=None, ge=1, le=100)
    page_size: Optional[int] = Field(default=None, ge=1, le=200)
    http_timeout: Optional[float] = Field(default=None, ge=1, le=120)
    result_polls: Optional[int] = Field(default=None, ge=0, le=20)

    @field_validator("base")
    @classmethod
    def _check_base(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().rstrip("/")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base 必须以 http:// 或 https:// 开头")
        return value

    @field_validator("task_types")
    @classmethod
    def _check_task_types(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return value
        if not value:
            raise ValueError("task_types 不能为空")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("task_types 元素必须是非空字符串")
        return value


class TokenPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(min_length=1)


# ============================ 应用 ============================
def _engine(request: Request) -> SniperEngine:
    return request.app.state.engine


def _bus(request: Request) -> Broadcaster:
    return request.app.state.bus


def _sse_frame(payload: Any) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    directory = persist.data_dir()
    try:
        persist.ensure_dir(directory)
    except Exception:
        pass

    bus = Broadcaster()
    engine = SniperEngine(bus.publish)
    app.state.bus = bus
    app.state.engine = engine
    app.state.data_dir = directory

    # ---- 恢复 config.json ----
    saved_config = None
    try:
        saved_config = persist.load_config(directory)
    except Exception:
        saved_config = None
    if saved_config:
        try:
            engine.update_config(saved_config)
        except ValueError as exc:
            engine.log(f"config.json 含无效配置，已忽略：{exc}", "WARN")

    # ---- 恢复 token.json（token 本身绝不入日志）----
    try:
        saved_token = persist.load_token(directory)
    except Exception:
        saved_token = None
    if saved_token:
        try:
            engine.set_token(saved_token)
        except ValueError:
            engine.log("token.json 中的 token 无法解析，请在界面重新填写", "WARN")

    # ---- 恢复 history.jsonl（最后 200 行）----
    try:
        engine.load_history(persist.load_history(directory, 200))
    except Exception:
        pass

    try:
        yield
    finally:
        try:
            engine.stop()
        except Exception:
            pass


app = FastAPI(title="materials-task-sniper", version="1.0.0", lifespan=lifespan)
app.add_middleware(SameOriginMiddleware)
app.mount("/static", _OptionalStaticFiles(directory=str(WEB_DIR), check_dir=False), name="static")


# ---------------------------- 状态 / 配置 ----------------------------
@app.get("/api/status")
def get_status(request: Request) -> dict:
    return _engine(request).status()


@app.get("/api/config")
def get_config(request: Request) -> dict:
    return _engine(request).snapshot()


@app.put("/api/config")
def put_config(patch: ConfigPatch, request: Request) -> dict:
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        return _engine(request).snapshot()
    try:
        return _engine(request).update_config(changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ------------------------------ token ------------------------------
@app.put("/api/token")
def put_token(payload: TokenPatch, request: Request) -> dict:
    try:
        return _engine(request).set_token(payload.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------- 启动 / 停止 ----------------------------
@app.post("/api/start")
def post_start(request: Request) -> dict:
    try:
        _engine(request).start()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "status": _engine(request).status()}


@app.post("/api/stop")
def post_stop(request: Request) -> dict:
    _engine(request).stop()
    return {"ok": True, "status": _engine(request).status()}


# ---------------------------- 任务 / 历史 ----------------------------
@app.get("/api/tasks")
def get_tasks(request: Request) -> dict:
    return _engine(request).tasks()


@app.get("/api/history")
def get_history(request: Request) -> dict:
    return {"items": _engine(request).history()}


# ------------------------------- SSE -------------------------------
@app.get("/api/events")
def get_events(request: Request):
    engine = _engine(request)
    bus = _bus(request)

    def snapshot_payload() -> dict:
        return {
            "type": "snapshot",
            "status": engine.status(),
            "tasks": engine.tasks(),
            "history": engine.history(),
            "logs": engine.logs(),
        }

    def event_stream():  # 同步生成器：由 starlette 线程池迭代
        q = bus.subscribe()
        try:
            yield _sse_frame(snapshot_payload())
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    yield b": ping\n\n"      # 心跳注释行
                    continue
                except Exception:
                    break
                yield _sse_frame(event)
        finally:
            bus.unsubscribe(q)

    return EventSourceResponse(event_stream(), media_type="text/event-stream", ping=15)


# ------------------------------ 静态 ------------------------------
@app.get("/", include_in_schema=False)
def index():
    index_file = WEB_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(str(index_file))
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>materials-task-sniper</title>"
        "<h3>前端资源未生成</h3>"
        "<p>请在项目根目录创建 <code>web/index.html</code>（由前端 lane 产出），"
        "然后刷新本页。</p>"
        "<p>API 可用：<code>/api/status</code> <code>/api/config</code> "
        "<code>/api/tasks</code> <code>/api/history</code> <code>/api/events</code></p>",
        status_code=200,
    )
