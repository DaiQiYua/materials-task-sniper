#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动拉一次任务列表（POST /api/tasks/refresh）的离线单元测试。

覆盖：
1. 无可用 Token → HTTP 200 + ``ok=False`` + ``errors`` 非空；
2. 有 Token 且上游返回任务 → ``ok=True``、``count>0``、``tasks.items`` 非空；
3. 引擎**停止**状态下仍能拉到任务（本次改动的核心语义：手动路径不受
   ``_is_active`` 提前退出影响）。

全部离线：monkeypatch ``_get_tasks`` / store，不开 uvicorn、不连网。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import RateLimiter, SniperEngine  # noqa: E402
from server.app import post_tasks_refresh  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MATERIALS_DATA_DIR", str(directory))
    return directory


def _engine(data_dir) -> SniperEngine:
    return SniperEngine(lambda event: None)


class FakeStore:
    """最小凭据仓库：只回答 list_all / get_token。"""

    def __init__(self, tokens: dict):
        self._tokens = tokens

    def list_all(self):
        return [{"id": cid} for cid in self._tokens]

    def get_token(self, cid):
        return self._tokens.get(int(cid))


def _task_item(tid=999):
    return {
        "id": tid, "taskType": "standard", "title": f"测试任务{tid}",
        "baseCommission": "20.00", "remainingQuota": 1,
        "claimEligibility": {"canClaim": True, "accounts": [{"id": 2175}]},
    }


def _fake_request(engine) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        engine=engine, credentials={1: {"id": 1}})))


def _stub_upstream(engine, monkeypatch, items, total=None, calls=None):
    """替掉真实上游：``_get_tasks`` 恒返回 200 + items；并放开限速桶。"""
    monkeypatch.setattr(engine, "_get_store", lambda: FakeStore({1: "tok-1"}),
                        raising=False)

    def fake_get_tasks(cred_id, task_type, page, cfg, page_size=None):
        if calls is not None:
            calls.append((int(cred_id), str(task_type), int(page)))
        return (200, list(items),
                {"total": len(items) if total is None else int(total),
                 "page_size": int(page_size or cfg.page_size)})

    monkeypatch.setattr(engine, "_get_tasks", fake_get_tasks, raising=False)
    # 手动刷新串行打多个 (账号, 类型) 组合，放开 poll 桶避免被限流跳页
    monkeypatch.setattr(engine, "_get_limiter",
                        lambda kind, cred_id, scope="": RateLimiter(1000, 1.0),
                        raising=False)


def test_refresh_no_token_returns_ok_false(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        monkeypatch.setattr(engine, "_get_store", lambda: FakeStore({}),
                            raising=False)
        body = asyncio.run(post_tasks_refresh(_fake_request(engine)))
        assert body["ok"] is False
        assert body["errors"], body
        assert body["count"] == 0
        assert body["accounts"] == 0
        assert isinstance(body["fetched_at"], float)
    finally:
        engine.stop()


def test_refresh_with_token_returns_tasks(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        calls: list = []
        _stub_upstream(engine, monkeypatch, [_task_item(999)], calls=calls)
        body = asyncio.run(post_tasks_refresh(_fake_request(engine)))
        assert body["ok"] is True, body
        assert body["accounts"] == 1
        assert body["count"] > 0, body
        assert body["tasks"]["items"], body
        assert body["tasks"]["items"][0]["id"] == 999
        assert calls, "应至少发起一次上游列表请求"
        assert body["elapsed"] >= 0.0
    finally:
        engine.stop()


def test_refresh_works_while_engine_stopped(data_dir, monkeypatch):
    """核心语义：引擎停止（state=idle / running=False）时手动刷新仍能入库。"""
    engine = _engine(data_dir)
    try:
        engine.stop()                      # 确保处于停止态
        assert engine._is_active() is False
        calls: list = []
        _stub_upstream(engine, monkeypatch, [_task_item(4242)], calls=calls)
        body = asyncio.run(post_tasks_refresh(_fake_request(engine)))
        assert body["ok"] is True, body
        assert body["count"] > 0, body
        assert body["tasks"]["items"][0]["id"] == 4242
        assert calls, "停止态下也应真正发起上游请求"
        # 任务确实进了任务池（停止态 _ingest 仍工作）
        assert engine.tasks()["items"][0]["id"] == 4242
    finally:
        engine.stop()


def test_refresh_single_account_error_does_not_abort(data_dir, monkeypatch):
    """单个账号/类型出错不影响整体：记进 errors，其余照常入库。"""
    engine = _engine(data_dir)
    try:
        monkeypatch.setattr(engine, "_get_store",
                            lambda: FakeStore({1: "tok-1", 2: "tok-2"}),
                            raising=False)

        def fake_get_tasks(cred_id, task_type, page, cfg, page_size=None):
            if int(cred_id) == 2:
                raise RuntimeError("Token 无效")
            return (200, [_task_item(77)], {"total": 1, "page_size": 10})

        monkeypatch.setattr(engine, "_get_tasks", fake_get_tasks, raising=False)
        monkeypatch.setattr(engine, "_get_limiter",
                            lambda kind, cred_id, scope="": RateLimiter(1000, 1.0),
                            raising=False)
        body = asyncio.run(post_tasks_refresh(_fake_request(engine)))
        assert body["accounts"] == 2
        assert body["count"] > 0, body
        assert any("账号 #2" in e for e in body["errors"]), body
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-x", "-q"]))
