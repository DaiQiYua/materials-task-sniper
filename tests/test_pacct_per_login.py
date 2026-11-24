#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台子账号「按登录账号归属」能力测试。

覆盖（对应规格 A/B/C/E + 启动迁移）：
1. ``persist`` 的 ``tokens.json`` / ``platform_accounts.json`` 读写往返、
   文件缺失、文件损坏容错；
2. ``CredentialStore.set_token`` 只写 ``tokens.json``，**不**写 ``token.json``；
3. ``activate=false`` 的 fetch-token 不改 ``token.json`` / 引擎活跃 token；
4. ``GET /api/platform-accounts/by-login`` 响应结构、``stale``、``missing_token``；
5. ``_token_for_platform_account()`` 归属解析 + 抢单时 ``_api_call`` 收到的是
   owner token（monkeypatch 捕获 ``token`` 实参）；
6. ``delete(cred_id)`` 级联清理 token 与平台缓存；
7. 启动时的旧全局 Token 一次性迁移（分配给 ``last_ok_at`` 最大的账号）且幂等。

两种跑法都支持：
    python -m pytest tests/test_pacct_per_login.py -x -q
    python tests/test_pacct_per_login.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx  # noqa: F401 - TestClient 依赖 httpx（starlette 内部使用）
import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GOOD_PHONE = "13800001111"
GOOD_PASSWORD = "pass123"


