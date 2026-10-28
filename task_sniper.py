#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
materials.xlya.cn 任务低时延狙击脚本
====================================
不停轮询任务列表，一旦发现「可领取」的任务立刻领取，尽量抢在别人前面。

特性
----
- 零依赖：仅用 Python 标准库（urllib / threading / json）。
- 全局令牌桶限速：遵守接口 6 次/10 秒 的硬限流，避免被限流反而变慢。
- 领取优先：列表轮询与领取共用令牌桶，但领取请求不会被轮询饿死。
- 遵守冷却/配额：领取前读取 claim-limits，尊重 nextAllowedAtMs / pendingLimit / windowLimit。
- 自动选号：默认从任务的 claimEligibility.accounts 取第一个可领取账号，也可固定。
- 成功提醒：Windows 下抢到会"滴"一声；所有动作带时间戳日志。

使用方法
--------
1) 复制你的 JWT（浏览器打开 materials.xlya.cn → F12 → Network 任选一个 /api 请求 →
   Request Headers 里的 `Authorization: Bearer xxxx`，把 xxxx 复制出来）。
   方式任选其一：
     a. 设置环境变量：  set MATERIALS_TOKEN=eyJxxxx   (Windows)
                       export MATERIALS_TOKEN=eyJxxxx (Linux/Mac)
     b. 直接改下面 CONFIG 里的 TOKEN。
2) 按需修改 CONFIG（轮询间隔、监控的任务类型、固定账号等）。
3) 运行：  python task_sniper.py
4) 停止：  Ctrl+C

注意
----
- 同一 JWT 约 7 天过期，过期后重新复制即可。脚本启动会检查过期时间并告警。
- 限流 6/10s 是硬性天花板：轮询再快也会被桶卡在 ~1.7s 一次。若你确认该限流是「按接口」而非全局，
  可把 RATE_LIMIT_MAX 调高（如 12）以获得更快探测。
