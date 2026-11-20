#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D:\\...\\materials-task-sniper 第一轮高危修复（A1–A8）的回归测试。

覆盖：
1. (A1) 待提交只阻塞「同一子账号 × 同一任务」，不再冻结整个子账号；
2. (A3) 限速桶隔离：轮询桶按任务类型分开，limits 桶与 poll 桶互不相干；
3. (A3) 尾页某一页被限流跳过时，不丢掉整轮（后续页仍入库）；
4. (A4) 任务池 GC：下架任务退役 + 防重抢集合按 TTL 清理；
5. (A4) 任务快照广播节流（200ms 窗口 + 挂起补发标记）；
6. (A5) 多账号下「平台子账号归属未知」不再静默借用活跃 Token；
7. (A6) 429 重试次数真正封顶（到顶后不标记 dead，次数归零仍退避）。

全部离线构造（``SniperEngine(lambda e: None)`` + 手工注入内存字段），
不开 uvicorn、不连网、数据目录指向 pytest 的临时目录。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import (  # noqa: E402
    MAX_CLAIM_ATTEMPTS,
    TASK_DEAD_TTL,
    TASK_RETIRE_SECONDS,
    EngineConfig,
    SniperEngine,
)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """数据目录指向 tmp，绝不碰仓库根目录真实数据文件。"""
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MATERIALS_DATA_DIR", str(directory))
    return directory


def _engine(data_dir, monkeypatch, accounts=(12,), cred=1, token="tok"):
    """最小可用引擎：已知归属、有 token、无冷却、空配额快照。"""
    engine = SniperEngine(lambda event: None)
    engine._pacct_owner = {aid: cred for aid in accounts}
    engine._pacct_meta = {aid: {"platform": "", "verifyStatus": "verified"}
                          for aid in accounts}
    engine._pacct_loaded = True
    monkeypatch.setattr(engine, "_cred_token", lambda cid: token, raising=False)
    monkeypatch.setattr(engine, "_cred_records",
                        lambda: [{"id": cred}], raising=False)
    return engine


# ===================================================================
# 1) A1：待提交只阻塞同一任务
# ===================================================================
def test_pending_only_blocks_same_task(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch)
    try:
        now = time.time()
        # 子账号 12 对任务 100 有「待提交」（已领到、等提交结果）
        engine._set_pending(12, 100, "standard", 1, source="claim_response",
                            persist=False)
        assert (12, 100) in engine._pending_submissions

        # 同一任务 → 不可领
        assert engine._account_available(12, 100, "standard", now) == (False, None)
        # 同一子账号的**另一个**任务 → 仍可领（旧实现会整号冻结）
        ok, cid = engine._account_available(12, 200, "standard", now)
        assert ok is True and cid == 1, (ok, cid)

        # cred 级「待处理已满」逻辑保持不变：pendingCount >= pendingLimit 时整号不可领
        engine._lim_data_by_cred[1] = {"pendingCount": 3, "pendingLimit": 3}
        engine._lim_mono_by_cred[1] = time.monotonic()
        assert engine._account_available(12, 200, "standard", now) == (False, None)
    finally:
        engine.stop()


# ===================================================================
# 2) A3：轮询桶按任务类型隔离；limits 桶独立
# ===================================================================
def test_limiters_isolated_by_scope_and_kind(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch)
    try:
        std = engine._get_limiter("poll", 1, "standard")
        lng = engine._get_limiter("poll", 1, "long_term")
        # 同一 cred 的两种任务类型不得共用同一个桶（否则 5/s 被两线程分摊）
        assert std is not lng
        # 同一 (cred, 类型) 重复取到同一个对象
        assert engine._get_limiter("poll", 1, "standard") is std

        limits = engine._get_limiter("limits", 1)
        assert limits is not std and limits is not lng
        assert engine._get_limiter("limits", 1) is limits
        # claim 桶也不与 poll / limits 混用
        claim = engine._get_limiter("claim", 1)
        assert claim is not std and claim is not limits
    finally:
        engine.stop()


# ===================================================================
# 3) A3：尾页单页被跳过不丢整轮
# ===================================================================
def test_poll_once_skipped_tail_page_does_not_drop_round(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch)
    try:
        engine._cfg = EngineConfig(tail_pages=2, total_refresh_seconds=10)
        calls: list = []

        def fake_get_tasks(cred_id, task_type, page, cfg, page_size=None):
            calls.append(page)
            if page == 1:
                return (200, [{"id": 1, "taskType": task_type}],
                        {"total": 25, "page_size": 10})
            if page == 3:
                # 尾页第一次就被轮询桶跳过（-1）
                return (-1, [], {})
            return (200, [{"id": 200, "taskType": task_type}],
                    {"total": 25, "page_size": 10})

        monkeypatch.setattr(engine, "_is_active", lambda: True, raising=False)
        monkeypatch.setattr(engine, "_get_tasks", fake_get_tasks, raising=False)

        engine._poll_once(1, "standard", engine._cfg)

        # 第一轮：3（跳过）→ 2（入库）→ 1（校准页最后入库）都走到了
        assert calls[:3] == [1, 3, 2], calls
        assert 200 in engine._tasks_map, list(engine._tasks_map)
        assert 1 in engine._tasks_map, list(engine._tasks_map)
        assert engine._stats["skipped"] >= 1
    finally:
        engine.stop()


