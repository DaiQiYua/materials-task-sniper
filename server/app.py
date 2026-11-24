# -*- coding: utf-8 -*-
"""FastAPI 应用：REST + SSE + 静态托管 + 同源校验（严格按 api-contract.md）。"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from . import persist
from .core import PLATFORM_CACHE_STALE_SECONDS, SniperEngine, jwt_exp
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
    # ---- 性能 / 策略 ----
    # 上限 20 与 core.EngineConfig._validate_field 保持一致：此前这里写 le=10，
    # 而引擎允许 1~20，导致 PUT claim_workers=15 被 API 层 422（文档却写 1~20）。
    claim_workers: Optional[int] = Field(default=None, ge=1, le=20)
    poll_rate_max: Optional[int] = Field(default=None, ge=1, le=10000)
    poll_rate_window: Optional[float] = Field(default=None, ge=0.1, le=3600)
    seen_ttl: Optional[float] = Field(default=None, ge=0, le=3600)
    bottom_first: Optional[bool] = None
    early_fire: Optional[bool] = None
    # ---- 多账号并发 / 最新任务 ----
    # 这四项引擎侧一直支持（EngineConfig 有字段、_validate_field 有校验），
    # 但 ConfigPatch 因 extra="forbid" 没声明它们，于是 README 里承诺可用的
    # PUT /api/config 实际返回 422。这里补齐，边界与引擎保持一致。
    newest_first: Optional[bool] = None
    tail_pages: Optional[int] = Field(default=None, ge=1, le=10)
    total_refresh_seconds: Optional[float] = Field(default=None, ge=1.0, le=3600)
    per_account_inflight: Optional[int] = Field(default=None, ge=1, le=5)
    # ---- 轮询成本 ----
    single_poller: Optional[bool] = None
    poll_rotate_seconds: Optional[float] = Field(default=None, ge=0, le=3600)
    # ---- 调试 / 抓包 ----
    #: 上游请求代理（如 http://127.0.0.1:9090）；null/空串 = 直连。
    #: 具体规范化与校验由引擎侧 EngineConfig._validate_field 统一做，这里不加 validator。
    proxy: Optional[str] = None

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


class DataClearIn(BaseModel):
    """POST /api/data/clear：``confirm`` 必须为「清空」，错词 → 400 且不删任何文件。"""

    model_config = ConfigDict(extra="ignore")

    confirm: str = ""


# ============================ 应用 ============================
def _engine(request: Request) -> SniperEngine:
    return request.app.state.engine


def _bus(request: Request) -> Broadcaster:
    return request.app.state.bus


def _sse_frame(payload: Any) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _stale_platform_cache(fetched_at: Any, now: Optional[float] = None) -> bool:
    """缓存是否过期：无 ``fetched_at`` 或早于 6 小时前 → True。"""
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return True
    base = time.time() if now is None else float(now)
    return (base - float(fetched_at)) > PLATFORM_CACHE_STALE_SECONDS


def _pick_migration_cred(records: list) -> Optional[dict]:
    """推断旧全局 Token 属于哪个账号：``last_ok_at`` 最大者；全空取 id 最小。"""
    candidates = [r for r in records if isinstance(r, dict) and r.get("last_ok_at")]
    if candidates:
        try:
            return max(candidates,
                       key=lambda r: (float(r.get("last_ok_at") or 0),
                                      -int(r.get("id") or 0)))
        except Exception:
            return candidates[0]
    if not records:
        return None
    try:
        return min(records, key=lambda r: int(r.get("id") or 0))
    except Exception:
        return records[0]


def _migrate_legacy_token(engine: SniperEngine, store: CredentialStore,
                          directory: Path) -> None:
    """把旧的**全局** token（``token.json``）一次性迁移到某一个账号名下。

    条件全部满足才动作：
      ① ``tokens.json`` 为空（不存在或没有任何条目）；
      ② ``token.json`` 里有 token 且未过期；
      ③ ``credentials.json`` 里至少有一条凭据。
    幂等：``tokens.json`` 已有内容时直接返回，绝不覆盖、不重复迁移。
    迁移后立即用该 token 拉一次平台子账号写缓存（失败只记错误，不影响启动）。
    """
    if persist.load_tokens(directory):
        return                                    # 幂等：已迁移过
    try:
        token = persist.load_token(directory)
    except Exception:
        token = None
    if not token:
        return
    exp = jwt_exp(token)
    if exp is not None and exp <= time.time():
        engine.log("token.json 里的旧 Token 已过期，跳过一次性迁移", "WARN")
        return
    records = [r for r in store.list_all() if isinstance(r, dict)]
    if not records:
        return
    target = _pick_migration_cred(records)
    if target is None:
        return
    try:
        cid = int(target.get("id"))
    except Exception:
        return
    store.set_token(cid, token, exp=exp, adopted=True)
    engine.log(f"旧全局 Token 已迁移到账号 #{cid}（按最近成功登录时间推断）", "INFO")
    # 首屏体验：立刻用该 token 拉一次平台子账号并写缓存
    try:
        result = engine.platform_accounts_for_cred(cid, refresh=True)
    except Exception as exc:
        engine.log(f"迁移后拉取平台子账号失败：{exc}", "WARN")
        return
    error = (result or {}).get("error") if isinstance(result, dict) else None
    if error:
        engine.log(f"迁移后拉取平台子账号失败：{error}（启动不受影响）", "WARN")
    else:
        count = len((result or {}).get("items") or [])
        engine.log(f"已为账号 #{cid} 拉取平台子账号：{count} 个", "INFO")


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
    engine.attach_credentials(store)      # 引擎按 cred 取 token / 写平台缓存
    if not encryption.get("available"):
        engine.log(f"凭据加密不可用：{encryption.get('detail')}", "WARN")

    # ---- 登录管理（交互式会话 + 凭据直登 + 自动续登）----
    login_manager = LoginManager(engine, bus=bus, store=store)
    app.state.login_manager = login_manager
    # 引擎的 401 自动重登钩子：只更新该账号自己的 token，不切活跃 token
    engine.attach_relogin(login_manager.relogin_cred)
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

    # ---- 若没有可用的轮询 Token，自动采用某个账号的有效 Token（不再需要「设为当前」）----
    tok_status = engine.token_status()
    if not tok_status.get("present") or not tok_status.get("valid"):
        try:
            stored_tokens = persist.load_tokens(directory) or {}
        except Exception:
            stored_tokens = {}
        picked = None
        for cid, rec in stored_tokens.items():
            if not isinstance(rec, dict):
                continue
            token = rec.get("token")
            if not isinstance(token, str) or not token.strip():
                continue
            exp = rec.get("exp")
            try:
                exp_num = int(exp) if exp is not None else 0
            except Exception:
                exp_num = 0
            if exp_num and exp_num <= int(time.time()):
                continue                              # 过期 Token 不采用
            try:
                saved_at = float(rec.get("saved_at") or 0)
            except Exception:
                saved_at = 0.0
            score = (exp_num, saved_at)
            if picked is None or score > picked[0]:
                picked = (score, token, cid)
        if picked is not None:
            try:
                engine.set_token(picked[1])
                engine.log(f"未发现轮询 Token，已自动采用账号 #{picked[2]} 的 Token", "INFO")
            except ValueError:
                engine.log("tokens.json 中的 Token 无法解析，跳过自动采用", "WARN")

    # ---- 恢复 history.jsonl（最后 200 行）----
    try:
        engine.load_history(persist.load_history(directory, 200))
    except Exception:
        pass

    # ---- 平台子账号缓存 → 归属索引（account_id -> cred_id）----
    try:
        engine.refresh_platform_index()
    except Exception as exc:
        engine.log(f"平台子账号归属索引重建失败：{exc}", "WARN")

    # ---- 一次性旧全局 Token 迁移（token.json → tokens.json）----
    try:
        _migrate_legacy_token(engine, store, directory)
    except Exception as exc:
        engine.log(f"旧 Token 迁移失败（可忽略）：{exc}", "WARN")

    # ---- 启动后低优先补一次领取记录：恢复任务冷却 / 待提交（best effort）----
    def _startup_sync_claims() -> None:
        time.sleep(1.5)
        for record in store.list_all():
            try:
                cid = int(record.get("id"))
                if store.get_token(cid):
                    engine.sync_claims_for_cred(cid)
            except Exception:
                pass
            time.sleep(0.3)

    try:
        threading.Thread(target=_startup_sync_claims, daemon=True,
                         name="claims-sync").start()
    except Exception as exc:
        engine.log(f"启动领取记录同步失败（可忽略）：{exc}", "WARN")

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


# ------------------------- 一键清空（恢复出厂） -------------------------
@app.post("/api/data/clear")
def post_clear_data(payload: DataClearIn, request: Request) -> dict:
    """清空所有数据：账号 / Token / 平台缓存 / 历史 / 日志 / 配置（恢复默认）。

    ``confirm`` 必须是「清空」二字；否则 400 且**不删除任何文件**。
    """
    if (payload.confirm or "").strip() != "清空":
        raise HTTPException(status_code=400, detail="请输入「清空」以确认清空操作")
    engine = _engine(request)
    # 先关掉可能开着的 Playwright 登录浏览器；失败只记日志，绝不 500
    manager = getattr(request.app.state, "login_manager", None)
    if manager is not None:
        try:
            manager.cancel()
        except Exception as exc:
            try:
                engine.log(f"清空前关闭登录会话失败（已忽略）：{exc}", "WARN")
            except Exception:
                pass
    cleared = engine.clear_all_data()
    # 让其它标签页立即同步为空（前端已支持 snapshot 事件）
    try:
        _bus(request).publish({
            "type": "snapshot",
            "status": engine.status(),
            "tasks": engine.tasks(),
            "history": engine.history(),
            "logs": engine.logs(),
        })
    except Exception:
        pass
    return {"ok": True, "cleared": cleared}


# ---------------------------- 任务 / 历史 ----------------------------
@app.get("/api/tasks")
def get_tasks(request: Request) -> dict:
    return _engine(request).tasks()


@app.post("/api/tasks/refresh")
async def post_tasks_refresh(request: Request) -> dict:
    """手动拉一次任务列表（供「任务速览」刷新按钮）。

    对每个有 Token 的账号 × 每种任务类型各跑一轮，引擎停止时也能拉。
    不抛 5xx：失败原因收集进 ``errors`` 由前端展示；只有引擎内部异常才 500。
    """
    engine = _engine(request)
    result = await asyncio.to_thread(engine.refresh_tasks_once)
    if not isinstance(result, dict):
        result = {}
    tasks = engine.tasks()
    items = tasks.get("items") or []
    return {
        "ok": bool(result.get("ok")),
        "accounts": int(result.get("accounts") or 0),
        "types": list(result.get("types") or []),
        "tasks": tasks,
        "count": len(items),
        "fetched_pages": int(result.get("fetched_pages") or 0),
        "errors": list(result.get("errors") or []),
        "fetched_at": time.time(),
        "elapsed": float(result.get("elapsed") or 0.0),
    }


@app.get("/api/history")
def get_history(request: Request) -> dict:
    return {"items": _engine(request).history()}


@app.get("/api/platform-accounts")
def get_platform_accounts(request: Request, platform: Optional[str] = None) -> dict:
    """已发现的平台子账号（用于抢任务的 小红书/抖音/快手/微信视频号 等）。

    无 ``platform`` 返回全部；带值时返回该平台子集；若上游未标注平台归属，
    则回退返回全部并置 ``fallback=True``（前端据此提示）。任何失败均无日志噪音。
    本接口**保持旧语义**（引擎活跃 token + 原返回结构）。
    """
    return _engine(request).platform_accounts(platform)


@app.get("/api/platform-accounts/by-login")
def get_platform_accounts_by_login(request: Request) -> dict:
    """按登录账号分组的平台子账号（**只用缓存**，不触发任何上游请求）。

    响应：``{"items": {"<cred_id>": {...}}, "missing_token": [int...],
    "default_platform_account_id": int|None, "default_owner_cred_id": int|None}``。
    每个凭据都出现在 ``items`` 里（没缓存的给空 items + ``stale=true``）。
    """
    engine = _engine(request)
    store = request.app.state.credentials
    directory = request.app.state.data_dir
    try:
        cache = persist.load_platform_cache(directory)
    except Exception:
        cache = {}
    now = time.time()
    items: dict = {}
    missing_token: list = []
    for record in store.list_all():
        cid = int(record["id"])
        info = store.get_token_info(cid) or {}
        if not info.get("token"):
            missing_token.append(cid)
        entry = cache.get(cid) or {}
        fetched_at = entry.get("fetched_at")
        accounts = engine._merge_cooldowns(entry.get("items") or [], cid)
        items[str(cid)] = {
            "fetched_at": fetched_at,
            "stale": _stale_platform_cache(fetched_at, now),
            "fallback": bool(entry.get("fallback")),
            "count": len(accounts),
            "items": accounts,
            "adopted": bool(info.get("adopted")),
        }

    default_id = engine.snapshot().get("platform_account_id")
    default_owner = engine.owner_of_platform_account(default_id) if default_id else None
    return {
        "items": items,
        "missing_token": missing_token,
        "default_platform_account_id": default_id,
        "default_owner_cred_id": default_owner,
    }


@app.get("/api/cooldowns")
def get_cooldowns(request: Request) -> dict:
    """冷却 / 待提交 / 可领状态明细（核对用）。

    返回 ``{"now", "request", "tasks", "pending", "eligibility", "claim_limits"}``：
    - ``request``：登录账号级领取请求冷却（10s，来自 claim-limits / 领取响应）
    - ``tasks``  ：任务级冷却（同任务 ``claimIntervalDays``，通常 3 天）
    - ``pending``：领取后未提交（真实阻塞继续领取的原因）
    - ``eligibility``：从任务快照推导的「子账号 × 任务类型」可领/冷却/待提交
    - ``claim_limits``：当前上游 claim-limits 快照
    """
    return _engine(request).cooldown_state()


@app.post("/api/platform-accounts/refresh-all")
async def post_refresh_all_platform_accounts(request: Request) -> dict:
    """对**所有有 token 的账号串行**刷新平台子账号（每个之间 sleep 0.3s）。"""
    engine = _engine(request)
    store = request.app.state.credentials
    started = time.monotonic()
    results: dict = {}
    for record in store.list_all():
        cid = int(record["id"])
        if not store.get_token(cid):
            continue                     # 没 token 的账号跳过（missing_token 里可见）
        result = await asyncio.to_thread(engine.platform_accounts_for_cred, cid, True)
        if not isinstance(result, dict):
            result = {}
        accounts = result.get("items") or []
        results[str(cid)] = {
            "ok": not result.get("error"),
            "error": result.get("error"),
            "count": len(accounts),
        }
        await asyncio.sleep(0.3)         # 串行 + 间隔，避免连环打上游
    return {"items": results,
            "took_ms": int((time.monotonic() - started) * 1000)}


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
    try:
        _engine(request).refresh_pollers()
    except Exception:
        pass
    return item


@app.delete("/api/credentials/{cred_id}")
def delete_credential(cred_id: int, request: Request) -> dict:
    store = request.app.state.credentials
    engine = _engine(request)
    record = store.get(cred_id)
    deleted = store.delete(cred_id)      # 级联清理 tokens.json / 平台缓存
    if deleted:
        # 归属索引里可能还留着已删账号的子账号 → 立刻重建
        try:
            engine.refresh_platform_index()
        except Exception:
            pass
        try:
            engine.refresh_pollers()
        except Exception:
            pass
    if deleted and record is not None:
        engine.log(f"已删除登录凭据：{record.get('name') or '未命名'}"
                   f"（{mask_phone(record.get('phone'))}）", "INFO")
    return {"ok": True, "id": cred_id, "deleted": bool(deleted)}


@app.post("/api/credentials/{cred_id}/fetch-token")
def post_fetch_token(cred_id: int, request: Request, activate: bool = True) -> dict:
    """用单条凭据登录取 token。失败仍 200（由 ok 区分），上游 5xx → 502。

    ``activate``（默认 ``true``，**旧行为不变**）：是否把拿到的 token 设为引擎
    活跃 Token 并写 ``token.json``；``false`` 时只更新该账号自己的
    ``tokens.json``，响应新增 ``platform_accounts`` / ``platform_error``。
    """
    manager = request.app.state.login_manager
    if request.app.state.credentials.get(cred_id) is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    result = manager.fetch_one(cred_id, activate=activate)
    upstream_error = bool(result.pop("_upstream_error", False))
    if not result.get("ok") and (upstream_error or is_upstream_error(result.get("error"))):
        raise HTTPException(status_code=502, detail=result.get("error") or "上游服务错误")
    result.setdefault("platform_accounts", [])
    result.setdefault("platform_error", None)
    try:
        _engine(request).refresh_pollers()
    except Exception:
        pass
    return result


@app.post("/api/credentials/{cred_id}/open-site")
def post_open_site(cred_id: int, request: Request, headless: bool = False) -> dict:
    """用该账号的 Token 打开上游站点并自动登录（新开浏览器窗口）。

    Token 在页面脚本执行前注入 ``localStorage``，站点后续请求自带 Bearer，
    因此打开即是登录态。``headless=true`` 时无窗口（仅调试用）。
    """
    manager = request.app.state.login_manager
    if request.app.state.credentials.get(cred_id) is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    try:
        result = manager.open_site(cred_id, headless=headless)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    upstream_error = bool(result.pop("_upstream_error", False))
    if not result.get("ok"):
        error = result.get("error") or ""
        if upstream_error or is_upstream_error(error):
            raise HTTPException(status_code=502, detail=error or "上游服务错误")
        raise HTTPException(status_code=503, detail=error or "无法打开站点")
    return result


@app.post("/api/credentials/{cred_id}/refresh-platform-accounts")
def post_refresh_platform_accounts(cred_id: int, request: Request) -> dict:
    """刷新单个账号名下的全部平台子账号（一次上游调用，结果写缓存）。"""
    engine = _engine(request)
    if request.app.state.credentials.get(cred_id) is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    result = engine.platform_accounts_for_cred(cred_id, refresh=True)
    if not isinstance(result, dict):
        result = {}
    accounts = result.get("items")
    if not isinstance(accounts, list):
        accounts = []
    return {
        "ok": not result.get("error"),
        "items": accounts,
        "fallback": bool(result.get("fallback")),
        "fetched_at": result.get("fetched_at"),
        "error": result.get("error"),
        "count": len(accounts),
    }


@app.get("/api/claims")
def get_claims(request: Request, refresh: bool = False) -> dict:
    """进行中/历史任务（领取记录）。``refresh=true`` 时先强制拉一次上游。

    refresh 会对所有**有 token** 的凭据逐个拉取（串行打上游，可能几秒），
    单个失败不抛，只收集进 ``errors``；返回值始终是引擎聚合快照 + ``ok``。
    """
    engine = _engine(request)
    errors: dict = {}
    if refresh:
        store = engine._get_store()
        for record in store.list_all():
            try:
                cid = int(record.get("id"))
            except Exception:
                continue
            if not store.get_token(cid):
                continue
            try:
                result = engine.fetch_claims(cid)
            except Exception as exc:
                errors[str(cid)] = f"拉取异常：{exc}"
                engine.log(f"账号 {cid} 领取记录刷新失败：{exc}", "WARN")
                continue
            if not result.get("ok"):
                errors[str(cid)] = str(result.get("error") or "unknown")
    snapshot = engine.claims_snapshot()
    if errors:
        merged = dict(snapshot.get("errors") or {})
        merged.update(errors)
        snapshot["errors"] = merged
    return {"ok": True, **snapshot}


@app.post("/api/credentials/{cred_id}/sync-claims")
def post_sync_claims(cred_id: int, request: Request) -> dict:
    """用该账号 Token 拉一次领取记录，补「任务冷却 / 待提交」。

    没有任务下发、或账号在其他工具领取过时使用；低频调用，可手动触发。
    """
    if request.app.state.credentials.get(cred_id) is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    result = _engine(request).sync_claims_for_cred(cred_id)
    result["claims"] = _engine(request).claims_snapshot()["by_cred"].get(str(cred_id))
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
