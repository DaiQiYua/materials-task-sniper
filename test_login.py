#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pass 2 登录 / token 获取端到端测试（仿 test_webapp.py 的模式）。

- 线程里起 mock 上游（port 0）：``POST /api/auth/user/login``（好/坏账号、三种
  响应形状、可切 5xx）、任务列表（可用 token 集合模拟 401）、``/mock-login``
  交互式登录页（点按钮 → fetch /api/auth/me + 写 localStorage）。
- 线程里起 uvicorn（port 0），``MATERIALS_DATA_DIR`` 指向 tempfile.mkdtemp()。
- 覆盖：交互式会话捕获 token、凭据增删列查/脱敏、单账号取 token、fetch-any、
  自动续登（401 → paused → 自动换 token → running）、日志脱敏断言。

两种跑法都支持：
    python -m pytest test_login.py -x -q
    python test_login.py
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

GOOD_PHONE = "13800001111"
GOOD_PASSWORD = "pass123"
BAD_PHONE = "13900002222"
BAD_PASSWORD = "P@ssW0rd!XYZ"          # 唯一标记：任何日志/响应里都不许出现

# ---------------------------------------------------------------- mock 上游
MOCK = {
    "login_hits": 0,
    "list_hits": 0,
    "issued": [],        # 发出过的全部 token（断言日志里没有裸 token）
    "revoked": set(),    # 已作废 token（模拟上游 401）
    "shape": "data",     # data | nested | bare | garbage
    "force_5xx": False,
    "last_auth": "",
}


def _b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_ISSUE_LOCK = threading.Lock()
_ISSUE_SEQ = {"n": 0}


def fake_jwt(exp_offset: float = 7 * 24 * 3600) -> str:
    """真实结构的假 JWT（header.payload.signature，payload 带 exp/jti）。"""
    with _ISSUE_LOCK:
        _ISSUE_SEQ["n"] += 1
        seq = _ISSUE_SEQ["n"]
    header = _b64({"alg": "HS256", "typ": "JWT"})
    now = int(time.time())
    payload = _b64({"sub": GOOD_PHONE, "iat": now, "nbf": now,
                    "exp": int(now + exp_offset), "jti": f"tok-{seq}-{now}"})
    token = f"{header}.{payload}.fakesignature{seq}"
    with _ISSUE_LOCK:
        MOCK["issued"].append(token)
    return token


def _token_of(auth: str) -> str:
    auth = auth or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>mock login</title></head>
