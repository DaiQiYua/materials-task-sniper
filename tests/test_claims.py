#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领取记录（/api/user/claims）归一化 + 快照 + 路由的离线单元测试。

覆盖：
1. ``claim_stage()`` 全部分支（参数化）；
2. ``_normalize_claim()`` 用真实样本断言输出字段齐全；
3. ``fetch_claims()`` 成功 / 失败路径（失败保留上一次快照）；
4. ``sync_claims_for_cred()`` 也会更新 ``_claims``（零新增刷新线程的验证）；
5. 路由 ``GET /api/claims``：refresh=False 只读快照；refresh=True 对每个有 token
   的凭据各调一次 ``fetch_claims``。

全部离线：monkeypatch ``_api_call`` / store，不开 uvicorn、不连网。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import (  # noqa: E402
    ACTIVE_STAGE_KEYS,
    STAGE_LABELS,
    SniperEngine,
    claim_stage,
)
from server.app import get_claims, post_sync_claims  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MATERIALS_DATA_DIR", str(directory))
    return directory


def _engine(data_dir) -> SniperEngine:
    return SniperEngine(lambda event: None)


#: 真实样本（脱敏）：approved + settlement.retention_pending → 留存期
REAL_ITEM = {
    "id": 501,
    "taskId": 8127,
    "taskType": "standard",
    "taskTypeName": "常规任务",
    "platform": "xiaohongshu",
    "platformName": "小红书",
    "platformAccountId": 2175,
    "account": "xhs_2175",
    "accountNickname": "十年老粉哟哟哟",
    "status": "approved",
    "claimedAt": "2026-10-27T10:00:00.000Z",
    "expiresAt": "2026-10-27T22:00:00.000Z",
    "cancelledAt": None,
    "cancelReason": None,
    "longTerm": False,
    "material": {},
    "latestSubmission": {"id": 9001, "viewCount": 1234,
                         "publishUrl": "https://xhs.link/abc"},
    "settlement": {"id": 77, "baseCommission": "20.00", "extraReward": "0.00",
                   "totalAmount": "20.00", "status": "retention_pending"},
    "task": {"id": 8127, "title": "20 岁，骑车摔掉门牙后来种了牙",
             "baseCommission": "20.00", "incentives": []},
    "department": None,
    "claimScene": None,
    "inviteId": None,
}


def _upstream(items, total=None, stats=None):
    return {"success": True, "data": {
        "total": len(items) if total is None else total,
        "page": 1, "pageSize": 20,
        "stats": stats if stats is not None else {},
        "items": items}}


class FakeStore:
    """最小凭据仓库：只回答 list_all / get_token。"""

    def __init__(self, tokens: dict):
        self._tokens = tokens

    def list_all(self):
        return [{"id": cid} for cid in self._tokens]

    def get_token(self, cid):
        return self._tokens.get(int(cid))


# ===================================================================
# 1) claim_stage 全分支
# ===================================================================
@pytest.mark.parametrize("status,settlement,has_sub,expect", [
    # 1) 结算异常最高优先
    ("approved", {"status": "abnormal"}, False, ("abnormal", "结算异常")),
    ("settled", {"status": "abnormal"}, False, ("abnormal", "结算异常")),
    # 2) 已结算
    ("settled", None, False, ("settled", "已结算")),
    ("approved", {"status": "paid"}, False, ("settled", "已结算")),
    # 3) 未通过 / 4) 过期 / 5) 取消
    ("rejected", None, False, ("rejected", "未通过")),
    ("expired", None, False, ("expired", "已过期")),
    ("cancelled", None, False, ("cancelled", "已取消")),
    # 6) 留存期（approved + retention_pending）
    ("approved", {"status": "retention_pending"}, False, ("retention", "留存期")),
    # 7) 待结算（approved + pending）
    ("approved", {"status": "pending"}, False, ("pending_settlement", "待结算")),
    ("claimed", {"status": "whatever"}, False, ("pending_settlement", "待结算")),
    # 8) 有提交无结算 → 审核中
    ("approved", None, True, ("reviewing", "审核中")),
    ("claimed", None, True, ("reviewing", "审核中")),
    # 9) claimed/approved 且无提交 → 待提交
    ("claimed", None, False, ("to_submit", "待提交")),
    ("approved", None, False, ("to_submit", "待提交")),
    # 未知 status（含空）
    ("", None, False, ("unknown", "未知")),
    ("weird", None, False, ("unknown", "weird")),
    (None, None, False, ("unknown", "未知")),
])
def test_claim_stage_branches(status, settlement, has_sub, expect):
    assert claim_stage(status, settlement, has_sub) == expect


