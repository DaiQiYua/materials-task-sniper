#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「一键清空所有数据（彻底恢复出厂）」能力测试。

覆盖：
1. ``POST /api/data/clear`` 的确认词校验：缺失 / 错词（「确认」）→ 400，且
   **不删除任何文件**；
2. ``{"confirm":"清空"}`` → 200，``cleared`` 统计正确，7 个数据文件 + 日志轮转
   备份全部删除（``sniper.log`` 由引擎立刻重建为空文件），内存态（凭据 / 历史 /
   token / 状态 / 统计）与配置（回到默认 base）全部恢复；
3. 日志 handler 被正确关闭并重建：清空前的标记消失、清空后新日志仍能落盘；
4. 空数据目录下直接清空也 200 且不抛；
5. 白名单边界：数据目录里的其它文件（哨兵文件 / 子目录）不会被删。

跑法：
    python -m pytest tests/test_clear_all.py -q
    python tests/test_clear_all.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx  # noqa: F401 - TestClient 依赖 httpx（starlette 内部使用）
import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GOOD_PHONE = "13800001111"
GOOD_PASSWORD = "pass123"
DEFAULT_BASE = "https://materials.xlya.cn"

#: 清空时必须消失的数据文件（``sniper.log`` 会被引擎重建，单独断言）
DATA_FILES = ("token.json", "tokens.json", "platform_accounts.json",
              "config.json", "history.jsonl", "credentials.json")
ALL_DATA_FILES = DATA_FILES + ("sniper.log",)


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


