#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 后端端到端测试。

- 线程里起 mock 上游（port 0）：/api/user/tasks、claim-limits、claim(201)、claim-result
- 线程里起 uvicorn（port 0），MATERIALS_DATA_DIR 指向 tempfile.mkdtemp()
- httpx 调本机 API，覆盖契约里的全部硬性路径

两种跑法都支持：
    python -m pytest test_webapp.py -x -q
    python test_webapp.py
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

# ---------------------------------------------------------------- mock 上游
MOCK = {
    "tasks": [{
        "id": 999, "taskType": "standard", "title": "测试任务A",
        "baseCommission": "20.00", "remainingQuota": 1,
        "claimEligibility": {"canClaim": True, "accounts": [{"id": 2175}]},
    }],
    "claim_hits": 0,
    "list_hits": 0,
    "last_auth": None,
}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> str:
        auth = self.headers.get("authorization") or ""
        MOCK["last_auth"] = auth
        return auth

    def do_GET(self) -> None:
        self._auth()
        if self.path.startswith("/api/user/tasks?taskType="):
            MOCK["list_hits"] += 1
            self._send(200, {"success": True,
                             "data": {"items": MOCK["tasks"], "total": len(MOCK["tasks"])}})
        elif self.path.startswith("/api/user/tasks/claim-limits"):
            now_ms = int(time.time() * 1000)
            self._send(200, {"success": True, "data": {
                "serverNowMs": now_ms, "nextAllowedAtMs": 0,
                "pendingCount": 0, "pendingLimit": 3, "cooldownSeconds": 10,
                "recentCount": 0, "windowLimit": 3,
                "requestLimits": {"serverNowMs": now_ms, "nextAllowedAtMs": 0,
                                  "retryAfterSeconds": 0, "recentCount": 0,
                                  "requestLimit": 6, "windowSeconds": 10}}})
        elif "claim-result" in self.path:
            self._send(200, {"success": True, "data": {"status": "claimed"}})
        else:
            self._send(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        self._auth()
        if "/claim" in self.path:
            MOCK["claim_hits"] += 1
            self._send(201, {"success": True, "message": "ok",
                             "data": {"id": 1, "taskId": 999, "platformAccountId": 2175,
                                      "status": "claimed",
                                      "expiresAt": "2026-10-28T10:01:58.000Z",
                                      "claimLimits": {"retryAfterSeconds": 10}}})
        else:
            self._send(404, {"success": False})


def _fake_jwt(exp_offset: float = 7 * 24 * 3600) -> str:
    """真实结构的假 JWT：header.payload.signature，payload 带 exp。"""
    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({"sub": "tester", "iat": int(time.time()),
                   "exp": int(time.time() + exp_offset)})
    return f"{header}.{payload}.fakesignature"


def _start_mock() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _start_app():
    """在子线程里跑 uvicorn，返回 (server, base_url, stop_fn)。"""
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


# ---------------------------------------------------------------- 测试本体
def test_webapp_end_to_end():
    MOCK["claim_hits"] = 0
    MOCK["list_hits"] = 0
    MOCK["last_auth"] = None
    MOCK["tasks"][0]["remainingQuota"] = 1
    MOCK["tasks"][0]["claimEligibility"]["canClaim"] = True

    data_dir = tempfile.mkdtemp(prefix="sniper-web-")
    os.environ["MATERIALS_DATA_DIR"] = data_dir

    mock_server, upstream = _start_mock()
    app_server, base, stop_app = _start_app()
    client = httpx.Client(base_url=base, timeout=20.0)
    try:
        # 1) PUT /api/token：真实结构的假 JWT -> 200 valid --------------
        token = _fake_jwt()
        r = client.put("/api/token", json={"token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is True, body
        assert body["exp"], body
        assert token not in r.text

        # 2) PUT /api/config：接受子集，返回全量 ------------------------
        r = client.put("/api/config", json={"base": upstream, "poll_interval": 0.3,
                                            "rate_limit_max": 20,
                                            "rate_limit_window": 1.0})
        assert r.status_code == 200, r.text
        cfg = r.json()
        for field in ("base", "poll_interval", "task_types", "platform_account_id",
                      "rate_limit_max", "rate_limit_window", "max_pages",
                      "page_size", "http_timeout", "result_polls"):
            assert field in cfg, f"Config 缺少字段 {field}: {cfg}"
        assert cfg["poll_interval"] == 0.3, cfg
        assert cfg["base"] == upstream, cfg
        # 非法配置 -> 422
        assert client.put("/api/config", json={"poll_interval": 0.01}).status_code == 422
        assert client.put("/api/config", json={"rate_limit_max": 0}).status_code == 422

        # 3) POST /api/start -> GET /api/status running -----------------
        r = client.post("/api/start")
        assert r.status_code == 200, r.text
        status = client.get("/api/status").json()
        assert status["running"] is True, status
        assert status["state"] == "running", status
        assert status["token"]["present"] is True and status["token"]["valid"] is True
        assert status["upstream_base"] == upstream

        # 4) GET /api/history 直到出现 1 条 ok（<=15s）------------------
        deadline = time.time() + 15
        ok_items = []
        while time.time() < deadline:
            items = client.get("/api/history").json()["items"]
            ok_items = [i for i in items if i.get("status") == "ok"]
            if ok_items:
                break
            time.sleep(0.2)
        assert ok_items, f"15s 内没有领取成功记录：{client.get('/api/history').json()}"
        rec = ok_items[0]
        for field in ("ts", "task_id", "title", "commission", "account_id",
                      "status", "http", "message"):
            assert field in rec, f"History 缺少字段 {field}: {rec}"
        assert rec["task_id"] == 999 and rec["account_id"] == 2175 and rec["http"] == 201

        # 5) GET /api/tasks items 非空 ---------------------------------
        tasks = client.get("/api/tasks").json()
        assert isinstance(tasks.get("items"), list) and tasks["items"], tasks
        assert tasks["items"][0]["id"] == 999

        # 6) SSE 收到 snapshot 帧且含 type 字段 -------------------------
        first = None
        with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200, resp.status_code
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    first = json.loads(line[len("data:"):].strip())
                    break
        assert first is not None, "SSE 没有收到任何 data 帧"
        assert first["type"] == "snapshot", first
        assert set(("status", "tasks", "history", "logs")).issubset(first.keys()), first

        # 7) POST /api/stop -> state idle ------------------------------
        r = client.post("/api/stop")
        assert r.status_code == 200, r.text
        status = client.get("/api/status").json()
        assert status["state"] == "idle", status
        assert status["running"] is False, status

        # 8) 伪造 Origin 的 POST -> 403 ---------------------------------
        r = client.post("/api/start", headers={"Origin": "http://evil.com"})
        assert r.status_code == 403, (r.status_code, r.text)
        # 同源 Origin 允许
        r = client.post("/api/start", headers={"Origin": base})
        assert r.status_code == 200, (r.status_code, r.text)
        client.post("/api/stop")

        # 9) 全程 mock claim_hits 恰好 1（去重）------------------------
        assert MOCK["claim_hits"] == 1, f"应当只领取 1 次，实际 {MOCK['claim_hits']}"
        # token 校验路径：上游确实收到了 Bearer <token>
        assert MOCK["last_auth"] == "Bearer " + token, MOCK["last_auth"]
        # 落盘检查
        hist_path = os.path.join(data_dir, "history.jsonl")
        assert os.path.isfile(hist_path), "history.jsonl 未落盘"
        with open(hist_path, "r", encoding="utf-8") as f:
            assert any(json.loads(line).get("status") == "ok"
                       for line in f if line.strip()), "history.jsonl 里没有 ok 记录"
        assert os.path.isfile(os.path.join(data_dir, "token.json"))
        assert os.path.isfile(os.path.join(data_dir, "config.json"))
        assert os.path.isfile(os.path.join(data_dir, "sniper.log"))
    finally:
        client.close()
        stop_app()
        mock_server.shutdown()
        mock_server.server_close()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([os.path.abspath(__file__), "-x", "-q"]))
