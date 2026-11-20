#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HAR 抓包发现的两个问题 + 抓包代理支持的回归测试。

覆盖：
1. ``_ingest`` 写出的 ``last_poll_at`` / ``tasks.ts`` 必须是 **epoch 秒**（time.time），
   而不是 ``time.monotonic()`` 的开机秒数（否则前端 ``new Date(ts*1000)`` 显示 1970 年）；
   内部 ``_task_first_seen`` / ``_task_last_seen`` 仍必须是 monotonic（TTL 逻辑不能动）。
2. ``_validate_field("proxy", ...)`` 的规范化与拒绝规则。
3. ``PUT /api/config {"proxy": ...}`` 的 200 / 422 契约，以及 ``snapshot()`` 回显。
4. ``HttpPool._target`` 把 proxy 纳入连接 key；``_connect`` 在有代理时对 HTTPS 目标
   ``set_tunnel`` 到真实 host/port（用假连接类断言，不真连网）。
5. 单账号轮询（``single_poller``）：同一时刻只有一个登录账号刷列表，且能轮转覆盖所有账号；
   关闭开关则回到「每账号各一条轮询线程」的旧行为。

两种跑法都支持：
    python -m pytest tests/test_har_fixes.py -x -q
    python tests/test_har_fixes.py
"""

from __future__ import annotations

import http.client
import sys
import time
from pathlib import Path

import httpx  # noqa: F401 - TestClient 依赖 httpx（starlette 内部使用）
import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import (  # noqa: E402
    SINGLE_POLLER_KEY,
    EngineConfig,
    HttpPool,
    SniperEngine,
    _validate_field,
)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """数据目录指向 tmp，且不继承宿主机的 SNIPER_PROXY。"""
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MATERIALS_DATA_DIR", str(directory))
    monkeypatch.delenv("SNIPER_PROXY", raising=False)
    return directory


def _client(app_base: str = "http://127.0.0.1:18999"):
    """TestClient（Host 必须是 localhost/127.0.0.1，否则同源中间件回 403）。"""
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app, base_url=app_base)


# ===================================================================
# 1) last_poll_at / tasks.ts 必须是 epoch 秒
# ===================================================================
def test_ingest_timestamps_are_epoch_seconds(data_dir):
    """对外字段用 epoch 秒；内部 first/last_seen 仍是 monotonic。"""
    engine = SniperEngine(lambda event: None)
    try:
        item = {"id": 1, "taskType": "standard", "title": "测试任务",
                "baseCommission": "20.00", "remainingQuota": 1}
        engine._ingest("standard", [item], 1)

        status = engine.status()
        last_poll_at = status["last_poll_at"]
        tasks_ts = engine.tasks()["ts"]

        for label, value in (("last_poll_at", last_poll_at), ("tasks.ts", tasks_ts)):
            assert isinstance(value, float), f"{label} 应为 float，实际 {type(value)}"
            # epoch 秒形如 1.79e9；monotonic 是开机秒数（通常 1e4~1e6）
            assert value > 1e8, f"{label}={value} 不像 epoch 秒"
            assert abs(time.time() - value) < 60, f"{label}={value} 与当前时间差过大"

        # 内部 TTL 逻辑必须继续用 monotonic（否则会被误判为「很久以前」）
        mono = engine._task_first_seen[1]
        assert mono == engine._task_last_seen[1]
        assert mono < 1e8, f"内部 first_seen={mono} 竟然不是 monotonic"
        assert abs(time.monotonic() - mono) < 60
    finally:
        engine._stop.set()


def test_engine_config_proxy_env_fallback(data_dir, monkeypatch):
    """不设 SNIPER_PROXY → 默认 None（默认行为零变化）；设了 → 兜底生效。"""
    monkeypatch.delenv("SNIPER_PROXY", raising=False)
    assert EngineConfig().proxy is None
    monkeypatch.setenv("SNIPER_PROXY", "  http://127.0.0.1:9090  ")
    assert EngineConfig().proxy == "http://127.0.0.1:9090"


# ===================================================================
# 2) _validate_field("proxy", ...)
# ===================================================================
def test_validate_proxy_normalizes_and_rejects():
    assert _validate_field("proxy", None) is None
    assert _validate_field("proxy", "") is None
    assert _validate_field("proxy", "   ") is None
    # 省略协议头会自动补 http://
    assert _validate_field("proxy", "127.0.0.1:9090") == "http://127.0.0.1:9090"
    assert _validate_field("proxy", "http://127.0.0.1:9090") == "http://127.0.0.1:9090"

    for bad in ("socks5://x:1", "https://127.0.0.1:9090"):
        with pytest.raises(ValueError) as exc:
            _validate_field("proxy", bad)
        assert "只支持 http://host:port" in str(exc.value)

    # 没有端口 → 报错
    with pytest.raises(ValueError) as exc:
        _validate_field("proxy", "http://x")
    assert "端口" in str(exc.value)

    # 非字符串 → 报错
    with pytest.raises(ValueError):
        _validate_field("proxy", 9090)


# ===================================================================
# 3) PUT /api/config 契约
# ===================================================================
def test_put_config_proxy_roundtrip(data_dir):
    with _client() as client:
        resp = client.put("/api/config", json={"proxy": "http://127.0.0.1:9090"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["proxy"] == "http://127.0.0.1:9090"

        # 回读
        assert client.get("/api/config").json()["proxy"] == "http://127.0.0.1:9090"

        # 关闭代理（显式 null）
        resp = client.put("/api/config", json={"proxy": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["proxy"] is None

        # 非法值 → 422
        for bad in ("bad", "socks5://x:1", "http://x"):
            resp = client.put("/api/config", json={"proxy": bad})
            assert resp.status_code == 422, (bad, resp.text)


# ===================================================================
# 4) HttpPool：proxy 进 key + CONNECT 隧道
# ===================================================================
class _FakeConn:
    """假连接：只记录构造参数 / set_tunnel / connect，绝不真连网。"""

    def __init__(self, host, port=None, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.kwargs = kwargs
        self.tunnel = None
        self.connected = False

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, int(port))

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False


def test_pool_target_includes_proxy_in_key():
    pool = HttpPool()
    direct = pool._target(EngineConfig(base="https://materials.xlya.cn", proxy=None))
    proxied = pool._target(EngineConfig(base="https://materials.xlya.cn",
                                        proxy="http://127.0.0.1:9090"))
    assert direct == ("https", "materials.xlya.cn", 443, None)
    assert proxied == ("https", "materials.xlya.cn", 443, "http://127.0.0.1:9090")
    # 换代理 → key 不同 → 线程本地池不会复用旧连接
    assert direct != proxied


def test_pool_connect_uses_tunnel_for_https_target(monkeypatch):
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConn)
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    pool = HttpPool()

    cfg = EngineConfig(base="https://materials.xlya.cn",
                       proxy="http://127.0.0.1:9090")
    key = pool._target(cfg)
    conn = pool._connect(cfg, key)
    # 连的是代理
    assert (conn.host, conn.port) == ("127.0.0.1", 9090)
    # 隧道指向真实目标
    assert conn.tunnel == ("materials.xlya.cn", 443)
    assert conn.connected is True
    # 不许额外传 context=（会绕过 Windows 系统证书库）
    assert "context" not in conn.kwargs


def test_pool_connect_without_proxy_unchanged(monkeypatch):
    """默认行为零变化：不设代理时直连目标，不调用 set_tunnel。"""
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConn)
    monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)
    pool = HttpPool()

    cfg = EngineConfig(base="https://materials.xlya.cn", proxy=None)
    conn = pool._connect(cfg, pool._target(cfg))
    assert (conn.host, conn.port) == ("materials.xlya.cn", 443)
    assert conn.tunnel is None
    assert conn.connected is True

    # http 目标直连 80 端口
    cfg_http = EngineConfig(base="http://127.0.0.1:8000", proxy=None)
    conn_http = pool._connect(cfg_http, pool._target(cfg_http))
    assert (conn_http.host, conn_http.port) == ("127.0.0.1", 8000)


# ===================================================================
# 5) 单账号轮询：同一时刻只有一个登录账号刷列表
# ===================================================================
def _offline_engine(monkeypatch, targets):
    """离线引擎：不真连网（warm / _poll_once 都替身），只验证线程与选号逻辑。"""
    engine = SniperEngine(lambda event: None)
    monkeypatch.setattr(engine, "_poll_targets", lambda: list(targets))
    monkeypatch.setattr(engine._http, "warm", lambda cfg: None)
    monkeypatch.setattr(engine, "_poll_once", lambda *a, **k: None)
    engine._running = True
    engine._state = "running"
    return engine


def test_single_poller_creates_one_thread_per_task_type(data_dir, monkeypatch):
    engine = _offline_engine(monkeypatch, [(1, "tok1"), (2, "tok2")])
    try:
        engine._cfg = EngineConfig(single_poller=True,
                                   task_types=("standard", "long_term"))
        engine._sync_pollers(engine._cfg)
        keys = set(engine._pollers)
        # 每个任务类型只有一条线程，且都不是「真实账号」的 key
        assert keys == {(SINGLE_POLLER_KEY, "standard"),
                        (SINGLE_POLLER_KEY, "long_term")}, keys
        assert len(engine._pollers) == 2
    finally:
        engine.stop()
        engine._stop.set()


def test_poller_disabled_falls_back_to_per_account_threads(data_dir, monkeypatch):
    engine = _offline_engine(monkeypatch, [(1, "tok1"), (2, "tok2")])
    try:
        engine._cfg = EngineConfig(single_poller=False, task_types=("standard",))
        engine._sync_pollers(engine._cfg)
        assert set(engine._pollers) == {(1, "standard"), (2, "standard")}
    finally:
        engine.stop()
        engine._stop.set()


def test_pick_poller_cred_rotates_and_skips_blocked(data_dir, monkeypatch):
    engine = _offline_engine(monkeypatch, [(1, "tok1"), (2, "tok2")])
    try:
        # 不轮转：永远用同一个账号（最省请求）
        engine._cfg = EngineConfig(single_poller=True, poll_rotate_seconds=0.0)
        engine._poll_cred = None
        assert engine._pick_poller_cred() == 1
        assert engine._pick_poller_cred() == 1

        # 允许轮转：到点后换下一个账号，保证每个账号的任务都能被看到
        engine._cfg = EngineConfig(single_poller=True, poll_rotate_seconds=0.01)
        engine._poll_cred = 1
        engine._poll_cred_at = time.monotonic() - 1.0
        assert engine._pick_poller_cred() == 2

        # 被限流阻塞的账号会被跳过
        engine._poll_cred = 2
        engine._poll_cred_at = time.monotonic() - 1.0
        engine._poll_block_until_by_cred[2] = time.monotonic() + 60
        assert engine._pick_poller_cred() == 1

        # 没有任何可用账号（还没 token）→ None，轮询线程只等待
        monkeypatch.setattr(engine, "_poll_targets", lambda: [])
        assert engine._pick_poller_cred() is None
        assert SINGLE_POLLER_KEY == -1
    finally:
        engine.stop()
        engine._stop.set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-x", "-q"]))