def _add_cred(client, name: str, phone: str = GOOD_PHONE) -> dict:
    resp = client.post("/api/credentials",
                       json={"name": name, "phone": phone,
                             "password": GOOD_PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===================================================================
# 1) 确认词校验：缺失 / 错词 → 400，且一个文件都不许删
# ===================================================================
def test_clear_requires_confirm_word(data_dir):
    with _client() as client:
        item = _add_cred(client, "a")
        assert item["id"] >= 1
        assert (data_dir / "credentials.json").is_file()
        log_file = data_dir / "sniper.log"
        assert log_file.is_file()

        for payload in ({}, {"confirm": ""}, {"confirm": "   "},
                        {"confirm": "确认"}, {"confirm": "清空所有"},
                        {"confirm": "clear"}):
            resp = client.post("/api/data/clear", json=payload)
            assert resp.status_code == 400, (payload, resp.text)
            assert "清空" in resp.json()["detail"], resp.text
            # 错误确认词绝不能删任何文件
            assert (data_dir / "credentials.json").is_file(), payload
            assert log_file.is_file(), payload
            listing = client.get("/api/credentials").json()["items"]
            assert [i["id"] for i in listing] == [item["id"]], (payload, listing)


# ===================================================================
# 2) 完整清空：统计正确 + 文件删除 + 内存态 / 配置复位
# ===================================================================
def test_clear_all_removes_everything(data_dir):
    from server import persist

    with _client() as client:
        app = client.app
        engine = app.state.engine
        store = app.state.credentials

        # ---- 造数据：凭据 / 活跃 token / 账号 token / 平台缓存 / 配置 ----
        created = _add_cred(client, "主号")
        cid = created["id"]
        assert client.put("/api/token", json={"token": TOK_A}).status_code == 200
        store.set_token(cid, TOK_A, exp=int(time.time()) + 3600)
        persist.save_platform_cache({
            cid: {"fetched_at": time.time(), "fallback": False,
                  "items": [{"id": 2175, "platform": "xiaohongshu"}]},
        }, data_dir)
        assert client.put("/api/config",
                          json={"base": "http://127.0.0.1:1"}).status_code == 200
        persist.append_history({"task_id": 1, "status": "ok"}, data_dir)
        engine.load_history([{"task_id": 1, "status": "ok"}])
        # 日志轮转备份（RotatingFileHandler backupCount=3 的产物）
        (data_dir / "sniper.log.1").write_text("old-1\n", encoding="utf-8")
        (data_dir / "sniper.log.2").write_text("old-2\n", encoding="utf-8")
        # 白名单边界：哨兵文件 + 子目录（绝不能被删）
        (data_dir / "keep_me.txt").write_text("keep\n", encoding="utf-8")
        (data_dir / "sub").mkdir(exist_ok=True)
        (data_dir / "sub" / "inner.txt").write_text("inner\n", encoding="utf-8")

        for name in ALL_DATA_FILES:
            assert (data_dir / name).is_file(), name
        assert len(client.get("/api/history").json()["items"]) == 1
        assert client.get("/api/config").json()["base"] == "http://127.0.0.1:1"

        time.sleep(0.3)          # 让异步日志先落盘，确认 sniper.log 非空

        # ---- 清空 ----
        resp = client.post("/api/data/clear", json={"confirm": "清空"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True, body
        cleared = body["cleared"]

        # ---- 统计口径 ----
        assert cleared["credentials"] == 1, cleared
        assert cleared["tokens"] == 1, cleared
        assert cleared["platform_cache"] == 1, cleared
        assert cleared["history"] == 1, cleared
        assert cleared["logs"] == 3, cleared          # sniper.log + .1 + .2
        assert cleared["config_reset"] is True, cleared
        assert cleared["failed"] == [], cleared
        deleted = set(cleared["files"])
        for name in ALL_DATA_FILES + ("sniper.log.1", "sniper.log.2"):
            assert name in deleted, (name, cleared["files"])
        # 只删白名单内的文件
        assert set(cleared["files"]) <= set(ALL_DATA_FILES
                                            + tuple(f"{n}.tmp" for n in ALL_DATA_FILES)
                                            + ("sniper.log.1", "sniper.log.2",
                                               "sniper.log.3")), cleared["files"]

        # ---- 盘上：数据文件全没了，sniper.log 被立即重建为空文件 ----
        for name in DATA_FILES + ("sniper.log.1", "sniper.log.2", "sniper.log.3"):
            assert not (data_dir / name).exists(), name
        log_file = data_dir / "sniper.log"
        assert log_file.is_file(), "清空后必须重建 sniper.log（对外接口约定）"
        # 白名单边界：其它文件与子目录原样保留
        assert (data_dir / "keep_me.txt").read_text(encoding="utf-8") == "keep\n"
        assert (data_dir / "sub" / "inner.txt").exists()

        # ---- 接口：全部为空 / 恢复默认 ----
        assert client.get("/api/credentials").json()["items"] == []
        assert client.get("/api/history").json()["items"] == []
        assert client.get("/api/tasks").json()["items"] == []
        status = client.get("/api/status").json()
        assert status["token"]["present"] is False, status
        assert status["state"] == "idle" and status["running"] is False, status
        assert status["started_at"] is None and status["last_poll_at"] is None, status
        assert status["queue"] == 0 and status["workers"] == 0, status
        assert set(status["stats"]) == {"polls", "skipped", "claims_ok",
                                        "claims_fail", "claims_conflict",
                                        "claims_attempted", "retries"}, status
        assert all(v == 0 for v in status["stats"].values()), status
        config = client.get("/api/config").json()
        assert config["base"] == DEFAULT_BASE, config
        # 引擎内存态确实清了（不只是接口层）
        assert engine.has_token() is False
        assert engine.logs()                     # 至少有一条「已清空所有数据」
        assert any("已清空所有数据" in e["msg"] for e in engine.logs())
        # 配置恢复默认没有把 config.json 写回去
        assert not (data_dir / "config.json").exists()


# ===================================================================
# 3) 日志 handler：清空前标记消失，清空后新日志仍能落盘
# ===================================================================
def test_log_handlers_closed_and_rebuilt(data_dir):
    log_file = data_dir / "sniper.log"
    with _client() as client:
        # 清空前：添加凭据会产生一条带名字标记的日志
        _add_cred(client, "清空前标记AAA")
        time.sleep(1.2)
        before = log_file.read_text(encoding="utf-8", errors="replace")
        assert "清空前标记AAA" in before, before

        resp = client.post("/api/data/clear", json={"confirm": "清空"})
        assert resp.status_code == 200, resp.text
        assert log_file.is_file(), "清空后必须重建 sniper.log"
        # 清空写入的日志 + 旧内容都不该残留旧标记
        time.sleep(0.3)
        after = log_file.read_text(encoding="utf-8", errors="replace")
        assert "清空前标记AAA" not in after, after

        # 清空后：再添加一个凭据，新的 handler 必须能写盘
        _add_cred(client, "清空后标记BBB", phone="13900002222")
        time.sleep(1.2)
        rebuilt = log_file.read_text(encoding="utf-8", errors="replace")
        assert "清空后标记BBB" in rebuilt, rebuilt
        assert "清空前标记AAA" not in rebuilt, rebuilt


# ===================================================================
# 4) 空数据目录：直接清空也 200，不抛
# ===================================================================
def test_clear_on_empty_data_dir(data_dir):
    with _client() as client:
        # 首尾空格也算正确确认词（接口按 strip 后比对）
        resp = client.post("/api/data/clear", json={"confirm": "  清空  "})
        assert resp.status_code == 200, resp.text
        cleared = resp.json()["cleared"]
        assert cleared["credentials"] == 0, cleared
        assert cleared["tokens"] == 0, cleared
        assert cleared["platform_cache"] == 0, cleared
        assert cleared["history"] == 0, cleared
        assert cleared["config_reset"] is True, cleared
        assert cleared["failed"] == [], cleared
        # 启动时 get_logger 已建过空日志，所以至少删掉 1 个
        assert cleared["logs"] >= 1, cleared
        assert (data_dir / "sniper.log").is_file(), "清空后必须重建 sniper.log"
        assert client.get("/api/credentials").json()["items"] == []
        # 幂等：再清一次也不抛
        again = client.post("/api/data/clear", json={"confirm": "清空"})
        assert again.status_code == 200, again.text
        assert again.json()["cleared"]["failed"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-x", "-q"]))