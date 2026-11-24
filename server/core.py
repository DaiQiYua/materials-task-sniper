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

账号选择（见 ``_schedule_once``）：
- 同一时间只有一个任务下发 → 用「排序靠前」的账号（平台子账号列表顺序优先）；
- 多个任务同时下发 → 每个账号分别领取不同任务（同一轮不重复分配账号）；
- 同一任务全局只分配给一个账号；重试沿用上次的账号（换号再撞只会多一个 409）。

资格判定：**以账号级为准**（``claimEligibility.accounts[].canClaim``），任务级
``claimEligibility.canClaim`` 只是登录账号维度的聚合结论，不同登录账号视图下并不一致。

上游不填 ``nextClaimableAt``、``remainingQuota`` 常年为 1，冷却/待提交只能由本机按
(子账号, 任务) 推导；``_task_snapshot()`` 会把它们按 task_id 透出给前端（``local*`` 字段）。
"""

from __future__ import annotations

import base64
import heapq
import http.client
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import persist
from .credentials import CredentialStore

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
      "Gecko/20100101 Firefox/158.0")

MAX_HISTORY = 200          # 历史环形缓冲
MAX_LOGS = 500             # 日志环形缓冲
LIMITS_TTL = 2.0           # claim-limits 后台刷新间隔（秒）
LIMITS_MAX_AGE = 15.0      # 超过该秒数的配额快照视为不可用
RESULT_POLL_INTERVAL = 0.25
MAX_CLAIM_ATTEMPTS = 4     # 429 最大重试次数
MAX_PENDING_ATTEMPTS = 6   # 待处理满最大重排次数
#: 用领取记录（/api/user/claims）后台纠正「任务冷却 / 待提交」的间隔（秒）。
#: 之前只在启动时同步一次，运行期领到的新记录（尤其在其他工具领的）永远看不到，
#: 只能靠列表里的待提交标记，导致本地状态与上游长期偏离。
CLAIMS_SYNC_TTL = 60.0
#: 任务退役：超过该秒数没有再被任何账号的列表刷到，说明上游已下架，从池里移除
TASK_RETIRE_SECONDS = 900.0
#: 已抢走 / 已放弃任务的 TTL（只用于防重抢，不需要永久记住）
TASK_DEAD_TTL = 3600.0
#: 本进程领取成功任务的 TTL（上游任务列表本身也不会长期保留）
CLAIMED_TTL = 86400.0
#: 任务池 GC 的最短执行间隔（秒）——每 50ms 全表扫一遍太浪费
GC_INTERVAL = 5.0
#: 任务快照（tasks 事件）最小广播间隔（秒）：轮询一入库就广播会把前端刷爆
TASKS_EMIT_MIN_INTERVAL = 0.2

# 平台子账号独立限流桶（与领取/轮询完全隔离，绝不占用抢单配额）
PLAT_RATE_MAX = 4
PLAT_RATE_WINDOW = 10.0
#: 平台子账号缓存超过该时长视为 stale（供 API 层展示）
PLATFORM_CACHE_STALE_SECONDS = 6 * 3600

#: 单账号轮询模式下轮询线程的 key 里的「虚拟账号」（真实 cred_id 都是 >=1，0 表示活跃 Token）
SINGLE_POLLER_KEY = -1


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
        # proxy 也进 key：线程本地池无法从外部统一清空，换代理时靠 key 变化自动重建连接
        parts = urlsplit(str(cfg.base))
        scheme = (parts.scheme or "https").lower()
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        proxy = (getattr(cfg, "proxy", None) or "").strip() or None
        return scheme, host, port, proxy

    def _connect(self, cfg, key: tuple):
        scheme, host, port, proxy = key
        if proxy:
            p = urlsplit(str(proxy))
            p_host = p.hostname or ""
            p_port = int(p.port or 80)
            if scheme == "https":
                # 走 CONNECT 隧道：TLS 由 http.client 在隧道建立后自动完成，
                # SNI 默认用 tunnel host（真实目标域名），故不要传 context=，
                # 以保证默认 SSL 上下文读取 Windows 系统证书库。
                conn = http.client.HTTPSConnection(
                    p_host, p_port, timeout=cfg.http_timeout)
                conn.set_tunnel(host, int(port))
            else:
                conn = http.client.HTTPConnection(
                    p_host, p_port, timeout=cfg.http_timeout)
                conn.set_tunnel(host, int(port))
        elif scheme == "https":
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
                # 重连：_connect 只返回连接对象，key 保持不变。
                # （早前这里写成 `key, conn = self._connect(...)`，把连接对象
                #  当元组解包 → "cannot unpack non-iterable HTTPSConnection
                #  object"，于是「第一次请求偶发失败」直接变成「本轮请求失败」，
                #  重试从未真正发生，任务列表因此间歇性不更新。）
                conn = self._connect(cfg, key)
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


# ------------------------------ 小工具 ------------------------------
def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


#: 领取记录里「进行中」的阶段键（与上游 stats 对齐）
ACTIVE_STAGE_KEYS = ("to_submit", "reviewing", "retention", "pending_settlement")
#: 阶段键 -> 中文标签（前端直接展示用）
STAGE_LABELS = {
    "to_submit": "待提交", "reviewing": "审核中", "retention": "留存期",
    "pending_settlement": "待结算", "settled": "已结算", "rejected": "未通过",
    "expired": "已过期", "cancelled": "已取消", "abnormal": "结算异常",
}


def claim_stage(status: Any, settlement: Any,
                has_submission: bool) -> tuple:
    """把 ``(claim.status, settlement.status, 是否有提交)`` 映射成 ``(stage_key, 中文标签)``。

    按顺序判定，取第一个命中的：

      1) settlement 是 dict 且其 ``status == "abnormal"``       → abnormal / 结算异常
      2) status == "settled" 或 settlement.status == "paid"     → settled / 已结算
      3) status == "rejected"                                   → rejected / 未通过
      4) status == "expired"                                    → expired / 已过期
      5) status == "cancelled"                                  → cancelled / 已取消
      6) settlement.status == "retention_pending"               → retention / 留存期
      7) settlement 是 dict（未被上面命中，含 pending 等）        → pending_settlement / 待结算
      8) 有提交（has_submission）                                → reviewing / 审核中
      9) 其余（claimed 且无提交）                                 → to_submit / 待提交

    未知 status 且无 settlement 且无提交 → ``("unknown", str(status or "未知"))``。
    """
    stat = str(status or "").strip()
    settle_status = ""
    if isinstance(settlement, dict):
        settle_status = str(settlement.get("status") or "").strip()
    # 1) 结算异常优先级最高（钱出了问题，无论 claim 本身什么状态都要人看）
    if settle_status == "abnormal":
        return ("abnormal", STAGE_LABELS["abnormal"])
    # 2) 已结算
    if stat == "settled" or settle_status == "paid":
        return ("settled", STAGE_LABELS["settled"])
    # 3) 未通过
    if stat == "rejected":
        return ("rejected", STAGE_LABELS["rejected"])
    # 4) 已过期
    if stat == "expired":
        return ("expired", STAGE_LABELS["expired"])
    # 5) 已取消
    if stat == "cancelled":
        return ("cancelled", STAGE_LABELS["cancelled"])
    # 6) 留存期（approved 且结算处于留存待结）
    if settle_status == "retention_pending":
        return ("retention", STAGE_LABELS["retention"])
    # 7) 待结算：只要 settlement 是 dict 且未被上面命中（pending / 其它）都算
    if isinstance(settlement, dict):
        return ("pending_settlement", STAGE_LABELS["pending_settlement"])
    # 8) 有提交 → 审核中
    if has_submission:
        return ("reviewing", STAGE_LABELS["reviewing"])
    # 9) claimed/approved 且无提交 → 待提交
    if stat in ("claimed", "approved"):
        return ("to_submit", STAGE_LABELS["to_submit"])
    # 未知 status（含空）且无 settlement 无提交
    return ("unknown", stat or "未知")


def _iso_to_epoch(value: Any) -> Optional[float]:
    """ISO 时间（带 Z / 偏移 / 本地）→ epoch 秒；解析失败返回 None。"""
    if value in (None, ""):
        return None
    num = _as_float(value)
    if num is not None:
        return num
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ------------------------------ Config ------------------------------
def _default_proxy() -> Optional[str]:
    """环境变量兜底：不设 ``SNIPER_PROXY`` 时返回 None（= 直连，行为与旧版一致）。"""
    return os.environ.get("SNIPER_PROXY", "").strip() or None


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
    # ---- 多账号并发 / 最新任务 ----
    newest_first: bool = True       # 优先抢 createdAt 最新的任务
    tail_pages: int = 2             # 每轮只轮询列表最后 N 页（新任务追加在尾部）
    total_refresh_seconds: float = 10.0  # 回看 page=1 校准 total 的间隔
    per_account_inflight: int = 1   # 每个平台子账号同时最多几个领取请求
    # ---- 轮询成本 ----
    #: 单账号轮询：同一时刻只用一个登录账号刷任务列表（有任务时仍由全部账号并行领取），
    #: 把列表刷新的请求/限流开销从「N 个账号」降到「1 个账号」。false=旧行为（每账号各刷）
    single_poller: bool = True
    #: 单账号轮询时的轮转间隔（秒）：每隔这么久换下一个登录账号，确保每个账号的任务都能被看到；
    #: 0 = 固定用同一个账号（最省请求，但看不到其他登录账号独有的任务）
    poll_rotate_seconds: float = 10.0
    # ---- 调试 / 抓包 ----
    #: 上游请求代理，如 http://127.0.0.1:9090；None=直连（默认读环境变量 SNIPER_PROXY）
    proxy: Optional[str] = field(default_factory=_default_proxy)

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
    if key == "proxy":
        # None / 空串都视为「直连」；其余做 http://host:port 规范化校验
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("proxy 必须是字符串或 null")
        text = raw.strip()
        if not text:
            return None
        if not text.startswith("http://"):
            if "://" in text:
                raise ValueError("proxy 目前只支持 http://host:port")
            text = "http://" + text
        parts = urlsplit(text)
        scheme = (parts.scheme or "").lower()
        if scheme != "http":
            raise ValueError("proxy 目前只支持 http://host:port")
        if not parts.hostname:
            raise ValueError("proxy 必须包含主机名，形如 http://127.0.0.1:9090")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("proxy 端口非法，形如 http://127.0.0.1:9090")
        if port is None:
            raise ValueError("proxy 必须包含端口，形如 http://127.0.0.1:9090")
        return f"http://{parts.hostname}:{int(port)}"
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
        return _number(key, raw, minimum=1, maximum=20, integer=True)
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
    if key == "newest_first":
        return _boolean(key, raw)
    if key == "tail_pages":
        return _number(key, raw, minimum=1, maximum=10, integer=True)
    if key == "total_refresh_seconds":
        return _number(key, raw, minimum=1.0, maximum=3600.0)
    if key == "per_account_inflight":
        return _number(key, raw, minimum=1, maximum=5, integer=True)
    if key == "single_poller":
        return _boolean(key, raw)
    if key == "poll_rotate_seconds":
        return _number(key, raw, minimum=0, maximum=3600.0)
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
        self._lim_lock = threading.Lock()     # claim-limits 快照
        self._log_lock = threading.Lock()     # 日志环形缓冲

        self._data_dir = persist.data_dir()
        self._logger = persist.get_logger(self._data_dir)

        self._cfg: EngineConfig = DEFAULT_CONFIG
        # 按登录账号隔离的限速桶（多账号并行的关键）
        self._claim_limiters: dict = {}
        self._poll_limiters: dict = {}
        # 配额刷新（claim-limits）独立桶：之前它走 poll 桶，把轮询额度吃掉了
        self._limits_limiters: dict = {}
        self._limiter_key = (float(self._cfg.rate_limit_max),
                             float(self._cfg.rate_limit_window))
        self._poll_key = (float(self._cfg.poll_rate_max),
                          float(self._cfg.poll_rate_window))
        # 平台子账号专用桶：容量小、**永不阻塞**、超限直接让上层「稍后再试」
        self._plat_limiter = RateLimiter(PLAT_RATE_MAX, PLAT_RATE_WINDOW)

        self._http = HttpPool()
        self._sched = ClaimScheduler()

        self._token: Optional[str] = None
        self._token_exp: Optional[int] = None

        # 凭据存储 / 重登钩子（由 app 注入；未注入时按 data_dir 懒建）
        self._store: Optional[CredentialStore] = None
        self._relogin_fn: Optional[Callable[[int], Any]] = None
        # 平台子账号 -> 归属登录账号（cred_id）的内存索引
        self._pacct_owner: dict = {}
        self._pacct_meta: dict = {}        # aid -> 平台子账号原始信息（platform/verify/selfDisabled）
        self._pacct_order: dict = {}       # aid -> 排序名次（越小越靠前：按平台子账号列表顺序）
        self._pacct_loaded = False

        # 任务池 / 全局分配锁 / 在途状态
        self._task_assigned: dict = {}     # task_id -> platform_account_id（全局唯一，防同平台互抢）
        self._task_last_account: dict = {}  # task_id -> 上次分配到的平台账号（重试沿用，避免换号再撞 409）
        self._account_inflight: dict = {}  # platform_account_id -> in-flight 数
        self._task_retry_at: dict = {}     # task_id -> monotonic 重试时刻
        self._task_attempts: dict = {}     # task_id -> 尝试次数
        self._task_dead: dict = {}        # task_id -> monotonic（已抢走/已放弃，带 TTL 清理）
        self._task_first_seen: dict = {}   # task_id -> monotonic
        self._task_last_seen: dict = {}    # task_id -> monotonic（用于清理）
        self._task_sources: dict = {}      # task_id -> set(cred_id)
        self._task_types_map: dict = {}    # task_id -> task_type
        self._poll_state: dict = {}        # (cred_id, task_type) -> {total,page_size,last_total,backfilled}
        # 任务池治理：GC 时间戳 + 排序缓存（避免每 50ms 全表排序）
        self._gc_at: float = 0.0
        self._pool_dirty: bool = True
        self._sorted_cache: list = []

        # 去重 / 在途 / 已领取（保留旧字段供兼容）
        self._seen: dict = {}
        self._in_flight: set = set()
        self._claimed: dict = {}           # task_id -> monotonic（本进程内领取成功，带 TTL）

        self._stop = threading.Event()
        self._running = False
        self._state = "idle"
        self._state_reason = ""
        self._started_at: Optional[float] = None
        #: epoch 秒（time.time），不是 monotonic；对外 API 用
        self._last_poll_at: Optional[float] = None

        self._stats = {"polls": 0, "skipped": 0, "claims_ok": 0,
                       "claims_fail": 0, "claims_conflict": 0,
                       "claims_attempted": 0, "retries": 0}

        self._tasks_map: dict = {}
        #: epoch 秒（time.time），不是 monotonic；对外 API（/api/tasks、tasks 事件）用
        self._tasks_ts: Optional[float] = None
        # 任务快照广播节流：上次 emit 时刻（epoch 秒）+ 是否有挂起待补发
        self._tasks_emit_at: float = 0.0
        self._tasks_emit_pending: bool = False
        # 「调度 #tid」日志的每任务节流（task_id -> monotonic）
        self._sched_log_next: dict = {}

        self._history: deque = deque(maxlen=MAX_HISTORY)
        self._logs: deque = deque(maxlen=MAX_LOGS)

        # claim-limits 快照（按登录账号隔离；_lim_data 保留为活跃账号的兼容视图）
        self._lim_data: dict = {}
        self._lim_mono: float = 0.0
        self._lim_data_by_cred: dict = {}
        self._lim_mono_by_cred: dict = {}
        self._lim_next_by_cred: dict = {}
        # 后台用领取记录纠正「任务冷却 / 待提交」的下次时刻（monotonic）
        self._claims_sync_next: dict = {}
        self._clock_offset_ms: float = 0.0
        self._rtt_ms: float = 0.0

        # 冷却 / 退避（按 cred 隔离）
        self._next_claim_at: float = 0.0          # 兼容：活跃账号
        self._next_claim_at_by_cred: dict = {}    # cred_id -> 本地 epoch
        self._poll_block_until: float = 0.0
        self._poll_block_until_by_cred: dict = {}
        self._cool_log_at: float = 0.0

        self._pollers: dict = {}          # (cred_id, task_type) -> {"thread":Thread,"stop":Event}
        # 单账号轮询（single_poller）状态：当前轮询账号 + 上次轮转时刻（monotonic）
        self._poll_cred: Optional[int] = None
        self._poll_cred_at: float = 0.0
        # 手动刷新防重入：与轮询线程共用 _ingest（自带锁），这里只挡两次手动刷新并发
        self._refresh_lock = threading.Lock()
        self._refresh_inflight = False
        self._claim_threads: list = []
        self._claim_stops: list = []
        self._limits_thread: Optional[threading.Thread] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_stop: Optional[threading.Event] = None
        self._account_cursor: int = 0     # 抢单账号轮转游标（所有可领取账号轮流上）
        # 冷却检测（与任务列表解耦，见 cooldown_state / _load_cooldowns_state）
        self._request_cooldowns: dict = {}     # cred_id -> 请求冷却（10s 级）
        self._task_cooldowns: dict = {}        # (平台子账号id, 任务id) -> 任务冷却（3天级）
        self._pending_submissions: dict = {}   # (平台子账号id, 任务id) -> 待提交
        # 领取记录（/api/user/claims）归一化缓存：cred_id -> {"fetched_at","items","stats","total"}
        self._claims: dict = {}
        # 最近一次拉取失败原因：cred_id -> 错误串（供快照暴露给前端）
        self._claim_errors: dict = {}
        self._load_cooldowns_state()

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
        # 快照带上本机冷却 / 待提交（前端「任务速览」据此标注状态）
        self._emit({"type": "tasks", "ts": ts, "items": self._task_snapshot()})

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
            old_cfg = self._cfg
            cfg = old_cfg.merge(patch)
            self._cfg = cfg
            key = (float(cfg.rate_limit_max), float(cfg.rate_limit_window))
            if key != self._limiter_key:
                self._claim_limiters = {}
                self._limiter_key = key
            poll_key = (float(cfg.poll_rate_max), float(cfg.poll_rate_window))
            if poll_key != self._poll_key:
                self._poll_limiters = {}
                self._poll_key = poll_key
        # 代理 / 目标变化不需要手工清连接池：proxy 已是 HttpPool key 的一部分，
        # key 变了就会新建连接，旧连接不会被复用。
        changed = {(old_cfg.proxy or None), (cfg.proxy or None)}
        if len(changed) > 1:
            self._log(f"上游代理已更新：{cfg.proxy or '直连'}", "INFO")
        if old_cfg.base != cfg.base:
            self._log(f"上游地址已更新：{cfg.base}", "INFO")
        try:
            persist.save_config(cfg.to_dict(), self._data_dir)
        except Exception:
            pass
        if "task_types" in patch or "single_poller" in patch:
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
            has_any_token = bool(self._token)
            if not has_any_token:
                try:
                    has_any_token = any(
                        self._get_store().get_token(int(r.get("id")))
                        for r in self._cred_records())
                except Exception:
                    has_any_token = False
            if not has_any_token:
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
                # 全新一轮：清空上一轮残留的调度/任务状态
                self._sched.clear()
                self._in_flight.clear()
                self._seen.clear()
                self._task_assigned.clear()
                self._task_last_account.clear()
                self._account_inflight.clear()
                self._task_retry_at.clear()
                self._task_attempts.clear()
                self._task_dead.clear()
                self._task_first_seen.clear()
                self._task_last_seen.clear()
                self._task_sources.clear()
                self._task_types_map.clear()
                self._poll_state.clear()
                self._poll_cred = None        # 单账号轮询：新一轮重新选号
                self._poll_cred_at = 0.0
                self._next_claim_at = 0.0
                self._next_claim_at_by_cred.clear()
                self._poll_block_until = 0.0
                self._poll_block_until_by_cred.clear()
                # 新一轮的任务池治理 / 快照节流 / 领取记录同步也要复位
                self._gc_at = 0.0
                self._pool_dirty = True
                self._sorted_cache = []
                self._tasks_emit_at = 0.0
                self._tasks_emit_pending = False
                self._sched_log_next.clear()
                self._claims_sync_next.clear()
        self._sync_pollers(cfg)
        self._sync_workers(cfg)
        self._ensure_limits_thread()
        self._ensure_scheduler_thread()
        if cfg.single_poller:
            poll_desc = (f"单账号（每 {cfg.poll_rotate_seconds:g}s 换号）"
                         if cfg.poll_rotate_seconds else "单账号（固定）")
        else:
            poll_desc = "每账号各刷"
        self._log(f"开始狙击 | 监控类型={list(cfg.task_types)} | 账号=全部平台账号 | 领取限速="
                  f"{cfg.rate_limit_max}/{cfg.rate_limit_window}s | 轮询="
                  f"{cfg.poll_interval}s×尾{cfg.tail_pages}页 | 并发={cfg.claim_workers} | "
                  f"优先={'最新任务' if cfg.newest_first else '列表顺序'} | "
                  f"列表刷新={poll_desc}", "INFO")
        self._emit_status()

    def stop(self) -> None:
        """置停标志 + join + 清空调度队列。"""
        with self._lock:
            was_active = (self._running or bool(self._pollers)
                          or bool(self._claim_threads)
                          or self._limits_thread is not None
                          or self._scheduler_thread is not None)
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
            scheduler_thread = self._scheduler_thread
            scheduler_stop = self._scheduler_stop
            self._scheduler_thread = None
            self._scheduler_stop = None
        self._stop.set()
        for p in pollers:
            p["stop"].set()
        for ev in claim_stops:
            ev.set()
        if scheduler_stop is not None:
            scheduler_stop.set()
        threads = [p["thread"] for p in pollers] + list(claim_threads)
        if limits_thread is not None:
            threads.append(limits_thread)
        if scheduler_thread is not None:
            threads.append(scheduler_thread)
        for t in threads:
            try:
                t.join(timeout=3)
            except Exception:
                pass
        dropped = self._sched.clear()
        with self._lock:
            self._in_flight.clear()
            self._task_assigned.clear()
            self._task_last_account.clear()
            self._account_inflight.clear()
        if not was_active:
            return
        if dropped:
            self._log(f"已停止（丢弃 {dropped} 个待领取任务）", "WARN")
        else:
            self._log("已停止", "INFO")
        self._emit_status()

    def clear_all_data(self) -> dict:
        """一键清空所有数据（账号 / Token / 平台缓存 / 历史 / 日志 / 配置）。

        顺序固定：停引擎 → 摘掉文件日志并抽干日志队列 → 锁内清内存态 →
        删盘上数据 → 重建一个全新的空日志文件 → 记一条 WARN 日志 + 广播状态。
        返回 ``persist.clear_all()`` 的统计 dict（含 ``files`` / ``failed``）。
        """
        # a) 先停线程（未运行也不抛）
        self.stop()

        # b) 摘掉 logger 并抽干积压：删文件期间异步日志线程不能再建文件
        self._logger = None
        while True:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

        # c) 锁内清内存态（等同于刚构造出来的引擎）
        with self._lock:
            self._token = None
            self._token_exp = None
            self._history = deque(maxlen=MAX_HISTORY)
            self._tasks_map.clear()
            self._tasks_ts = None
            for key in list(self._stats):
                self._stats[key] = 0
            self._seen.clear()
            self._in_flight.clear()
            self._claimed.clear()
            self._task_assigned.clear()
            self._task_last_account.clear()
            self._account_inflight.clear()
            self._task_retry_at.clear()
            self._task_attempts.clear()
            self._task_dead.clear()
            self._task_first_seen.clear()
            self._task_last_seen.clear()
            self._task_sources.clear()
            self._task_types_map.clear()
            self._poll_state.clear()
            self._pacct_owner.clear()
            self._pacct_meta.clear()
            self._pacct_order.clear()
            self._pacct_loaded = False
            # 任务池治理 / 快照节流 / 领取记录同步
            self._gc_at = 0.0
            self._pool_dirty = True
            self._sorted_cache = []
            self._tasks_emit_at = 0.0
            self._tasks_emit_pending = False
            self._sched_log_next.clear()
            self._claims_sync_next.clear()
            self._state = "idle"
            self._state_reason = ""
            self._running = False
            self._started_at = None
            self._last_poll_at = None
            self._next_claim_at = 0.0
            self._next_claim_at_by_cred.clear()
            self._poll_block_until = 0.0
            self._poll_block_until_by_cred.clear()
            self._poll_cred = None
            self._poll_cred_at = 0.0
            self._cool_log_at = 0.0
            self._lim_data = {}
            self._lim_mono = 0.0
            self._lim_data_by_cred.clear()
            self._lim_mono_by_cred.clear()
            self._lim_next_by_cred.clear()
            self._request_cooldowns.clear()
            self._task_cooldowns.clear()
            self._pending_submissions.clear()
            self._claims.clear()
            self._claim_errors.clear()
            self._account_cursor = 0
            self._clock_offset_ms = 0.0
            self._rtt_ms = 0.0
            # 恢复默认配置：**不**走 update_config()（那会写回 config.json）
            self._cfg = DEFAULT_CONFIG
            self._claim_limiters = {}
            self._poll_limiters = {}
            self._limits_limiters = {}
            self._limiter_key = (float(self._cfg.rate_limit_max),
                                 float(self._cfg.rate_limit_window))
            self._poll_key = (float(self._cfg.poll_rate_max),
                              float(self._cfg.poll_rate_window))
        with self._log_lock:
            self._logs = deque(maxlen=MAX_LOGS)

        # d) 删盘上数据（白名单 + 失败不抛）
        cleared = persist.clear_all(self._data_dir)

        # e) 重新装配一个全新的空日志文件（对外接口约定 sniper.log 始终存在）
        self._logger = persist.get_logger(self._data_dir)

        # f) 记一条并广播状态
        self._log("已清空所有数据（账号 / Token / 平台缓存 / 历史 / 日志 / 配置已恢复默认）",
                  "WARN")
        self._emit_status()
        return cleared

    # ============================ 线程管理 ============================
    def _sync_pollers(self, cfg: EngineConfig) -> None:
        """维护轮询线程。

        默认「单账号轮询」（``cfg.single_poller``）：**同一时刻只有一个登录账号在刷任务列表**
        （每个任务类型一条线程），把列表刷新的请求与限流开销从「N 个账号」降到「1 个账号」；
        领取不受影响 —— 任务进池后仍由全部账号并行领取。
        ``single_poller=False`` 时回到旧行为：每个「登录账号 × 任务类型」各一条线程。
        """
        to_start = []
        targets = self._poll_targets()
        with self._lock:
            if not self._running:
                return  # 未运行时不创建线程（start() 会负责）
            if cfg.single_poller:
                wanted = {(SINGLE_POLLER_KEY, str(tt)) for tt in cfg.task_types}
            else:
                wanted = {(int(cid), str(tt)) for cid, _tok in targets
                          for tt in cfg.task_types}
            for key in list(self._pollers):
                if key not in wanted:
                    info = self._pollers.pop(key)
                    info["stop"].set()
            for key in sorted(wanted):
                cid_key, task_type = key
                info = self._pollers.get(key)
                if info is not None and info["thread"].is_alive():
                    continue
                # 单账号轮询：cred_id=None 表示「每轮动态挑一个账号」
                cred_arg = None if cid_key == SINGLE_POLLER_KEY else int(cid_key)
                stop_ev = threading.Event()
                thread = threading.Thread(
                    target=self._poller_loop, args=(cred_arg, task_type, stop_ev),
                    name=(f"sniper-poller-single-{task_type}" if cred_arg is None
                          else f"sniper-poller-{cred_arg}-{task_type}"),
                    daemon=True,
                )
                self._pollers[key] = {"thread": thread, "stop": stop_ev}
                to_start.append(thread)
        for thread in to_start:
            thread.start()

    def refresh_pollers(self) -> None:
        """账号增删后重建轮询线程（运行中才会真正创建）。"""
        self._sync_pollers(self._cfg)
        try:
            self._sync_workers(self._cfg)
        except Exception:
            pass

    def _sync_workers(self, cfg: EngineConfig) -> None:
        """按 ``claim_workers`` 扩容/缩容领取线程；账号多时自动补到账号数。"""
        with self._lock:
            accounts = len(self._pacct_meta)
        wanted = max(int(cfg.claim_workers), accounts, 1)
        wanted = min(20, wanted)
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
            if self._scheduler_thread is not None:
                alive.append(self._scheduler_thread.is_alive())
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
            "queue": self._sched.size() + len(self._task_assigned),
            "assigned": len(self._task_assigned),
            "workers": workers,
            "rtt_ms": round(rtt, 1),
            "clock_offset_ms": round(offset, 1),
            "request_cooldowns": self.request_cooldowns(),
            "task_cooldowns": self.task_cooldowns(),
            "pending_submissions": self.pending_submissions(),
            "claims": self.claims_snapshot(),
            "account_inflight": {str(aid): int(n)
                                 for aid, n in self._account_inflight.items() if n},
        }

    def _local_task_state(self) -> dict:
        """本机视角的任务状态：``task_id -> 冷却 / 待提交``。

        上游任务列表里没有冷却信息（``nextClaimableAt`` 常年为空），冷却是本机
        按 ``claimedAt + claimIntervalDays`` 推出来的，所以「任务速览」要显示
        冷却 / 待提交，必须由后端按 task_id 透出。
        """
        now = time.time()
        out: dict = {}
        with self._lock:
            for (aid, tid), rec in list(self._task_cooldowns.items()):
                until = _as_float(rec.get("until")) or 0.0
                if until <= now:
                    continue
                slot = out.setdefault(tid, {"cooldown_until": 0.0,
                                            "cooldown_accounts": [],
                                            "pending_accounts": []})
                slot["cooldown_until"] = max(slot["cooldown_until"], until)
                if aid not in slot["cooldown_accounts"]:
                    slot["cooldown_accounts"].append(aid)
            for (aid, tid), rec in list(self._pending_submissions.items()):
                since = _as_float(rec.get("since")) or now
                if now - since > 3 * 86400:
                    continue
                slot = out.setdefault(tid, {"cooldown_until": 0.0,
                                            "cooldown_accounts": [],
                                            "pending_accounts": []})
                if aid not in slot["pending_accounts"]:
                    slot["pending_accounts"].append(aid)
        return out

    def _task_snapshot(self) -> list:
        """任务快照（附带本机冷却 / 待提交信息，供「任务速览」标注状态）。"""
        local = self._local_task_state()
        with self._lock:
            items = list(self._tasks_map.values())
        out = []
        for it in items:
            tid = it.get("id")
            rec = local.get(tid)
            if not rec:
                out.append(it)
                continue
            item = dict(it)
            item["localCooldownUntil"] = rec["cooldown_until"]
            item["localCooldownAccounts"] = list(rec["cooldown_accounts"])
            item["localPendingAccounts"] = list(rec["pending_accounts"])
            out.append(item)
        return out

    def tasks(self) -> dict:
        with self._lock:
            ts = self._tasks_ts
        return {"ts": ts, "items": self._task_snapshot()}

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

    def _block_polling(self, seconds: float, reason: str,
                       cred_id: Any = None) -> None:
        until = time.monotonic() + max(0.5, float(seconds))
        if cred_id is None:
            self._poll_block_until = until
            return
        try:
            self._poll_block_until_by_cred[int(cred_id)] = until
        except Exception:
            self._poll_block_until = until
        self._cool_log(f"列表轮询退避 {seconds:.0f}s：{reason}", "WARN")

    def _poll_auth_failed(self, cid: int) -> None:
        """列表轮询 401 的处理。

        单账号轮询且还有别的可用账号时：只把该账号挡住并换号继续刷，
        **不**暂停整台引擎（其他账号的 Token 可能都好好的）。
        """
        if self._cfg.single_poller:
            others = []
            for other, _tok in self._poll_targets():
                try:
                    other = int(other)
                except Exception:
                    continue
                if other != int(cid) and other not in others:
                    others.append(other)
            if others:
                self._block_polling(60.0, f"账号 {cid} Token 失效，已切换其他账号轮询",
                                    cred_id=cid)
                self._log(f"账号 {cid} 的 Token 已失效，轮询已切换到其他账号"
                          f"（其他账号继续狙击）", "WARN")
                return
        active = self._active_cred_id()
        if active is None or int(active) == int(cid):
            self._pause("token 无效或已过期")
        else:
            self._block_polling(30.0, f"账号 {cid} Token 失效", cred_id=cid)

    # ============================ 网络层 ============================
    def _api_call(self, method: str, path: str, body: Any = None,
                  cfg: Optional[EngineConfig] = None,
                  blocking: bool = True, kind: str = "claim",
                  token: Optional[str] = None,
                  cred_id: Optional[int] = None,
                  limiter_scope: str = "") -> tuple:
        """发起一次上游调用。返回 (status, dict_or_None)。

        ``kind``：
          - ``claim``：领取额度（阻塞拿令牌，永不丢请求）；
          - ``poll``：轮询额度（非阻塞，拿不到就跳过本轮）；
          - ``limits``：claim-limits 后台刷新额度（**非阻塞**，独立桶，
            不占用轮询/领取配额）；
          - ``plat``：平台子账号专用桶（**非阻塞**，超限返回
            ``error="rate_limited"``，绝不占用抢单配额）；
          - ``none``：调用方已自行拿到令牌。
        ``limiter_scope``：轮询桶再按「任务类型」细分（同一 cred 的不同任务
        类型各用一个桶，互不抢令牌）。
        ``token``：本次调用使用的 Bearer token。缺省（``None``）用引擎的活跃
        token；传值即「临时借用某个登录账号的 token」，**不改**引擎状态、
        不写盘（多账号并存的唯一正确用法）。
        网络层使用 keep-alive 长连接，避免每次握手。
        """
        cfg = cfg or self._cfg
        if token is None:
            with self._lock:
                token = self._token
        token = (token or "").strip()
        if not token:
            return (0, {"_error": "未配置 token"})

        limiter = None
        if kind == "plat":
            limiter = self._plat_limiter
        elif kind == "claim":
            limiter = self._get_limiter("claim", cred_id)
        elif kind == "limits":
            limiter = self._get_limiter("limits", cred_id)
        elif kind == "poll":
            limiter = self._get_limiter("poll", cred_id, scope=limiter_scope)
        if limiter is not None:
            if not limiter.try_acquire(1):
                if kind in ("poll", "plat", "limits") or not blocking:
                    if kind == "plat":
                        return (0, {"_error": "rate_limited",
                                    "error": "rate_limited",
                                    "_rate_limited": True})
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

    def _get_tasks(self, cred_id: Any, task_type: str, page: int,
                   cfg: EngineConfig, page_size: Optional[int] = None) -> tuple:
        """返回 ``(status, items, meta)``；meta 含 total/pageSize。"""
        token = self._cred_token(cred_id)
        if not token:
            return (-1, [], {})
        psize = max(1, int(page_size or cfg.page_size))
        path = (f"/api/user/tasks?taskType={task_type}&page={page}"
                f"&pageSize={psize}&platform=&materialType=&categoryId=&keyword=")
        status, obj = self._api_call("GET", path, cfg=cfg, kind="poll",
                                     token=token, cred_id=cred_id,
                                     limiter_scope=str(task_type))
        if isinstance(obj, dict) and obj.get("_skipped"):
            return (-1, [], {})
        if status == 0:
            err = obj.get("_error", "") if isinstance(obj, dict) else ""
            if err:
                self._log(f"列表[{task_type}] 请求失败：{err}", "WARN")
            return (0, [], {})
        if status == 401:
            self._relogin_cred(cred_id)
            return (401, [], {})
        if status == 304:
            return (304, [], {})
        if status == 429:
            self._block_polling(5.0, f"列表[{task_type}] 被限流", cred_id=cred_id)
            return (429, [], {})
        if status != 200:
            msg = obj.get("message", "") if isinstance(obj, dict) else ""
            self._log(f"列表[{task_type}] HTTP {status} {msg}", "WARN")
            return (status, [], {})
        items = []
        meta: dict = {}
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                items = data.get("items") or []
                try:
                    meta["total"] = int(data.get("total"))
                except Exception:
                    meta["total"] = None
                try:
                    meta["page_size"] = int(data.get("pageSize") or psize)
                except Exception:
                    meta["page_size"] = psize
        return (200, [it for it in items if isinstance(it, dict)], meta)

    # ---------------- claim-limits（后台刷新，领取路径零阻塞） ----------------
    def _limits_snapshot(self, cred_id: Any = None) -> dict:
        cid = None
        if cred_id is not None:
            try:
                cid = int(cred_id)
            except Exception:
                cid = None
        if cid is None:
            active = self._active_cred_id()
            cid = int(active) if active is not None else 0
        with self._lim_lock:
            data = self._lim_data_by_cred.get(cid)
            mono = self._lim_mono_by_cred.get(cid, 0.0)
            if not data:
                return {}
            if time.monotonic() - mono > LIMITS_MAX_AGE:
                return {}
            return dict(data)

    def _store_limits(self, data: dict, cred_id: Any = 0) -> None:
        try:
            cid = int(cred_id)
        except Exception:
            cid = 0
        with self._lim_lock:
            self._lim_data_by_cred[cid] = dict(data or {})
            self._lim_mono_by_cred[cid] = time.monotonic()
            self._lim_data = dict(data or {})          # 兼容：最近一次快照
            self._lim_mono = self._lim_mono_by_cred[cid]
            try:
                server_ms = float((data or {}).get("serverNowMs") or 0)
            except Exception:
                server_ms = 0.0
            if server_ms:
                sample = server_ms - time.time() * 1000.0
                self._clock_offset_ms = (sample if not self._clock_offset_ms
                                         else self._clock_offset_ms * 0.8 + sample * 0.2)
        self._set_request_cooldown(cid, data, source="claim_limits", persist=False)

    # ---------------- 冷却检测：请求冷却 / 任务冷却 / 待提交 ----------------
    # 为什么分三类：
    #   10s   = claim-limits 的领取请求冷却（登录账号级，HAR 实证 reason=cooldown）
    #   3天   = 每个任务自己的 claimIntervalDays（同任务重复领取间隔）
    #   pending_submission = 领取后未提交，才是继续领新任务的真实阻塞
    def _load_cooldowns_state(self) -> None:
        try:
            data = persist.load_cooldowns(self._data_dir)
        except Exception:
            data = {}
        req, tasks, pending = {}, {}, {}
        for key, rec in (data.get("request") or {}).items():
            try:
                cid = int(key)
            except Exception:
                continue
            req[cid] = dict(rec)
        for section, bucket in (("tasks", tasks), ("pending", pending)):
            for key, rec in (data.get(section) or {}).items():
                try:
                    aid_s, tid_s = str(key).split(":", 1)
                    bucket[(int(aid_s), int(tid_s))] = dict(rec)
                except Exception:
                    continue
        with self._lock:
            self._request_cooldowns = req
            self._task_cooldowns = tasks
            self._pending_submissions = pending

    def _save_cooldowns_state(self) -> None:
        now = time.time()
        with self._lock:
            data = {
                "request": {str(k): dict(v) for k, v in self._request_cooldowns.items()},
                "tasks": {},
                "pending": {},
            }
            for (aid, tid), rec in self._task_cooldowns.items():
                until = _as_float(rec.get("until")) or 0.0
                if until > now:
                    data["tasks"][f"{aid}:{tid}"] = dict(rec)
            for (aid, tid), rec in self._pending_submissions.items():
                since = _as_float(rec.get("since")) or now
                if now - since <= 3 * 86400:      # 超过 3 天的待提交视为陈旧
                    data["pending"][f"{aid}:{tid}"] = dict(rec)
        try:
            persist.save_cooldowns(data, self._data_dir)
        except Exception:
            pass

    def _active_cred_id(self) -> Optional[int]:
        """当前引擎活跃 Token 对应的 cred_id（用于归属请求冷却）。"""
        store = self._get_store()
        with self._lock:
            token = self._token
        if not store or not token:
            return None
        try:
            for rec in store.list_all():
                cid = int(rec.get("id"))
                if store.get_token(cid) == token:
                    return cid
        except Exception:
            return None
        return None

    def _set_request_cooldown(self, cred_id: Any, limits: dict,
                              source: str = "claim_limits",
                              persist: bool = True) -> None:
        if not isinstance(limits, dict):
            return
        try:
            cid = int(cred_id)
        except Exception:
            return
        now = time.time()
        server_ms = _as_float(limits.get("serverNowMs"))
        next_ms = _as_float(limits.get("nextAllowedAtMs"))
        retry = _as_float(limits.get("retryAfterSeconds")) or 0.0
        if next_ms and server_ms:
            until = now + max(0.0, (next_ms - server_ms) / 1000.0)
        else:
            until = now + max(0.0, retry)
        rec = {
            "until": until,
            "set_at": now,
            "seconds": max(retry, until - now),
            "pending_count": limits.get("pendingCount"),
            "pending_limit": limits.get("pendingLimit"),
            "pending_unlimited": limits.get("pendingUnlimited"),
            "recent_count": limits.get("recentCount"),
            "window_limit": limits.get("windowLimit"),
            "cooldown_seconds": limits.get("cooldownSeconds"),
            "reason": limits.get("reason"),
            "source": source,
        }
        with self._lock:
            self._request_cooldowns[cid] = rec
        if persist:
            self._save_cooldowns_state()

    def _set_task_cooldown(self, account_id: Any, task_id: Any, seconds: Any,
                           task_type: str = "", cred_id: Any = None,
                           source: str = "claim", persist: bool = True) -> None:
        try:
            aid = int(account_id)
            tid = int(task_id)
            secs = float(seconds)
        except Exception:
            return
        if secs <= 0:
            return
        now = time.time()
        rec = {
            "until": now + secs,
            "set_at": now,
            "seconds": secs,
            "task_type": str(task_type or ""),
            "cred_id": int(cred_id) if cred_id is not None else None,
            "source": str(source or "claim"),
        }
        with self._lock:
            self._task_cooldowns[(aid, tid)] = rec
        if persist:
            self._save_cooldowns_state()

    def _set_pending(self, account_id: Any, task_id: Any, task_type: str = "",
                     cred_id: Any = None, source: str = "claim",
                     status: str = "claimed", persist: bool = True) -> None:
        try:
            aid = int(account_id)
            tid = int(task_id)
        except Exception:
            return
        now = time.time()
        rec = {
            "since": now,
            "task_type": str(task_type or ""),
            "cred_id": int(cred_id) if cred_id is not None else None,
            "source": str(source or "claim"),
            "status": str(status or "claimed"),
        }
        with self._lock:
            self._pending_submissions[(aid, tid)] = rec
        if persist:
            self._save_cooldowns_state()

    def _clear_pending(self, account_id: Any, task_id: Any,
                       persist: bool = True) -> None:
        try:
            key = (int(account_id), int(task_id))
        except Exception:
            return
        with self._lock:
            existed = self._pending_submissions.pop(key, None)
        if existed is not None and persist:
            self._save_cooldowns_state()

    def _claim_interval_days(self, task_id: Any) -> float:
        """任务定义的领取间隔天数；缺失时回退 3 天。"""
        try:
            tid = int(task_id)
        except Exception:
            tid = None
        with self._lock:
            task = self._tasks_map.get(tid) if tid is not None else None
        if isinstance(task, dict):
            days = _as_float(task.get("claimIntervalDays"))
            if days and days > 0:
                return days
        return 3.0

    def request_cooldowns(self) -> dict:
        """``{cred_id: {...}}``；含 ``remaining_seconds``，过期也保留 pending 信息。"""
        now = time.time()
        out: dict = {}
        with self._lock:
            for cid, rec in self._request_cooldowns.items():
                item = dict(rec)
                until = _as_float(item.get("until")) or 0.0
                item["remaining_seconds"] = max(0.0, until - now)
                out[str(cid)] = item
        return out

    def task_cooldowns(self) -> list:
        """扁平化的任务冷却列表（同任务 3 天级），过期自动清理。"""
        now = time.time()
        out = []
        with self._lock:
            for (aid, tid), rec in list(self._task_cooldowns.items()):
                until = _as_float(rec.get("until")) or 0.0
                if until <= now:
                    self._task_cooldowns.pop((aid, tid), None)
                    continue
                item = dict(rec)
                item["platform_account_id"] = aid
                item["task_id"] = tid
                item["remaining_seconds"] = max(0.0, until - now)
                out.append(item)
        return out

    def pending_submissions(self) -> list:
        """扁平化的待提交列表（超过 3 天的自动清理）。"""
        now = time.time()
        out = []
        with self._lock:
            for (aid, tid), rec in list(self._pending_submissions.items()):
                since = _as_float(rec.get("since")) or now
                if now - since > 3 * 86400:
                    self._pending_submissions.pop((aid, tid), None)
                    continue
                item = dict(rec)
                item["platform_account_id"] = aid
                item["task_id"] = tid
                out.append(item)
        return out

    def _eligibility_index(self) -> dict:
        """从当前任务快照 + 本地冷却/待提交推导每个子账号×任务类型的状态。

        返回 ``{aid: {task_type: {...}}}``，含：
        ``claimable_task_ids`` / ``cooling_task_ids`` / ``pending_task_ids`` /
        ``next_claimable_at`` / ``reasons``。
        """
        now = time.time()
        out: dict = {}

        def slot(aid: int, ttype: str) -> dict:
            return out.setdefault(str(aid), {}).setdefault(str(ttype or ""), {
                "task_ids": [],
                "claimable_task_ids": [],
                "cooling_task_ids": [],
                "pending_task_ids": [],
                "next_claimable_at": None,
                "reasons": [],
                "live_has_tasks": False,
            })

        with self._lock:
            tasks = list(self._tasks_map.values())
            task_cd = dict(self._task_cooldowns)
            pending = dict(self._pending_submissions)

        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            ttype = str(t.get("taskType") or "")
            elig = t.get("claimEligibility") or {}
            reason = str(elig.get("reason") or "")
            for a in elig.get("accounts") or []:
                if not isinstance(a, dict) or a.get("id") is None:
                    continue
                try:
                    aid = int(a.get("id"))
                except Exception:
                    continue
                s = slot(aid, ttype)
                s["live_has_tasks"] = True
                if tid not in s["task_ids"]:
                    s["task_ids"].append(tid)
                if reason:
                    s["reasons"].append(reason)
                if a.get("canClaim") is True:
                    s["claimable_task_ids"].append(tid)
                nca = _iso_to_epoch(a.get("nextClaimableAt"))
                if nca and nca > now:
                    s["cooling_task_ids"].append(tid)
                    s["next_claimable_at"] = max(s["next_claimable_at"] or 0, nca)
                if reason == "pending_submission":
                    s["pending_task_ids"].append(tid)

        for (aid, tid), rec in task_cd.items():
            until = _as_float(rec.get("until")) or 0.0
            if until <= now:
                continue
            s = slot(aid, str(rec.get("task_type") or ""))
            if tid not in s["task_ids"]:
                s["task_ids"].append(tid)
            if tid not in s["cooling_task_ids"]:
                s["cooling_task_ids"].append(tid)
            s["next_claimable_at"] = max(s["next_claimable_at"] or 0, until)

        for (aid, tid), rec in pending.items():
            since = _as_float(rec.get("since")) or now
            if now - since > 3 * 86400:
                continue
            s = slot(aid, str(rec.get("task_type") or ""))
            if tid not in s["task_ids"]:
                s["task_ids"].append(tid)
            if tid not in s["pending_task_ids"]:
                s["pending_task_ids"].append(tid)

        for by_type in out.values():
            for s in by_type.values():
                for key in ("task_ids", "claimable_task_ids",
                            "cooling_task_ids", "pending_task_ids"):
                    s[key] = list(dict.fromkeys(s[key]))
                s["has_tasks"] = bool(s["task_ids"])
                s["claimable"] = bool(s["claimable_task_ids"])
                s["cooling"] = bool(s["cooling_task_ids"])
                s["pending"] = bool(s["pending_task_ids"])
            # 空 task_type 的本地记录并到第一个真实类型（保底展示）
            if "" in by_type and len(by_type) > 1:
                empty = by_type.pop("")
                for ttype, s in by_type.items():
                    for key in ("task_ids", "cooling_task_ids", "pending_task_ids"):
                        s[key] = list(dict.fromkeys(s[key] + empty.get(key, [])))
                    s["cooling"] = bool(s["cooling_task_ids"])
                    s["pending"] = bool(s["pending_task_ids"])
                    s["has_tasks"] = bool(s["task_ids"])
        return out

    def cooldown_state(self) -> dict:
        """完整冷却/待提交/可领状态（API 与前端核对用）。"""
        return {
            "now": time.time(),
            "request": self.request_cooldowns(),
            "tasks": self.task_cooldowns(),
            "pending": self.pending_submissions(),
            "eligibility": self._eligibility_index(),
            "claim_limits": self._limits_snapshot(),
        }

    def _merge_cooldowns(self, items: Any,
                         cred_id: Any = None) -> list:
        """把冷却/待提交/可领状态并进平台子账号列表。"""
        try:
            cid = int(cred_id) if cred_id is not None else None
        except Exception:
            cid = None
        task_items = self.task_cooldowns()
        pending_items = self.pending_submissions()
        elig = self._eligibility_index()
        req = self.request_cooldowns()
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                out.append(it)
                continue
            rec = dict(it)
            try:
                aid = int(rec.get("id"))
            except Exception:
                aid = None
            if aid is not None:
                key = str(aid)
                rec["task_cooldowns"] = [
                    x for x in task_items if x.get("platform_account_id") == aid]
                rec["pending"] = [
                    x for x in pending_items if x.get("platform_account_id") == aid]
                rec["eligibility"] = elig.get(key, {})
            else:
                rec["task_cooldowns"] = []
                rec["pending"] = []
                rec["eligibility"] = {}
            if cid is not None:
                rec["request_cooldown"] = req.get(str(cid))
            out.append(rec)
        return out

    def sync_claims_for_cred(self, cred_id: Any) -> dict:
        """用该账号 Token 拉一次领取记录，补「任务冷却 / 待提交」。

        用于没有任务下发、或账号在其他工具领取过的情况。低频调用，不参与
        领取关键路径。
        """
        store = self._get_store()
        try:
            cid = int(cred_id)
        except Exception:
            return {"ok": False, "error": "bad_id", "items": 0, "updated": 0}
        token = store.get_token(cid) if store else None
        if not token:
            return {"ok": False, "error": "no_token", "items": 0, "updated": 0}
        status, obj = self._api_call(
            "GET", "/api/user/claims?page=1&pageSize=20",
            token=token, kind="plat")
        if status != 200 or not isinstance(obj, dict):
            return {"ok": False, "error": f"HTTP {status}" if status else "network",
                    "items": 0, "updated": 0}
        data = obj.get("data")
        items = (data.get("items") if isinstance(data, dict) else None) or []
        stats = (data.get("stats") if isinstance(data, dict) else None) or {}
        total = data.get("total") if isinstance(data, dict) else None
        now = time.time()
        updated = 0
        pending_count = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            aid = it.get("platformAccountId")
            tid = it.get("taskId")
            if aid is None or tid is None:
                continue
            ttype = str(it.get("taskType") or "")
            claimed = _iso_to_epoch(it.get("claimedAt"))
            stat = str(it.get("status") or "")
            if claimed:
                days = self._claim_interval_days(tid)
                until = claimed + days * 86400.0
                if until > now:
                    self._set_task_cooldown(aid, tid, until - now, ttype, cid,
                                            source="claim_history", persist=False)
            if stat == "claimed" and (not claimed or now - claimed <= 3 * 86400):
                self._set_pending(aid, tid, ttype, cid,
                                  source="claim_history", status=stat, persist=False)
                pending_count += 1
            elif stat:
                self._clear_pending(aid, tid, persist=False)
            updated += 1
        self._save_cooldowns_state()
        self._store_claims(cid, items, stats, total)
        return {"ok": True, "items": len(items), "updated": updated,
                "pending": pending_count, "error": None,
                "claims": self.claims_snapshot()["by_cred"].get(str(cid))}

    # ========================= 领取记录（任务展示） =========================
    def _normalize_claim(self, item: dict, cred_id: int) -> Optional[dict]:
        """把上游领取记录归一化成对外唯一形状；非 dict / 缺 taskId 返回 None（跳过）。"""
        if not isinstance(item, dict):
            return None
        raw_task_id = item.get("taskId")
        if raw_task_id is None:
            return None
        try:
            task_id: Optional[int] = int(raw_task_id)
        except Exception:
            task_id = None
        try:
            claim_id: Optional[int] = int(item.get("id"))
        except Exception:
            claim_id = None
        try:
            pacct_id: Optional[int] = int(item.get("platformAccountId"))
        except Exception:
            pacct_id = None
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        submission = (item.get("latestSubmission")
                      if isinstance(item.get("latestSubmission"), dict) else None)
        settlement = (item.get("settlement")
                      if isinstance(item.get("settlement"), dict) else None)
        status = str(item.get("status") or "")
        stage, stage_label = claim_stage(status, settlement, submission is not None)
        view_count = submission.get("viewCount") if submission else None
        if isinstance(view_count, bool) or not isinstance(view_count, (int, float)):
            try:
                view_count = int(view_count) if view_count not in (None, "") else None
            except Exception:
                view_count = None
        else:
            view_count = int(view_count)
        try:
            sub_id: Optional[int] = int(submission.get("id")) if submission else None
        except Exception:
            sub_id = None
        return {
            "cred_id": int(cred_id),
            "claim_id": claim_id,
            "task_id": task_id,
            "title": str(task.get("title") or ""),
            "task_type": str(item.get("taskType") or ""),
            "task_type_name": str(item.get("taskTypeName") or ""),
            "platform": str(item.get("platform") or ""),
            "platform_name": str(item.get("platformName") or ""),
            "platform_account_id": pacct_id,
            "account_nickname": str(item.get("accountNickname") or ""),
            "status": status,
            "stage": stage,
            "stage_label": stage_label,
            "active": stage in ACTIVE_STAGE_KEYS,
            "claimed_at": _iso_to_epoch(item.get("claimedAt")),
            "expires_at": _iso_to_epoch(item.get("expiresAt")),
            "submission_id": sub_id,
            "view_count": view_count,
            "settlement_status": str(settlement.get("status") or "") if settlement else "",
            "settlement_amount": str(settlement.get("totalAmount") or "") if settlement else "",
        }

    def _store_claims(self, cid: int, items: Any, stats: Any,
                      total: Any) -> list:
        """把上游 items 归一化后写入 ``self._claims[cid]``（成功路径共用）。"""
        norm = []
        for it in items or []:
            rec = self._normalize_claim(it, cid)
            if rec is not None:
                norm.append(rec)
        with self._lock:
            self._claims[int(cid)] = {
                "fetched_at": time.time(),
                "items": norm,
                "stats": dict(stats) if isinstance(stats, dict) else {},
                "total": total,
            }
            self._claim_errors.pop(int(cid), None)
        return norm

    def fetch_claims(self, cred_id: Any, page_size: int = 20) -> dict:
        """拉一次某账号的领取记录并更新展示缓存。

        失败（无 token / 网络 / 非 200）返回 ``ok=False`` 且**保留上一次成功快照**，
        只在失败时打一条 WARN（成功由周期刷新调用，刷日志会刷屏）。
        """
        store = self._get_store()
        try:
            cid = int(cred_id)
        except Exception:
            return {"ok": False, "error": "bad_id", "items": []}
        token = store.get_token(cid) if store else None
        if not token:
            return {"ok": False, "error": "no_token", "items": []}
        try:
            status, obj = self._api_call(
                "GET", f"/api/user/claims?page=1&pageSize={int(page_size)}",
                token=token, kind="plat")
        except Exception as exc:
            with self._lock:
                self._claim_errors[cid] = f"网络异常：{exc}"
            self._log(f"账号 {cid} 领取记录拉取失败：网络异常", "WARN")
            return {"ok": False, "error": "network", "items": []}
        if status != 200 or not isinstance(obj, dict):
            err = f"HTTP {status}" if status else "network"
            with self._lock:
                self._claim_errors[cid] = err
            self._log(f"账号 {cid} 领取记录拉取失败：{err}", "WARN")
            return {"ok": False, "error": err, "items": []}
        data = obj.get("data")
        items = (data.get("items") if isinstance(data, dict) else None) or []
        stats = (data.get("stats") if isinstance(data, dict) else None) or {}
        total = data.get("total") if isinstance(data, dict) else None
        norm = self._store_claims(cid, items, stats, total)
        return {"ok": True, "items": norm, "stats": stats, "total": total,
                "error": None}

    def claims_snapshot(self) -> dict:
        """所有账号领取记录的聚合快照（便宜：浅拷贝 + 拼接，供 SSE 频繁调用）。"""
        with self._lock:
            by_cred_raw = {str(cid): dict(rec)
                           for cid, rec in self._claims.items()}
            errors = dict(self._claim_errors)
        by_cred: dict = {}
        merged: list = []
        fetched_at: Optional[float] = None
        active_count = 0
        for key, rec in by_cred_raw.items():
            items = list(rec.get("items") or [])
            entry = {
                "fetched_at": rec.get("fetched_at"),
                "stats": dict(rec.get("stats") or {}),
                "items": items,
            }
            by_cred[key] = entry
            fa = rec.get("fetched_at")
            if isinstance(fa, (int, float)) and (fetched_at is None or fa > fetched_at):
                fetched_at = float(fa)
            for it in items:
                if it.get("active"):
                    active_count += 1
            merged.extend(items)
        merged.sort(key=lambda it: (it.get("claimed_at") is None,
                                    -(it.get("claimed_at") or 0.0)))
        return {
            "fetched_at": fetched_at,
            "items": merged,
            "by_cred": by_cred,
            "errors": {str(k): str(v) for k, v in errors.items()},
            "active_count": active_count,
        }

    def _fetch_limits(self, cfg: EngineConfig, kind: str = "limits",
                      cred_id: Any = 0) -> dict:
        token = self._cred_token(cred_id)
        if not token:
            return {}
        status, obj = self._api_call("GET", "/api/user/tasks/claim-limits",
                                     cfg=cfg, kind=kind, token=token,
                                     cred_id=cred_id)
        if status == 401:
            self._relogin_cred(cred_id)
            active = self._active_cred_id()
            if active is None or int(active) == int(cred_id):
                self._pause("token 无效或已过期")
            else:
                self._block_polling(30.0, f"账号 {cred_id} Token 失效",
                                    cred_id=cred_id)
            return {}
        if status == 429:
            self._block_polling(5.0, "配额接口被限流", cred_id=cred_id)
            return {}
        if status == 200 and isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, dict):
                self._store_limits(data, cred_id)
                return data
        return {}

    def _limits_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.2)
                    continue
                now = time.monotonic()
                for cid, _token in self._poll_targets():
                    due = self._lim_next_by_cred.get(int(cid), 0.0)
                    if now < due:
                        continue
                    self._lim_next_by_cred[int(cid)] = now + LIMITS_TTL
                    # 走独立的 limits 桶（非阻塞）：之前借 poll 桶，每 2s 一次
                    # 的配额刷新会把同一个 cred 的轮询令牌吃光
                    self._fetch_limits(self._cfg, kind="limits", cred_id=cid)
                # 用领取记录定期纠正「任务冷却 / 待提交」：启动时只同步一次，
                # 运行期上游状态变化（别处领取、审批通过）本地永远看不到。
                # sync_claims_for_cred 走 plat 非阻塞桶，被跳过也无所谓，下一轮再试。
                for cid, _token in self._poll_targets():
                    cid_i = int(cid)
                    if now < self._claims_sync_next.get(cid_i, 0.0):
                        continue
                    self._claims_sync_next[cid_i] = now + CLAIMS_SYNC_TTL
                    try:
                        self.sync_claims_for_cred(cid_i)
                    except Exception as exc:
                        # 绝不能把异常抛出去，否则整条配额刷新线程会被打断
                        self._cool_log(f"账号 {cid_i} 领取记录同步失败：{exc}", "WARN")
                self._sleep(0.5)
            except Exception as exc:
                self._loop_error("配额刷新", exc)
                self._sleep(1.0)

    # ============================ 平台子账号 ============================
    def platform_accounts(self, platform: Optional[str] = None,
                          token: Optional[str] = None,
                          kind: str = "plat") -> dict:
        """已发现的平台子账号（用于抢任务的 小红书/抖音/快手/微信视频号 等）。

        汇总来源：① 任务 ``claimEligibility.accounts``（带 id，可能无名称）；
        ② 上游 ``{base}/api/user/platform-accounts``（即 ``/pages/accounts/index``
        实际调用的接口，HAR 已确认字段）。``platform`` 为空返回全部；带值时返回
        该平台子集，若上游未标注任何平台归属，则回退返回全部并置 ``fallback=True``。

        ``token``/``kind``：本方法的**上游拉取**用哪个 token、走哪个限流桶；
        缺省（``None``/``plat``）用引擎活跃 token + 独立平台桶，因此不会吃
        抢单配额。
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
        status, upstream, _fallback, plat_err = self._fetch_platform_accounts(
            token=token, kind=kind)
        if plat_err == "rate_limited":
            self._cool_log("平台子账号接口被限流（plat 桶），稍后再试", "WARN")
        elif plat_err == "network":
            self._cool_log(f"平台子账号接口不可用（HTTP {status}），稍后再试", "WARN")
        for u in upstream:
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
        items = self._merge_cooldowns(items)
        return {"items": items, "fallback": fallback}

    @staticmethod
    def _normalize_platform_accounts(obj: Any) -> list:
        """把上游 ``/api/user/platform-accounts`` 的响应体归一化成列表。

        字段对照（来自 ``/pages/accounts/index`` 调用的接口，已在 HAR 中确认）：
          id           平台账号 id
          platform     "xiaohongshu" / "douyin" / "kuaishou" / "wx_channels" 等（小写代码）
          platformName "小红书" / "抖音" / "快手" / "wx视频号"（中文）
          account      账号标识（如绑定的手机号/账号名）
          realName     实名
          verifyStatus "verified" 等
          selfDisabled bool
          activeClaimCount int
        """
        if not isinstance(obj, dict):
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

    def _fetch_platform_accounts(self, token: Optional[str] = None,
                                 kind: str = "plat") -> tuple:
        """单次拉取上游平台子账号 → ``(status, items, fallback, error)``。

        ``status``：0=网络错/限流/未配置 token，401=token 失效，其它=上游原始
        HTTP 状态码。``error`` 为细分原因（``network`` / ``rate_limited`` /
        ``no_token`` / ``""``）。``fallback`` 表示上游返回的条目没有任何
        ``platform`` 归属标记（此时无法按平台过滤）。返回值**不含**共享可变
        状态，可并发调用。
        """
        try:
            status, obj = self._api_call("GET", "/api/user/platform-accounts",
                                         cfg=self._cfg, kind=kind, token=token)
        except Exception:
            return (0, [], False, "network")
        if status == 0:
            if isinstance(obj, dict) and obj.get("_rate_limited"):
                return (0, [], False, "rate_limited")
            if isinstance(obj, dict) and obj.get("_error") == "未配置 token":
                return (0, [], False, "no_token")
            return (0, [], False, "network")
        if status != 200:
            return (status, [], False, "")
        items = self._normalize_platform_accounts(obj)
        fallback = bool(items) and not any(it.get("platform") for it in items)
        return (200, items, fallback, "")

    def _fetch_upstream_platform_accounts(self, token: Optional[str] = None,
                                          kind: str = "plat") -> list:
        """最佳努力拉取上游平台账号；任何异常/非 200 都返回空列表。"""
        status, items, _fallback, _err = self._fetch_platform_accounts(
            token=token, kind=kind)
        if status != 200:
            return []
        return items

    # ---------------- 按登录账号拉取 / 缓存平台子账号 ----------------
    def attach_credentials(self, store: "CredentialStore") -> None:
        """由 app 注入凭据存储（未注入时按 data_dir 懒建）。"""
        with self._lock:
            self._store = store

    def attach_relogin(self, fn: Optional[Callable[[int], Any]]) -> None:
        """注入「用凭据静默重登」回调：``fn(cred_id) -> dict``，必须不切活跃 token。"""
        with self._lock:
            self._relogin_fn = fn

    def _get_store(self) -> "CredentialStore":
        store = self._store
        if store is not None:
            return store
        with self._lock:
            if self._store is None:
                self._store = CredentialStore(self._data_dir)
            return self._store

    def _cred_records(self) -> list:
        try:
            return [r for r in self._get_store().list_all() if isinstance(r, dict)]
        except Exception:
            return []

    def _poll_targets(self) -> list:
        """本轮轮询的登录账号：``[(cred_id, token), ...]``。

        有凭据 Token 时全部参与；另外把「活跃 Token 不属于任何凭据」的情况
        作为 ``cred_id=0`` 的虚拟账号加入（兼容旧用法/测试）。
        """
        out: list = []
        seen_tokens: set = set()
        for rec in self._cred_records():
            try:
                cid = int(rec.get("id"))
                tok = self._get_store().get_token(cid)
            except Exception:
                continue
            if tok:
                out.append((cid, tok))
                seen_tokens.add(tok)
        with self._lock:
            active = self._token
        if active and active not in seen_tokens:
            out.append((0, active))
        return out

    def _cred_token(self, cred_id: Any) -> Optional[str]:
        try:
            cid = int(cred_id)
        except Exception:
            return None
        if cid == 0:
            with self._lock:
                return self._token
        try:
            return self._get_store().get_token(cid)
        except Exception:
            return None

    def _get_limiter(self, kind: str, cred_id: Any,
                     scope: str = "") -> RateLimiter:
        """按 cred 取（必要时创建）限速桶。

        ``kind`` = ``claim``（领取）/ ``limits``（claim-limits 后台刷新）/
        ``poll``（任务列表轮询）。``scope``：轮询桶再按任务类型细分——默认
        两个任务类型线程共用同一个 cred 轮询桶（容量 5/1s），需求却是
        ~10 req/s，约一半轮次白丢；按 (cred, 任务类型) 隔离后各用各的。
        """
        try:
            cid = int(cred_id) if cred_id is not None else 0
        except Exception:
            cid = 0
        if kind == "limits":
            # 配额刷新与轮询/领取完全隔离，用平台子账号那档小容量桶
            with self._lock:
                limiter = self._limits_limiters.get(cid)
                if limiter is None:
                    limiter = RateLimiter(PLAT_RATE_MAX, PLAT_RATE_WINDOW)
                    self._limits_limiters[cid] = limiter
                return limiter
        if kind == "claim":
            with self._lock:
                bucket = self._claim_limiters
                limiter = bucket.get(cid)
                if limiter is None:
                    limiter = RateLimiter(self._cfg.rate_limit_max,
                                          self._cfg.rate_limit_window)
                    bucket[cid] = limiter
                return limiter
        key = (cid, str(scope or ""))
        with self._lock:
            bucket = self._poll_limiters
            limiter = bucket.get(key)
            if limiter is None:
                limiter = RateLimiter(self._cfg.poll_rate_max,
                                      self._cfg.poll_rate_window)
                bucket[key] = limiter
            return limiter

    def _task_cred_for_account(self, account_id: Any) -> int:
        """平台子账号归属的 cred；查不到时回退活跃 cred / 0。"""
        owner = self.owner_of_platform_account(account_id)
        if owner is not None:
            return int(owner)
        active = self._active_cred_id()
        return int(active) if active is not None else 0

    def _load_platform_cache(self) -> dict:
        try:
            return persist.load_platform_cache(self._data_dir)
        except Exception:
            return {}

    def _refresh_pacct_index(self, cache: Optional[dict] = None) -> None:
        """重建 ``account_id -> cred_id`` 索引（加载缓存或写完缓存后调用）。"""
        data = cache if cache is not None else self._load_platform_cache()
        index: dict = {}
        meta: dict = {}
        order: dict = {}
        rank = 0
        for cid, rec in (data or {}).items():
            if not isinstance(rec, dict):
                continue
            try:
                owner = int(cid)
            except Exception:
                continue
            for item in rec.get("items") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    aid = int(item.get("id"))
                except Exception:
                    continue
                index.setdefault(aid, owner)
                meta.setdefault(aid, dict(item))
                # 「排序靠前」名次：按平台子账号列表（登录账号 → 子账号）的展示顺序
                if aid not in order:
                    order[aid] = rank
                    rank += 1
        with self._lock:
            self._pacct_owner = index
            self._pacct_meta = meta
            self._pacct_order = order
            self._pacct_loaded = True

    def _ensure_pacct_index(self) -> None:
        with self._lock:
            loaded = self._pacct_loaded
        if not loaded:
            self._refresh_pacct_index()

    def refresh_platform_index(self) -> dict:
        """外部改了缓存文件后强制重建索引；返回索引副本。"""
        self._refresh_pacct_index()
        with self._lock:
            return dict(self._pacct_owner)

    def owner_of_platform_account(self, account_id: Any) -> Optional[int]:
        """平台子账号归属的登录账号 id（缓存里查不到 → None）。"""
        try:
            aid = int(account_id)
        except Exception:
            return None
        self._ensure_pacct_index()
        with self._lock:
            return self._pacct_owner.get(aid)

    def _owner_of_platform_account(self, account_id: Any) -> Optional[int]:
        return self.owner_of_platform_account(account_id)   # 内部别名

    def _token_for_platform_account(self, account_id: Any) -> Optional[str]:
        """抢某平台子账号时应当使用的 token（= 它所属登录账号的 token）。

        查不到归属或该账号没有 token → ``None``（调用方回退到活跃 token）。
        """
        owner = self.owner_of_platform_account(account_id)
        if owner is None:
            return None
        token = self._get_store().get_token(owner)
        return token or None

    def _save_pacct_cache_entry(self, cred_id: int, items: list,
                                fallback: bool, fetched_at: float) -> None:
        cache = self._load_platform_cache()
        cache[int(cred_id)] = {
            "fetched_at": float(fetched_at),
            "fallback": bool(fallback),
            "items": list(items or []),
        }
        try:
            persist.save_platform_cache(cache, self._data_dir)
        except Exception:
            pass   # 落盘失败不影响本次返回
        self._refresh_pacct_index(cache)

    def _relogin_cred(self, cred_id: int) -> bool:
        """静默重登某个 cred（activate=False 语义），成功返回 True。

        绝不调用 ``set_token()``：临时 token 只走 ``_api_call(token=...)``。
        """
        with self._lock:
            fn = self._relogin_fn
        if fn is None:
            return False
        try:
            result = fn(int(cred_id))
        except Exception:
            return False
        if isinstance(result, dict):
            return bool(result.get("ok"))
        return False

    def platform_accounts_for_cred(self, cred_id: Any, refresh: bool = False) -> dict:
        """取某个登录账号名下的**全部**平台子账号（带缓存 + 自动重登）。

        - ``refresh=False`` 且有缓存 → 直接返回缓存（附 ``fetched_at/stale``）；
        - ``refresh=True`` → 上游调用**一次**（不带 platform 过滤）并写缓存；
        - 无 token → ``{"error": "no_token", "items": []}``（不算异常）；
        - 401 → 用登录能力静默重登该 cred **一次** → 刷新 token → 重试一次，
          仍失败 → ``{"error": "token_expired"}`` 并记 ``last_platform_error``；
        - 网络/5xx/限流 → ``{"error": "network"|"rate_limited"}`` 并记错误；
        - 成功 → 清空该账号的 ``last_platform_error``。

        任何分支都**不使用** ``engine.set_token()``，因此不会影响正在运行的
        引擎，也不会覆盖 ``token.json``。
        """
        store = self._get_store()
        try:
            cid = int(cred_id)
        except Exception:
            return {"error": "no_token", "items": []}

        cache = self._load_platform_cache()
        cached = cache.get(cid)
        if not refresh and isinstance(cached, dict):
            return {
                "items": self._merge_cooldowns(cached.get("items") or [], cid),
                "fallback": bool(cached.get("fallback")),
                "fetched_at": cached.get("fetched_at"),
                "stale": self._pacct_stale(cached.get("fetched_at")),
            }

        token = store.get_token(cid)
        if not token:
            return {"error": "no_token", "items": []}

        status, items, fallback, err = self._fetch_platform_accounts(token=token)
        if status == 401:
            if self._relogin_cred(cid):
                token = store.get_token(cid)
                if token:
                    status, items, fallback, err = self._fetch_platform_accounts(
                        token=token)
            if status == 401 or not token:
                store.mark_platform_error(cid, "token 已过期")
                return {"error": "token_expired", "items": []}
        if status != 200:
            if status == 0:
                reason = err if err in ("rate_limited", "no_token") else "network"
            elif status >= 500:
                reason = "network"          # 上游 5xx 按网络故障处理
            else:
                reason = f"upstream_http_{status}"
            store.mark_platform_error(cid, reason)
            return {"error": reason, "items": []}

        fetched_at = time.time()
        self._save_pacct_cache_entry(cid, items, fallback, fetched_at)
        try:
            store.clear_platform_error(cid)
        except Exception:
            pass
        return {"items": self._merge_cooldowns(items, cid), "fallback": bool(fallback),
                "fetched_at": fetched_at, "stale": False}

    @staticmethod
    def _pacct_stale(fetched_at: Any) -> bool:
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            return True
        return (time.time() - float(fetched_at)) > PLATFORM_CACHE_STALE_SECONDS

    # ============================ 轮询 ============================
    def _poller_loop(self, cred_id: Any, task_type: str,
                     stop_ev: threading.Event) -> None:
        """轮询循环。``cred_id=None`` = 单账号轮询模式，每轮动态挑一个登录账号。"""
        tag = "single" if cred_id is None else cred_id
        self._log(f"启动轮询线程：cred={tag} type={task_type}", "INFO")
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
                if cred_id is None:
                    cid = self._pick_poller_cred()
                    if cid is None:
                        self._sleep(0.5, stop_ev)   # 还没有可用账号
                        continue
                else:
                    cid = int(cred_id)
                now_mono = time.monotonic()
                with self._lock:
                    block_until = max(float(self._poll_block_until),
                                      float(self._poll_block_until_by_cred.get(int(cid), 0.0)))
                if now_mono < block_until:
                    self._sleep(0.2, stop_ev)
                    continue
                self._poll_once(cid, task_type, cfg)
                self._sleep(cfg.poll_interval, stop_ev)
            except Exception as exc:                 # 线程不死：日志+退避续跑
                self._loop_error(f"轮询[{tag}:{task_type}]", exc)
                self._sleep(1.0, stop_ev)

    def _pick_poller_cred(self) -> Optional[int]:
        """单账号轮询：挑出「这一轮该由哪个登录账号刷列表」。

        规则（任何时刻只有一个账号在刷）：
        1. 优先沿用当前轮询账号；
        2. 超过 ``poll_rotate_seconds`` 或当前账号被限流阻塞 / Token 失效被摘掉时，
           轮转到下一个可用账号 —— 保证每个账号的任务迟早都能被看到，又不用同时刷；
        3. ``poll_rotate_seconds=0`` 表示固定不换（最省请求）。
        """
        cids: list = []
        for cid, _tok in self._poll_targets():
            try:
                value = int(cid)
            except Exception:
                continue
            if value not in cids:
                cids.append(value)
        if not cids:
            return None
        now = time.monotonic()
        with self._lock:
            cur = self._poll_cred
            cur_at = float(self._poll_cred_at or 0.0)
            blocked = {c: float(self._poll_block_until_by_cred.get(c, 0.0))
                       for c in cids}
            global_block = float(self._poll_block_until or 0.0)
        usable = [c for c in cids if blocked.get(c, 0.0) <= now]
        if not usable:
            # 全被阻塞：留着当前账号，让 _poller_loop 的阻塞判断去等待
            return cur if cur in cids else cids[0]
        rotate = float(self._cfg.poll_rotate_seconds or 0.0)
        if cur in usable and (rotate <= 0 or now - cur_at < rotate):
            return cur
        if global_block > now and cur in usable:
            return cur
        start = cids.index(cur) if cur in cids else -1
        pick = None
        for step in range(1, len(cids) + 1):
            cand = cids[(start + step) % len(cids)]
            if cand in usable:
                pick = cand
                break
        if pick is None:
            pick = usable[0]
        with self._lock:
            self._poll_cred = pick
            self._poll_cred_at = now
        return pick

    def _poll_once(self, cred_id: Any, task_type: str,
                   cfg: EngineConfig, allow_idle: bool = False) -> int:
        """轮询一个登录账号的一种任务类型；默认只取列表最后 N 页（最新）。

        返回本轮成功入库的页数（HTTP 200 的页数）。``allow_idle=True`` 时
        即使引擎处于停止态也继续拉完（供手动刷新用）；默认行为不变，轮询
        线程仍受 ``_is_active`` 控制。
        """
        cid = int(cred_id)
        key = (cid, str(task_type))
        ingested_pages = 0
        state = self._poll_state.setdefault(key, {
            "total": None, "page_size": None, "last_total": 0.0,
            "backfilled": False,
        })
        now = time.monotonic()
        page_size = int(state.get("page_size") or cfg.page_size)
        total = state.get("total")
        refresh = (now - float(state.get("last_total") or 0)
                   >= float(cfg.total_refresh_seconds))
        fetched: set = set()
        page1_items = None
        if refresh or not total:
            status, items, meta = self._get_tasks(
                cid, task_type, 1, cfg, page_size)
            if status == -1:
                self._bump("skipped")
                return 0
            if status == 401:
                self._poll_auth_failed(cid)
                return 0
            self._bump("polls")
            if status == 200:
                page1_items = items
                fetched.add(1)
                state["last_total"] = now
                if meta.get("total") is not None:
                    state["total"] = int(meta["total"])
                if meta.get("page_size"):
                    state["page_size"] = int(meta["page_size"])
                total = state.get("total")
                page_size = int(state.get("page_size") or page_size)
                self._clear_error()

        last_page = 1
        if total and page_size:
            last_page = max(1, (int(total) + page_size - 1) // page_size)

        tail = max(1, int(cfg.tail_pages))
        if not state.get("backfilled"):
            # 最新页优先（最后一页最新），再回补旧页
            pages = list(range(last_page, 0, -1))
            state["backfilled"] = True
        else:
            pages = list(range(last_page, max(1, last_page - tail + 1) - 1, -1))

        for page in pages:
            if page == 1 and page1_items is not None:
                # page1 是最旧的一页，放在尾页之后入库，避免先抢旧任务
                self._ingest(task_type, page1_items, cid,
                             (int(cfg.page_size) if cfg.page_size else 0))
                continue
            if page in fetched:
                continue
            if not allow_idle and not self._is_active():
                return ingested_pages
            status, items, meta = self._get_tasks(
                cid, task_type, page, cfg, page_size)
            if status == -1:
                # 尾页被限流桶跳过只丢这一页，不能整轮 return：否则一旦某页
                # 拿不到令牌，本轮后面所有页（含最新页）全都不入库
                self._bump("skipped")
                continue
            if status == 401:
                self._poll_auth_failed(cid)
                return ingested_pages
            self._bump("polls")
            if status == 200:
                self._ingest(task_type, items, cid, (page - 1) * page_size)
                ingested_pages += 1
                if meta.get("total") is not None:
                    state["total"] = int(meta["total"])
                self._clear_error()

        # 常规尾页轮询不含 page1：把校准用的 page1 放到最后入库
        if page1_items is not None and 1 not in pages:
            self._ingest(task_type, page1_items, cid,
                         (int(cfg.page_size) if cfg.page_size else 0))
            ingested_pages += 1
        return ingested_pages

    def refresh_tasks_once(self, timeout: float = 25.0) -> dict:
        """手动拉一次任务列表：对每个登录账号 × 每种任务类型各跑一轮。

        与轮询线程共用 ``_ingest``（自带锁），这里不再包大锁；只用一个独立
        布尔标志防两次手动刷新并发。引擎停止时也能跑完（``allow_idle=True``）。
        整体受 ``timeout`` 秒上限保护，超时就把已完成的部分照常入库并返回。
        """
        started = time.monotonic()
        with self._refresh_lock:
            if self._refresh_inflight:
                return {"ok": False, "accounts": 0, "types": [],
                        "fetched_pages": 0, "tasks": len(self.tasks().get("items") or []),
                        "errors": ["已有一次手动刷新在进行中，请稍候"],
                        "elapsed": round(time.monotonic() - started, 3)}
            self._refresh_inflight = True
        try:
            return self._refresh_tasks_once_locked(timeout, started)
        finally:
            with self._refresh_lock:
                self._refresh_inflight = False

    def _refresh_tasks_once_locked(self, timeout: float, started: float) -> dict:
        cfg = self._cfg
        types = [str(t) for t in (cfg.task_types or ())]
        targets = self._poll_targets()
        errors: list = []
        fetched_pages = 0
        if not targets:
            self._log("手动拉取任务列表：没有可用的 Token", "WARN")
            return {"ok": False, "accounts": 0, "types": types,
                    "fetched_pages": 0,
                    "tasks": len(self.tasks().get("items") or []),
                    "errors": ["没有可用的 Token，先在账号里取 Token"],
                    "elapsed": round(time.monotonic() - started, 3)}
        for cid, _tok in targets:
            for task_type in types:
                if time.monotonic() - started >= float(timeout):
                    errors.append(f"账号 #{cid}：手动刷新超时（>{timeout:.0f}s），已停止继续拉取")
                    break
                try:
                    fetched_pages += int(self._poll_once(cid, task_type, cfg,
                                                         allow_idle=True) or 0)
                except Exception as exc:
                    errors.append(f"账号 #{cid}（{task_type}）：{exc}")
                    self._log(f"手动拉取任务列表：账号 #{cid} 类型 {task_type} 失败：{exc}",
                              "WARN")
            else:
                continue
            break
        self._flush_tasks_emit()
        elapsed = round(time.monotonic() - started, 3)
        task_count = len(self.tasks().get("items") or [])
        ok = fetched_pages > 0 or not errors
        self._log(f"手动拉取任务列表：账号 {len(targets)} 个 × 类型 {len(types)} 种，"
                  f"成功入库 {fetched_pages} 页，用时 {elapsed:.1f}s", "INFO")
        for err in errors:
            self._log(f"手动拉取任务列表：{err}", "WARN")
        return {"ok": ok, "accounts": len(targets), "types": types,
                "fetched_pages": fetched_pages, "tasks": task_count,
                "errors": errors, "elapsed": elapsed}

    # ============================ 入库 / 入队 ============================
    def _pick_account(self, item: dict) -> Optional[int]:
        """在任务的可领取账号中按轮转顺序选一个（**历史遗留，不参与调度**）。

        实际的账号选择见 ``_schedule_once()``：单任务用「排序靠前」的账号，
        多任务让每个账号分别领不同任务，重试沿用上次的账号。这里只保留给
        旧调用方 / 测试用，语义与调度器不同，不要拿它推断线上行为。
        """
        accounts = (item.get("claimEligibility") or {}).get("accounts") or []
        ids: list = []
        for a in accounts:
            raw = a.get("id") if isinstance(a, dict) else a
            try:
                aid = int(raw)
            except Exception:
                continue
            if aid not in ids:
                ids.append(aid)
        if not ids:
            return None
        with self._lock:
            start = self._account_cursor
            self._account_cursor = (start + 1) % len(ids)
        return ids[start % len(ids)]

    def _merge_task_item(self, old: Optional[dict], item: dict,
                         cred_id: int) -> dict:
        """合并同一任务在不同登录账号视图下的数据（账号集合取并集）。"""
        if not isinstance(old, dict):
            return dict(item)
        merged = dict(old)
        for key, value in item.items():
            if key == "claimEligibility":
                continue
            if value is not None or key not in merged:
                merged[key] = value
        old_ce = old.get("claimEligibility") or {}
        new_ce = item.get("claimEligibility") or {}
        accounts: dict = {}
        for a in (old_ce.get("accounts") or []):
            if isinstance(a, dict) and a.get("id") is not None:
                try:
                    accounts[int(a.get("id"))] = a
                except Exception:
                    pass
        for a in (new_ce.get("accounts") or []):
            if isinstance(a, dict) and a.get("id") is not None:
                try:
                    accounts[int(a.get("id"))] = a
                except Exception:
                    pass
        ce = dict(new_ce)
        ce["accounts"] = list(accounts.values())
        ce["canClaim"] = bool(old_ce.get("canClaim") or new_ce.get("canClaim"))
        if not ce.get("reason") and old_ce.get("reason"):
            ce["reason"] = old_ce.get("reason")
        merged["claimEligibility"] = ce
        return merged

    def _ingest(self, task_type: str, items: list, cred_id: Any = 0,
                base_rank: int = 0) -> None:
        """合并任务快照到任务池；调度由 ``_scheduler_loop`` 统一做。"""
        # monotonic 只给内部 TTL / 清理用（_task_first_seen / _task_last_seen 等）；
        # 对外字段（_tasks_ts / _last_poll_at）必须是 epoch 秒，否则前端按
        # ``new Date(ts*1000)`` 解析会显示成 1970 年。
        now = time.monotonic()
        now_epoch = time.time()
        cid = int(cred_id) if cred_id is not None else 0
        with self._lock:
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                tid = item.get("id")
                if tid is None:
                    continue
                merged = self._merge_task_item(self._tasks_map.get(tid), item, cid)
                self._tasks_map[tid] = merged
                self._task_first_seen.setdefault(tid, now)
                self._task_last_seen[tid] = now
                self._task_sources.setdefault(tid, set()).add(cid)
                self._task_types_map[tid] = str(task_type)
            self._tasks_ts = now_epoch
            self._last_poll_at = now_epoch
            self._pool_dirty = True          # 池内容变了，调度排序缓存失效
            snapshot = list(self._tasks_map.values())
            # 快照广播节流：一个轮询线程每次 _ingest 都全量广播，多任务类型 ×
            # 多账号时前端/WebSocket 会被刷爆；同一 200ms 窗口内只发最后一次
            if now_epoch - self._tasks_emit_at >= TASKS_EMIT_MIN_INTERVAL:
                self._tasks_emit_at = now_epoch
                self._tasks_emit_pending = False
                emit_now = True
            else:
                self._tasks_emit_pending = True
                emit_now = False
        if emit_now:
            self._emit_tasks(now_epoch, snapshot)

    def _flush_tasks_emit(self) -> None:
        """把节流期间挂起的任务快照补发一次（最多 ``TASKS_EMIT_MIN_INTERVAL`` 一次）。

        没有这一步，最后一次 ``_ingest`` 被节流掉后前端会停在旧快照上，
        直到下一轮轮询才有更新。
        """
        now_epoch = time.time()
        with self._lock:
            if not self._tasks_emit_pending:
                return
            if now_epoch - self._tasks_emit_at < TASKS_EMIT_MIN_INTERVAL:
                return
            self._tasks_emit_at = now_epoch
            self._tasks_emit_pending = False
            snapshot = list(self._tasks_map.values())
        self._emit_tasks(now_epoch, snapshot)

    def _gc_state(self, mono: float) -> None:
        """任务池内存治理：退役下架任务 + 按 TTL 清理防重抢集合。

        任务池此前只增不减（``_tasks_map`` / first_seen / last_seen / sources /
        types / attempts / retry_at 全都没有删除路径），长时间运行会无限增长。
        这里最多每 ``GC_INTERVAL`` 秒执行一次，代价可控。
        """
        with self._lock:
            if mono - self._gc_at < GC_INTERVAL:
                return
            self._gc_at = mono
            # 1) 退役：长时间没再被任何账号刷到，且没有被调度中的任务
            retired = [tid for tid, seen in list(self._task_last_seen.items())
                       if mono - float(seen or mono) > TASK_RETIRE_SECONDS
                       and tid not in self._task_assigned]
            for tid in retired:
                self._tasks_map.pop(tid, None)
                self._task_first_seen.pop(tid, None)
                self._task_last_seen.pop(tid, None)
                self._task_sources.pop(tid, None)
                self._task_types_map.pop(tid, None)
                self._task_attempts.pop(tid, None)
                self._task_retry_at.pop(tid, None)
                self._sched_log_next.pop(tid, None)
            # 2) TTL 清理：防重抢只需记住「最近」抢过的，超出 TTL 一律丢弃
            for tid, at in list(self._task_dead.items()):
                if mono - float(at or 0.0) > TASK_DEAD_TTL:
                    self._task_dead.pop(tid, None)
            for tid, at in list(self._claimed.items()):
                if mono - float(at or 0.0) > CLAIMED_TTL:
                    self._claimed.pop(tid, None)
            if retired:
                self._pool_dirty = True      # 池变小了，排序缓存必须重建
                snapshot = list(self._tasks_map.values())
                now_epoch = time.time()
                self._tasks_emit_at = now_epoch
                self._tasks_emit_pending = False
            else:
                snapshot = None
                now_epoch = 0.0
        if retired:
            self._emit_tasks(now_epoch, snapshot)
        self._flush_tasks_emit()

    def _ensure_scheduler_thread(self) -> None:
        with self._lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            stop_ev = threading.Event()
            thread = threading.Thread(target=self._scheduler_loop,
                                      args=(stop_ev,),
                                      name="sniper-scheduler", daemon=True)
            self._scheduler_thread = thread
            self._scheduler_stop = stop_ev
        thread.start()

    def _scheduler_loop(self, stop_ev: threading.Event) -> None:
        while not self._stop.is_set() and not stop_ev.is_set():
            try:
                if not self._is_active():
                    self._sleep(0.1, stop_ev)
                    continue
                self._schedule_once()
                self._sleep(0.05, stop_ev)
            except Exception as exc:
                self._loop_error("调度", exc)
                self._sleep(0.3, stop_ev)

    def _account_rank(self, aid: int) -> int:
        """平台子账号的「排序靠前」名次（越小越靠前）。

        依据平台子账号列表（登录账号 → 子账号）的展示顺序；列表里没有的账号
        排到最后，再按账号 id 兜底排序。
        """
        with self._lock:
            rank = self._pacct_order.get(int(aid))
        if rank is None:
            return 10 ** 6 + int(aid)
        return int(rank)

    def _account_available(self, aid: int, task_id: Any, task_type: str,
                           now_epoch: float, task_platform: str = "") -> tuple:
        """返回 (是否可用, cred_id)。"""
        with self._lock:
            meta = self._pacct_meta.get(aid) or {}
            inflight = int(self._account_inflight.get(aid, 0))
            # 只看「这个子账号 × 这个任务」自己的待提交：本地曾用「任一待提交
            # 即冻结整号」（比上游严格），实际效果是每个子账号领一单就停摆，
            # 后面的任务全被卡住。上游真实语义是按任务维度的。
            same_pending = (task_id is not None
                            and (aid, int(task_id)) in self._pending_submissions)
            cd = self._task_cooldowns.get((aid, int(task_id))) if task_id is not None else None
        meta_platform = str(meta.get("platform") or "").lower()
        if task_platform and meta_platform and task_platform.lower() != meta_platform:
            return (False, None)
        if meta.get("selfDisabled"):
            return (False, None)
        verify = str(meta.get("verifyStatus") or "").lower()
        if verify and verify != "verified":
            return (False, None)
        if inflight >= max(1, int(self._cfg.per_account_inflight)):
            return (False, None)
        if same_pending:
            return (False, None)
        if isinstance(cd, dict):
            until = _as_float(cd.get("until")) or 0.0
            if until > now_epoch:
                return (False, None)
        # 归属未知时不能静默回退到「活跃 Token」：多账号并存时会用错账号去领，
        # 上游只会回 403/409（看起来像「被抢走」），问题极难定位。
        owner = self.owner_of_platform_account(aid)
        if owner is None and len(self._cred_records()) > 1:
            self._cool_log(f"平台子账号 {aid} 归属未知，需先刷新平台子账号再抢", "WARN")
            return (False, None)
        cid = self._task_cred_for_account(aid)
        if not self._cred_token(cid):
            return (False, None)
        with self._lock:
            next_at = max(float(self._next_claim_at_by_cred.get(cid, 0.0)),
                          float((self._request_cooldowns.get(cid) or {}).get("until") or 0.0))
        if next_at > now_epoch:
            return (False, None)
        limits = self._limits_snapshot(cid)
        if limits:
            try:
                pending_count = int(limits.get("pendingCount") or 0)
                pending_limit = limits.get("pendingLimit")
                if pending_limit is not None and pending_count >= int(pending_limit):
                    return (False, None)
            except Exception:
                pass
        return (True, cid)

    def _task_priority(self, task_id: Any) -> float:
        """小顶堆优先级：数值越小越先抢。最新任务 → 更小的负数。"""
        if self._cfg.newest_first:
            with self._lock:
                item = self._tasks_map.get(task_id) or {}
                first = float(self._task_first_seen.get(task_id) or 0.0)
            ts = _iso_to_epoch(item.get("createdAt"))
            return -(ts if ts else first)
        with self._lock:
            return float(self._task_first_seen.get(task_id) or 0.0)

    def _schedule_once(self) -> None:
        """扫描任务池，按最新优先分配给可用平台账号。

        账号选择策略（用户约定）：

        - **同一时间只有一个任务下发** → 用「排序靠前」的账号领取（平台子账号
          列表里越靠前的越优先；上游 ``claimEligibility.accounts`` 顺序作为
          次级依据）。
        - **多个任务同时下发** → 每个账号分别领取不同任务：同一轮调度里已经
          分配过的账号不再重复分配，避免多个账号挤同一个任务、也避免一个账号
          连着抢多单导致另一个账号闲下来（上游配额只有 1，撞车就是 409 浪费）。
        - 同一任务全局只分配给一个账号（``_task_assigned``），天然不会有两个
          账号同时抢同一个任务。
        """
        # 先做任务池治理（内部 5s 节流）：下架任务退役 + 防重抢集合 TTL 清理
        self._gc_state(time.monotonic())
        workers = max(1, len(self._claim_threads))
        with self._lock:
            active = len(self._task_assigned)
        if active >= workers:
            # 领取线程已排满：不调度，但把被节流的任务快照补发出去
            self._flush_tasks_emit()
            return
        with self._lock:
            tasks = list(self._tasks_map.values())
            now_epoch = time.time()
            mono = time.monotonic()
            assigned = set(self._task_assigned)
            dead = set(self._task_dead)
            claimed = set(self._claimed)
            retry_at = dict(self._task_retry_at)
            last_account = dict(self._task_last_account)

        # 按最新 createdAt / first_seen 排序；同一任务只分配一次
        def sort_key(t: dict) -> tuple:
            tid = t.get("id")
            ts = _iso_to_epoch(t.get("createdAt"))
            if ts is None:
                with self._lock:
                    ts = self._task_first_seen.get(tid, 0.0)
            # 越新越靠前（createdAt 大 → 负数小）
            return (-float(ts or 0.0), str(tid))

        # 只有池子变过（_ingest / GC 置 _pool_dirty）才重新全表排序；本函数
        # 每 50ms 跑一次，每次都 sorted() 纯属浪费 CPU。长度兜底防止外部
        # 直接改 _tasks_map（比如手工注入任务池的场景）用到过期缓存。
        with self._lock:
            if self._pool_dirty or len(self._sorted_cache) != len(tasks):
                self._sorted_cache = sorted(tasks, key=sort_key)
                self._pool_dirty = False
            ordered = list(self._sorted_cache)

        created = 0
        # 本轮已分配的账号：多任务同时下发时，让它们各领各的任务
        used_accounts: set = set()
        for item in ordered:
            if created >= workers - active:
                break
            tid = item.get("id")
            if tid is None or tid in dead or tid in claimed or tid in assigned:
                continue
            if retry_at.get(tid, 0.0) > mono:
                continue
            with self._lock:
                last_seen = float(self._task_last_seen.get(tid) or mono)
            if mono - last_seen > 600.0:
                continue                         # 长时间未再下发，跳过（保留 UI 数据）
            try:
                quota = float(item.get("remainingQuota") or 0)
            except Exception:
                quota = 0.0
            eligibility = item.get("claimEligibility") or {}
            accounts = eligibility.get("accounts") or []
            # 能否领取以「账号级」为准：上游任务级 canClaim 是登录账号维度的
            # 聚合结论，不同登录账号视图下可能不一致；真正能领的是某个账号
            # accounts[].canClaim === true。
            acc_can = any(isinstance(a, dict) and a.get("canClaim") is True
                          for a in accounts)
            if quota <= 0 or not (bool(eligibility.get("canClaim")) or acc_can):
                continue
            account_type = self._task_types_map.get(tid) or str(item.get("taskType") or "")

            # 候选账号：(排序名次, 上游顺序, aid, cred_id)
            candidates: list = []
            for order, a in enumerate(accounts):
                if not isinstance(a, dict):
                    continue
                if a.get("canClaim") is False:
                    continue
                try:
                    aid = int(a.get("id"))
                except Exception:
                    continue
                ok, cid = self._account_available(
                    aid, tid, account_type, now_epoch,
                    str(item.get("platform") or ""))
                if not ok:
                    continue
                candidates.append((self._account_rank(aid), order, aid, cid))
            if not candidates:
                continue
            # 多任务并发：优先挑本轮还没分配过的账号（各领各的任务）
            free = [c for c in candidates if c[2] not in used_accounts]
            pool = free or candidates
            attempts = int(self._task_attempts.get(tid, 0))
            # 重试同一个任务时沿用上次的账号：换号再撞一次只会再吃一个 409
            prev = last_account.get(tid)
            if attempts > 0 and prev is not None:
                same = [c for c in pool if c[2] == prev]
                if same:
                    best = same[0]
                else:
                    best = min(pool, key=lambda c: (c[0], c[1], c[2]))
            else:
                # 排序靠前的账号优先
                best = min(pool, key=lambda c: (c[0], c[1], c[2]))
            _rank, _order, aid, cid = best
            with self._lock:
                if tid in self._task_assigned or tid in self._task_dead:
                    continue
                self._task_assigned[tid] = aid
                self._task_last_account[tid] = aid
                self._account_inflight[aid] = int(self._account_inflight.get(aid, 0)) + 1
                attempts = int(self._task_attempts.get(tid, 0))
            priority = self._task_priority(tid)
            self._sched.push(item, account_type,
                             int(priority), aid, 0.0, attempts)
            created += 1
            used_accounts.add(aid)
            # 只在「首次调度」（attempts == 0）记一条，且每任务 5s 最多一条：
            # 之前每次调度成功都发一条日志，高并发下把日志区刷成瀑布，
            # 真正的领取结果反而被淹没。
            if attempts == 0 and mono >= self._sched_log_next.get(tid, 0.0):
                with self._lock:
                    self._sched_log_next[tid] = mono + 5.0
                self._log(f"调度 #{tid} [{item.get('title', '')}] → 平台账号 {aid}"
                          f"（cred {cid}，账号排序靠前）", "INFO")
        # 把节流期间挂起的任务快照补发一次（最多 200ms 一次）
        self._flush_tasks_emit()

    def _release_assignment(self, task_id: Any, account_id: Any = None) -> None:
        with self._lock:
            aid = self._task_assigned.pop(task_id, None)
            if aid is None:
                aid = account_id
            if aid is not None:
                try:
                    aid = int(aid)
                    self._account_inflight[aid] = max(
                        0, int(self._account_inflight.get(aid, 0)) - 1)
                except Exception:
                    pass

    def _requeue_task(self, item: dict, task_type: str, account_id: Any,
                      delay: float, attempts: int) -> None:
        """释放任务锁并延迟重试（下一轮由调度器重新选账号）。"""
        tid = item.get("id")
        self._release_assignment(tid, account_id)
        with self._lock:
            if tid is not None:
                self._task_retry_at[tid] = time.monotonic() + max(0.0, float(delay))
                self._task_attempts[tid] = int(attempts)
                self._in_flight.discard(tid)

    def _mark_task_dead(self, task_id: Any, reason: str = "") -> None:
        with self._lock:
            if task_id is not None:
                # dict 记录标记时刻，供 _gc_state 按 TASK_DEAD_TTL 清理
                self._task_dead[task_id] = time.monotonic()
                self._task_retry_at.pop(task_id, None)
                self._in_flight.discard(task_id)
        if reason:
            self._log(f"#{task_id} 结束领取：{reason}", "WARN")

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
                try:
                    self._claim(item, task_type, account_id, attempts)
                finally:
                    self._release_assignment(item.get("id"), account_id)
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

    def _cooldown_delay(self, cred_id: Any = None) -> float:
        """距离该登录账号再次可领取还有多少秒（含 RTT/2 补偿）。"""
        cfg = self._cfg
        early = (self._rtt_ms / 2000.0) if cfg.early_fire else 0.0
        try:
            cid = int(cred_id) if cred_id is not None else None
        except Exception:
            cid = None
        if cid is None:
            active = self._active_cred_id()
            cid = int(active) if active is not None else 0
        with self._lock:
            due = float(self._next_claim_at_by_cred.get(cid, self._next_claim_at))
            req = self._request_cooldowns.get(cid) or {}
            due = max(due, _as_float(req.get("until")) or 0.0)
        limits = self._limits_snapshot(cid)
        if limits:
            try:
                next_allowed = float(limits.get("nextAllowedAtMs") or 0) / 1000.0
            except Exception:
                next_allowed = 0.0
            if next_allowed:
                due = max(due, next_allowed - self._clock_offset_ms / 1000.0)
        wait = due - time.time() - early
        if wait <= 0:
            return 0.0
        self._cool_log(f"账号 {cid} 冷却中，{wait * 1000:.0f}ms 后再领取", "INFO")
        return wait

    def _claim(self, item: dict, task_type: str, account_id: Optional[int],
               attempts: int) -> None:
        task_id = item.get("id")
        title = item.get("title", "")
        commission = item.get("baseCommission", "")
        cfg = self._cfg
        try:
            if account_id is None:
                self._requeue_task(item, task_type, None, 0.1, attempts)
                return
            cred_id = self._task_cred_for_account(account_id)
            limits = self._limits_snapshot(cred_id)

            # 1) 待处理队列已满：该 cred 暂停，任务重新分配
            pending_wait = self._pending_delay(limits)
            if pending_wait > 0:
                self._cool_log(f"待处理已满，{pending_wait:.0f}s 后重试 #{task_id}", "WARN")
                # 计数真正封顶：已连续排队满 MAX_PENDING_ATTEMPTS 次就把次数归零
                # （仍退避等 pending_wait），避免同一个任务把重试次数无限累加
                next_attempts = (attempts + 1
                                 if attempts + 1 < MAX_PENDING_ATTEMPTS else 0)
                self._requeue_task(item, task_type, account_id, pending_wait,
                                   next_attempts)
                self._bump("retries")
                return

            # 2) 该登录账号自己的请求冷却
            wait = self._cooldown_delay(cred_id)
            if wait > 0:
                self._requeue_task(item, task_type, account_id, wait, attempts)
                self._bump("retries")
                return

            # 3) 立即领取（非阻塞拿该 cred 的领取令牌）
            self._bump("claims_attempted")
            owner_cid = self._owner_of_platform_account(account_id)
            tok = self._cred_token(cred_id)
            token_note = (f"用账号 #{cred_id} 的 Token" if tok
                          else "用当前活跃 Token")
            status, obj = self._api_call(
                "POST", f"/api/user/tasks/{task_id}/claim",
                {"platformAccountId": account_id}, cfg=cfg, kind="claim",
                token=tok, cred_id=cred_id, blocking=False,
            )
            obj = obj if isinstance(obj, dict) else {}
            if obj.get("_skipped"):
                self._requeue_task(item, task_type, account_id, 0.2, attempts)
                self._bump("retries")
                return

            if status == 401 and owner_cid is not None:
                # owner token 失效：静默重登该账号一次
                if self._relogin_cred(owner_cid):
                    tok = self._cred_token(cred_id)
                    if tok:
                        self._log(f"#{task_id} 账号 #{owner_cid} 的 Token 已过期，"
                                  f"自动重登后重试 1 次", "WARN")
                        status, obj = self._api_call(
                            "POST", f"/api/user/tasks/{task_id}/claim",
                            {"platformAccountId": account_id}, cfg=cfg,
                            kind="claim", token=tok, cred_id=cred_id,
                            blocking=False,
                        )
                        obj = obj if isinstance(obj, dict) else {}
                        if obj.get("_skipped"):
                            self._requeue_task(item, task_type, account_id, 0.2, attempts)
                            return

            if status == 401:
                self._record(task_id, title, commission, account_id, "fail", status,
                             "token 无效或已过期")
                self._bump("claims_fail")
                self._log(f"领取失败 #{task_id}：token 无效或已过期（{token_note}）", "WARN")
                with self._lock:
                    self._next_claim_at_by_cred[cred_id] = time.time() + 60.0
                active = self._active_cred_id()
                if active is None or int(active) == int(cred_id):
                    self._pause("token 无效或已过期")
                else:
                    self._requeue_task(item, task_type, account_id, 5.0, attempts + 1)
                return

            if status in (200, 201) and obj.get("success"):
                record = obj.get("data") or {}
                if not isinstance(record, dict):
                    record = {}
                limits_in_rec = record.get("claimLimits") or {}
                self._bump("claims_ok")
                self._record(task_id, title, commission, account_id, "ok", status, "领取成功")
                with self._lock:
                    mono = time.monotonic()
                    # dict 记录领取时刻，供 _gc_state 按 CLAIMED_TTL / TASK_DEAD_TTL 清理
                    self._claimed[task_id] = mono
                    self._task_dead[task_id] = mono
                try:
                    retry_after = float(limits_in_rec.get("retryAfterSeconds") or 0)
                except Exception:
                    retry_after = 0.0
                if retry_after > 0:
                    until = time.time() + retry_after
                    with self._lock:
                        self._next_claim_at = max(self._next_claim_at, until)
                        self._next_claim_at_by_cred[cred_id] = max(
                            float(self._next_claim_at_by_cred.get(cred_id, 0.0)), until)
                    self._log(f"#{task_id} 账号 {cred_id} 进入请求冷却 {retry_after:g}s", "INFO")
                task_cred = owner_cid if owner_cid is not None else cred_id
                self._set_request_cooldown(cred_id, limits_in_rec,
                                           source="claim_response", persist=False)
                try:
                    interval_days = float(item.get("claimIntervalDays") or 0)
                except Exception:
                    interval_days = 0.0
                if interval_days <= 0:
                    interval_days = 3.0
                self._set_task_cooldown(account_id, task_id,
                                        interval_days * 86400.0, task_type,
                                        task_cred, source="claim_response",
                                        persist=False)
                self._set_pending(account_id, task_id, task_type, task_cred,
                                  source="claim_response",
                                  status=str(record.get("status") or "claimed"),
                                  persist=False)
                self._save_cooldowns_state()
                self._log(
                    f"领取成功 #{task_id} 记录ID={record.get('id')} 失效={record.get('expiresAt')} "
                    f"请求冷却={retry_after:g}s 任务冷却={interval_days:g}天 "
                    f"子账号={account_id}（{token_note}）", "OK")
                if cfg.result_polls and int(cfg.result_polls) > 0:
                    self._poll_result(task_id, account_id, cfg)
                threading.Thread(target=self._beep, daemon=True).start()
                self._emit_status()
                return

            if status == 409:
                message = obj.get("message") or "已被领取"
                self._bump("claims_conflict")
                self._record(task_id, title, commission, account_id, "conflict", status, message)
                self._log(f"慢了一步 #{task_id}：{message}（{token_note}）", "WARN")
                self._mark_task_dead(task_id, "已被领取")
                self._emit_status()
                return

            if status == 429:
                message = obj.get("message") or "被上游限流"
                backoff = min(30.0, 2.0 * (attempts + 1))
                with self._lock:
                    self._next_claim_at = max(self._next_claim_at,
                                              time.time() + backoff)
                    self._next_claim_at_by_cred[cred_id] = max(
                        float(self._next_claim_at_by_cred.get(cred_id, 0.0)),
                        time.time() + backoff)
                self._bump("claims_fail")
                if attempts < MAX_CLAIM_ATTEMPTS:
                    self._log(f"被限流 #{task_id}，{backoff:.0f}s 后重试"
                              f"（第 {attempts + 1} 次，账号 {cred_id}）", "ERR")
                    self._requeue_task(item, task_type, account_id, backoff,
                                       attempts + 1)
                else:
                    # 次数封顶：**不**标记 dead（任务本身没死，只是被限流），
                    # 次数归零后仍退避重排，让调度器稍后重新评估这个任务
                    self._log(f"#{task_id} 已连续限流 {attempts} 次，退回调度器"
                              f"稍后重新评估（{backoff:.0f}s 后，账号 {cred_id}）",
                              "WARN")
                    self._requeue_task(item, task_type, account_id, backoff, 0)
                self._bump("retries")
                self._emit_status()
                return

            if status == 0 or status >= 500:
                delay = min(5.0, 0.4 * (attempts + 1))
                self._log(f"#{task_id} 上游异常（HTTP {status}），{delay:.1f}s 后重试", "WARN")
                self._requeue_task(item, task_type, account_id, delay,
                                   attempts + 1)
                self._bump("retries")
                return

            message = obj.get("message") or ""
            self._bump("claims_fail")
            self._record(task_id, title, commission, account_id, "fail", status,
                         message or "领取失败")
            self._log(f"领取失败 #{task_id} HTTP {status} {message}（账号 {cred_id}）", "ERR")
            if attempts < 2:
                self._requeue_task(item, task_type, account_id, 1.0, attempts + 1)
            else:
                self._mark_task_dead(task_id, f"HTTP {status}")
            self._emit_status()
        except Exception as exc:
            self._loop_error("领取", exc)
            self._release_assignment(task_id, account_id)

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
        # 领取结果的归属与领取本身一致：优先用该子账号所属登录账号的 token
        tok = self._token_for_platform_account(account_id)
        cid = self._task_cred_for_account(account_id)
        result = None
        for _ in range(max(0, int(cfg.result_polls))):
            status, obj = self._api_call("GET", path, cfg=cfg, kind="claim",
                                         token=tok, cred_id=cid)
            if status == 401:
                active = self._active_cred_id()
                if active is None or int(active) == int(cid):
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
