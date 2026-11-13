# -*- coding: utf-8 -*-
"""SniperEngine —— 低延迟、可重试的抢单引擎。

相对旧实现的关键变化：
1. HTTP keep-alive 连接复用（线程内一条长连接），砍掉每次请求的 TCP/TLS 握手；
2. 列表轮询与领取各自独立令牌桶，领取永远优先，列表不再抢领取额度；
3. 领取线程池（默认 3）+ 带优先级的延迟调度队列：冷却/限流只重排、不阻塞线程；
4. 默认「列表从下往上抢」（``bottom_first``）：多数人只抢列表首位，尾部命中率更高；
5. claim-limits 后台异步刷新（领取路径零阻塞），用 ``serverNowMs`` 校正时钟，
   并在冷却结束前 ``RTT/2`` 提前发射；
6. 日志文件写入异步化，领取关键路径不落盘；
7. 失败分类重试：429 / 网络抖动 / 待处理满 → 延迟重排；409 为终态；
   ``seen`` 带 TTL，短暂失败后过一段时间仍可重新发现；
8. 领取成功按上游 ``retryAfterSeconds`` 设置全局冷却，避免多 worker 把额度打爆。
"""

from __future__ import annotations

import base64
import heapq
import http.client
import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import persist

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
      "Gecko/20100101 Firefox/158.0")

MAX_HISTORY = 200          # 历史环形缓冲
MAX_LOGS = 500             # 日志环形缓冲
LIMITS_TTL = 2.0           # claim-limits 后台刷新间隔（秒）
LIMITS_MAX_AGE = 15.0      # 超过该秒数的配额快照视为不可用
RESULT_POLL_INTERVAL = 0.25
MAX_CLAIM_ATTEMPTS = 4     # 429 最大重试次数
MAX_PENDING_ATTEMPTS = 6   # 待处理满最大重排次数


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
    """经典令牌桶。每个引擎可持有多个实例（领取 / 轮询各一套）。"""

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


# ------------------------- 领取调度（优先级 + 延迟） -------------------
class ClaimScheduler:
    """最小堆调度器：按 (到期时间, 优先级, 入队序号) 出队。

    - ``priority`` 越小越先出队（「列表从下往上抢」用负数 rank 实现）；
    - ``delay`` 用于冷却 / 限流重排：任务在到期前不会占用领取线程；
    - ``pop`` 在没有任何到期任务时最多等 ``timeout`` 秒。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._heap: list = []
        self._seq = 0
        self._closed = False

    def push(self, item: dict, task_type: str, priority: int,
             account_id: Optional[int], delay: float, attempts: int) -> None:
        due = time.monotonic() + max(0.0, float(delay))
        with self._cond:
            self._seq += 1
            heapq.heappush(self._heap, (due, int(priority), self._seq,
                                        item, task_type, account_id, int(attempts)))
            self._cond.notify()

    def pop(self, timeout: float) -> Optional[tuple]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while True:
                now = time.monotonic()
                if self._heap:
                    due = self._heap[0][0]
                    if due <= now:
                        _, _, _, item, task_type, account_id, attempts = heapq.heappop(self._heap)
                        return (item, task_type, account_id, attempts)
                    wait = min(due - now, deadline - now)
                else:
                    wait = deadline - now
                if self._closed or wait <= 0:
                    return None
                self._cond.wait(wait)

    def clear(self) -> int:
        with self._cond:
            dropped = len(self._heap)
            self._heap = []
            return dropped

    def size(self) -> int:
        with self._cond:
            return len(self._heap)


# --------------------------- HTTP 连接复用 ---------------------------
class HttpPool:
    """线程内 keep-alive 连接池（每线程每个目标一条），断线自动重建一次。

    相比每次 ``urllib.urlopen`` 重新 TCP + TLS 握手，公网场景可省 1~2 个 RTT。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _store(self) -> dict:
        store = getattr(self._local, "conns", None)
        if store is None:
            store = {}
            self._local.conns = store
        return store

    def _target(self, cfg) -> tuple:
        parts = urlsplit(str(cfg.base))
        scheme = (parts.scheme or "https").lower()
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    def _connect(self, cfg, key: tuple):
        scheme, host, port = key
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=cfg.http_timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=cfg.http_timeout)
        conn.connect()
        return conn

    def get(self, cfg):
        key = self._target(cfg)
        store = self._store()
        conn = store.get(key)
        if conn is None:
            conn = self._connect(cfg, key)
            store[key] = conn
        return key, conn

    def drop(self, key: tuple) -> None:
        store = self._store()
        conn = store.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def warm(self, cfg) -> None:
        """预热：提前完成 DNS + TCP + TLS，把握手成本移出抢单瞬间。"""
        self.get(cfg)

    def request(self, cfg, method: str, path: str, headers: dict,
                body: Optional[bytes], timeout: float) -> tuple:
        key, conn = self.get(cfg)
        for attempt in (0, 1):
            try:
                return self._do(conn, method, path, headers, body, timeout)
            except Exception:
                self.drop(key)
                if attempt:
                    raise
                key, conn = self._connect(cfg, key)
                self._store()[key] = conn

    @staticmethod
    def _do(conn, method: str, path: str, headers: dict,
            body: Optional[bytes], timeout: float) -> tuple:
        conn.timeout = timeout
        if conn.sock is not None:
            try:
                conn.sock.settimeout(timeout)
            except Exception:
                pass
        started = time.monotonic()
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        try:
            resp.close()
        except Exception:
            pass
        return status, raw.decode("utf-8", "replace"), (time.monotonic() - started) * 1000.0


