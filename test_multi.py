#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多账号并发抢单测试。

覆盖：
1. 4 个登录账号各自独立 token / 引擎，可同时运行并各自抢到单；
2. 平台子账号按登录账号隔离（不再串到一个账号里）；
3. 同一任务被多账号看到时，任务列表按 taskId 去重并合并可领账号；
4. 单账号停止不影响其它账号。

跑法：python -m pytest test_multi.py -x -q
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

# 手机号 -> 该账号名下的平台子账号 id
ACCOUNTS = {
    "13900000001": 3001,
    "13900000002": 3002,
    "13900000003": 3003,
    "13900000004": 3004,
}

MOCK = {
    "token_phone": {},     # Authorization -> phone
    "claims": {},          # platformAccountId -> 次数
    "list_hits": 0,
}


def _phone_of(auth: str) -> str:
    token = auth.replace("Bearer ", "").strip()
    return MOCK["token_phone"].get(token, "")


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        auth = self.headers.get("authorization") or ""
        phone = _phone_of(auth)
        aid = ACCOUNTS.get(phone)
        if not aid:
            self._send(401, {"success": False, "message": "token 无效"})
            return
        if self.path.startswith("/api/user/tasks?taskType="):
            MOCK["list_hits"] += 1
            task = {
                "id": 999, "taskType": "standard", "title": "共享任务",
                "baseCommission": "20.00", "remainingQuota": 5,
                "claimEligibility": {"canClaim": True, "accounts": [{"id": aid}]},
            }
            self._send(200, {"success": True,
                             "data": {"items": [task], "total": 1}})
        elif self.path.startswith("/api/user/tasks/claim-limits"):
            now_ms = int(time.time() * 1000)
            self._send(200, {"success": True, "data": {
                "serverNowMs": now_ms, "nextAllowedAtMs": 0,
                "pendingCount": 0, "pendingLimit": 3, "cooldownSeconds": 0,
                "recentCount": 0, "windowLimit": 3,
                "requestLimits": {"serverNowMs": now_ms, "nextAllowedAtMs": 0,
                                  "retryAfterSeconds": 0, "recentCount": 0,
                                  "requestLimit": 6, "windowSeconds": 10}}})
        elif self.path.startswith("/api/user/platform-accounts"):
            self._send(200, {"success": True, "data": [
                {"id": aid, "name": f"账号{aid}",
                 "platform": "douyin" if aid % 2 else "xiaohongshu"},
            ]})
        elif "claim-result" in self.path:
            self._send(200, {"success": True, "data": {"status": "claimed"}})
        else:
            self._send(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        auth = self.headers.get("authorization") or ""
        phone = _phone_of(auth)
        aid = ACCOUNTS.get(phone)
        if not aid:
            self._send(401, {"success": False, "message": "token 无效"})
            return
        if "/claim" in self.path:
            MOCK["claims"][aid] = MOCK["claims"].get(aid, 0) + 1
            self._send(201, {"success": True, "message": "ok",
                             "data": {"id": 1, "taskId": 999,
                                      "platformAccountId": aid,
                                      "status": "claimed",
                                      "expiresAt": "2026-11-20T10:01:58.000Z",
                                      "claimLimits": {"retryAfterSeconds": 0}}})
        else:
            self._send(404, {"success": False})


def _fake_jwt(phone: str, exp_offset: float = 7 * 24 * 3600) -> str:
    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({"sub": phone, "iat": int(time.time()),
                   "exp": int(time.time() + exp_offset)})
    return f"{header}.{payload}.fakesignature"


def _start_mock() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _start_app():
    import uvicorn

    from server.app import app as fastapi_app

    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=0,
                            log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn 启动超时")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=15)

    return server, f"http://127.0.0.1:{port}", stop


def _wait(predicate, timeout: float = 25.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def test_multi_account_parallel_claim():
    MOCK["token_phone"].clear()
    MOCK["claims"].clear()
    MOCK["list_hits"] = 0

    data_dir = tempfile.mkdtemp(prefix="sniper-multi-")
    os.environ["MATERIALS_DATA_DIR"] = data_dir

    mock_server, upstream = _start_mock()
    app_server, base, stop_app = _start_app()
    client = httpx.Client(base_url=base, timeout=20.0)
    try:
        r = client.put("/api/config", json={"base": upstream,
                                            "poll_interval": 0.3,
                                            "rate_limit_max": 60,
                                            "rate_limit_window": 1.0})
        assert r.status_code == 200, r.text

        # ---- 建 4 个登录账号，并各自写入独立 token ----------------
        creds = []
        id_phone = {}
        for phone in ACCOUNTS:
            r = client.post("/api/credentials",
                            json={"name": phone[-2:], "phone": phone,
                                  "password": "pw-123456"})
            assert r.status_code == 201, r.text
            rec = r.json()
            id_phone[rec["id"]] = phone
            creds.append(rec)

        assert len(client.get("/api/credentials").json()["items"]) == 4

        for rec in creds:
            phone = id_phone[rec["id"]]
            token = _fake_jwt(phone)
            MOCK["token_phone"][token] = phone
            r = client.put(f"/api/accounts/{rec['id']}/token",
                           json={"token": token})
            assert r.status_code == 200, r.text
            assert r.json()["valid"] is True, r.text

        # ---- 平台子账号按登录账号隔离 -----------------------------
        seen = {}
        for rec in creds:
            data = client.get("/api/platform-accounts",
                              params={"cred_id": rec["id"]}).json()
            ids = {int(i["id"]) for i in data["items"]}
            assert ids == {ACCOUNTS[id_phone[rec["id"]]]}, (rec, data)
            for item in data["items"]:
                assert item["cred_id"] == rec["id"], item
            seen[rec["id"]] = ids
        all_ids = set().union(*seen.values())
        assert all_ids == set(ACCOUNTS.values()), seen

        # ---- 全部启动：4 个引擎并行 -------------------------------
        r = client.post("/api/start")
        assert r.status_code == 200, r.text

        ok = _wait(lambda: len([v for v in MOCK["claims"].values() if v > 0]) == 4)
        assert ok, MOCK["claims"]
        # 每个账号都用自己的平台子账号抢到单（互不串号）
        assert set(MOCK["claims"]) == set(ACCOUNTS.values()), MOCK["claims"]

        status = client.get("/api/status").json()
        assert status["running"] is True, status
        assert len(status["accounts"]) == 4, status
        assert all(a["running"] for a in status["accounts"]), status
        assert all(a["token"]["valid"] for a in status["accounts"]), status

        accounts = client.get("/api/accounts").json()["items"]
        assert {a["cred_id"] for a in accounts} == {c["id"] for c in creds}

        # ---- 同一任务多账号可见：列表去重、可领账号取并集 ----------
        ok = _wait(lambda: len(client.get("/api/tasks").json()["items"]) == 1)
        assert ok
        tasks = client.get("/api/tasks").json()["items"]
        assert len(tasks) == 1, tasks
        assert tasks[0]["id"] == 999
        assert set(tasks[0]["_cred_ids"]) == {c["id"] for c in creds}, tasks[0]
        elig = {int(a["id"]) for a in tasks[0]["claimEligibility"]["accounts"]}
        assert elig == set(ACCOUNTS.values()), tasks[0]

        # ---- 领取记录带账号归属 -----------------------------------
        history = client.get("/api/history").json()["items"]
        assert history, "应有领取记录"
        assert all(h.get("cred_id") in {c["id"] for c in creds} for h in history)
        assert len({h["cred_id"] for h in history}) == 4, history

        # ---- 停一个账号不影响其它 ---------------------------------
        stopped = creds[0]["id"]
        r = client.post(f"/api/accounts/{stopped}/stop")
        assert r.status_code == 200, r.text
        status = client.get("/api/status").json()
        state = {a["cred_id"]: a for a in status["accounts"]}
        assert state[stopped]["running"] is False, state[stopped]
        assert all(a["running"] for a in status["accounts"]
                   if a["cred_id"] != stopped), status

        assert client.post("/api/stop").status_code == 200
        status = client.get("/api/status").json()
        assert status["running"] is False
        assert all(not a["running"] for a in status["accounts"]), status

        # ---- 回归：全局默认子账号不会被广播给其它账号 -------------
        before = {a["cred_id"]: a["platform_account_id"]
                  for a in client.get("/api/status").json()["accounts"]}
        r = client.put("/api/config", json={"platform_account_id": 3001})
        assert r.status_code == 200, r.text
        after = {a["cred_id"]: a["platform_account_id"]
                 for a in client.get("/api/status").json()["accounts"]}
        assert after == before, (before, after)

        # ---- 回归：运行期新增账号不继承全局默认子账号 -------------
        r = client.post("/api/credentials",
                        json={"name": "extra", "phone": "13900000009",
                              "password": "pw-123456"})
        assert r.status_code == 201, r.text
        extra_id = r.json()["id"]
        status = client.get("/api/status").json()
        extra = next(a for a in status["accounts"]
                     if a["cred_id"] == extra_id)
        assert extra["platform_account_id"] is None, extra
    finally:
        client.close()
        stop_app()
        mock_server.shutdown()


if __name__ == "__main__":
    test_multi_account_parallel_claim()
    print("OK")