# ===================================================================
# 4) A4：GC 退役与 TTL 清理
# ===================================================================
def test_gc_retires_stale_tasks_and_expires_ttl_sets(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch)
    try:
        mono = time.monotonic()
        old = mono - (TASK_RETIRE_SECONDS + 60.0)
        engine._tasks_map = {7: {"id": 7}, 8: {"id": 8}}
        engine._task_first_seen = {7: old, 8: mono}
        engine._task_last_seen = {7: old, 8: mono}
        engine._task_sources = {7: {1}, 8: {1}}
        engine._task_types_map = {7: "standard", 8: "standard"}
        engine._task_attempts = {7: 1, 8: 0}
        engine._task_retry_at = {7: mono + 10.0, 8: 0.0}
        # 防重抢集合：7 标记很早（超 TASK_DEAD_TTL），9 标记很新（要保留）
        engine._task_dead = {7: mono - (TASK_DEAD_TTL + 60.0), 9: mono}
        engine._claimed = {8: mono}
        engine._gc_at = 0.0

        engine._gc_state(mono)

        # 长时间没再被刷到的任务 7 被退役（8 还在，不动）
        assert 7 not in engine._tasks_map
        assert 8 in engine._tasks_map
        for field in ("_task_first_seen", "_task_last_seen", "_task_sources",
                      "_task_types_map", "_task_attempts", "_task_retry_at"):
            assert 7 not in getattr(engine, field), (field, getattr(engine, field))
        # TTL：过期的 dead 被清，新的保留；claimed 未过期保留
        assert 7 not in engine._task_dead
        assert 9 in engine._task_dead
        assert 8 in engine._claimed
        # 退役后排序缓存必须重建
        assert engine._pool_dirty is True
        # 5s 内再次调用直接返回（不重复扫）
        engine._tasks_map[7] = {"id": 7}
        engine._task_last_seen[7] = old
        engine._gc_state(mono + 0.1)
        assert 7 in engine._tasks_map, "GC_INTERVAL 内不该再次执行"
    finally:
        engine.stop()


# ===================================================================
# 5) A4：任务快照广播节流
# ===================================================================
def test_tasks_emit_throttled(data_dir, monkeypatch):
    events: list = []
    engine = SniperEngine(events.append)
    try:
        engine._ingest("standard", [{"id": 1, "taskType": "standard"}], 1)
        engine._ingest("standard", [{"id": 2, "taskType": "standard"}], 1)

        tasks_events = [e for e in events if e.get("type") == "tasks"]
        assert len(tasks_events) == 1, tasks_events
        # 第二次被节流：标记挂起，等下一次补发
        assert engine._tasks_emit_pending is True
        # 协议结构不变
        assert set(tasks_events[0]) == {"type", "ts", "items"}
        assert isinstance(tasks_events[0]["ts"], float)
        assert tasks_events[0]["ts"] > 1e8

        # 过了最小间隔后补发一次，挂起标记清零
        engine._tasks_emit_at -= 1.0
        engine._flush_tasks_emit()
        tasks_events = [e for e in events if e.get("type") == "tasks"]
        assert len(tasks_events) == 2, tasks_events
        assert engine._tasks_emit_pending is False
        assert [it["id"] for it in tasks_events[-1]["items"]] == [1, 2]

        # 对外只读接口不受节流影响
        assert engine.tasks()["ts"] == engine.tasks()["ts"]
    finally:
        engine.stop()


# ===================================================================
# 6) A5：归属未知 + 多账号 → 不借用活跃 Token
# ===================================================================
def test_unknown_owner_blocks_claim_only_with_multi_creds(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch, accounts=(), cred=1)
    try:
        engine._pacct_owner = {}          # 归属完全未知
        engine._pacct_meta = {}
        engine._pacct_loaded = True
        monkeypatch.setattr(engine, "_cred_records",
                            lambda: [{"id": 1}, {"id": 2}], raising=False)
        now = time.time()
        assert engine._account_available(2175, 100, "standard", now) == (False, None)
        logs = " ".join(str(e.get("msg") or "") for e in engine.logs())
        assert "归属未知" in logs, logs

        # 只有一个登录账号 → 保持原回退行为（可用；cid 可能回退成活跃 Token 的 0）
        monkeypatch.setattr(engine, "_cred_records",
                            lambda: [{"id": 1}], raising=False)
        ok, _cid = engine._account_available(2175, 100, "standard", now)
        assert ok is True, (ok, _cid)
    finally:
        engine.stop()


# ===================================================================
# 7) A6：429 重试次数封顶
# ===================================================================
def test_429_reaches_cap_without_marking_dead(data_dir, monkeypatch):
    engine = _engine(data_dir, monkeypatch)
    try:
        monkeypatch.setattr(engine, "_api_call",
                            lambda *a, **k: (429, {"message": "被上游限流"}),
                            raising=False)
        item = {"id": 100, "title": "限流任务"}
        engine._task_attempts = {100: MAX_CLAIM_ATTEMPTS}

        engine._claim(item, "standard", 12, MAX_CLAIM_ATTEMPTS)

        # 到顶不标记 dead（任务本身没死），次数归零但仍退避重排
        assert 100 not in engine._task_dead
        assert engine._task_attempts[100] == 0
        assert engine._task_retry_at[100] > time.monotonic()
        assert 100 not in engine._task_assigned
        logs = " ".join(str(e.get("msg") or "") for e in engine.logs())
        assert "已连续限流" in logs, logs
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x", "-q"]))