# ------------------------------ Config ------------------------------
@dataclass(frozen=True)
class EngineConfig:
    """不可变配置快照。原有字段语义保持不变，新增字段均为性能/策略开关。"""

    base: str = "https://materials.xlya.cn"
    poll_interval: float = 0.4
    task_types: tuple = ("standard", "long_term")
    platform_account_id: Optional[int] = None
    rate_limit_max: int = 6
    rate_limit_window: float = 10.0
    max_pages: int = 2
    page_size: int = 10
    http_timeout: float = 8
    result_polls: int = 0
    # ---- 性能 / 策略 ----
    claim_workers: int = 3
    poll_rate_max: int = 5
    poll_rate_window: float = 1.0
    seen_ttl: float = 180.0
    bottom_first: bool = True
    early_fire: bool = True

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


def _boolean(key: str, raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return raw


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
    if key == "claim_workers":
        return _number(key, raw, minimum=1, maximum=10, integer=True)
    if key == "poll_rate_max":
        return _number(key, raw, minimum=1, maximum=10000, integer=True)
    if key == "poll_rate_window":
        return _number(key, raw, minimum=0.1, maximum=3600.0)
    if key == "seen_ttl":
        return _number(key, raw, minimum=0, maximum=3600.0)
    if key == "bottom_first":
        return _boolean(key, raw)
    if key == "early_fire":
        return _boolean(key, raw)
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

    def __init__(self, on_event: Callable[[dict], None],
                 data_dir: Optional[Any] = None) -> None:
        self._on_event = on_event
        self._emit_lock = threading.RLock()   # on_event 串行化
        self._lock = threading.RLock()        # 状态/配置/统计
        self._lim_lock = threading.Lock()     # claim-limits 快照
        self._log_lock = threading.Lock()     # 日志环形缓冲

        # 多账号场景下每个引擎使用自己的数据目录（token/config/history/log 隔离）
        self._data_dir = persist.ensure_dir(data_dir) if data_dir is not None \
            else persist.data_dir()
        self._logger = persist.get_logger(self._data_dir)

        self._cfg: EngineConfig = DEFAULT_CONFIG
        self._claim_limiter = RateLimiter(self._cfg.rate_limit_max,
                                          self._cfg.rate_limit_window)
        self._limiter_key = (float(self._cfg.rate_limit_max),
                             float(self._cfg.rate_limit_window))
        self._poll_limiter = RateLimiter(self._cfg.poll_rate_max,
                                         self._cfg.poll_rate_window)
        self._poll_key = (float(self._cfg.poll_rate_max),
                          float(self._cfg.poll_rate_window))

        self._http = HttpPool()
        self._sched = ClaimScheduler()

        self._token: Optional[str] = None
        self._token_exp: Optional[int] = None

        # 去重 / 在途 / 已领取
        self._seen: dict = {}          # task_id -> 入队时刻（monotonic），带 TTL
        self._in_flight: set = set()
        self._claimed: set = set()     # 本进程内领取成功的任务，永不再抢

        self._stop = threading.Event()
        self._running = False
        self._state = "idle"
        self._state_reason = ""
        self._started_at: Optional[float] = None
        self._last_poll_at: Optional[float] = None

        self._stats = {"polls": 0, "skipped": 0, "claims_ok": 0,
                       "claims_fail": 0, "claims_conflict": 0,
                       "claims_attempted": 0, "retries": 0}

        self._tasks_map: dict = {}
        self._tasks_ts: Optional[float] = None

        self._history: deque = deque(maxlen=MAX_HISTORY)
        self._logs: deque = deque(maxlen=MAX_LOGS)

        # claim-limits 快照（后台线程刷新）
        self._lim_data: dict = {}
        self._lim_mono: float = 0.0
        self._clock_offset_ms: float = 0.0
        self._rtt_ms: float = 0.0

        # 冷却 / 退避
        self._next_claim_at: float = 0.0
        self._poll_block_until: float = 0.0
        self._cool_log_at: float = 0.0

        self._pollers: dict = {}          # task_type -> {"thread":Thread,"stop":Event}
        self._claim_threads: list = []
        self._claim_stops: list = []
        self._limits_thread: Optional[threading.Thread] = None

        # 异步文件日志
        self._log_queue: "queue.Queue" = queue.Queue(maxsize=5000)
        self._log_thread = threading.Thread(target=self._log_writer_loop,
                                            name="sniper-log", daemon=True)
        self._log_thread.start()

    # ============================ 事件 ============================
    def _emit(self, event: dict) -> None:
        with self._emit_lock:
            try:
                self._on_event(event)
            except Exception:
                pass  # 广播方出问题不能带崩引擎线程

    def _log_writer_loop(self) -> None:
        """后台把日志写盘：领取关键路径只做内存 append + 入队。"""
        while True:
            try:
                entry = self._log_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                return
            if entry is None:
                return
            msg, level = entry
            self._file_log(msg, level)

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
        with self._log_lock:
            self._logs.append(event)
        try:
            self._log_queue.put_nowait((msg, level))
        except Exception:
            pass  # 队列满就丢弃磁盘日志，绝不阻塞抢单
        self._emit(event)

    def _cool_log(self, msg: str, level: str = "INFO") -> None:
        """高频冷却日志节流（最多 1 条/秒）。"""
        now = time.monotonic()
        if now - self._cool_log_at < 1.0:
            return
        self._cool_log_at = now
        self._log(msg, level)

    def _emit_status(self) -> None:
        self._emit({"type": "status", "status": self.status()})

    def _emit_tasks(self, ts: float, items: list) -> None:
        self._emit({"type": "tasks", "ts": ts, "items": items})

    def log(self, msg: str, level: str = "INFO") -> None:
        """对外暴露的日志入口（写环形缓冲 + sniper.log + log 事件）。"""
        self._log(msg, level)

    # ============================ 配置 ============================
    def data_dir(self) -> Any:
        """当前引擎的数据目录（多账号模式下用于隔离 token/config/history）。"""
        return self._data_dir

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
                self._claim_limiter = RateLimiter(cfg.rate_limit_max,
                                                  cfg.rate_limit_window)
                self._limiter_key = key
            poll_key = (float(cfg.poll_rate_max), float(cfg.poll_rate_window))
            if poll_key != self._poll_key:
                self._poll_limiter = RateLimiter(cfg.poll_rate_max,
                                                 cfg.poll_rate_window)
                self._poll_key = poll_key
        try:
            persist.save_config(cfg.to_dict(), self._data_dir)
        except Exception:
            pass
        if "task_types" in patch:
            self._sync_pollers(cfg)
        if "claim_workers" in patch:
            self._sync_workers(cfg)
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
            if not was_active:
                # 全新一轮：清空上一轮残留的调度/去重状态
                self._sched.clear()
                self._in_flight.clear()
                self._seen.clear()
                self._next_claim_at = 0.0
                self._poll_block_until = 0.0
        self._sync_pollers(cfg)
        self._sync_workers(cfg)
        self._ensure_limits_thread()
        self._log(f"开始狙击 | 监控类型={list(cfg.task_types)} | 账号="
                  f"{cfg.platform_account_id or '自动'} | 领取限速="
                  f"{cfg.rate_limit_max}/{cfg.rate_limit_window}s | 轮询="
                  f"{cfg.poll_interval}s×{cfg.max_pages}页 | 并发={cfg.claim_workers} | "
                  f"优先={'列表尾部' if cfg.bottom_first else '列表首位'}", "INFO")
        self._emit_status()

    def stop(self) -> None:
        """置停标志 + join + 清空调度队列。"""
        with self._lock:
            was_active = (self._running or bool(self._pollers)
                          or bool(self._claim_threads)
                          or self._limits_thread is not None)
            self._running = False
            self._state = "idle"
            self._state_reason = ""
            pollers = list(self._pollers.values())
            self._pollers = {}
            claim_threads = list(self._claim_threads)
            self._claim_threads = []
            claim_stops = list(self._claim_stops)
            self._claim_stops = []
            limits_thread = self._limits_thread
            self._limits_thread = None
        self._stop.set()
        for p in pollers:
            p["stop"].set()
        for ev in claim_stops:
            ev.set()
        threads = [p["thread"] for p in pollers] + list(claim_threads)
        if limits_thread is not None:
            threads.append(limits_thread)
        for t in threads:
            try:
                t.join(timeout=3)
            except Exception:
                pass
        dropped = self._sched.clear()
        with self._lock:
            self._in_flight.clear()
        if not was_active:
            return
        if dropped:
            self._log(f"已停止（丢弃 {dropped} 个待领取任务）", "WARN")
        else:
            self._log("已停止", "INFO")
        self._emit_status()

    # ============================ 线程管理 ============================
    def _sync_pollers(self, cfg: EngineConfig) -> None:
        to_start = []
        with self._lock:
            if not self._running:
                return  # 未运行时不创建线程（start() 会负责）
            wanted = list(cfg.task_types)
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

    def _sync_workers(self, cfg: EngineConfig) -> None:
        """按 ``claim_workers`` 扩容/缩容领取线程。"""
        wanted = max(1, min(10, int(cfg.claim_workers)))
        to_start = []
        with self._lock:
            if not self._running:
                return
            pairs = [(t, e) for t, e in zip(self._claim_threads, self._claim_stops)
                     if t.is_alive()]
            while len(pairs) > wanted:
                _, ev = pairs.pop()
                ev.set()
            while len(pairs) < wanted:
                ev = threading.Event()
                thread = threading.Thread(
                    target=self._worker_loop, args=(ev,),
                    name=f"sniper-claim-{len(pairs)}", daemon=True,
                )
                pairs.append((thread, ev))
                to_start.append(thread)
            self._claim_threads = [p[0] for p in pairs]
            self._claim_stops = [p[1] for p in pairs]
        for thread in to_start:
            thread.start()

    def _ensure_limits_thread(self) -> None:
        with self._lock:
            if self._limits_thread is not None and self._limits_thread.is_alive():
                return
            thread = threading.Thread(target=self._limits_loop,
                                      name="sniper-limits", daemon=True)
            self._limits_thread = thread
        thread.start()

    def threads_alive(self) -> bool:
        with self._lock:
            alive = [p["thread"].is_alive() for p in self._pollers.values()]
            alive.extend(t.is_alive() for t in self._claim_threads)
            if self._limits_thread is not None:
                alive.append(self._limits_thread.is_alive())
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
            rtt = self._rtt_ms
            offset = self._clock_offset_ms
            workers = len(self._claim_threads)
        return {
            "running": running,
            "state": state,
            "state_reason": reason,
            "token": self.token_status(),
            "started_at": started_at,
            "last_poll_at": last_poll_at,
            "upstream_base": cfg.base,
            "stats": stats,
            "queue": self._sched.size(),
            "workers": workers,
            "rtt_ms": round(rtt, 1),
            "clock_offset_ms": round(offset, 1),
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
        with self._log_lock:
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

    def _note_rtt(self, rtt_ms: float) -> None:
        if rtt_ms <= 0:
            return
        with self._lock:
            self._rtt_ms = rtt_ms if self._rtt_ms <= 0 else (self._rtt_ms * 0.7 + rtt_ms * 0.3)

    def _block_polling(self, seconds: float, reason: str) -> None:
        self._poll_block_until = time.monotonic() + max(0.5, float(seconds))
        self._cool_log(f"列表轮询退避 {seconds:.0f}s：{reason}", "WARN")

    # ============================ 网络层 ============================
    def _api_call(self, method: str, path: str, body: Any = None,
                  cfg: Optional[EngineConfig] = None,
                  blocking: bool = True, kind: str = "claim") -> tuple:
        """发起一次上游调用。返回 (status, dict_or_None)。

        ``kind``：
          - ``claim``：领取额度（阻塞拿令牌，永不丢请求）；
          - ``poll``：轮询额度（非阻塞，拿不到就跳过本轮）；
          - ``none``：调用方已自行拿到令牌。
        网络层使用 keep-alive 长连接，避免每次握手。
        """
        cfg = cfg or self._cfg
        with self._lock:
            token = self._token
        if not token:
            return (0, {"_error": "未配置 token"})

        limiter = None
        if kind == "claim":
            limiter = self._claim_limiter
        elif kind == "poll":
            limiter = self._poll_limiter
        if limiter is not None:
            if not limiter.try_acquire(1):
                if kind == "poll" or not blocking:
                    return (0, {"_skipped": True})
                limiter.acquire(1)

        headers = {
            "authorization": "Bearer " + token,
            "content-type": "application/json",
            "accept": "*/*",
            "user-agent": UA,
            "referer": cfg.base.rstrip("/") + "/pages/tasks/index",
        }
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            status, raw, rtt = self._http.request(cfg, method, path, headers,
                                                  payload, cfg.http_timeout)
        except Exception as exc:  # 超时 / 网络错误
            return (0, {"_error": str(exc)})
        if rtt:
            self._note_rtt(rtt)
        try:
            obj = json.loads(raw) if raw else {}
        except Exception:
            obj = {"_raw": raw[:200]}
        return (status, obj)

    def _get_tasks(self, task_type: str, page: int, cfg: EngineConfig) -> tuple:
        """返回 (status, items)。status: -1=本轮无令牌跳过, 0=网络错误。"""
        path = (f"/api/user/tasks?taskType={task_type}&page={page}"
                f"&pageSize={cfg.page_size}&platform=&materialType=&categoryId=&keyword=")
        status, obj = self._api_call("GET", path, cfg=cfg, kind="poll")
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
        if status == 429:
            self._block_polling(5.0, f"列表[{task_type}] 被限流")
            return (429, [])
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

    # ---------------- claim-limits（后台刷新，领取路径零阻塞） ----------------
    def _limits_snapshot(self) -> dict:
        with self._lim_lock:
            if not self._lim_data:
                return {}
            if time.monotonic() - self._lim_mono > LIMITS_MAX_AGE:
                return {}
            return dict(self._lim_data)

    def _store_limits(self, data: dict) -> None:
        with self._lim_lock:
            self._lim_data = dict(data or {})
            self._lim_mono = time.monotonic()
            try:
                server_ms = float((data or {}).get("serverNowMs") or 0)
            except Exception:
                server_ms = 0.0
            if server_ms:
                sample = server_ms - time.time() * 1000.0
                self._clock_offset_ms = (sample if not self._clock_offset_ms
                                         else self._clock_offset_ms * 0.8 + sample * 0.2)

    def _fetch_limits(self, cfg: EngineConfig, kind: str = "none") -> dict:
        status, obj = self._api_call("GET", "/api/user/tasks/claim-limits",
                                     cfg=cfg, kind=kind)
        if status == 401:
            self._pause("token 无效或已过期")
            return {}
        if status == 429:
            self._block_polling(5.0, "配额接口被限流")
            return {}
        if status == 200 and isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                self._store_limits(data)
                return data
        return {}

    def _limits_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.2)
                    continue
                if self._poll_limiter.try_acquire(1):
                    self._fetch_limits(self._cfg)
                self._sleep(LIMITS_TTL)
            except Exception as exc:
                self._loop_error("配额刷新", exc)
                self._sleep(1.0)

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
                "GET", "/api/user/platform-accounts", cfg=self._cfg, kind="claim")
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
        try:
            self._http.warm(self._cfg)   # 预热连接，抢单时不做握手
        except Exception:
            pass
        while not self._stop.is_set() and not stop_ev.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.2, stop_ev)
                    continue
                cfg = self._cfg
                if time.monotonic() < self._poll_block_until:
                    self._sleep(0.2, stop_ev)
                    continue
                self._poll_once(task_type, cfg)
                self._sleep(cfg.poll_interval, stop_ev)
            except Exception as exc:                 # 线程不死：日志+退避续跑
                self._loop_error(f"轮询[{task_type}]", exc)
                self._sleep(1.0, stop_ev)

    def _poll_once(self, task_type: str, cfg: EngineConfig) -> None:
        pages = max(1, int(cfg.max_pages))
        for page in range(1, pages + 1):
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
                self._ingest(task_type, items, (page - 1) * int(cfg.page_size))
                self._clear_error()

    # ============================ 入库 / 入队 ============================
    def _pick_account(self, item: dict) -> Optional[int]:
        cfg = self._cfg
        if cfg.platform_account_id:
            try:
                return int(cfg.platform_account_id)
            except Exception:
                return None
        accounts = (item.get("claimEligibility") or {}).get("accounts") or []
        if not accounts:
            return None
        first = accounts[0]
        raw = first.get("id") if isinstance(first, dict) else first
        try:
            return int(raw)
        except Exception:
            return None

    def _ingest(self, task_type: str, items: list, base_rank: int = 0) -> None:
        """合并任务快照 + 发现可领取任务入队 + 推 tasks 事件。

        入队优先级：默认 ``bottom_first=True``，即**列表越靠后越先抢**
        （多数人习惯抢首位，尾部竞争更小）。``rank`` 用负数实现。
        """
        new_items: list = []
        now = time.monotonic()
        cfg = self._cfg
        with self._lock:
            ttl = float(cfg.seen_ttl or 0)
            if ttl > 0:
                stale = [k for k, ts in self._seen.items() if now - ts >= ttl]
                for k in stale:
                    self._seen.pop(k, None)
            for offset, item in enumerate(items):
                tid = item.get("id")
                if tid is None:
                    continue
                self._tasks_map[tid] = item
                if tid in self._claimed:
                    continue          # 本进程已抢到，永不再抢
                try:
                    quota = float(item.get("remainingQuota") or 0)
                except Exception:
                    quota = 0.0
                eligibility = item.get("claimEligibility") or {}
                if not (quota > 0 and bool(eligibility.get("canClaim"))):
                    continue
                if tid in self._seen or tid in self._in_flight:
                    continue
                account_id = self._pick_account(item)
                if account_id is None:
                    continue
                self._seen[tid] = now
                self._in_flight.add(tid)
                new_items.append((item, base_rank + offset, account_id))
            self._tasks_ts = now
            self._last_poll_at = now
            snapshot = list(self._tasks_map.values())
        for item, rank, account_id in new_items:
            priority = -rank if cfg.bottom_first else rank
            self._sched.push(item, task_type, priority, account_id, 0.0, 0)
            self._log(f"发现可领取任务 #{item.get('id')} [{item.get('title', '')}] "
                      f"位置={rank + 1} -> 入队", "INFO")
        self._emit_tasks(now, snapshot)

    # ============================ 领取 ============================
    def _worker_loop(self, stop_ev: threading.Event) -> None:
        try:
            self._http.warm(self._cfg)
        except Exception:
            pass
        while not self._stop.is_set() and not stop_ev.is_set():
            try:
                if not self._is_active():
                    time.sleep(0.05)
                    continue
                entry = self._sched.pop(0.25)
                if entry is None:
                    continue
                item, task_type, account_id, attempts = entry
                self._claim(item, task_type, account_id, attempts)
            except Exception as exc:                 # 线程不死
                self._loop_error("领取", exc)
                self._sleep(0.5, stop_ev)

    def _finish(self, task_id: Any) -> None:
        with self._lock:
            self._in_flight.discard(task_id)

    def _pending_delay(self, limits: dict) -> float:
        if not limits:
            return 0.0
        try:
            pending = int(limits.get("pendingCount") or 0)
        except Exception:
            return 0.0
        raw_limit = limits.get("pendingLimit")
        if raw_limit is None:
            return 0.0
        try:
            limit = int(raw_limit)
        except Exception:
            return 0.0
        return 2.0 if pending >= limit else 0.0

    def _cooldown_delay(self) -> float:
        """距离可再次领取还有多少秒（含 RTT/2 提前发射补偿）。"""
        cfg = self._cfg
        early = (self._rtt_ms / 2000.0) if cfg.early_fire else 0.0
        due = self._next_claim_at
        limits = self._limits_snapshot()
        if limits:
            try:
                next_allowed = float(limits.get("nextAllowedAtMs") or 0) / 1000.0
            except Exception:
                next_allowed = 0.0
            if next_allowed:
                # serverNowMs 与本地时钟的偏移：把服务端时间换算成本地时间
                due = max(due, next_allowed - self._clock_offset_ms / 1000.0)
        wait = due - time.time() - early
        if wait <= 0:
            return 0.0
        self._cool_log(f"冷却中，{wait * 1000:.0f}ms 后再领取", "INFO")
        return wait

    def _claim(self, item: dict, task_type: str, account_id: Optional[int],
               attempts: int) -> None:
        task_id = item.get("id")
        title = item.get("title", "")
        commission = item.get("baseCommission", "")
        cfg = self._cfg
        try:
            limits = self._limits_snapshot()

            # 1) 待处理队列已满：延迟重排，不占用 worker
            pending_wait = self._pending_delay(limits)
            if pending_wait > 0:
                if attempts < MAX_PENDING_ATTEMPTS:
                    self._cool_log(f"待处理已满，{pending_wait:.0f}s 后重试 #{task_id}", "WARN")
                    self._sched.push(item, task_type, 0, account_id, pending_wait, attempts + 1)
                    self._bump("retries")
                    return
                self._bump("skipped")
                self._record(task_id, title, commission, account_id, "skip", 0, "待处理已满")
                self._finish(task_id)
                return

            # 2) 冷却：重排到到期时刻（提前 RTT/2 发射）
            wait = self._cooldown_delay()
            if wait > 0:
                self._sched.push(item, task_type, 0, account_id, wait, attempts)
                self._bump("retries")
                return

            if account_id is None:
                account_id = self._pick_account(item)
            if account_id is None:
                self._log(f"#{task_id} 无可领取账号，放弃", "WARN")
                self._finish(task_id)
                return

            # 3) 立即领取（日志在请求之后，关键路径不落盘）
            self._bump("claims_attempted")
            status, obj = self._api_call(
                "POST", f"/api/user/tasks/{task_id}/claim",
                {"platformAccountId": account_id}, cfg=cfg, kind="claim",
            )
            obj = obj if isinstance(obj, dict) else {}

            if status == 401:
                self._record(task_id, title, commission, account_id, "fail", status,
                             "token 无效或已过期")
                self._bump("claims_fail")
                self._pause("token 无效或已过期")
                self._finish(task_id)
                return

            if status in (200, 201) and obj.get("success"):
                record = obj.get("data") or {}
                if not isinstance(record, dict):
                    record = {}
                limits_in_rec = record.get("claimLimits") or {}
                self._bump("claims_ok")
                self._record(task_id, title, commission, account_id, "ok", status, "领取成功")
                with self._lock:
                    self._claimed.add(task_id)
                try:
                    retry_after = float(limits_in_rec.get("retryAfterSeconds") or 0)
                except Exception:
                    retry_after = 0.0
                if retry_after > 0:
                    with self._lock:
                        self._next_claim_at = max(self._next_claim_at,
                                                  time.time() + retry_after)
                self._log(
                    f"领取成功 #{task_id} 记录ID={record.get('id')} 失效={record.get('expiresAt')} "
                    f"剩余冷却={retry_after:g}s", "OK")
                if cfg.result_polls and int(cfg.result_polls) > 0:
                    self._poll_result(task_id, account_id, cfg)
                threading.Thread(target=self._beep, daemon=True).start()
                self._emit_status()
                self._finish(task_id)
                return

            if status == 409:
                message = obj.get("message") or "已被领取"
                self._bump("claims_conflict")
                self._record(task_id, title, commission, account_id, "conflict", status, message)
                self._log(f"慢了一步 #{task_id}：{message}", "WARN")
                self._emit_status()
                self._finish(task_id)
                return

            if status == 429:
                message = obj.get("message") or "被上游限流"
                backoff = min(30.0, 2.0 * (attempts + 1))
                with self._lock:
                    self._next_claim_at = max(self._next_claim_at,
                                              time.time() + backoff)
                self._bump("claims_fail")
                if attempts < MAX_CLAIM_ATTEMPTS:
                    self._log(f"被限流 #{task_id}，{backoff:.0f}s 后重试"
                              f"（第 {attempts + 1} 次）", "ERR")
                    self._sched.push(item, task_type, 0, account_id, backoff, attempts + 1)
                    self._bump("retries")
                    self._emit_status()
                    return
                self._record(task_id, title, commission, account_id, "ratelimit",
                             status, message)
                self._log(f"被限流 #{task_id}，已达重试上限，放弃", "ERR")
                self._emit_status()
                self._finish(task_id)
                return

            if status == 0 or status >= 500:
                # 网络抖动 / 上游 5xx：短暂重试
                if attempts < 2:
                    self._log(f"#{task_id} 上游异常（HTTP {status}），0.3s 后重试", "WARN")
                    self._sched.push(item, task_type, 0, account_id, 0.3, attempts + 1)
                    self._bump("retries")
                    return
                message = (obj.get("_error") or obj.get("message")
                           or f"HTTP {status}")
                self._bump("claims_fail")
                self._record(task_id, title, commission, account_id, "fail", status, message)
                self._log(f"领取失败 #{task_id} {message}", "ERR")
                self._emit_status()
                self._finish(task_id)
                return

            message = obj.get("message") or ""
            self._bump("claims_fail")
            self._record(task_id, title, commission, account_id, "fail", status,
                         message or "领取失败")
            self._log(f"领取失败 #{task_id} HTTP {status} {message}", "ERR")
            self._emit_status()
            self._finish(task_id)
        except Exception as exc:
            self._loop_error("领取", exc)
            self._finish(task_id)

    @staticmethod
    def _beep() -> None:
        try:
            import winsound
            winsound.Beep(1000, 200)
        except Exception:
            pass

    def _poll_result(self, task_id: Any, account_id: int, cfg: EngineConfig):
        path = (f"/api/user/tasks/{task_id}/claim-result"
                f"?platformAccountId={account_id}")
        result = None
        for _ in range(max(0, int(cfg.result_polls))):
            status, obj = self._api_call("GET", path, cfg=cfg, kind="claim")
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