def test_active_stage_keys_and_labels_consistent():
    for key in ACTIVE_STAGE_KEYS:
        assert key in STAGE_LABELS, key
    for key, label in STAGE_LABELS.items():
        assert isinstance(label, str) and label


# ===================================================================
# 2) _normalize_claim 真实样本
# ===================================================================
def test_normalize_claim_real_sample(data_dir):
    engine = _engine(data_dir)
    try:
        rec = engine._normalize_claim(REAL_ITEM, 3)
        assert rec is not None
        expected_keys = {
            "cred_id", "claim_id", "task_id", "title", "task_type",
            "task_type_name", "platform", "platform_name",
            "platform_account_id", "account_nickname", "status", "stage",
            "stage_label", "active", "claimed_at", "expires_at",
            "submission_id", "view_count", "settlement_status",
            "settlement_amount",
        }
        assert set(rec) == expected_keys, set(rec) ^ expected_keys
        assert rec["cred_id"] == 3
        assert rec["claim_id"] == 501
        assert rec["task_id"] == 8127
        assert rec["title"] == "20 岁，骑车摔掉门牙后来种了牙"
        assert rec["task_type"] == "standard"
        assert rec["task_type_name"] == "常规任务"
        assert rec["platform"] == "xiaohongshu"
        assert rec["platform_name"] == "小红书"
        assert rec["platform_account_id"] == 2175
        assert rec["account_nickname"] == "十年老粉哟哟哟"
        assert rec["status"] == "approved"
        assert rec["stage"] == "retention"
        assert rec["stage_label"] == "留存期"
        assert rec["active"] is True
        assert isinstance(rec["claimed_at"], float) and rec["claimed_at"] > 1e9
        assert isinstance(rec["expires_at"], float) and rec["expires_at"] > 1e9
        assert rec["submission_id"] == 9001
        assert rec["view_count"] == 1234
        assert rec["settlement_status"] == "retention_pending"
        assert rec["settlement_amount"] == "20.00"
    finally:
        engine.stop()


def test_normalize_claim_skips_bad_items(data_dir):
    engine = _engine(data_dir)
    try:
        assert engine._normalize_claim("nope", 1) is None
        assert engine._normalize_claim({"id": 1}, 1) is None          # 缺 taskId
        rec = engine._normalize_claim({"taskId": "x"}, 1)             # taskId 非数字
        assert rec is not None and rec["task_id"] is None
        assert rec["title"] == "" and rec["stage"] == "unknown"
        rec2 = engine._normalize_claim({"taskId": 9, "status": "claimed"}, 1)
        assert rec2["stage"] == "to_submit" and rec2["active"] is True
    finally:
        engine.stop()


# ===================================================================
# 3) fetch_claims 成功 / 失败
# ===================================================================
def test_fetch_claims_success_updates_snapshot(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({1: "tok-1"})
        seen = {}

        def fake_api(method, path, body=None, **kw):
            seen["path"] = path
            seen["token"] = kw.get("token")
            return (200, _upstream([REAL_ITEM], total=1,
                                   stats={"retentionCount": 1}))

        monkeypatch.setattr(engine, "_api_call", fake_api, raising=False)
        result = engine.fetch_claims(1)
        assert result["ok"] is True and result["error"] is None
        assert result["total"] == 1
        assert result["stats"] == {"retentionCount": 1}
        assert seen["path"] == "/api/user/claims?page=1&pageSize=20"
        assert seen["token"] == "tok-1"
        assert len(result["items"]) == 1 and result["items"][0]["active"] is True

        snap = engine.claims_snapshot()
        assert "1" in snap["by_cred"]
        assert snap["active_count"] == 1
        assert snap["fetched_at"] is not None
        assert snap["items"][0]["task_id"] == 8127
        assert snap["errors"] == {}
    finally:
        engine.stop()


def test_fetch_claims_no_token(data_dir):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({})
        result = engine.fetch_claims(1)
        assert result == {"ok": False, "error": "no_token", "items": []}
    finally:
        engine.stop()


def test_fetch_claims_failure_keeps_last_snapshot(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({1: "tok-1"})
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (200, _upstream([REAL_ITEM])),
                            raising=False)
        assert engine.fetch_claims(1)["ok"] is True
        before = engine.claims_snapshot()
        assert before["active_count"] == 1

        # HTTP 500 → ok=False，但上一次快照保留
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (500, {"message": "boom"}),
                            raising=False)
        bad = engine.fetch_claims(1)
        assert bad["ok"] is False and bad["error"] == "HTTP 500"
        assert bad["items"] == []
        after = engine.claims_snapshot()
        assert after["active_count"] == 1
        assert after["items"] == before["items"]
        assert after["errors"] == {"1": "HTTP 500"}

        # 网络异常 → ok=False，快照仍保留
        def boom(*a, **k):
            raise RuntimeError("conn reset")

        monkeypatch.setattr(engine, "_api_call", boom, raising=False)
        bad2 = engine.fetch_claims(1)
        assert bad2["ok"] is False and bad2["error"] == "network"
        assert engine.claims_snapshot()["active_count"] == 1
    finally:
        engine.stop()


