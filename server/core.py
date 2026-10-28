# -*- coding: utf-8 -*-
"""SniperEngine —— 把 ``task_sniper.py`` 的模块级逻辑实例化重写。

设计要点（对应 API 契约硬性约束）：
- 所有状态（seen/in_flight/限速器/队列/统计/快照/历史/日志）都是**实例属性**，
  没有模块级可变全局，可同时存在多个引擎（测试里每个进程一个）。
- 配置是**不可变 dataclass** ``EngineConfig``；``update_config`` 在锁内原子替换
  整个配置对象，线程每轮循环开头重新读取 ``self._cfg``，因此全部字段热更。
- 线程循环体整体 try/except：异常 → ERR 日志事件 + ``state="error"`` + 退避 1s
  续跑，线程不死。
- token 永不写入日志或事件（只出现在发往上游的 Authorization 头里）。
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import persist

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
      "Gecko/20100101 Firefox/158.0")

MAX_HISTORY = 200          # 历史环形缓冲
MAX_LOGS = 500             # 日志环形缓冲
LIMITS_TTL = 3.0           # claim-limits 缓存秒数
RESULT_POLL_INTERVAL = 0.4


# ------------------------------ JWT ---------------------------------
def jwt_exp(token: str) -> Optional[int]:
    """解出 JWT payload 里的 exp（秒级时间戳）；解析失败返回 None。"""
    if not isinstance(token, str) or token.count(".") < 1:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return int(exp)


def exp_to_iso(exp: Optional[int]) -> Optional[str]:
    if not exp:
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


# --------------------------- 令牌桶限速 ------------------------------
class RateLimiter:
    """经典令牌桶。与 task_sniper.RateLimiter 行为一致，但属于实例。"""

    def __init__(self, max_count: float, window: float) -> None:
        self.cap = float(max_count)
        self.rate = self.cap / float(window) if window else float(max_count)
        self.tokens = self.cap
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n: int = 1) -> None:
        """阻塞直到拿到 n 个令牌。"""
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + self.rate * (now - self.last))
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate if self.rate else 0.05
            time.sleep(max(wait, 0.01))

    def try_acquire(self, n: int = 1) -> bool:
        """非阻塞：有令牌立即拿，没有返回 False。"""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.cap, self.tokens + self.rate * (now - self.last))
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


# ------------------------------ Config ------------------------------
@dataclass(frozen=True)
class EngineConfig:
    """不可变配置快照（契约 Config 的字段）。"""

    base: str = "https://materials.xlya.cn"
    poll_interval: float = 1.5
    task_types: tuple = ("standard", "long_term")
    platform_account_id: Optional[int] = None
    rate_limit_max: int = 6
    rate_limit_window: float = 10.0
    max_pages: int = 1
    page_size: int = 10
    http_timeout: float = 8
    result_polls: int = 0

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["task_types"] = list(self.task_types)
        return out

    # ---- 校验 ----
    @staticmethod
    def validate_patch(patch: dict) -> dict:
        """校验并规范化任意子集；非法抛 ValueError（API 层转 422）。"""
        if not isinstance(patch, dict):
            raise ValueError("配置必须是对象")
        allowed = {f.name for f in fields(EngineConfig)}
        clean: dict = {}
        for key, raw in patch.items():
            if key not in allowed:
                raise ValueError(f"未知配置项：{key}")
            clean[key] = _validate_field(key, raw)
        return clean

    def merge(self, clean_patch: dict) -> "EngineConfig":
        return replace(self, **clean_patch)


def _number(key: str, raw: Any, *, minimum=None, maximum=None, integer: bool = False):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{key} 必须是数字")
    if integer:
        if float(raw) != int(raw):
            raise ValueError(f"{key} 必须是整数")
        value = int(raw)
    else:
        value = float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} 不能大于 {maximum}")
    return value


def _validate_field(key: str, raw: Any) -> Any:
    if key == "base":
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("base 必须是非空字符串")
        base = raw.strip().rstrip("/")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise ValueError("base 必须以 http:// 或 https:// 开头")
        return base
    if key == "poll_interval":
        return _number(key, raw, minimum=0.2, maximum=3600.0)
    if key == "task_types":
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError("task_types 必须是非空数组")
        out = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("task_types 元素必须是非空字符串")
            if item.strip() not in out:
                out.append(item.strip())
        return tuple(out)
    if key == "platform_account_id":
        if raw is None:
            return None
        return _number(key, raw, minimum=1, integer=True)
    if key == "rate_limit_max":
        return _number(key, raw, minimum=1, maximum=10000, integer=True)
    if key == "rate_limit_window":
        return _number(key, raw, minimum=0.1, maximum=3600.0)
    if key == "max_pages":
        return _number(key, raw, minimum=1, maximum=100, integer=True)
    if key == "page_size":
        return _number(key, raw, minimum=1, maximum=200, integer=True)
    if key == "http_timeout":
        return _number(key, raw, minimum=1, maximum=120, integer=True)
    if key == "result_polls":
        return _number(key, raw, minimum=0, maximum=20, integer=True)
    raise ValueError(f"未知配置项：{key}")


DEFAULT_CONFIG = EngineConfig()


# ------------------------------ Engine ------------------------------
class SniperEngine:
    """实例化狙击引擎。

    ``on_event(dict)`` 由引擎内部**串行**调用（单把发射锁），事件类型：
    - ``{"type":"log","ts":"HH:MM:SS.mmm","level":...,"msg":...}``
    - ``{"type":"status","status":<Status>}``
    - ``{"type":"tasks","ts":<float>,"items":[<Task>]}``
    """

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event
        self._emit_lock = threading.RLock()   # on_event 串行化
        self._lock = threading.RLock()        # 状态/配置/统计
        self._lim_lock = threading.Lock()     # claim-limits 缓存

        self._data_dir = persist.data_dir()
        self._logger = persist.get_logger(self._data_dir)

        self._cfg: EngineConfig = DEFAULT_CONFIG
        self._limiter = RateLimiter(self._cfg.rate_limit_max, self._cfg.rate_limit_window)
        self._limiter_key = (float(self._cfg.rate_limit_max), float(self._cfg.rate_limit_window))

        self._token: Optional[str] = None
        self._token_exp: Optional[int] = None

        # 去重 / 在途
        self._seen: set = set()
        self._in_flight: set = set()

        self._claim_queue: "queue.Queue[tuple]" = queue.Queue()

        self._stop = threading.Event()
        self._running = False
        self._state = "idle"
        self._state_reason = ""
        self._started_at: Optional[float] = None
        self._last_poll_at: Optional[float] = None

        self._stats = {"polls": 0, "skipped": 0, "claims_ok": 0,
                       "claims_fail": 0, "claims_conflict": 0}

        self._tasks_map: dict = {}
        self._tasks_ts: Optional[float] = None

        self._history: deque = deque(maxlen=MAX_HISTORY)
        self._logs: deque = deque(maxlen=MAX_LOGS)

        self._lim_data: dict = {}
        self._lim_ts: float = 0.0

        self._pollers: dict = {}          # task_type -> {"thread":Thread,"stop":Event}
        self._claimer: Optional[threading.Thread] = None

    # ============================ 事件 ============================
    def _emit(self, event: dict) -> None:
        with self._emit_lock:
            try:
                self._on_event(event)
            except Exception:
                pass  # 广播方出问题不能带崩引擎线程

    def _file_log(self, msg: str, level: str) -> None:
        if self._logger is None:
            return
        lvl = {"OK": logging.INFO, "CLAIM": logging.INFO, "INFO": logging.INFO,
               "WARN": logging.WARNING, "ERR": logging.ERROR}.get(level, logging.INFO)
        try:
            self._logger.log(lvl, msg)
        except Exception:
            pass

    def _log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        event = {"type": "log", "ts": ts, "level": level, "msg": msg}
        with self._lock:
            self._logs.append(event)
        self._file_log(msg, level)
        self._emit(event)

    def _emit_status(self) -> None:
        self._emit({"type": "status", "status": self.status()})

    def _emit_tasks(self, ts: float, items: list) -> None:
        self._emit({"type": "tasks", "ts": ts, "items": items})

    def log(self, msg: str, level: str = "INFO") -> None:
        """对外暴露的日志入口（写环形缓冲 + sniper.log + log 事件）。"""
        self._log(msg, level)

    # ============================ 配置 ============================
    def snapshot(self) -> dict:
        """当前配置全量快照（dict）。"""
        return self._cfg.to_dict()

    def update_config(self, partial: Optional[dict]) -> dict:
        """热更配置：校验→锁内原子替换；非法抛 ValueError。"""
        patch = EngineConfig.validate_patch(partial or {})
        with self._lock:
            cfg = self._cfg.merge(patch)
            self._cfg = cfg
            key = (float(cfg.rate_limit_max), float(cfg.rate_limit_window))
            if key != self._limiter_key:
                self._limiter = RateLimiter(cfg.rate_limit_max, cfg.rate_limit_window)
                self._limiter_key = key
        try:
            persist.save_config(cfg.to_dict(), self._data_dir)
        except Exception:
            pass
        if "task_types" in patch:
            self._sync_pollers(cfg)
        self._emit_status()
        return cfg.to_dict()

    # ============================ token ===========================
    def set_token(self, token: Optional[str]) -> dict:
        """校验并保存 token；JWT 无法解析时抛 ValueError。"""
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ValueError("token 不能为空")
        exp = jwt_exp(token)
        if exp is None:
            raise ValueError("无法解析 JWT（缺少 payload.exp）")
        with self._lock:
            self._token = token
            self._token_exp = exp
        try:
            persist.save_token(token, self._data_dir)
        except Exception:
            pass
        valid = exp > time.time()
        exp_iso = exp_to_iso(exp)
        if valid:
            left = max(0, exp - int(time.time()))
            self._log(f"已更新 token，剩余有效期约 {left // 3600}h{left % 3600 // 60}m", "OK")
        else:
            self._log("已更新 token，但该 token 已过期", "WARN")
        self._emit_status()
        return {"valid": valid, "exp": exp_iso}

    def has_token(self) -> bool:
        with self._lock:
            return bool(self._token)

    def token_status(self) -> dict:
        with self._lock:
            token, exp = self._token, self._token_exp
        if not token:
            return {"present": False, "exp": None, "valid": False}
        valid = exp is None or exp > time.time()
        return {"present": True, "exp": exp_to_iso(exp), "valid": valid}

    # ============================ 生命周期 ============================
    def start(self) -> None:
        """启动线程；无 token 抛 ValueError。paused 后再次 start 即恢复。"""
        with self._lock:
            if not self._token:
                raise ValueError("未配置 token，无法启动")
            was_active = self._running
            self._stop.clear()
            self._running = True
            self._state = "running"
            self._state_reason = ""
            if self._started_at is None:
                self._started_at = time.time()
            cfg = self._cfg
        # 防御：从停止态启动前排空遗留队列（上一轮 stop 后可能有竞态残留）；
        # 运行中重复 start 不排空，避免误杀调度中的合法任务
        if not was_active:
            while True:
                try:
                    self._claim_queue.get_nowait()
                    self._claim_queue.task_done()
                except queue.Empty:
                    break
        self._sync_pollers(cfg)
        self._ensure_claimer()
        self._log(f"开始狙击 | 监控类型={list(cfg.task_types)} | 账号="
                  f"{cfg.platform_account_id or '自动'} | 限速="
                  f"{cfg.rate_limit_max}/{cfg.rate_limit_window}s", "INFO")
        self._emit_status()

    def stop(self) -> None:
        """置停标志 + join(timeout=3) + 队列收尾。"""
        with self._lock:
            was_active = self._running or bool(self._pollers) or self._claimer is not None
            self._running = False
            self._state = "idle"
            self._state_reason = ""
            pollers = list(self._pollers.values())
            self._pollers = {}
            claimer = self._claimer
            self._claimer = None
        self._stop.set()
        threads = [p["thread"] for p in pollers]
        for p in pollers:
            p["stop"].set()
        if claimer is not None:
            threads.append(claimer)
        for t in threads:
            try:
                t.join(timeout=3)
            except Exception:
                pass
        drained = 0
        while True:  # 队列收尾
            try:
                self._claim_queue.get_nowait()
            except queue.Empty:
                break
            else:
                drained += 1
                try:
                    self._claim_queue.task_done()
                except Exception:
                    pass
        with self._lock:
            self._in_flight.clear()
        if not was_active:
            return
        if drained:
            self._log(f"已停止（丢弃 {drained} 个待领取任务）", "WARN")
        else:
            self._log("已停止", "INFO")
        self._emit_status()

    # ============================ 线程管理 ============================
    def _sync_pollers(self, cfg: EngineConfig) -> None:
        with self._lock:
            if not self._running:
                return  # 未运行时不创建线程（start() 会负责）
        wanted = list(cfg.task_types)
        to_start = []
        with self._lock:
            for task_type in list(self._pollers):
                if task_type not in wanted:
                    info = self._pollers.pop(task_type)
                    info["stop"].set()
            for task_type in wanted:
                info = self._pollers.get(task_type)
                if info is not None and info["thread"].is_alive():
                    continue
                stop_ev = threading.Event()
                thread = threading.Thread(
                    target=self._poller_loop, args=(task_type, stop_ev),
                    name=f"sniper-poller-{task_type}", daemon=True,
                )
                self._pollers[task_type] = {"thread": thread, "stop": stop_ev}
                to_start.append(thread)
        for thread in to_start:
            thread.start()

    def _ensure_claimer(self) -> None:
        with self._lock:
            if self._claimer is not None and self._claimer.is_alive():
                return
            thread = threading.Thread(target=self._claimer_loop,
                                      name="sniper-claimer", daemon=True)
            self._claimer = thread
        thread.start()

    def threads_alive(self) -> bool:
        with self._lock:
            alive = [p["thread"].is_alive() for p in self._pollers.values()]
            if self._claimer is not None:
                alive.append(self._claimer.is_alive())
        return any(alive)

    # ============================ 状态查询 ============================
    def status(self) -> dict:
        cfg = self._cfg
        with self._lock:
            running = bool(self._running)
            state = self._state
            reason = self._state_reason
            started_at = self._started_at
            last_poll_at = self._last_poll_at
            stats = dict(self._stats)
        return {
            "running": running,
            "state": state,
            "state_reason": reason,
            "token": self.token_status(),
            "started_at": started_at,
            "last_poll_at": last_poll_at,
            "upstream_base": cfg.base,
            "stats": stats,
        }

    def tasks(self) -> dict:
        with self._lock:
            items = list(self._tasks_map.values())
            ts = self._tasks_ts
        return {"ts": ts, "items": items}

    def history(self) -> list:
        with self._lock:
            return list(self._history)

    def logs(self) -> list:
        with self._lock:
            return list(self._logs)

    def load_history(self, items: list) -> None:
        with self._lock:
            self._history = deque(maxlen=MAX_HISTORY)
            for item in items or []:
                if isinstance(item, dict):
                    self._history.append(item)

    def config(self) -> dict:
        return self.snapshot()

    # ============================ 内部工具 ============================
    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def _is_active(self) -> bool:
        with self._lock:
            return self._running and self._state in ("running", "error")

    def _sleep(self, seconds: float, extra_stop: Optional[threading.Event] = None) -> None:
        """可被打断的睡眠（10ms 粒度检查停标志）。"""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self._stop.is_set() or (extra_stop is not None and extra_stop.is_set()):
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def _pause(self, reason: str) -> None:
        with self._lock:
            if self._state == "paused" and self._state_reason == reason:
                return
            self._state = "paused"
            self._state_reason = reason
        self._log(f"已暂停：{reason}（重新设置 token 后点启动即可恢复）", "WARN")
        self._emit_status()

    def _clear_error(self) -> None:
        with self._lock:
            if self._state == "error":
                self._state = "running" if self._running else "idle"
                self._state_reason = ""

    def _loop_error(self, where: str, exc: BaseException) -> None:
        with self._lock:
            self._state = "error"
            self._state_reason = f"{where}线程异常：{exc}"
        self._log(f"{where}线程异常：{exc}", "ERR")
        self._emit_status()

    # ============================ 网络层 ============================
    def _api_call(self, method: str, path: str, body: Any = None,
                  cfg: Optional[EngineConfig] = None,
                  blocking: bool = True) -> tuple:
        """发起一次上游调用（已纳入全局限速）。返回 (status, dict_or_None)。

        ``blocking=False``：拿不到令牌立即返回 ``(0, {"_skipped": True})``，
        不发请求（列表轮询宁可跳过本轮，也不占用领取请求的额度）。
        """
        cfg = cfg or self._cfg
        with self._lock:
            token = self._token
            limiter = self._limiter
        if not token:
            return (0, {"_error": "未配置 token"})
        if not limiter.try_acquire(1):
            if not blocking:
                return (0, {"_skipped": True})
            limiter.acquire(1)

        url = cfg.base.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("authorization", "Bearer " + token)
        req.add_header("content-type", "application/json")
        req.add_header("accept", "*/*")
        req.add_header("user-agent", UA)
        req.add_header("referer", cfg.base.rstrip("/") + "/pages/tasks/index")
        # 故意不带 If-None-Match，避免 304 缓存影响实时性
        try:
            with urllib.request.urlopen(req, timeout=cfg.http_timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            status = exc.code
        except Exception as exc:  # 超时 / 网络错误
            return (0, {"_error": str(exc)})
        try:
            obj = json.loads(raw) if raw else {}
        except Exception:
            obj = {"_raw": raw[:200]}
        return (status, obj)

    def _get_tasks(self, task_type: str, page: int, cfg: EngineConfig) -> tuple:
        """返回 (status, items)。status: -1=本轮无令牌跳过, 0=网络错误。"""
        path = (f"/api/user/tasks?taskType={task_type}&page={page}"
                f"&pageSize={cfg.page_size}&platform=&materialType=&categoryId=&keyword=")
        status, obj = self._api_call("GET", path, cfg=cfg, blocking=False)
        if isinstance(obj, dict) and obj.get("_skipped"):
            return (-1, [])
        if status == 0:
            err = obj.get("_error", "") if isinstance(obj, dict) else ""
            if err:
                self._log(f"列表[{task_type}] 请求失败：{err}", "WARN")
            return (0, [])
        if status == 401:
            return (401, [])
        if status == 304:
            return (304, [])
        if status != 200:
            msg = obj.get("message", "") if isinstance(obj, dict) else ""
            self._log(f"列表[{task_type}] HTTP {status} {msg}", "WARN")
            return (status, [])
        items = []
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                items = data.get("items") or []
        return (200, [it for it in items if isinstance(it, dict)])

    def _fetch_limits(self, cfg: EngineConfig) -> dict:
        status, obj = self._api_call("GET", "/api/user/tasks/claim-limits",
                                     cfg=cfg, blocking=True)
        if status == 401:
            self._pause("token 无效或已过期")
            return {}
        if status == 200 and isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                return data
        return {}

    def _cached_limits(self, cfg: EngineConfig, force: bool = False) -> dict:
        with self._lim_lock:
            now = time.monotonic()
            if force or (now - self._lim_ts) > LIMITS_TTL:
                self._lim_data = self._fetch_limits(cfg)
                self._lim_ts = now
            return self._lim_data

    # ============================ 平台子账号 ============================
    def platform_accounts(self, platform: Optional[str] = None) -> dict:
        """已发现的平台子账号（用于抢任务的 小红书/抖音/快手 等）。

        汇总来源：① 任务 ``claimEligibility.accounts``（带 id，可能无名称）；
        ② 上游 ``{base}/api/user/platform-accounts``（即 ``/pages/accounts/index``
        实际调用的接口，HAR 已确认字段）。``platform`` 为空返回全部；带值时返回
        该平台子集，若上游未标注任何平台归属，则回退返回全部并置 ``fallback=True``。
        """
        with self._lock:
            tasks = list(self._tasks_map.values())
        discovered: dict = {}
        for t in tasks:
            elig = t.get("claimEligibility") or {}
            accounts = elig.get("accounts") or []
            can = bool(elig.get("canClaim"))
            for a in accounts:
                raw_id = a.get("id") if isinstance(a, dict) else a
                if raw_id is None:
                    continue
                try:
                    aid = int(raw_id)
                except Exception:
                    continue
                rec = discovered.get(aid) or {
                    "id": aid, "name": None, "platform": None,
                    "platformName": None, "total": 0, "claimable": 0,
                }
                rec["total"] += 1
                if can:
                    rec["claimable"] += 1
                discovered[aid] = rec

        # 来自 /pages/accounts/index 的真实接口（失败即忽略）
        for u in self._fetch_upstream_platform_accounts():
            raw_id = u.get("id")
            if raw_id is None:
                continue
            try:
                aid = int(raw_id)
            except Exception:
                continue
            rec = discovered.get(aid) or {
                "id": aid, "name": None, "platform": None,
                "platformName": None, "total": 0, "claimable": 0,
            }
            rec["name"] = rec["name"] or u.get("name")
            if u.get("platform"):
                rec["platform"] = u["platform"]
            if u.get("platformName"):
                rec["platformName"] = u["platformName"]
            # 透传上游字段，供前端展示账号/实名/校验状态
            for k in ("account", "realName", "verifyStatus",
                      "selfDisabled", "activeClaimCount"):
                if u.get(k) is not None:
                    rec.setdefault(k, u.get(k))
            discovered[aid] = rec

        items = list(discovered.values())
        fallback = False
        if platform:
            tagged = [r for r in items
                      if (r.get("platform") or "").lower() == platform.lower()]
            if tagged:
                items = tagged
            else:
                fallback = True  # 上游未标注平台，回退返回全部
        return {"items": items, "fallback": fallback}

    def _fetch_upstream_platform_accounts(self) -> list:
        """最佳努力拉取上游平台账号；任何异常/非 200 都返回空列表。

        字段对照（来自 ``/pages/accounts/index`` 调用的接口，已在 HAR 中确认）：
          id           平台账号 id
          platform     "xiaohongshu" / "douyin" / "kuaishou" 等（小写代码）
          platformName "小红书" / "抖音" / "快手"（中文）
          account      账号标识（如绑定的手机号/账号名）
          realName     实名
          verifyStatus "verified" 等
          selfDisabled bool
          activeClaimCount int
        """
        try:
            status, obj = self._api_call(
                "GET", "/api/user/platform-accounts", cfg=self._cfg, blocking=True)
        except Exception:
            return []
        if status != 200 or not isinstance(obj, dict):
            return []
        data = obj.get("data")
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("items") or data.get("list") or []
        else:
            raw = []
        out = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            pid = (it.get("id") or it.get("platformAccountId")
                   or it.get("accountId"))
            if pid is None:
                continue
            # 显示名：实名优先，其次账号标识
            name = (it.get("realName") or it.get("account")
                    or it.get("nickname") or it.get("name") or "") or None
            out.append({
                "id": pid,
                "name": name,
                "platform": (it.get("platform") or "").lower(),
                "platformName": it.get("platformName") or "",
                "account": it.get("account"),
                "realName": it.get("realName"),
                "verifyStatus": it.get("verifyStatus"),
                "selfDisabled": it.get("selfDisabled"),
                "activeClaimCount": it.get("activeClaimCount"),
            })
        return out

    # ============================ 轮询 ============================
    def _poller_loop(self, task_type: str, stop_ev: threading.Event) -> None:
        self._log(f"启动轮询线程：{task_type}", "INFO")
        while not self._stop.is_set() and not stop_ev.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.25, stop_ev)
                    continue
                cfg = self._cfg                      # 每轮取最新配置引用
                self._poll_once(task_type, cfg)
                self._sleep(cfg.poll_interval, stop_ev)
            except Exception as exc:                 # 线程不死：日志+退避续跑
                self._loop_error(f"轮询[{task_type}]", exc)
                self._sleep(1.0, stop_ev)

    def _poll_once(self, task_type: str, cfg: EngineConfig) -> None:
        for page in range(1, max(1, int(cfg.max_pages)) + 1):
            if not self._is_active():
                return
            status, items = self._get_tasks(task_type, page, cfg)
            if status == -1:                          # 本轮额度不足，跳过
                self._bump("skipped")
                return
            if status == 401:
                self._pause("token 无效或已过期")
                return
            self._bump("polls")
            if status == 200:
                self._ingest(task_type, items)
                self._clear_error()

    def _ingest(self, task_type: str, items: list) -> None:
        """合并任务快照 + 发现可领取任务入队 + 推 tasks 事件。"""
        new_items: list = []
        now = time.time()
        with self._lock:
            for item in items:
                tid = item.get("id")
                if tid is None:
                    continue
                self._tasks_map[tid] = item
                try:
                    quota = float(item.get("remainingQuota") or 0)
                except Exception:
                    quota = 0.0
                eligibility = item.get("claimEligibility") or {}
                can_claim = bool(eligibility.get("canClaim"))
                if (quota > 0 and can_claim
                        and tid not in self._seen and tid not in self._in_flight):
                    self._seen.add(tid)
                    self._in_flight.add(tid)
                    new_items.append(item)
            self._tasks_ts = now
            self._last_poll_at = now
            snapshot = list(self._tasks_map.values())
        for item in new_items:
            self._claim_queue.put((item, task_type))
            self._log(f"发现可领取任务 #{item.get('id')} [{item.get('title', '')}] -> 入队", "INFO")
        self._emit_tasks(now, snapshot)

    # ============================ 领取 ============================
    def _claimer_loop(self) -> None:
        self._log("启动领取线程", "INFO")
        while not self._stop.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.25)
                    continue
                try:
                    item, task_type = self._claim_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    cfg = self._cfg
                    self._claim(item, task_type, cfg)
                    self._clear_error()
                finally:
                    try:
                        self._claim_queue.task_done()
                    except Exception:
                        pass
            except Exception as exc:                 # 线程不死
                self._loop_error("领取", exc)
                self._sleep(1.0)

    def _claim(self, item: dict, task_type: str, cfg: EngineConfig) -> None:
        task_id = item.get("id")
        title = item.get("title", "")
        commission = item.get("baseCommission", "")
        account_id: Optional[int] = None
        try:
            limits = self._cached_limits(cfg)
            if self._state_paused():
                return
            if isinstance(limits, dict) and limits:
                server_now = limits.get("serverNowMs") or int(time.time() * 1000)
                next_allowed = limits.get("nextAllowedAtMs") or 0
                if server_now < next_allowed:
                    wait_s = (next_allowed - server_now) / 1000.0 + 0.1
                    self._log(f"冷却中，{min(wait_s, 5):.1f}s 后再领取 #{task_id}", "WARN")
                    self._sleep(min(wait_s, 5))
                pending = limits.get("pendingCount") or 0
                pending_limit = limits.get("pendingLimit")
                if pending_limit is not None and pending >= pending_limit:
                    self._log(f"待处理已达上限，跳过 #{task_id}", "WARN")
                    return

            if cfg.platform_account_id:
                account_id = int(cfg.platform_account_id)
            else:
                accounts = (item.get("claimEligibility") or {}).get("accounts") or []
                if not accounts:
                    self._log(f"#{task_id} 无可领取账号，放弃", "WARN")
                    return
                first = accounts[0]
                account_id = first.get("id") if isinstance(first, dict) else first
                if account_id is None:
                    self._log(f"#{task_id} 账号信息缺失，放弃", "WARN")
                    return
                account_id = int(account_id)

            self._log(f"立即领取 #{task_id} [{title}] 账号={account_id} 佣金={commission}", "CLAIM")
            status, obj = self._api_call(
                "POST", f"/api/user/tasks/{task_id}/claim",
                {"platformAccountId": account_id}, cfg=cfg, blocking=True,
            )
            obj = obj if isinstance(obj, dict) else {}

            if status == 401:
                self._record(task_id, title, commission, account_id, "fail", status,
                             "token 无效或已过期")
                self._bump("claims_fail")
                self._pause("token 无效或已过期")
                return

            if status in (200, 201) and obj.get("success"):
                record = obj.get("data") or {}
                if not isinstance(record, dict):
                    record = {}
                limits_in_rec = record.get("claimLimits") or {}
                self._bump("claims_ok")
                self._record(task_id, title, commission, account_id, "ok", status, "领取成功")
                self._log(
                    f"领取成功 #{task_id} 记录ID={record.get('id')} 失效={record.get('expiresAt')} "
                    f"剩余冷却={limits_in_rec.get('retryAfterSeconds')}s", "OK")
                if cfg.result_polls and int(cfg.result_polls) > 0:
                    self._poll_result(task_id, account_id, cfg)
                try:
                    import winsound
                    winsound.Beep(1000, 200)
                except Exception:
                    pass
                self._emit_status()
            elif status == 409:
                message = obj.get("message") or "已被领取"
                self._bump("claims_conflict")
                self._record(task_id, title, commission, account_id, "conflict", status, message)
                self._log(f"慢了一步 #{task_id}：{message}", "WARN")
                self._emit_status()
            elif status == 429:
                message = obj.get("message") or "被上游限流"
                self._bump("claims_fail")
                self._record(task_id, title, commission, account_id, "ratelimit", status, message)
                self._log(f"被限流 #{task_id}，退避 2s 后重试", "ERR")
                self._sleep(2.0)
                # 仅在引擎仍活跃时重入队：stop 已排空队列，残留项会在下次
                # start 时被重新领取陈旧任务（oracle gate 发现的竞态）
                if self._is_active():
                    self._claim_queue.put((item, task_type))
                self._emit_status()
            else:
                message = obj.get("message") or ""
                self._bump("claims_fail")
                self._record(task_id, title, commission, account_id, "fail", status,
                             message or "领取失败")
                self._log(f"领取失败 #{task_id} HTTP {status} {message}", "ERR")
                self._emit_status()
        finally:
            with self._lock:
                self._in_flight.discard(task_id)

    def _state_paused(self) -> bool:
        with self._lock:
            return self._state == "paused"

    def _poll_result(self, task_id: Any, account_id: int, cfg: EngineConfig):
        path = (f"/api/user/tasks/{task_id}/claim-result"
                f"?platformAccountId={account_id}")
        result = None
        for _ in range(max(0, int(cfg.result_polls))):
            status, obj = self._api_call("GET", path, cfg=cfg, blocking=True)
            if status == 401:
                self._pause("token 无效或已过期")
                return None
            if status == 200 and isinstance(obj, dict) and obj.get("data") is not None:
                result = obj.get("data")
                break
            self._sleep(RESULT_POLL_INTERVAL)
        return result

    def _record(self, task_id: Any, title: str, commission: Any, account_id: Any,
                kind: str, http: int, message: str) -> None:
        """写一条 History（内存环形 + history.jsonl）。"""
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "task_id": task_id,
            "title": title,
            "commission": commission,
            "account_id": account_id,
            "status": kind,
            "http": http,
            "message": message,
        }
        with self._lock:
            self._history.append(record)
        try:
            persist.append_history(record, self._data_dir)
        except Exception:
            pass