- 只抢 remainQuota>0 且 canClaim 的任务；canClaim=false（如无对应平台审核号）的任务不会碰。
"""

import os
import sys
import time
import json
import base64
import threading
import queue
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Windows 控制台常为 GBK，无法编码 emoji（🎯/✅ 等），
# 降级为替换而不是让 log 崩溃、带崩整个领取线程
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

# ============================ CONFIG ============================
BASE = "https://materials.xlya.cn"
# 优先用环境变量，否则用这里硬编码的 token（不要带 "Bearer " 前缀）
TOKEN = os.environ.get("MATERIALS_TOKEN", "PASTE_YOUR_BEARER_TOKEN_HERE")

# 要监控的任务类型：standard=常规, long_term=长期
TASK_TYPES = ["standard", "long_term"]

# 固定领取账号 id；设为 None 则自动从 claimEligibility.accounts 取第一个
PLATFORM_ACCOUNT_ID = None

# 每个列表端点的轮询间隔（秒）。实际速度会被令牌桶上限夹住。
POLL_INTERVAL = 1.5

# 全局速率限制（令牌桶）。接口硬上限是 6/10s，默认就按它来。
RATE_LIMIT_MAX = 6          # 桶容量
RATE_LIMIT_WINDOW = 10.0    # 窗口秒数；rate = MAX / WINDOW

# 只抓第几页（新任务通常在第一页）。如需多页可改大。
MAX_PAGES = 1
PAGE_SIZE = 10

# 领取后是否轮询 claim-result 确认。经验证：成功领取的 201 响应体已直接返回领取记录，
# 而 claim-result 始终返回 data:null，且每次轮询都吃 6/10s 全局额度 -> 默认关闭。
RESULT_POLLS = 0
RESULT_POLL_INTERVAL = 0.4

# 网络超时（秒）
HTTP_TIMEOUT = 8

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
      "Gecko/20100101 Firefox/158.0")
# ========================== END CONFIG ==========================


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    tag = {"INFO": "·", "OK": "✅", "WARN": "⚠", "ERR": "✖", "CLAIM": "🎯"}.get(level, "·")
    line = f"[{ts}] {tag} {msg}"
    print(line, flush=True)


# ---------------------- JWT 过期检查 ----------------------
def jwt_exp(token):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p))
        return d.get("exp")
    except Exception:
        return None


# ---------------------- 令牌桶限速 ----------------------
class RateLimiter:
    def __init__(self, max_count, window):
        self.cap = float(max_count)
        self.rate = self.cap / float(window)  # tokens per second
        self.tokens = self.cap
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n=1):
        """阻塞直到能拿到 n 个令牌。"""
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + self.rate * (now - self.last))
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(max(wait, 0.01))

    def try_acquire(self, n=1):
        """非阻塞：有令牌立即拿，没有返回 False（不等待）。"""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.cap, self.tokens + self.rate * (now - self.last))
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


LIMITER = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)


# ---------------------- 网络层 ----------------------
def api_call(method, path, body=None, blocking=True):
    """发起一次 API 调用（已纳入全局限速）。返回 (status, dict_or_None)。
    blocking=False：拿不到令牌立即返回 (0, {"_skipped": True})，不发出请求
    （用于列表轮询——宁可跳过本轮，也不占用/阻塞领取请求的额度）。"""
    if not LIMITER.try_acquire(1):
        if not blocking:
            return (0, {"_skipped": True})
        LIMITER.acquire(1)
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("authorization", "Bearer " + TOKEN)
    req.add_header("content-type", "application/json")
    req.add_header("accept", "*/*")
    req.add_header("user-agent", UA)
    req.add_header("referer", BASE + "/pages/tasks/index")
    # 故意不发送 If-None-Match，避免拿到 304 缓存，保证每次都是最新数据
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # 超时 / 网络错误
        return (0, {"_error": str(e)})
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {"_raw": raw[:200]}
    return (status, obj)


# ---------------------- 业务接口 ----------------------
def get_tasks(task_type, page=1, blocking=True):
    path = (f"/api/user/tasks?taskType={task_type}&page={page}"
            f"&pageSize={PAGE_SIZE}&platform=&materialType=&categoryId=&keyword=")
    status, obj = api_call("GET", path, blocking=blocking)
    if status == 0 and isinstance(obj, dict) and obj.get("_skipped"):
        return []  # 本轮额度不够，跳过（不告警，避免刷屏）
    if status == 200 and isinstance(obj, dict) and obj.get("data"):
        return obj["data"].get("items", [])
    if status == 304:  # 服务器说没变化（一般不会，因为我们不带 etag）
        return []
    if status != 200:
        msg = obj.get("message") if isinstance(obj, dict) else ""
        log(f"列表[{task_type}] HTTP {status} {msg}", "WARN")
    return []


def get_limits():
    status, obj = api_call("GET", "/api/user/tasks/claim-limits")
    if status == 200 and isinstance(obj, dict):
        return obj.get("data", {})
    return {}


def do_claim(task_id, account_id):
    path = f"/api/user/tasks/{task_id}/claim"
    status, obj = api_call("POST", path, {"platformAccountId": account_id})
    return status, obj


def poll_result(task_id, account_id):
    path = f"/api/user/tasks/{task_id}/claim-result?platformAccountId={account_id}"
    for _ in range(RESULT_POLLS):
        status, obj = api_call("GET", path)
        if status == 200 and isinstance(obj, dict):
            d = obj.get("data")
            if d is not None:
                return d
        time.sleep(RESULT_POLL_INTERVAL)
    return None


# ---------------------- 状态 ----------------------
seen = set()          # 已发现过的任务 id
in_flight = set()     # 正在/已经尝试领取的任务 id
limits_cache = {"ts": 0, "data": {}}
limits_lock = threading.Lock()
running = True
claim_queue = queue.Queue()


def cached_limits(force=False):
    now = time.monotonic()
    with limits_lock:
        if force or now - limits_cache["ts"] > 3:
            limits_cache["data"] = get_limits()
            limits_cache["ts"] = now
        return limits_cache["data"]


# ---------------------- 领取线程 ----------------------
def claimer_loop():
    while running:
        try:
            item, task_type = claim_queue.get(timeout=1)
        except Exception:
            continue
        task_id = item["id"]
        try:
            # 遵守冷却 / 配额
            lim = cached_limits()
            if lim:
                server_now = lim.get("serverNowMs", int(time.time() * 1000))
                next_allowed = lim.get("nextAllowedAtMs", 0)
                if server_now < next_allowed:
                    wait_s = (next_allowed - server_now) / 1000.0 + 0.1
                    log(f"冷却中，{wait_s:.1f}s 后再领取 #{task_id}", "WARN")
                    time.sleep(min(wait_s, 5))
                if lim.get("pendingCount", 0) >= lim.get("pendingLimit", 99):
                    log(f"待处理已达上限，跳过 #{task_id}", "WARN")
                    continue

            # 选账号
            if PLATFORM_ACCOUNT_ID:
                acct = PLATFORM_ACCOUNT_ID
            else:
                accts = (item.get("claimEligibility") or {}).get("accounts") or []
                if not accts:
                    log(f"#{task_id} 无可领取账号，放弃", "WARN")
                    continue
                acct = accts[0]["id"]

            title = item.get("title", "")
            comm = item.get("baseCommission", "")
            log(f"立即领取 #{task_id} [{title}] 账号={acct} 佣金={comm}", "CLAIM")
            status, obj = do_claim(task_id, acct)
            # 成功：HTTP 200 或 201 且 success=true（实测成功返回 201）
            if status in (200, 201) and isinstance(obj, dict) and obj.get("success"):
                rec = obj.get("data") or {}
                exp = rec.get("expiresAt")
                log(f"✅ 领取成功 #{task_id} 记录ID={rec.get('id')} "
                    f"失效={exp} 剩余冷却={rec.get('claimLimits', {}).get('retryAfterSeconds')}s",
                    "OK")
                if RESULT_POLLS > 0:
                    poll_result(task_id, acct)
                try:
                    import winsound
                    winsound.Beep(1000, 200)
                except Exception:
                    pass
            elif status == 409:
                log(f"⚡ 慢了一步 #{task_id}：{obj.get('message','已被领取')}", "WARN")
            elif status == 429:
                log(f"被限流 #{task_id}，退避后重试", "ERR")
                time.sleep(2)
                claim_queue.put((item, task_type))  # 放回队列
            else:
                msg = obj.get("message", "") if isinstance(obj, dict) else ""
                log(f"领取失败 #{task_id} HTTP {status} {msg}", "ERR")
        finally:
            in_flight.discard(task_id)
        claim_queue.task_done()


# ---------------------- 轮询线程 ----------------------
def poller_loop(task_type):
    log(f"启动轮询线程：{task_type}")
    while running:
        for page in range(1, MAX_PAGES + 1):
            # 非阻塞：额度不够就跳过本轮，绝不占用/阻塞领取请求的令牌
            items = get_tasks(task_type, page, blocking=False)
            for it in items:
                tid = it.get("id")
                if tid is None:
                    continue
                elig = (it.get("claimEligibility") or {})
                quota = it.get("remainingQuota", 0)
                can = elig.get("canClaim", False)
                if quota > 0 and can and tid not in seen and tid not in in_flight:
                    seen.add(tid)
                    in_flight.add(tid)
                    claim_queue.put((it, task_type))
                    log(f"发现可领取任务 #{tid} [{it.get('title','')}] -> 入队", "INFO")
        time.sleep(POLL_INTERVAL)


# ---------------------- 主函数 ----------------------
def main():
    global running
    exp = jwt_exp(TOKEN)
    if exp:
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        left = exp_dt - datetime.now(tz=timezone.utc)
        if left.total_seconds() <= 0:
            log("JWT 已过期！请重新从浏览器复制 token。", "ERR")
            return
        elif left.total_seconds() < 3600:
            log(f"JWT 即将过期（剩 {left}），请尽快更新。", "WARN")
        else:
            log(f"JWT 有效，约剩 {left}。", "INFO")

    if not TOKEN or TOKEN == "PASTE_YOUR_BEARER_TOKEN_HERE":
        log("未配置 TOKEN！请设置环境变量 MATERIALS_TOKEN 或修改脚本 CONFIG。", "ERR")
        return

    log(f"开始狙击 | 监控类型={TASK_TYPES} | 账号="
        f"{PLATFORM_ACCOUNT_ID or '自动'} | 限速={RATE_LIMIT_MAX}/{RATE_LIMIT_WINDOW}s")
    log("按 Ctrl+C 停止。", "INFO")

    threads = []
    t_claim = threading.Thread(target=claimer_loop, daemon=True)
    t_claim.start()
    threads.append(t_claim)
    for tt in TASK_TYPES:
        t = threading.Thread(target=poller_loop, args=(tt,), daemon=True)
        t.start()
        threads.append(t)

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到停止信号，正在退出…", "WARN")
    finally:
        running = False
        for t in threads:
            t.join(timeout=2)
        log("已停止。", "INFO")


if __name__ == "__main__":
    main()
