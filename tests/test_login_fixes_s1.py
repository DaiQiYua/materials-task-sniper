#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 / B2 / B3 / B4 修复的单元测试（纯内存 + 临时目录，**不**起 uvicorn）。

覆盖：
1. ``relogin_cred``（静默重登）**绝不**改活跃 Token / ``token.json``；
2. ``fetch_one(activate=False)``（HTTP 接口语义）在引擎没有任何 Token 时仍自动
   adopt —— 保护既有锁定语义；
3. 重登的并发锁 + 非网络类失败退避（60s 内直接 ``relogin_cooling``，不再打上游）；
4. ``CredentialStore`` 的内存缓存：写路径立即失效、外部改文件在 TTL 内仍命中旧值；
5. ``history.jsonl`` 轮转（超过阈值改名成 ``history.jsonl.1``，只留 1 份）。

跑法：
    python -m pytest tests/test_login_fixes_s1.py -q -p no:cacheprovider
    python tests/test_login_fixes_s1.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GOOD_PHONE = "13800001111"
GOOD_PASSWORD = "pass123"
BASE = "https://materials.invalid"


def _b64(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_jwt(tag: str = "a", exp_offset: float = 7 * 24 * 3600) -> str:
    """真实结构的假 JWT（payload 带 exp，供 ``core.jwt_exp`` 解析）。"""
    now = int(time.time())
    header = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64({"sub": tag, "iat": now, "nbf": now,
                    "exp": int(now + exp_offset), "jti": tag})
    return f"{header}.{payload}.sig-{tag}"


TOK_A = fake_jwt("a")
TOK_B = fake_jwt("b")


class FakeEngine:
    """``core.SniperEngine`` 的最小替身：只实现 LoginManager 会用到的方法。"""

    def __init__(self, present: bool = False, valid: bool = False) -> None:
        self._present = bool(present)
        self._valid = bool(valid)
        self.set_token_calls: list = []
        self.logs: list = []

    def token_status(self) -> dict:
        return {"present": self._present, "valid": self._valid}

    def set_token(self, token: str) -> dict:
        # 打点：任何一次调用都说明「活跃 Token 被改了」——B1 要断言它是 0 次
        self.set_token_calls.append(token)
        self._present = True
        self._valid = True
        return {"present": True, "valid": True, "exp": "2026-01-01T00:00:00Z"}

    def log(self, msg: str, level: str = "INFO") -> None:
        self.logs.append((level, msg))

    def snapshot(self) -> dict:
        return {"base": BASE}


@pytest.fixture()
def store(tmp_path):
    from server.credentials import CredentialStore

    return CredentialStore(tmp_path)


def _make_manager(engine, store):
    from server.login import LoginManager

    return LoginManager(engine, store=store)


# ===================================================================
# 1) 静默重登不改活跃 Token（B1）
# ===================================================================
def test_relogin_cred_never_touches_active_token(store, monkeypatch):
    from server import login as login_mod

    # 引擎当前有 Token 但已失效 —— 这正是最容易被误 adopt 的场景
    engine = FakeEngine(present=True, valid=False)
    manager = _make_manager(engine, store)
    cid = store.add("主号", GOOD_PHONE, GOOD_PASSWORD)["id"]

    monkeypatch.setattr(login_mod, "api_login",
                        lambda base, phone, password, log=None: (True, TOK_A))

    result = manager.relogin_cred(cid)
    assert result["ok"] is True, result
    # 关键断言：静默重登绝不覆写活跃 Token（core._relogin_cred 的承诺）
    assert engine.set_token_calls == [], engine.set_token_calls
    # 但该账号自己的 token 必须更新
    assert store.get_token(cid) == TOK_A
    assert result["token_saved"] is True


# ===================================================================
# 2) fetch_one(activate=False) 在引擎没有 Token 时仍自动 adopt（既有语义）
# ===================================================================
def test_fetch_one_activate_false_adopts_when_no_active_token(store, monkeypatch):
    from server import login as login_mod

    engine = FakeEngine(present=False, valid=False)
    manager = _make_manager(engine, store)
    cid = store.add("首个账号", GOOD_PHONE, GOOD_PASSWORD)["id"]

    monkeypatch.setattr(login_mod, "api_login",
                        lambda base, phone, password, log=None: (True, TOK_A))

    result = manager.fetch_one(cid, activate=False)      # allow_adopt 默认 True
    assert result["ok"] is True, result
    assert engine.set_token_calls == [TOK_A], engine.set_token_calls


# ===================================================================
# 3) 重登冷却：非网络类失败后 60s 内直接拒绝，不再打上游（B2）
# ===================================================================
def test_relogin_cools_down_after_credential_failure(store, monkeypatch):
    from server import login as login_mod

    engine = FakeEngine(present=True, valid=True)
    manager = _make_manager(engine, store)
    cid = store.add("坏号", GOOD_PHONE, GOOD_PASSWORD)["id"]

    hits = {"n": 0}

    def fake_api_login(base, phone, password, log=None):
        hits["n"] += 1
        return (False, "密码错误")

    monkeypatch.setattr(login_mod, "api_login", fake_api_login)

    first = manager.relogin_cred(cid)
    assert first["ok"] is False and "密码错误" in first["error"], first
    assert hits["n"] == 1

    second = manager.relogin_cred(cid)
    assert second["ok"] is False, second
    assert second["error"] == "relogin_cooling", second
    # 冷却期内不许再打上游登录接口
    assert hits["n"] == 1, hits
    assert engine.set_token_calls == []


# ===================================================================
# 4) 凭据存储缓存：写路径立即失效 + 外部改文件在 TTL 内仍命中旧值（B3）
# ===================================================================
def test_store_cache_invalidated_on_write(tmp_path, store):
    from server import persist

    new_id = store.add("主号", GOOD_PHONE, GOOD_PASSWORD)["id"]
    # add 之后立即读：新账号还没有 token，不许被缓存挡住
    assert store.get_token(new_id) is None
    # set_token 后立即读：必须立刻读到新 token（缓存必须被失效）
    store.set_token(new_id, TOK_A, exp=999999999)
    assert store.get_token(new_id) == TOK_A

    # 预期行为：外部（另一个进程/工具）改了文件，TTL 内读到的是缓存里的旧值
    store.get_token(new_id)                     # 确保缓存是热的
    store._tokens_at = time.monotonic()         # 手工把时间戳置回「新鲜」
    persist.save_tokens({new_id: {"token": TOK_B, "exp": None, "saved_at": 1.0}},
                        tmp_path)
    assert store.get_token(new_id) == TOK_A, "TTL 内应命中缓存旧值"

    # TTL 过期后（把时间戳拨老）才重读磁盘 → 看到外部写入
    store._tokens_at = time.monotonic() - (store._CACHE_TTL + 1.0)
    assert store.get_token(new_id) == TOK_B

    # clear_token 同样立即失效
    assert store.clear_token(new_id) is True
    assert store.get_token(new_id) is None


# ===================================================================
# 5) history.jsonl 轮转（B4）
# ===================================================================
def test_history_rotation_keeps_single_backup(tmp_path, monkeypatch):
    from server import persist

    monkeypatch.setattr(persist, "HISTORY_MAX_BYTES", 100)
    record = {"task_id": 1, "status": "ok", "title": "任务" * 5}
    line_bytes = len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    total = 0
    for _ in range(30):
        persist.append_history(record, tmp_path)
        total += line_bytes

    live = tmp_path / "history.jsonl"
    backup = tmp_path / "history.jsonl.1"
    assert backup.is_file(), "超过阈值应轮转出 history.jsonl.1"
    assert live.is_file(), "轮转后 history.jsonl 必须继续存在（新写入）"
    # 轮转后主文件变小：不再持有全部 30 行，只留最后几条
    live_lines = live.read_text(encoding="utf-8").splitlines()
    assert 1 <= len(live_lines) < 30, len(live_lines)
    assert live.stat().st_size < total, (live.stat().st_size, total)
    assert backup.stat().st_size >= persist.HISTORY_MAX_BYTES
    assert backup.read_text(encoding="utf-8").strip(), "备份不能为空"
    # 只保留 1 份备份
    assert not (tmp_path / "history.jsonl.2").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-x", "-q"]))