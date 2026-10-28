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
from .credentials import (PHONE_RE, CredentialStore, mask_phone,
                          normalize_phone, verify_dpapi)
from .login import LoginBusy, LoginManager, is_upstream_error

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


class CredentialIn(BaseModel):
    """POST /api/credentials：手机号不符 ``/^1[3-9]\\d{9}$/`` → 422。"""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    phone: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str) -> str:
        value = normalize_phone(value)
        if not PHONE_RE.match(value):
            raise ValueError("手机号格式不正确（应为 1[3-9] 开头的 11 位数字）")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: Optional[str]) -> Optional[str]:
        return (value or "").strip()[:64]

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        if not value:
            raise ValueError("密码不能为空")
        return value


class LoginSessionIn(BaseModel):
    """POST /api/login-session。target_url 仅允许 http/https。"""

    model_config = ConfigDict(extra="ignore")

    mode: str = "interactive"
    debug: bool = False
    target_url: Optional[str] = None


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

    # ---- 凭据存储 + DPAPI 加密状态（启动探测，供 UI 展示）----
    store = CredentialStore(directory, on_log=engine.log)
    encryption = verify_dpapi()
    app.state.credentials = store
    app.state.encryption = encryption
    if not encryption.get("available"):
        engine.log(f"凭据加密不可用：{encryption.get('detail')}", "WARN")

    # ---- 登录管理（交互式会话 + 凭据直登 + 自动续登）----
    login_manager = LoginManager(engine, bus=bus, store=store)
    app.state.login_manager = login_manager
    try:
        login_manager.start_auto_renew()
    except Exception as exc:
        engine.log(f"自动续登线程启动失败：{exc}", "WARN")

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
        # 关停顺序：登录会话/自动续登 → 杀残留 playwright/Edge → 引擎线程
        try:
            login_manager.shutdown()
        except Exception:
            pass
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


# -------------------------- 凭据 / 登录会话 --------------------------
@app.get("/api/credentials")
def get_credentials(request: Request) -> dict:
    """脱敏列表：phone 中间四位打码，永不返回 password / password_enc。

    ``encryption``（含 ``available/mode/detail``）是附加信息，供 UI 展示加密
    状态；``dpapi`` 是同一信息的布尔快捷字段（前端 `data.dpapi === false`
    即提示「降级明文存储」）。
    """
    encryption = request.app.state.encryption
    return {
        "items": request.app.state.credentials.list_public(),
        "encryption": encryption,
        "dpapi": bool(encryption.get("available")),
    }


@app.post("/api/credentials", status_code=201)
def post_credential(payload: CredentialIn, request: Request) -> dict:
    try:
        item = request.app.state.credentials.add(payload.name, payload.phone,
                                                 payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _engine(request).log(f"已添加登录凭据：{item['name'] or '未命名'}（{item['phone']}）",
                         "INFO")
    return item


@app.delete("/api/credentials/{cred_id}")
def delete_credential(cred_id: int, request: Request) -> dict:
    store = request.app.state.credentials
    record = store.get(cred_id)
    deleted = store.delete(cred_id)
    if deleted and record is not None:
        _engine(request).log(f"已删除登录凭据：{record.get('name') or '未命名'}"
                             f"（{mask_phone(record.get('phone'))}）", "INFO")
    return {"ok": True, "id": cred_id, "deleted": bool(deleted)}


@app.post("/api/credentials/{cred_id}/fetch-token")
def post_fetch_token(cred_id: int, request: Request) -> dict:
    """用单条凭据登录取 token。失败仍 200（由 ok 区分），上游 5xx → 502。"""
    manager = request.app.state.login_manager
    if request.app.state.credentials.get(cred_id) is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    result = manager.fetch_one(cred_id)
    upstream_error = bool(result.pop("_upstream_error", False))
    if not result.get("ok") and (upstream_error or is_upstream_error(result.get("error"))):
        raise HTTPException(status_code=502, detail=result.get("error") or "上游服务错误")
    return result


@app.post("/api/token/fetch-any")
def post_fetch_any(request: Request) -> dict:
    """依次尝试全部凭据直到首个成功。"""
    result = request.app.state.login_manager.fetch_any()
    result.pop("_upstream_error", None)
    return result


@app.post("/api/login-session")
def post_login_session(payload: LoginSessionIn, request: Request) -> dict:
    mode = (payload.mode or "interactive").strip().lower()
    if mode != "interactive":
        raise HTTPException(status_code=400, detail="仅支持 interactive 模式")
    base = str(_engine(request).snapshot().get("base") or "").rstrip("/")
    target = (payload.target_url or "").strip() or (base + "/pages/auth/login")
    try:
        return request.app.state.login_manager.start_interactive(
            target, debug=bool(payload.debug))
    except LoginBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/login-session")
def get_login_session(request: Request) -> dict:
    return request.app.state.login_manager.status()


@app.delete("/api/login-session")
def delete_login_session(request: Request) -> dict:
    """取消并关闭浏览器；幂等（没有会话也 200）。"""
    manager = request.app.state.login_manager
    try:
        status = manager.cancel()
    except Exception as exc:
        _engine(request).log(f"关闭登录会话失败：{exc}", "WARN")
        status = manager.status()
    return {"ok": True, **status}


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