<body>
<h3>mock 登录页</h3>
<button id="login">登录</button>
<script>
const JWT = "__JWT__";
document.getElementById('login').addEventListener('click', async () => {
  localStorage.setItem('pp_user_token', JWT);
  try {
    await fetch('/api/auth/me', {headers: {Authorization: 'Bearer ' + JWT}});
  } catch (e) {}
});
setTimeout(() => { try { document.getElementById('login').click(); } catch (e) {} }, 300);
</script>
</body></html>
"""


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    # ---- 工具 ----
    def _send(self, code: int, obj) -> None:
        if isinstance(obj, str):
            body = obj.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth(self) -> str:
        auth = self.headers.get("authorization") or ""
        MOCK["last_auth"] = auth
        return auth

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("content-length") or 0)
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            obj = {}
        return obj if isinstance(obj, dict) else {}

    def _authorized(self) -> bool:
        token = _token_of(self._auth())
        return bool(token) and token not in MOCK["revoked"]

    # ---- GET ----
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/mock-login":
            token = fake_jwt()
            self._send(200, LOGIN_HTML.replace("__JWT__", token))
            return
        if path == "/api/auth/me":
            if not self._authorized():
                self._send(401, {"success": False, "message": "token 无效"})
                return
            self._send(200, {"success": True,
                             "data": {"phone": GOOD_PHONE, "nickname": "tester"}})
            return
        if path == "/api/user/tasks":
            if not self._authorized():
                self._send(401, {"success": False, "message": "token 无效或已过期"})
                return
            MOCK["list_hits"] += 1
            self._send(200, {"success": True, "data": {
                "items": [{"id": 999, "taskType": "standard", "title": "测试任务A",
                           "baseCommission": "20.00", "remainingQuota": 0,
                           "claimEligibility": {"canClaim": False, "accounts": []}}],
                "total": 1}})
            return
        if path == "/api/user/tasks/claim-limits":
            if not self._authorized():
                self._send(401, {"success": False, "message": "token 无效或已过期"})
                return
            now_ms = int(time.time() * 1000)
            self._send(200, {"success": True, "data": {
                "serverNowMs": now_ms, "nextAllowedAtMs": 0,
                "pendingCount": 0, "pendingLimit": 3, "cooldownSeconds": 10,
                "recentCount": 0, "windowLimit": 3}})
            return
        self._send(404, {"success": False, "message": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/user/login":
            MOCK["login_hits"] += 1
            body = self._read_json()
            phone = str(body.get("phone") or "")
            password = str(body.get("password") or "")
            if MOCK["force_5xx"]:
                # 上游故障：故意把提交内容原样回显，用来验证日志脱敏
                self._send(500, {"success": False,
                                 "message": f"服务异常 phone={phone} password={password}"})
                return
            if phone != GOOD_PHONE:
                self._send(400, {"success": False, "message": f"账号不存在 {phone}"})
                return
            if password != GOOD_PASSWORD:
                # 故意回显 phone，验证后端脱敏（不允许 phone/password 进日志/响应）
                self._send(400, {"success": False,
                                 "message": f"密码错误 phone={phone}"})
                return
            token = fake_jwt()
            shape = MOCK["shape"]
            if shape == "nested":       # 多层嵌套：token 在深度 3 上
                self._send(200, {"success": True, "result": {
                    "payload": {"session": {"accessToken": token}}}})
            elif shape == "bare":       # 裸 token 形状
                self._send(200, {"token": token})
            elif shape == "garbage":    # key 含 token 但值不是 JWT → 必须被判无效
                self._send(200, {"success": True, "data": {"token": "not-a-jwt"}})
            else:                       # 契约主形状
                self._send(200, {"success": True,
                                 "data": {"token": token, "user": {"phone": phone}}})
            return
        if "/api/user/tasks" in path:
            self._send(404, {"success": False, "message": "not found"})
            return
        self._send(404, {"success": False, "message": "not found"})


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
        thread.join(timeout=20)

    return server, f"http://127.0.0.1:{port}", stop


def _wait_phase(client: httpx.Client, wanted: tuple, timeout: float) -> dict:
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = client.get("/api/login-session").json()
        if state.get("phase") in wanted:
            return state
        time.sleep(0.25)
    return state


def _wait_state(client: httpx.Client, wanted: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    status: dict = {}
    while time.time() < deadline:
        status = client.get("/api/status").json()
        if status.get("state") == wanted:
            return status
        time.sleep(0.2)
    return status


def _saved_token(data_dir: str) -> str:
    """读 token.json 里的 token（测试内部用，绝不打印）。"""
    try:
        obj = json.loads((Path(data_dir) / "token.json").read_text("utf-8"))
        return str(obj.get("token") or "")
    except Exception:
        return ""


# ===================================================================
# 1) 凭据存储 / DPAPI 单元测试
# ===================================================================
def test_credentials_dpapi_unit():
    from server import persist
    from server.credentials import (CredentialStore, dpapi_protect,
                                   dpapi_unprotect, mask_phone, verify_dpapi)

    data_dir = tempfile.mkdtemp(prefix="sniper-cred-")
    old = os.environ.get("MATERIALS_DATA_DIR")
    os.environ["MATERIALS_DATA_DIR"] = data_dir
    try:
        info = verify_dpapi()
        assert isinstance(info, dict), info
        assert info["mode"] in ("dpapi", "plain"), info
        assert "detail" in info and "scope" in info

        # DPAPI 往返（不可用时降级明文，往返仍然成立）
        assert dpapi_unprotect(dpapi_protect(BAD_PASSWORD)) == BAD_PASSWORD
        if info["available"]:
            assert BAD_PASSWORD not in dpapi_protect(BAD_PASSWORD)

        store = CredentialStore(persist.data_dir())
        item = store.add("主号", GOOD_PHONE, BAD_PASSWORD)
        assert item["id"] == 1, item
        assert item["phone"] == "138****1111", item
        assert item["has_password"] is True
        assert item["last_ok_at"] is None and item["last_error"] == ""
        assert "password_enc" not in json.dumps(item), item
        assert '"password"' not in json.dumps(item), item
        assert BAD_PASSWORD not in json.dumps(item), item
        assert mask_phone("13800005678") == "138****5678"

        assert store.password_for(1) == BAD_PASSWORD
        assert store.list_public()[0]["phone"] == "138****1111"

        # 文件里没有明文密码
        text = (Path(data_dir) / "credentials.json").read_text(encoding="utf-8")
        assert GOOD_PHONE in text, "phone 应可见（脱敏只在 API 层）"
        assert "password_enc" in text
        if info["mode"] == "dpapi":
            assert BAD_PASSWORD not in text, "credentials.json 里出现了密码明文！"

        # 手机号校验（与前端 /^1[3-9]\\d{9}$/ 一致）
        for bad in ("12345678901", "1380000111", "138000011112", "23800001111", ""):
            try:
                store.add("x", bad, "p")
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法手机号未被拒绝：{bad!r}")

        store.mark_ok(1)
        assert store.get(1)["last_ok_at"] is not None
        store.mark_error(1, "登录失败")
        assert store.get(1)["last_error"] == "登录失败"
        assert store.delete(1) is True and store.delete(1) is False
        assert store.list_public() == []
    finally:
        if old is None:
            os.environ.pop("MATERIALS_DATA_DIR", None)
        else:
            os.environ["MATERIALS_DATA_DIR"] = old


# ===================================================================
# 2) 端到端：交互式会话 + 凭据流 + 自动续登 + 日志脱敏
# ===================================================================
def test_login_end_to_end():
    MOCK["login_hits"] = 0
    MOCK["list_hits"] = 0
    MOCK["issued"] = []
    MOCK["revoked"] = set()
    MOCK["shape"] = "data"
    MOCK["force_5xx"] = False

    data_dir = tempfile.mkdtemp(prefix="sniper-login-")
    os.environ["MATERIALS_DATA_DIR"] = data_dir

    mock_server, upstream = _start_mock()
    app_server, base, stop_app = _start_app()
    client = httpx.Client(base_url=base, timeout=30.0)
    try:
        # ---- 0) base 指向 mock；空凭据表 --------------------------
        r = client.put("/api/config", json={"base": upstream, "poll_interval": 0.3,
                                            "rate_limit_max": 60,
                                            "rate_limit_window": 1.0})
        assert r.status_code == 200, r.text
        listing = client.get("/api/credentials").json()
        assert listing["items"] == [], listing
        assert listing["encryption"]["mode"] in ("dpapi", "plain"), listing
        assert listing["dpapi"] is (listing["encryption"]["mode"] == "dpapi"), listing

        # 没有会话时：DELETE 幂等 200，GET 为 idle
        assert client.delete("/api/login-session").status_code == 200
        idle = client.get("/api/login-session").json()
        assert idle["active"] is False and idle["phase"] == "idle", idle

        # ---- 1) 交互式会话：Playwright 捕获 token ------------------
        page_url = upstream + "/mock-login"
        r = client.post("/api/login-session",
                        json={"mode": "interactive", "debug": True,
                              "target_url": page_url})
        assert r.status_code == 200, r.text
        started = r.json()
        assert started["active"] is True and started["session_id"], started

        # 已存在会话 → 409
        r = client.post("/api/login-session",
                        json={"mode": "interactive", "debug": True,
                              "target_url": page_url})
        assert r.status_code == 409, (r.status_code, r.text)

        state = _wait_phase(client, ("captured", "error", "closed"), 120)
        assert state.get("phase") == "captured", state
        assert state.get("active") is False, state
        assert state.get("started_at") and state.get("elapsed") is not None
        # 手机号只以打码形式出现在会话状态里
        assert GOOD_PHONE not in json.dumps(state, ensure_ascii=False), state

        status = client.get("/api/status").json()
        assert status["token"]["present"] is True, status
        assert status["token"]["valid"] is True, status
        assert status["token"]["exp"], status

        # DELETE 幂等（连删两次都 200）
        assert client.delete("/api/login-session").status_code == 200
        assert client.delete("/api/login-session").status_code == 200
        # 非 http/https 的 target_url → 400
        assert client.post("/api/login-session",
                           json={"target_url": "file:///C:/windows/win.ini"}
                           ).status_code == 400
        # 非 interactive 模式 → 400
        assert client.post("/api/login-session",
                           json={"mode": "headless-shell"}).status_code == 400

        # ---- 2) 凭据增删列查 --------------------------------------
        r = client.post("/api/credentials",
                        json={"name": "坏号", "phone": BAD_PHONE,
                              "password": BAD_PASSWORD})
        assert r.status_code == 201, r.text
        bad = r.json()
        r = client.post("/api/credentials",
                        json={"name": "主号", "phone": GOOD_PHONE,
                              "password": GOOD_PASSWORD})
        assert r.status_code == 201, r.text
        good = r.json()
        assert bad["id"] != good["id"]
        assert bad["phone"] == "139****2222" and good["phone"] == "138****1111"

        # 手机号不合 /^1[3-9]\\d{9}$/ → 422；缺密码 → 422
        assert client.post("/api/credentials",
                           json={"name": "x", "phone": "12345678901",
                                 "password": "p"}).status_code == 422
        assert client.post("/api/credentials",
                           json={"phone": GOOD_PHONE}).status_code == 422

        raw = client.get("/api/credentials").text
        assert GOOD_PASSWORD not in raw and BAD_PASSWORD not in raw, raw
        assert "password_enc" not in raw, raw
        assert '"password"' not in raw, raw
        items = client.get("/api/credentials").json()["items"]
        assert len(items) == 2, items

        # ---- 3) 单账号取 token：坏账号失败且错误已脱敏 -------------
        r = client.post(f"/api/credentials/{bad['id']}/fetch-token")
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["ok"] is False, body
        assert body.get("error"), body
        assert BAD_PASSWORD not in r.text, r.text
        assert BAD_PHONE not in r.text, r.text        # 上游回显的 phone 也要滤掉

        # 上游 5xx → 502
        MOCK["force_5xx"] = True
        try:
            r = client.post(f"/api/credentials/{good['id']}/fetch-token")
        finally:
            MOCK["force_5xx"] = False
        assert r.status_code == 502, (r.status_code, r.text)

        # 不存在的凭据 → 404
        assert client.post("/api/credentials/99999/fetch-token").status_code == 404

        # ---- 4) 好账号成功 + 递归提取各形状 ----------------------
        r = client.post(f"/api/credentials/{good['id']}/fetch-token")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert body["token_saved"] is True and body["valid"] is True, body
        assert body["exp"], body
        assert body["phone"] == GOOD_PHONE, body
        first_token = _saved_token(data_dir)
        assert first_token, "fetch-token 应把 token 落盘"

        for shape in ("nested", "bare"):
            MOCK["shape"] = shape
            r = client.post(f"/api/credentials/{good['id']}/fetch-token")
            assert r.status_code == 200, (shape, r.text)
            assert r.json()["ok"] is True, (shape, r.json())
        # key 含 token 但值不是 JWT → 必须判失败（jwt_exp 验证）
        MOCK["shape"] = "garbage"
        r = client.post(f"/api/credentials/{good['id']}/fetch-token")
        assert r.status_code == 200 and r.json()["ok"] is False, r.text
        MOCK["shape"] = "data"

        # 凭据文件里没有密码明文
        cred_text = (Path(data_dir) / "credentials.json").read_text(encoding="utf-8")
        if client.get("/api/credentials").json()["encryption"]["mode"] == "dpapi":
            assert BAD_PASSWORD not in cred_text and GOOD_PASSWORD not in cred_text

        # 结果回写：好账号 last_ok_at 有值，坏账号 last_error 有值（且已脱敏）
        after = {i["id"]: i for i in client.get("/api/credentials").json()["items"]}
        assert after[good["id"]]["last_ok_at"], after[good["id"]]
        assert after[bad["id"]]["last_error"], after[bad["id"]]
        assert BAD_PASSWORD not in json.dumps(after, ensure_ascii=False)
        assert BAD_PHONE not in json.dumps(after, ensure_ascii=False)

        # ---- 5) fetch-any：跳过坏账号，命中好账号 -----------------
        r = client.post("/api/token/fetch-any")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert body["phone"] == GOOD_PHONE, body
        assert body["attempts"] and body["attempts"][0]["phone"] == BAD_PHONE, body
        assert body["attempts"][0]["error"], body
        # attempts 的 phone 是账号标识（契约形状 {phone,error}），但密码必须不出现
        assert BAD_PASSWORD not in r.text and GOOD_PASSWORD not in r.text, r.text

        # ---- 6) 自动续登：401 → paused → 换新 token → running -----
        assert client.delete(f"/api/credentials/{bad['id']}").status_code == 200
        assert bad["id"] not in [i["id"] for i in
                                client.get("/api/credentials").json()["items"]]

        old_token = _saved_token(data_dir)
        r = client.put("/api/token", json={"token": old_token})
        assert r.status_code == 200, r.text
        assert client.post("/api/start").status_code == 200
        assert _wait_state(client, "running", 15)["state"] == "running"

        # 让 mock 只认新 token：作废旧 token → 引擎收到 401 → paused
        # （自动续登很快，paused 窗口可能 <50ms；抓不到就以日志断言兜底）
        MOCK["revoked"].add(old_token)
        deadline = time.time() + 25
        while time.time() < deadline:
            state_now = client.get("/api/status").json()
            if state_now["state"] == "paused":
                assert "token" in (state_now.get("state_reason") or "").lower(), state_now
                break
            if _saved_token(data_dir) != old_token:
                break
            time.sleep(0.05)

        resumed = _wait_state(client, "running", 30)
        assert resumed["state"] == "running", resumed
        new_token = _saved_token(data_dir)
        assert new_token != old_token, "自动续登后 token 应该换新"
        assert resumed["token"]["present"] and resumed["token"]["valid"], resumed
        assert client.post("/api/stop").status_code == 200

        # ---- 7) 日志脱敏：密码与裸 token 一律不进日志 -------------
        log_text = (Path(data_dir) / "sniper.log").read_text(encoding="utf-8",
                                                             errors="replace")
        assert "自动续登成功" in log_text, log_text[-2000:]
        assert "已暂停" in log_text, "自动续登应以 401→paused 为触发点"
        assert BAD_PASSWORD not in log_text, "密码泄露到 sniper.log！"
        assert GOOD_PASSWORD not in log_text, "密码泄露到 sniper.log！"
        for token in set(MOCK["issued"]):
            assert token not in log_text, "token 泄露到 sniper.log！"
            assert token.split(".")[1] not in log_text, "token payload 泄露到 sniper.log！"

        # SSE 快照里的 logs 同样干净
        first = None
        with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200, resp.status_code
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    first = json.loads(line[len("data:"):].strip())
                    break
        assert first is not None and first["type"] == "snapshot", first
        logs_blob = json.dumps(first["logs"], ensure_ascii=False)
        assert BAD_PASSWORD not in logs_blob and GOOD_PASSWORD not in logs_blob
        for token in set(MOCK["issued"]):
            assert token not in logs_blob
        assert any("自动续登成功" in (entry.get("msg") or "")
                   for entry in first["logs"]), first["logs"][-5:]

        assert MOCK["login_hits"] >= 3, MOCK["login_hits"]
        assert (Path(data_dir) / "credentials.json").is_file()
    finally:
        client.close()
        stop_app()
        mock_server.shutdown()
        mock_server.server_close()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([os.path.abspath(__file__), "-x", "-q"]))