def b64(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_jwt(tag: str = "a", exp_offset: float = 7 * 24 * 3600) -> str:
    """真实结构的假 JWT（header.payload.signature，payload 带 exp）。"""
    now = int(time.time())
    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({"sub": tag, "iat": now, "nbf": now,
                   "exp": int(now + exp_offset), "jti": tag})
    return f"{header}.{payload}.sig-{tag}"


TOK_A = fake_jwt("a")
TOK_B = fake_jwt("b")
TOK_LEGACY = fake_jwt("legacy")

ACCOUNT_2175 = {
    "id": 2175, "platform": "xiaohongshu", "platformName": "小红书",
    "account": "13800001111", "realName": "张三", "verifyStatus": "verified",
    "selfDisabled": False, "activeClaimCount": 0,
}


# ---------------------------------------------------------------- mock 上游
MOCK = {
    "by_token": {},          # token -> 平台子账号列表
    "platform_hits": 0,
    "claim_auth": [],        # 领取请求里的 Authorization
}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _token(self) -> str:
        auth = self.headers.get("authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/user/platform-accounts":
            MOCK["platform_hits"] += 1
            self._send(200, {"success": True,
                             "data": MOCK["by_token"].get(self._token(), [])})
            return
        if path == "/api/user/tasks/claim-limits":
            now_ms = int(time.time() * 1000)
            self._send(200, {"success": True, "data": {
                "serverNowMs": now_ms, "nextAllowedAtMs": 0,
                "pendingCount": 0, "pendingLimit": 3}})
            return
        self._send(404, {"success": False, "message": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if "/claim" in path:
            MOCK["claim_auth"].append(self.headers.get("authorization") or "")
            self._send(201, {"success": True, "data": {
                "id": 1, "taskId": 42, "platformAccountId": 2175,
                "claimLimits": {"retryAfterSeconds": 0}}})
            return
        self._send(404, {"success": False, "message": "not found"})


@pytest.fixture()
def mock_upstream():
    MOCK["by_token"] = {}
    MOCK["platform_hits"] = 0
    MOCK["claim_auth"] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MATERIALS_DATA_DIR", str(directory))
    return directory


def _client(app_base: str = "http://127.0.0.1:18999"):
    """TestClient（Host 必须是 localhost/127.0.0.1，否则同源中间件回 403）。"""
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app, base_url=app_base)


# ===================================================================
# 1) persist：tokens / platform cache 往返、缺失、损坏
# ===================================================================
def test_persist_tokens_and_platform_cache_roundtrip(tmp_path):
    from server import persist

    directory = tmp_path / "persist"

    # 文件缺失 → 空 dict（不抛）
    assert persist.load_tokens(directory) == {}
    assert persist.load_platform_cache(directory) == {}

    # 写入 → 读回（int 键、字段齐全）
    persist.save_tokens({3: {"token": TOK_A, "exp": 111, "saved_at": 222.5},
                         5: {"token": TOK_B, "exp": None, "saved_at": 333.0,
                             "adopted": True}}, directory)
    tokens = persist.load_tokens(directory)
    assert set(tokens) == {3, 5}, tokens
    assert tokens[3]["token"] == TOK_A and tokens[3]["exp"] == 111
    assert tokens[3]["saved_at"] == 222.5
    assert tokens[5]["exp"] is None and tokens[5].get("adopted") is True
    raw = json.loads((directory / "tokens.json").read_text("utf-8"))
    assert raw["version"] == 1
    assert isinstance(raw["items"], dict) and "3" in raw["items"]   # JSON 字符串键

    cache_in = {3: {"fetched_at": 444.0, "fallback": True,
                    "items": [{"id": 2175, "name": "张三"}]},
                5: {"fetched_at": 555.0, "fallback": False, "items": []}}
    persist.save_platform_cache(cache_in, directory)
    cache = persist.load_platform_cache(directory)
    assert set(cache) == {3, 5}, cache
    assert cache[3]["fallback"] is True
    assert cache[3]["fetched_at"] == 444.0
    assert cache[3]["items"][0]["id"] == 2175
    assert cache[5]["items"] == []

    # 损坏文件 → 返回 {}（记 WARN，不抛）
    (directory / "tokens.json").write_text("{ not json", encoding="utf-8")
    (directory / "platform_accounts.json").write_text("[1,2,3]", encoding="utf-8")
    assert persist.load_tokens(directory) == {}
    assert persist.load_platform_cache(directory) == {}

    # 结构合法但条目脏 → 跳过脏条目，保留好的
    (directory / "tokens.json").write_text(json.dumps({
        "version": 1,
        "items": {"1": {"token": TOK_A}, "bad": {"token": TOK_B},
                  "2": {"token": ""}, "3": "not-a-dict"},
    }), encoding="utf-8")
    tokens = persist.load_tokens(directory)
    assert set(tokens) == {1} and tokens[1]["token"] == TOK_A, tokens


# ===================================================================
# 2) set_token 只写 tokens.json；3) delete 级联清理
# ===================================================================
def test_set_token_writes_only_tokens_file(data_dir):
    from server import persist
    from server.credentials import CredentialStore, mask_phone

    store = CredentialStore(data_dir)
    item = store.add("主号", GOOD_PHONE, GOOD_PASSWORD)
    cid = item["id"]
    assert item["has_token"] is False and item["token_exp"] is None
    assert item["last_platform_error"] == ""
    assert mask_phone(GOOD_PHONE) == item["phone"]

    store.set_token(cid, TOK_A, exp=1234)
    assert store.get_token(cid) == TOK_A
    info = store.get_token_info(cid)
    assert info["token"] == TOK_A and info["exp"] == 1234
    assert isinstance(info["saved_at"], float)
    assert (data_dir / "tokens.json").is_file()
    # **不**写 token.json（引擎活跃 token 由引擎自己维护）
    assert not (data_dir / "token.json").exists()

    public = store.list_public()[0]
    assert public["has_token"] is True and public["token_exp"] == 1234
    assert public["adopted"] is False
    # 列表接口永不返回 token 本体 / 密码
    blob = json.dumps(store.list_public(), ensure_ascii=False)
    assert TOK_A not in blob and "password_enc" not in blob
    assert persist.load_token(data_dir) is None

    # clear_token 清掉
    assert store.clear_token(cid) is True
    assert store.get_token(cid) is None
    assert store.list_public()[0]["has_token"] is False


def test_delete_cascades_token_and_platform_cache(data_dir):
    from server import persist
    from server.credentials import CredentialStore

    store = CredentialStore(data_dir)
    keep = store.add("保留", "13900002222", GOOD_PASSWORD)
    drop = store.add("删除", GOOD_PHONE, GOOD_PASSWORD)
    store.set_token(keep["id"], TOK_A, exp=1)
    store.set_token(drop["id"], TOK_B, exp=2)
    persist.save_platform_cache({
        keep["id"]: {"fetched_at": time.time(), "fallback": False,
                     "items": [{"id": 2175}]},
        drop["id"]: {"fetched_at": time.time(), "fallback": False,
                     "items": [{"id": 9999}]},
    }, data_dir)

    assert store.delete(drop["id"]) is True

    tokens = persist.load_tokens(data_dir)
    assert drop["id"] not in tokens and keep["id"] in tokens, tokens
    cache = persist.load_platform_cache(data_dir)
    assert drop["id"] not in cache and keep["id"] in cache, cache
    assert store.delete(drop["id"]) is False        # 幂等


# ===================================================================
# 4) 归属解析 + 抢单用 owner token
# ===================================================================
def test_token_for_platform_account_and_claim_uses_owner_token(data_dir, monkeypatch):
    from server import persist
    from server.core import SniperEngine
    from server.credentials import CredentialStore

    store = CredentialStore(data_dir)
    cid_a = store.add("A", GOOD_PHONE, GOOD_PASSWORD)["id"]
    cid_b = store.add("B", "13900002222", GOOD_PASSWORD)["id"]
    store.set_token(cid_a, TOK_A, exp=999999999)
    store.set_token(cid_b, TOK_B, exp=999999999)
    # 平台子账号 2175 属于 B 号；2176 属于 A 号
    persist.save_platform_cache({
        cid_a: {"fetched_at": time.time(), "fallback": False,
                "items": [{"id": 2176, "name": "A-账号"}]},
        cid_b: {"fetched_at": time.time(), "fallback": False,
                "items": [{"id": 2175, "name": "B-账号"}]},
    }, data_dir)

    engine = SniperEngine(lambda event: None)
    engine.attach_credentials(store)
    engine.refresh_platform_index()

    assert engine.owner_of_platform_account(2175) == cid_b
    assert engine.owner_of_platform_account(2176) == cid_a
    assert engine._token_for_platform_account(2175) == TOK_B
    assert engine._token_for_platform_account(2176) == TOK_A
    assert engine._token_for_platform_account(12345) is None
    # 未注入/无缓存时也不炸
    assert engine._token_for_platform_account("oops") is None

    # 抢单：捕获 _api_call 收到的 token 实参
    captured: list = []

    def fake_api_call(method, path, body=None, cfg=None, blocking=True,
                      kind="claim", token=None, cred_id=None):
        captured.append({"method": method, "path": path, "kind": kind,
                         "token": token, "body": body})
        return (201, {"success": True,
                      "data": {"id": 1, "claimLimits": {"retryAfterSeconds": 0}}})

    monkeypatch.setattr(engine, "_api_call", fake_api_call, raising=False)
    engine._claim({"id": 42, "title": "测试任务"}, "standard", 2175, 0)

    assert captured, "抢单没有发出请求"
    assert captured[0]["path"] == "/api/user/tasks/42/claim"
    assert captured[0]["token"] == TOK_B, captured[0]
    assert captured[0]["body"] == {"platformAccountId": 2175}

    logs = json.dumps(engine.logs(), ensure_ascii=False)
    assert f"用账号 #{cid_b} 的 Token" in logs, logs

    # 归属未知 → 回退活跃 token（token 实参为 None，由 _api_call 内部取 self._token）
    captured.clear()
    engine._claim({"id": 43, "title": "未知归属"}, "standard", 88888, 0)
    assert captured and captured[0]["token"] is None, captured


# ===================================================================
# 5) fetch-token?activate=false：不写 token.json、不改活跃 token
# ===================================================================
def test_fetch_token_activate_false_keeps_global_token(data_dir, mock_upstream,
                                                       monkeypatch):
    from server import login as login_mod

    def fake_api_login(base, phone, password, log=None):
        return (True, TOK_A)

    monkeypatch.setattr(login_mod, "api_login", fake_api_login)
    MOCK["by_token"] = {TOK_A: [ACCOUNT_2175]}

    with _client() as client:
        app = client.app
        assert client.put("/api/config", json={"base": mock_upstream}).status_code == 200
        created = client.post("/api/credentials",
                              json={"name": "A", "phone": GOOD_PHONE,
                                    "password": GOOD_PASSWORD})
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        assert created.json()["has_token"] is False

        # 先模拟「已经有一个轮询 Token」，验证 activate=false 不会改动它
        app.state.engine.set_token(TOK_B)
        assert (data_dir / "token.json").is_file()

        # activate=false：只更新该账号自己的 token
        r = client.post(f"/api/credentials/{cid}/fetch-token?activate=false")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["token_saved"] is True, body
        assert isinstance(body["platform_accounts"], list) and body["platform_accounts"]
        assert body["platform_accounts"][0]["id"] == 2175
        assert body["platform_error"] is None

        saved = json.loads((data_dir / "token.json").read_text("utf-8"))["token"]
        assert saved == TOK_B, "已有轮询 Token 时，activate=false 不该覆盖 token.json"
        tokens = json.loads((data_dir / "tokens.json").read_text("utf-8"))["items"]
        assert tokens[str(cid)]["token"] == TOK_A
        # 引擎活跃 token 未变
        status = client.get("/api/status").json()
        assert status["token"]["present"] is True, status
        # 平台缓存已落盘
        cache = json.loads((data_dir / "platform_accounts.json").read_text("utf-8"))
        assert cache["items"][str(cid)]["items"][0]["id"] == 2175
        # 凭据列表里能看到 has_token
        listing = {i["id"]: i for i in client.get("/api/credentials").json()["items"]}
        assert listing[cid]["has_token"] is True
        assert listing[cid]["token_exp"] == int(json.loads(
            base64.urlsafe_b64decode(TOK_A.split(".")[1] + "=="))["exp"])

        # 默认 activate=true：旧行为不变（写 token.json + 成为活跃 token）
        r = client.post(f"/api/credentials/{cid}/fetch-token")
        assert r.status_code == 200, r.text
        assert (data_dir / "token.json").is_file(), "默认 activate=true 必须写 token.json"
        saved = json.loads((data_dir / "token.json").read_text("utf-8"))["token"]
        assert saved == TOK_A
        assert client.get("/api/status").json()["token"]["present"] is True


def test_fetch_token_activate_false_adopts_when_no_active_token(data_dir,
                                                               mock_upstream,
                                                               monkeypatch):
    """引擎没有任何轮询 Token 时，activate=false 会自动采用该账号的 Token。

    这样去掉界面上的「设为当前」后，新装用户添加第一个账号即可开始轮询。
    """
    from server import login as login_mod

    def fake_api_login(base, phone, password, log=None):
        return (True, TOK_A)

    monkeypatch.setattr(login_mod, "api_login", fake_api_login)
    MOCK["by_token"] = {TOK_A: [ACCOUNT_2175]}

    with _client() as client:
        app = client.app
        assert client.put("/api/config", json={"base": mock_upstream}).status_code == 200
        created = client.post("/api/credentials",
                              json={"name": "首个账号", "phone": GOOD_PHONE,
                                    "password": GOOD_PASSWORD})
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        assert app.state.engine.has_token() is False
        assert not (data_dir / "token.json").exists()

        r = client.post(f"/api/credentials/{cid}/fetch-token?activate=false")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["token_saved"] is True, body

        assert app.state.engine.has_token() is True
        assert client.get("/api/status").json()["token"]["present"] is True
        saved = json.loads((data_dir / "token.json").read_text("utf-8"))["token"]
        assert saved == TOK_A, "无轮询 Token 时应自动采用该账号的 Token"


# ===================================================================
# 6) GET /api/platform-accounts/by-login
# ===================================================================
def test_platform_accounts_by_login_structure(data_dir, mock_upstream):
    from server import persist

    with _client() as client:
        app = client.app
        store = app.state.credentials
        one = client.post("/api/credentials", json={"name": "有 token",
                                                    "phone": GOOD_PHONE,
                                                    "password": GOOD_PASSWORD})
        two = client.post("/api/credentials", json={"name": "没 token",
                                                    "phone": "13900002222",
                                                    "password": GOOD_PASSWORD})
        assert one.status_code == 201 and two.status_code == 201
        cid_one, cid_two = one.json()["id"], two.json()["id"]

        store.set_token(cid_one, TOK_A, exp=int(time.time()) + 3600)
        MOCK["by_token"] = {TOK_A: [ACCOUNT_2175]}
        persist.save_platform_cache({
            cid_one: {"fetched_at": time.time(), "fallback": False,
                      "items": [ACCOUNT_2175]},
        }, data_dir)
        app.state.engine.refresh_platform_index()
        assert client.put("/api/config",
                          json={"base": mock_upstream,
                                "platform_account_id": 2175}).status_code == 200

        body = client.get("/api/platform-accounts/by-login").json()
        assert set(body) == {"items", "missing_token",
                             "default_platform_account_id",
                             "default_owner_cred_id"}, body
        assert set(body["items"]) == {str(cid_one), str(cid_two)}, body

        fresh = body["items"][str(cid_one)]
        assert fresh["count"] == 1 and fresh["stale"] is False
        assert fresh["fallback"] is False and fresh["adopted"] is False
        assert isinstance(fresh["fetched_at"], (int, float))
        assert fresh["items"][0]["id"] == 2175

        empty = body["items"][str(cid_two)]
        assert empty["count"] == 0 and empty["items"] == []
        assert empty["stale"] is True and empty["fetched_at"] is None

        assert body["missing_token"] == [cid_two], body
        assert body["default_platform_account_id"] == 2175
        assert body["default_owner_cred_id"] == cid_one, body

        # 缓存过期（> 6 小时）→ stale=True
        persist.save_platform_cache({
            cid_one: {"fetched_at": time.time() - 7 * 3600, "fallback": True,
                      "items": [ACCOUNT_2175]},
        }, data_dir)
        body = client.get("/api/platform-accounts/by-login").json()
        assert body["items"][str(cid_one)]["stale"] is True
        assert body["items"][str(cid_one)]["fallback"] is True

        # refresh-platform-accounts：一次上游调用 + 写缓存
        r = client.post(f"/api/credentials/{cid_one}/refresh-platform-accounts")
        assert r.status_code == 200, r.text
        refreshed = r.json()
        assert set(refreshed) == {"ok", "items", "fallback", "fetched_at",
                                  "error", "count"}, refreshed
        assert refreshed["ok"] is True and refreshed["count"] == 1
        assert refreshed["error"] is None
        # 没有 token 的账号 → no_token（200，不算异常）
        r = client.post(f"/api/credentials/{cid_two}/refresh-platform-accounts")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False and r.json()["error"] == "no_token"
        # 不存在的凭据 → 404
        assert client.post("/api/credentials/99999/refresh-platform-accounts"
                           ).status_code == 404

        # 旧接口语义不变（引擎活跃 token 路径，返回 items/fallback）
        legacy = client.get("/api/platform-accounts").json()
        assert set(legacy) == {"items", "fallback"}, legacy

        # 串行刷新所有有 token 的账号（refresh-all）
        r = client.post("/api/platform-accounts/refresh-all")
        assert r.status_code == 200, r.text
        all_refreshed = r.json()
        assert set(all_refreshed) == {"items", "took_ms"}, all_refreshed
        assert set(all_refreshed["items"]) == {str(cid_one)}, all_refreshed
        one_result = all_refreshed["items"][str(cid_one)]
        assert one_result["ok"] is True and one_result["count"] == 1
        assert one_result["error"] is None
        assert isinstance(all_refreshed["took_ms"], int)


# ===================================================================
# 7) 启动时旧全局 Token 一次性迁移（幂等）
# ===================================================================
def test_legacy_token_migrated_to_best_cred(data_dir, mock_upstream):
    from server import persist
    from server.credentials import CredentialStore

    store = CredentialStore(data_dir)
    older = store.add("旧", "13900002222", GOOD_PASSWORD)["id"]
    newer = store.add("新", GOOD_PHONE, GOOD_PASSWORD)["id"]
    store.mark_ok(older, ts=1000.0)
    store.mark_ok(newer, ts=2000.0)
    persist.save_token(TOK_LEGACY, data_dir)
    persist.save_config({"base": mock_upstream}, data_dir)
    MOCK["by_token"] = {TOK_LEGACY: [ACCOUNT_2175]}

    with _client() as client:
        # 迁移：token 分配给 last_ok_at 最大的 newer 账号
        tokens = persist.load_tokens(data_dir)
        assert set(tokens) == {newer}, tokens
        assert tokens[newer]["token"] == TOK_LEGACY
        assert tokens[newer]["exp"] == int(
            json.loads(base64.urlsafe_b64decode(
                TOK_LEGACY.split(".")[1] + "=="))["exp"])
        assert tokens[newer].get("adopted") is True
        # 迁移后立即拉了平台子账号写缓存
        cache = persist.load_platform_cache(data_dir)
        assert cache[newer]["items"][0]["id"] == 2175
        # 日志留痕
        logs = json.dumps(client.app.state.engine.logs(), ensure_ascii=False)
        assert f"旧全局 Token 已迁移到账号 #{newer}" in logs, logs
        # 归属索引可直接用
        assert client.app.state.engine.owner_of_platform_account(2175) == newer
        # by-login 上体现 adopted
        by_login = client.get("/api/platform-accounts/by-login").json()
        assert by_login["items"][str(newer)]["adopted"] is True
        assert by_login["items"][str(older)]["adopted"] is False
        assert older in by_login["missing_token"]

    # 幂等：再来一次（换了 token.json 内容）不覆盖已迁移结果
    persist.save_token(TOK_A, data_dir)
    with _client() as client:
        tokens = persist.load_tokens(data_dir)
        assert tokens[newer]["token"] == TOK_LEGACY, "tokens.json 已有内容时不得重复迁移"
        assert set(tokens) == {newer}
        logs = json.dumps(client.app.state.engine.logs(), ensure_ascii=False)
        assert "旧全局 Token 已迁移" not in logs


def test_pick_account_rotates_across_eligible_accounts(data_dir):
    """所有可领取账号轮流抢单，不再被 config.platform_account_id 钉死。

    ``data_dir`` 不是摆设：`engine.update_config()` 会 ``persist.save_config()``
    落盘。此前该用例没有绑定数据目录 fixture，于是把「全套默认值 +
    platform_account_id=2175」写进了**仓库根目录的 config.json**，覆盖用户真实
    配置（跑一次全量测试就污染一次）。
    """
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine.update_config({"platform_account_id": 2175})
        item = {"claimEligibility": {"accounts": [{"id": 11}, {"id": 12}, {"id": 13}]}}
        picked = [engine._pick_account(item) for _ in range(6)]
        assert picked == [11, 12, 13, 11, 12, 13], picked
        # 无可领取账号 → None（调用方会放弃该任务）
        assert engine._pick_account({"claimEligibility": {"accounts": []}}) is None
        # 非法 / 重复 id 被去重与跳过
        dup = {"claimEligibility": {"accounts": [{"id": 7}, {"id": 7}, {"id": "x"}, None]}}
        assert [engine._pick_account(dup) for _ in range(2)] == [7, 7]
    finally:
        engine.stop()


def test_cooldown_models_separate_request_task_pending(data_dir):
    """请求冷却(10s) / 任务冷却(同任务3天) / 待提交 三类分开，互不混淆。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        # 1) 请求冷却：10s，按登录账号 cred 归属，与任务类型无关
        engine._set_request_cooldown(1, {
            "serverNowMs": 1795000000000,
            "nextAllowedAtMs": 1795000010000,
            "retryAfterSeconds": 10,
            "pendingCount": 1, "pendingLimit": 3,
            "windowLimit": 3, "cooldownSeconds": 10, "reason": "cooldown",
        }, source="claim_response")
        req = engine.request_cooldowns()
        assert req["1"]["remaining_seconds"] > 0, req
        assert req["1"]["reason"] == "cooldown"
        assert req["1"]["seconds"] == 10

        # 2) 任务冷却：只对 (子账号, 任务) 生效；同类型其他任务不受影响
        engine._set_task_cooldown(2175, 15783, 3 * 86400, "standard", 1,
                                  source="claim_response")
        tasks = engine.task_cooldowns()
        assert len(tasks) == 1, tasks
        assert tasks[0]["task_id"] == 15783
        assert tasks[0]["task_type"] == "standard"
        assert tasks[0]["remaining_seconds"] > 3 * 86400 - 10
        # 同类型另一个任务没有被标记冷却
        assert not any(x["task_id"] == 15825 for x in tasks)

        # 3) 待提交：按 (子账号, 任务) 记录
        engine._set_pending(2175, 15783, "standard", 1, source="claim_response")
        pend = engine.pending_submissions()
        assert pend and pend[0]["task_id"] == 15783, pend

        # 合并到平台子账号：按账号过滤，请求冷却按 cred 附加
        items = engine._merge_cooldowns([{"id": 2175}, {"id": 9999}], cred_id=1)
        assert items[0]["task_cooldowns"][0]["task_id"] == 15783, items[0]
        assert items[1]["task_cooldowns"] == [], items[1]
        assert items[0]["request_cooldown"]["remaining_seconds"] > 0

        # status 里三类都在，供前端实时更新
        st = engine.status()
        assert st["request_cooldowns"]["1"]["until"] > time.time()
        assert st["task_cooldowns"][0]["task_id"] == 15783
        assert st["pending_submissions"][0]["task_id"] == 15783

        # 4) 持久化：重启后仍能恢复
        engine2 = SniperEngine(lambda event: None)
        try:
            assert engine2.request_cooldowns()["1"]["reason"] == "cooldown"
            assert engine2.task_cooldowns()[0]["task_id"] == 15783
            assert engine2.pending_submissions()[0]["task_id"] == 15783
        finally:
            engine2.stop()

        # 过期任务冷却自动清理；非法参数不登记、不抛
        engine._task_cooldowns[(2175, 15783)]["until"] = time.time() - 1
        assert engine.task_cooldowns() == []
        engine._set_task_cooldown(None, 1, 10)
        engine._set_task_cooldown(1, 1, "oops")
        engine._set_task_cooldown(1, 1, 0)
        assert engine.task_cooldowns() == []
    finally:
        engine.stop()


def test_eligibility_index_from_tasks(data_dir):
    """任务快照 + 本地记录 → 子账号 × 任务类型的可领/冷却/待提交。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine._tasks_map[1] = {
            "id": 1, "taskType": "standard",
            "claimEligibility": {"canClaim": True, "reason": "claimable",
                                 "accounts": [{"id": 2175, "canClaim": True,
                                               "nextClaimableAt": None}]},
        }
        engine._tasks_map[2] = {
            "id": 2, "taskType": "standard",
            "claimEligibility": {"canClaim": False, "reason": "pending_submission",
                                 "accounts": [{"id": 2175, "canClaim": False,
                                               "nextClaimableAt": "2099-01-01T00:00:00Z"}]},
        }
        engine._tasks_map[3] = {
            "id": 3, "taskType": "long_term",
            "claimEligibility": {"canClaim": False, "reason": "no_verified_account",
                                 "accounts": []},
        }
        idx = engine._eligibility_index()
        std = idx["2175"]["standard"]
        assert std["has_tasks"] is True
        assert 1 in std["claimable_task_ids"]
        assert 2 in std["pending_task_ids"]
        assert 2 in std["cooling_task_ids"]
        assert std["next_claimable_at"]
        # long_term 下这个账号没有出现在 accounts 里
        assert "2175" not in idx or "long_term" not in idx["2175"]

        # 没有任务下发时，本地记录也能撑起状态
        engine._set_task_cooldown(2175, 99, 3 * 86400, "standard", 1,
                                  source="claim_history")
        engine._set_pending(2175, 99, "standard", 1, source="claim_history")
        std2 = engine._eligibility_index()["2175"]["standard"]
        assert 99 in std2["cooling_task_ids"]
        assert 99 in std2["pending_task_ids"]
    finally:
        engine.stop()


def test_store_limits_sets_request_cooldown(data_dir, monkeypatch):
    """claim-limits 的 10s 冷却只写「请求冷却」，不写任务冷却。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        monkeypatch.setattr(engine, "_active_cred_id", lambda: 1, raising=False)
        engine._store_limits({
            "serverNowMs": 1795000000000,
            "nextAllowedAtMs": 1795000005000,
            "retryAfterSeconds": 5,
            "pendingCount": 1, "pendingLimit": 3,
            "windowLimit": 3, "cooldownSeconds": 10, "reason": "cooldown",
        }, cred_id=1)
        req = engine.request_cooldowns()
        assert req["1"]["reason"] == "cooldown"
        assert req["1"]["seconds"] == 5
        assert 3 < req["1"]["remaining_seconds"] <= 5.5, req["1"]
        # 任务冷却不应被 claim-limits 写入
        assert engine.task_cooldowns() == []
    finally:
        engine.stop()


def test_sync_claims_restores_task_cooldown_and_pending(data_dir, monkeypatch):
    """没任务下发时，领取记录也能补任务冷却 / 待提交。"""
    from server.core import SniperEngine
    from server.credentials import CredentialStore

    store = CredentialStore(data_dir)
    cid = store.add("A", GOOD_PHONE, GOOD_PASSWORD)["id"]
    store.set_token(cid, TOK_A, exp=999999999)

    engine = SniperEngine(lambda event: None)
    try:
        engine.attach_credentials(store)
        now = datetime.now(timezone.utc)
        claimed = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        approved = "2026-01-01T00:00:00.000Z"

        def fake_api_call(method, path, body=None, cfg=None, blocking=True,
                          kind="none", token=None):
            assert "/api/user/claims" in path
            return (200, {"success": True, "data": {"items": [
                {"id": 14002, "taskId": 15783, "taskType": "standard",
                 "platformAccountId": 2175, "claimedAt": claimed,
                 "status": "claimed"},
                {"id": 12000, "taskId": 15000, "taskType": "standard",
                 "platformAccountId": 2175, "claimedAt": approved,
                 "status": "approved"},
            ]}})

        monkeypatch.setattr(engine, "_api_call", fake_api_call, raising=False)
        r = engine.sync_claims_for_cred(cid)
        assert r["ok"] is True and r["updated"] == 2, r

        tasks = engine.task_cooldowns()
        assert any(x["task_id"] == 15783 and x["source"] == "claim_history"
                   for x in tasks), tasks
        # 已通过的领取不进入待提交
        pend = engine.pending_submissions()
        assert [p["task_id"] for p in pend] == [15783], pend

        # 下一次同步看到 approved → 清掉待提交
        def fake_api_call2(method, path, body=None, cfg=None, blocking=True,
                           kind="none", token=None):
            return (200, {"success": True, "data": {"items": [
                {"id": 14002, "taskId": 15783, "taskType": "standard",
                 "platformAccountId": 2175, "claimedAt": claimed,
                 "status": "approved"},
            ]}})

        monkeypatch.setattr(engine, "_api_call", fake_api_call2, raising=False)
        engine.sync_claims_for_cred(cid)
        assert engine.pending_submissions() == []
    finally:
        engine.stop()


def test_scheduler_newest_first_and_one_account_per_task(data_dir, monkeypatch):
    """最新任务优先；同一任务全局只分配给一个平台账号（不自己挤自己）。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        # 两个登录账号、两个平台账号（同平台）
        engine._pacct_owner = {11: 1, 12: 2}
        engine._pacct_meta = {11: {"verifyStatus": "verified"},
                              12: {"verifyStatus": "verified"}}
        engine._pacct_loaded = True
        monkeypatch.setattr(engine, "_cred_token",
                            lambda cid: f"tok{cid}", raising=False)
        engine._claim_threads = [object(), object()]     # 2 个 worker
        engine._sched.clear()

        older = {
            "id": 100, "taskType": "standard", "title": "旧任务",
            "createdAt": "2026-01-01T00:00:00.000Z", "remainingQuota": 1,
            "claimEligibility": {"canClaim": True, "accounts": [
                {"id": 11, "canClaim": True}, {"id": 12, "canClaim": True}]},
        }
        newer = {
            "id": 200, "taskType": "standard", "title": "新任务",
            "createdAt": "2026-02-01T00:00:00.000Z", "remainingQuota": 1,
            "claimEligibility": {"canClaim": True, "accounts": [
                {"id": 11, "canClaim": True}, {"id": 12, "canClaim": True}]},
        }
        now = time.monotonic()
        engine._tasks_map = {100: older, 200: newer}
        engine._task_first_seen = {100: now, 200: now}
        engine._task_last_seen = {100: now, 200: now}

        engine._schedule_once()
        assigned = dict(engine._task_assigned)
        assert set(assigned) == {100, 200}, assigned
        # 每个任务只分给一个账号；两个任务分给不同账号（负载均衡）
        assert assigned[100] != assigned[200], assigned
        # 新任务在堆顶（priority 更小）
        first = engine._sched.pop(0)
        assert first is not None and first[0]["id"] == 200, first
    finally:
        engine.stop()


def test_scheduler_respects_per_cred_cooldown(data_dir, monkeypatch):
    """一个登录账号冷却时，另一个账号仍可被调度。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine._pacct_owner = {11: 1, 12: 2}
        engine._pacct_meta = {11: {"verifyStatus": "verified"},
                              12: {"verifyStatus": "verified"}}
        engine._pacct_loaded = True
        monkeypatch.setattr(engine, "_cred_token",
                            lambda cid: f"tok{cid}", raising=False)
        engine._claim_threads = [object(), object()]
        engine._next_claim_at_by_cred = {1: time.time() + 30.0}  # cred 1 冷却
        engine._sched.clear()

        task = {
            "id": 300, "taskType": "standard", "title": "任务",
            "createdAt": "2026-03-01T00:00:00.000Z", "remainingQuota": 1,
            "claimEligibility": {"canClaim": True, "accounts": [
                {"id": 11, "canClaim": True}, {"id": 12, "canClaim": True}]},
        }
        now = time.monotonic()
        engine._tasks_map = {300: task}
        engine._task_first_seen = {300: now}
        engine._task_last_seen = {300: now}

        engine._schedule_once()
        assert engine._task_assigned.get(300) == 12, engine._task_assigned
    finally:
        engine.stop()


def test_poll_once_prefers_newest_tail_pages(data_dir, monkeypatch):
    """任务列表旧→新：首次回补从最后一页开始，之后每轮只取尾页。"""
    from server.core import EngineConfig, SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine._cfg = EngineConfig(tail_pages=2, total_refresh_seconds=10)
        calls: list = []

        def fake_get(cred_id, task_type, page, cfg, page_size=None):
            calls.append(page)
            if page == 1:
                return (200, [{"id": 1, "taskType": task_type}],
                        {"total": 25, "page_size": 10})
            return (200, [{"id": page * 100, "taskType": task_type}],
                    {"total": 25, "page_size": 10})

        ingested: list = []
        monkeypatch.setattr(engine, "_is_active", lambda: True, raising=False)
        monkeypatch.setattr(engine, "_get_tasks", fake_get, raising=False)
        monkeypatch.setattr(engine, "_ingest",
                            lambda tt, items, cid=0, rank=0: ingested.append(
                                (tt, [x["id"] for x in items])),
                            raising=False)

        engine._poll_once(1, "standard", engine._cfg)
        # 先 page1 校准 total；回补顺序 3 -> 2，page1 最后入库（旧任务不抢先）
        assert calls[:3] == [1, 3, 2], calls
        assert ingested[0] == ("standard", [300])     # page3 最新先入库
        assert ingested[1] == ("standard", [200])     # page2
        assert ingested[2] == ("standard", [1])       # page1 最旧

        calls.clear()
        ingested.clear()
        engine._poll_once(1, "standard", engine._cfg)
        # 10s 内不再回看 page1，只取最后 2 页（3 -> 2）
        assert calls == [3, 2], calls
        assert [x[1] for x in ingested] == [[300], [200]], ingested
    finally:
        engine.stop()


def test_scheduler_picks_front_account_and_spreads_tasks(data_dir, monkeypatch):
    """账号选择策略：单任务用排序靠前的账号；多任务各账号分别领不同任务。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        # 三个平台子账号，列表顺序：77（靠前）→ 11 → 99（靠后）
        engine._pacct_owner = {77: 1, 11: 1, 99: 1}
        engine._pacct_meta = {77: {"verifyStatus": "verified"},
                              11: {"verifyStatus": "verified"},
                              99: {"verifyStatus": "verified"}}
        engine._pacct_order = {77: 0, 11: 1, 99: 2}
        engine._pacct_loaded = True
        monkeypatch.setattr(engine, "_cred_token",
                            lambda cid: f"tok{cid}", raising=False)
        engine._claim_threads = [object(), object(), object()]
        engine._sched.clear()

        now = time.monotonic()

        # 1) 只有一个任务下发 → 排序靠前的账号 77 领取（不是 id 最小的 11）
        task = {
            "id": 400, "taskType": "standard", "title": "单任务",
            "createdAt": "2026-04-01T00:00:00.000Z", "remainingQuota": 1,
            "claimEligibility": {"canClaim": True, "accounts": [
                {"id": 11, "canClaim": True}, {"id": 77, "canClaim": True},
                {"id": 99, "canClaim": True}]},
        }
        engine._tasks_map = {400: task}
        engine._task_first_seen = {400: now}
        engine._task_last_seen = {400: now}
        engine._schedule_once()
        assert engine._task_assigned.get(400) == 77, engine._task_assigned

        # 2) 多个任务同时下发 → 分配给不同账号，各领各的任务
        engine._sched.clear()
        engine._task_assigned.clear()
        engine._account_inflight.clear()
        engine._task_last_account.clear()
        engine._task_attempts.clear()
        tasks = {}
        first_seen = {}
        last_seen = {}
        for idx, tid in enumerate((401, 402)):
            tasks[tid] = {
                "id": tid, "taskType": "standard", "title": f"任务{tid}",
                "createdAt": f"2026-05-0{idx + 1}T00:00:00.000Z",
                "remainingQuota": 1,
                "claimEligibility": {"canClaim": True, "accounts": [
                    {"id": 11, "canClaim": True}, {"id": 77, "canClaim": True},
                    {"id": 99, "canClaim": True}]},
            }
            first_seen[tid] = now
            last_seen[tid] = now
        engine._tasks_map = tasks
        engine._task_first_seen = first_seen
        engine._task_last_seen = last_seen
        engine._schedule_once()
        assigned = dict(engine._task_assigned)
        assert set(assigned) == {401, 402}, assigned
        assert assigned[401] != assigned[402], assigned
        # 靠前的账号先拿新任务，另一个任务落到下一个账号
        assert assigned[402] == 77, assigned       # 更新 → 排序靠前
        assert assigned[401] == 11, assigned       # 其次 → 下一个靠前账号
    finally:
        engine.stop()


def test_scheduler_retry_keeps_same_account(data_dir, monkeypatch):
    """同一任务重试时沿用上次的账号：换号再撞只会多一个 409。"""
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine._pacct_owner = {11: 1, 12: 1}
        engine._pacct_meta = {11: {"verifyStatus": "verified"},
                              12: {"verifyStatus": "verified"}}
        engine._pacct_order = {11: 0, 12: 1}
        engine._pacct_loaded = True
        monkeypatch.setattr(engine, "_cred_token",
                            lambda cid: f"tok{cid}", raising=False)
        engine._claim_threads = [object(), object()]
        engine._sched.clear()

        now = time.monotonic()
        task = {
            "id": 500, "taskType": "standard", "title": "重试任务",
            "createdAt": "2026-06-01T00:00:00.000Z", "remainingQuota": 1,
            "claimEligibility": {"canClaim": True, "accounts": [
                {"id": 11, "canClaim": True}, {"id": 12, "canClaim": True}]},
        }
        engine._tasks_map = {500: task}
        engine._task_first_seen = {500: now}
        engine._task_last_seen = {500: now}
        # 上一次是 12 领的（11 当时不可用）
        engine._task_last_account = {500: 12}
        engine._task_attempts = {500: 1}

        engine._schedule_once()
        assert engine._task_assigned.get(500) == 12, engine._task_assigned
    finally:
        engine.stop()


def test_task_snapshot_annotates_local_cooldown_and_pending(data_dir):
    """任务快照按 task_id 透出本机冷却 / 待提交（「任务速览」标注状态用）。

    上游不填 nextClaimableAt、remainingQuota 恒为 1，冷却与待提交只能由本机
    按 (子账号, 任务) 记录推导，所以必须在任务快照里带上。
    """
    from server.core import SniperEngine

    engine = SniperEngine(lambda event: None)
    try:
        engine._tasks_map = {
            900: {"id": 900, "taskType": "standard", "title": "已领待提交"},
            901: {"id": 901, "taskType": "standard", "title": "冷却中"},
            902: {"id": 902, "taskType": "standard", "title": "无关任务"},
        }
        now = time.time()
        engine._set_task_cooldown(2175, 901, 3600, "standard", 1,
                                  source="claim_history")
        engine._set_pending(2175, 900, "standard", 1, source="claim_history")
        engine._set_task_cooldown(2175, 900, 3600, "standard", 1,
                                  source="claim_history")

        items = {t["id"]: t for t in engine.tasks()["items"]}
        # 待提交 + 冷却都透出
        assert items[900]["localPendingAccounts"] == [2175], items[900]
        assert items[900]["localCooldownUntil"] > now, items[900]
        assert items[900]["localCooldownAccounts"] == [2175], items[900]
        # 只有冷却
        assert items[901]["localPendingAccounts"] == [], items[901]
        assert items[901]["localCooldownUntil"] > now, items[901]
        # 无关任务不带这些字段
        assert "localPendingAccounts" not in items[902], items[902]
        # 原始任务对象不被就地改写（池里的 item 保持干净）
        assert "localPendingAccounts" not in engine._tasks_map[900]
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-x", "-q"]))