def test_fetch_claims_failure_logs_warn_once(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({1: "tok-1"})
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (200, _upstream([REAL_ITEM])),
                            raising=False)
        engine.fetch_claims(1)
        engine._logs.clear()
        # 成功不刷日志
        engine.fetch_claims(1)
        assert engine.logs() == []
        # 失败打一条 WARN，且不含 token
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (503, None), raising=False)
        engine.fetch_claims(1)
        logs = engine.logs()
        assert len(logs) == 1 and logs[0]["level"] == "WARN"
        assert "tok-1" not in logs[0]["msg"]
    finally:
        engine.stop()


# ===================================================================
# 4) sync_claims_for_cred 也更新 _claims（零新增刷新）
# ===================================================================
def test_sync_claims_for_cred_updates_claims(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({2: "tok-2"})
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (200, _upstream([REAL_ITEM])),
                            raising=False)
        result = engine.sync_claims_for_cred(2)
        assert result["ok"] is True
        assert result["claims"] is not None
        assert result["claims"]["items"][0]["task_id"] == 8127
        snap = engine.claims_snapshot()
        assert "2" in snap["by_cred"]
        assert snap["active_count"] == 1
    finally:
        engine.stop()


# ===================================================================
# 5) 路由 GET /api/claims
# ===================================================================
def _fake_request(engine) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


def test_route_get_claims_readonly(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({1: "tok-1", 2: "tok-2"})
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (200, _upstream([REAL_ITEM])),
                            raising=False)
        engine.fetch_claims(1)
        calls: list = []
        monkeypatch.setattr(engine, "fetch_claims",
                            lambda cid, page_size=20: calls.append(cid)
                            or {"ok": False, "error": "nope", "items": []},
                            raising=False)
        body = get_claims(_fake_request(engine), refresh=False)
        assert body["ok"] is True
        assert calls == [], "refresh=False 不该触发拉取"
        assert body["active_count"] == 1
        assert "1" in body["by_cred"]
    finally:
        engine.stop()


def test_route_get_claims_refresh_calls_each_cred(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({1: "tok-1", 2: "tok-2", 3: None})
        calls: list = []

        def fake_fetch(cid, page_size=20):
            calls.append(cid)
            return {"ok": cid != 2, "error": None if cid != 2 else "HTTP 500",
                    "items": []}

        monkeypatch.setattr(engine, "fetch_claims", fake_fetch, raising=False)
        body = get_claims(_fake_request(engine), refresh=True)
        assert body["ok"] is True
        assert sorted(calls) == [1, 2], "只对有 token 的凭据拉取"
        assert body["errors"] == {"2": "HTTP 500"}
    finally:
        engine.stop()


def test_route_sync_claims_appends_claims(data_dir, monkeypatch):
    engine = _engine(data_dir)
    try:
        engine._store = FakeStore({5: "tok-5"})
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (200, _upstream([REAL_ITEM])),
                            raising=False)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            engine=engine, credentials={5: {"id": 5}})))
        body = post_sync_claims(5, request)
        assert body["ok"] is True
        assert body["claims"] is not None
        assert body["claims"]["items"][0]["stage_label"] == "留存期"
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x", "-q"]))
