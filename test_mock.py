#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端联调：用本地 mock 服务器模拟 materials API，验证 轮询->入队->领取 全链路。"""
import sys, time, json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import task_sniper as m

# ---- 共享状态：先给一个可领取任务，2 秒后改成已抢光，验证只领一次 ----
STATE = {
    "tasks": [{
        "id": 999, "taskType": "standard", "title": "测试任务A",
        "baseCommission": "20.00", "remainingQuota": 1,
        "claimEligibility": {"canClaim": True, "accounts": [{"id": 2175}]}
    }],
    "claim_hits": 0,
    "list_hits": 0,
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api/user/tasks?taskType=standard"):
            STATE["list_hits"] += 1
            # 第一次轮询后，把任务变不可领取，验证去重/不重复领
            if STATE["list_hits"] > 3:
                STATE["tasks"][0]["remainingQuota"] = 0
                STATE["tasks"][0]["claimEligibility"]["canClaim"] = False
            self._send(200, {"success": True, "data": {"items": STATE["tasks"], "total": len(STATE["tasks"])}})
        elif self.path.startswith("/api/user/tasks/claim-limits"):
            self._send(200, {"success": True, "data": {
                "serverNowMs": int(time.time()*1000), "nextAllowedAtMs": 0,
                "pendingCount": 0, "pendingLimit": 3, "cooldownSeconds": 10,
                "recentCount": 0, "windowLimit": 3,
                "requestLimits": {"serverNowMs": int(time.time()*1000), "nextAllowedAtMs": 0,
                                  "retryAfterSeconds": 0, "recentCount": 0,
                                  "requestLimit": 6, "windowSeconds": 10}}})
        elif "claim-result" in self.path:
            self._send(200, {"success": True, "data": {"status": "claimed"}})
        else:
            self._send(404, {"success": False, "message": "not found"})
    def do_POST(self):
        if "/claim" in self.path:
            STATE["claim_hits"] += 1
            # 真实接口成功时返回 201（不是 200）
            self._send(201, {"success": True, "message": "ok",
                             "data": {"id": 999, "taskId": 999, "platformAccountId": 2175,
                                      "status": "claimed",
                                      "expiresAt": "2026-10-28T10:01:58.000Z",
                                      "claimLimits": {"retryAfterSeconds": 10}}})
        else:
            self._send(404, {"success": False})

if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    # 指向本地 mock
    m.BASE = f"http://127.0.0.1:{port}"
    m.TOKEN = "dummy"
    m.POLL_INTERVAL = 0.5
    m.RATE_LIMIT_MAX = 20  # mock 不限流，测逻辑
    m.RATE_LIMIT_WINDOW = 1.0
    m.LIMITER = m.RateLimiter(m.RATE_LIMIT_MAX, m.RATE_LIMIT_WINDOW)
    m.running = True
    m.seen.clear(); m.in_flight.clear()

    ct = threading.Thread(target=m.claimer_loop, daemon=True); ct.start()
    pt = threading.Thread(target=m.poller_loop, args=("standard",), daemon=True); pt.start()

    time.sleep(4)
    m.running = False
    time.sleep(0.5)
    print("RESULT list_hits=%d claim_hits=%d" % (STATE["list_hits"], STATE["claim_hits"]))
    assert STATE["claim_hits"] == 1, "应当只领取 1 次，实际 %d" % STATE["claim_hits"]
    print("PASS: 轮询发现任务 -> 入队 -> 领取成功，且仅领取一次（去重正确）")
