#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「打开站点并自动登录」的单元测试（**不**启动浏览器、**不**联网）。

覆盖：
1. ``build_token_init_script`` 的纯函数行为（JSON 转义 / 键名 / 单行 / 包裹）；
2. ``LoginManager.open_site`` happy path：token 注入 + 导航到工作台首页；
3. 无 token 且用密码重登失败 → ``ok=False``，且不启动浏览器；
4. token 已过期 → 先静默重登拿新 token 再注入；
5. 浏览器起不来 → ``ok=False`` + 会话从 ``_sites`` 摘除（可重试）；
6. 凭据不存在 → ``ValueError``；
7. 已打开时重复点击 → 第二次 ``state="already_open"``；
8. ``POST /api/credentials/{id}/open-site`` 路由的状态码映射
   （404 / 502 / 503 / 200，直接调用路由函数，不起 uvicorn）。

跑法：
    python -m pytest tests/test_open_site.py -q
    python -m pytest tests -x -q
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# 让测试文件无论从项目根、tests/ 还是直接运行都能导入 server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.login import build_token_init_script  # noqa: E402  (需先插入 sys.path)

GOOD_PHONE = "13800001111"
GOOD_PASSWORD = "pass123"
BASE = "https://materials.invalid"
LANDING_SUFFIX = "/pages/tasks/index"


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
TOK_EXPIRED = fake_jwt("old", exp_offset=-3600)


# ------------------------------------------------------------------ 替身
class FakeEngine:
    """``core.SniperEngine`` 的最小替身：只实现 LoginManager 会用到的方法。"""

    def __init__(self, base: str = BASE) -> None:
        self._base = base
        self.logs: list = []
        self.set_token_calls: list = []

    def token_status(self) -> dict:
        return {"present": False, "valid": False}

    def set_token(self, token: str) -> dict:
        self.set_token_calls.append(token)
        return {"present": True, "valid": True, "exp": "2026-01-01T00:00:00Z"}

    def log(self, msg: str, level: str = "INFO") -> None:
        self.logs.append((level, msg))

    def snapshot(self) -> dict:
        return {"base": self._base}


class FakePage:
    def __init__(self) -> None:
        self.gotos: list = []
        self.handlers: dict = {}
        self._closed = False
        self.probe_calls: list = []
        self.probe_exc: BaseException | None = None

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append({"url": url, "wait_until": wait_until,
                           "timeout": timeout})
        return None

    def wait_for_function(self, expression, timeout=None, **kwargs):
        """存活探测的「真实往返」：``probe_exc`` 非空时按设定抛异常。"""
        self.probe_calls.append({"expression": expression, "timeout": timeout})
        if self.probe_exc is not None:
            raise self.probe_exc
        return _FakeHandle()

    def is_closed(self) -> bool:
        """返回本地缓存标志（真实世界里浏览器被外部杀掉后它**仍然是** False）。"""
        return self._closed

    def close(self) -> None:
        self._closed = True

    def on(self, event, handler=None):
        self.handlers[event] = handler
        return self


class _FakeHandle:
    """``wait_for_function`` 返回的 JSHandle 替身（只需 ``dispose``）。"""

    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class TargetClosedError(Exception):
    """模拟 ``playwright._impl._errors.TargetClosedError``。"""


class PlaywrightTimeoutError(Exception):
    """模拟 ``playwright._impl._errors.TimeoutError``（名字不含 closed）。"""


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.init_scripts: list = []
        self._page = page

    def add_init_script(self, script=None, **kwargs):
        self.init_scripts.append(script)

    def new_page(self) -> FakePage:
        return self._page


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts: list = []
        self._page = page
        self.closed = False
        self.connected = True

    def new_context(self) -> FakeContext:
        ctx = FakeContext(self._page)
        self.contexts.append(ctx)
        return ctx

    def is_connected(self) -> bool:
        return bool(self.connected) and not self.closed

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeEnv:
    """一次 open_site 用到的全部假对象 + 打点。"""

    def __init__(self) -> None:
        self.page = FakePage()
        self.browser = FakeBrowser(self.page)
        self.pw = FakePlaywright()
        self.calls: list = []
        self.raise_on_open = None

    @property
    def context(self) -> FakeContext:
        assert self.browser.contexts, "browser.new_context() 没有被调用"
        return self.browser.contexts[0]


@pytest.fixture()
def fake_env(monkeypatch):
    """把 ``_open_site_browser`` 换成假实现：测试绝不真拉起 Edge。"""
    from server import login as login_mod

    env = _FakeEnv()

    def fake_open_site_browser(headless):
        env.calls.append(bool(headless))
        if env.raise_on_open is not None:
            raise env.raise_on_open
        return (env.pw, env.browser)

    monkeypatch.setattr(login_mod, "_open_site_browser", fake_open_site_browser)
    return env


@pytest.fixture()
def store(tmp_path):
    from server.credentials import CredentialStore

    return CredentialStore(tmp_path)


def _make_manager(engine, store):
    from server.login import LoginManager

    return LoginManager(engine, store=store)


def _cred(store, tok: str = TOK_A, exp=None) -> int:
    """加一条凭据并写入 token（``tok=""`` 表示没有 token）。"""
    from server.core import jwt_exp

    cid = store.add("主号", GOOD_PHONE, GOOD_PASSWORD)["id"]
    if tok:
        store.set_token(cid, tok, jwt_exp(tok) if exp is None else exp)
    return cid


def _stop_sites(manager) -> None:
    """让所有站点线程退出（避免测试挂住）。"""
    try:
        manager.shutdown()
    except Exception:
        pass
    with manager._lock:                        # noqa: SLF001 - 测试收尾
        sites = list(manager._sites.values())
    for site in sites:
        site["stop"].set()
        thread = site.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)


def _wait_until(predicate, timeout: float = 6.0,
                interval: float = 0.05) -> bool:
    """轮询等待条件成立（worker 的探测/收尾是异步的）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _reset_env(env) -> None:
    """模拟「又开了一个新窗口」：把假 page/browser/pw 复活并清掉打点。

    ``_open_site_browser`` 的假实现每次返回同一套对象，所以重开前要把上一轮
    worker 收尾时留下的 closed/stopped 状态复位。
    """
    env.page._closed = False            # noqa: SLF001 - 测试替身内部状态
    env.page.probe_exc = None
    env.page.gotos.clear()
    env.page.probe_calls.clear()
    env.browser.closed = False
    env.browser.connected = True
    env.browser.contexts.clear()
    env.pw.stopped = False


def _sites_empty(manager) -> bool:
    with manager._lock:                        # noqa: SLF001 - 测试断言
        return manager._sites == {}


# ===================================================================
# 1) build_token_init_script：纯函数行为
# ===================================================================
def test_build_token_init_script_is_wrapped_and_keyed():
    from server import login as login_mod

    script = login_mod.build_token_init_script(TOK_A)
    assert script.startswith("try{localStorage.setItem(")
    assert script.endswith("}catch(e){}")
    assert '"pp_user_token"' in script
    assert login_mod.TOKEN_STORAGE_KEY == "pp_user_token"
    # 简单 token 原文恰好出现一次
    assert script.count(TOK_A) == 1
    # 单行输出（不能带换行，否则 JS 里会断句）
    assert "\n" not in script


@pytest.mark.parametrize("token", [
    'a"b',                       # 双引号
    "a\\b",                      # 反斜杠
    "line1\nline2",              # 换行
    '");alert(1);//',            # 注入尝试
    "中文-token-\u00e9",         # 非 ASCII（ensure_ascii=False 也必须是合法 JS 字符串）
    "a\"b\\c\nd",
])
def test_build_token_init_script_escapes_json(token):
    from server import login as login_mod

    script = login_mod.build_token_init_script(token)
    assert script == ('try{localStorage.setItem("pp_user_token", %s);}catch(e){}'
                     % json.dumps(token, ensure_ascii=False))
    assert "\n" not in script
    matched = re.match(
        r'^try\{localStorage\.setItem\("pp_user_token", (.*)\);\}catch\(e\)\{\}$',
        script)
    assert matched, script
    # JSON 字面量必须能原样还原成同一个 token（= 转义正确、无法逃逸）
    assert json.loads(matched.group(1)) == token


def test_build_token_init_script_handles_empty_token():
    from server import login as login_mod

    script = login_mod.build_token_init_script("")     # type: ignore[arg-type]
    assert script == 'try{localStorage.setItem("pp_user_token", "");}catch(e){}'


# ===================================================================
# 2) open_site happy path：注入 token + 导航到工作台首页
# ===================================================================
def test_open_site_injects_token_and_navigates_to_landing(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)
    assert store.get_token(cid) == TOK_A

    try:
        result = manager.open_site(cid)
    finally:
        _stop_sites(manager)

    assert result["ok"] is True, result
    assert result["state"] == "opened", result
    assert result.get("refreshed") is False
    assert result["url"].endswith(LANDING_SUFFIX)
    assert GOOD_PHONE[-11:] in result["message"] or "138" in result["message"]

    # 浏览器被真起来了（且用的是默认 headless=False）
    assert fake_env.calls == [False]
    # goto 打点：URL 以工作台首页结尾、domcontentloaded
    assert len(fake_env.page.gotos) == 1
    goto = fake_env.page.gotos[0]
    assert goto["url"] == BASE + LANDING_SUFFIX
    assert goto["url"].endswith(LANDING_SUFFIX)
    assert goto["wait_until"] == "domcontentloaded"

    # 关键：init script 在 new_page/goto 之前挂上，且含该账号 token
    assert len(fake_env.context.init_scripts) == 1
    script = fake_env.context.init_scripts[0]
    assert script == build_token_init_script(TOK_A)
    assert TOK_A in script
    assert '"pp_user_token"' in script

    # 日志里绝不出现 token 原文
    assert all(TOK_A not in str(msg) for _, msg in engine.logs), engine.logs
    # 且有一条 OK 成功的日志
    assert any(level == "OK" for level, _ in engine.logs), engine.logs


def test_open_site_headless_flag_is_forwarded(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)
    try:
        result = manager.open_site(cid, headless=True)
    finally:
        _stop_sites(manager)
    assert result["ok"] is True, result
    assert fake_env.calls == [True]


# ===================================================================
# 3) 无 token + 重登失败 → ok=False，且压根不启动浏览器
# ===================================================================
def test_open_site_reports_relogin_failure(store, fake_env, monkeypatch):
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store, tok="")                  # 没有任何 token

    monkeypatch.setattr(login_mod, "api_login",
                        lambda base, phone, password, log=None: (False, "密码错误"))

    result = manager.open_site(cid)

    assert result["ok"] is False, result
    assert "密码错误" in str(result["error"]), result
    assert fake_env.calls == []                 # 失败前不该开浏览器
    with manager._lock:                         # noqa: SLF001
        assert manager._sites == {}


def test_open_site_relogin_upstream_error_sets_flag(store, fake_env, monkeypatch):
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store, tok="")
    monkeypatch.setattr(
        login_mod, "api_login",
        lambda base, phone, password, log=None: (
            False, f"{login_mod.UPSTREAM_ERROR_TAG}（HTTP 502）"))

    result = manager.open_site(cid)

    assert result["ok"] is False, result
    assert result.get("_upstream_error") is True, result
    assert login_mod.is_upstream_error(result["error"]) is True
    assert fake_env.calls == []


# ===================================================================
# 4) token 已过期 → 先静默重登取新 token 再注入
# ===================================================================
def test_open_site_refreshes_expired_token(store, fake_env, monkeypatch):
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store, tok=TOK_EXPIRED, exp=int(time.time()) - 120)

    monkeypatch.setattr(login_mod, "api_login",
                        lambda base, phone, password, log=None: (True, TOK_B))

    try:
        result = manager.open_site(cid)
    finally:
        _stop_sites(manager)

    assert result["ok"] is True, result
    assert result.get("refreshed") is True, result
    # 注入的是重登后的新 token，而不是过期的旧 token
    script = fake_env.context.init_scripts[0]
    assert TOK_B in script
    assert TOK_EXPIRED not in script
    assert store.get_token(cid) == TOK_B
    # 静默重登绝不碰活跃 Token
    assert engine.set_token_calls == []


# ===================================================================
# 5) 浏览器起不来 → ok=False，且会话被摘除（用户可重试）
# ===================================================================
def test_open_site_reports_browser_launch_failure(store, fake_env, monkeypatch):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)
    fake_env.raise_on_open = RuntimeError("msedge channel 不可用")

    result = manager.open_site(cid)

    assert result["ok"] is False, result
    assert "无法启动浏览器" in str(result["error"]), result
    assert "msedge" in str(result["error"]), result
    with manager._lock:                         # noqa: SLF001
        assert manager._sites == {}


def test_open_site_goto_failure_reports_error(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    def boom(url, wait_until=None, timeout=None):
        raise TimeoutError("page.goto: Timeout 45000ms exceeded")

    fake_env.page.goto = boom                    # type: ignore[assignment]

    result = manager.open_site(cid)

    assert result["ok"] is False, result
    assert "打开站点失败" in str(result["error"]), result
    # 异常分支也要把浏览器关掉：
    assert fake_env.pw.stopped is True
    with manager._lock:                         # noqa: SLF001
        assert manager._sites == {}


# ===================================================================
# 6) 凭据不存在 → ValueError
# ===================================================================
def test_open_site_missing_credential_raises(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    with pytest.raises(ValueError) as info:
        manager.open_site(999)
    assert "凭据不存在" in str(info.value)
    assert fake_env.calls == []


# ===================================================================
# 7) 已打开 → 第二次 already_open（页面未关闭）
# ===================================================================
def test_open_site_twice_returns_already_open(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    try:
        first = manager.open_site(cid)
        assert first["ok"] is True, first
        assert first["state"] == "opened", first

        second = manager.open_site(cid)
        assert second["ok"] is True, second
        assert second["state"] == "already_open", second
        assert second["url"] == first["url"]
        assert "已打开" in second["message"]

        # 只起了一个浏览器 / 一次导航
        assert fake_env.calls == [False]
        assert len(fake_env.page.gotos) == 1
    finally:
        _stop_sites(manager)


# ===================================================================
# 8) 路由状态码映射（直接调用路由函数，不起 uvicorn）
# ===================================================================
def _fake_request(manager, store):
    return SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(login_manager=manager, credentials=store)))


def test_route_open_site_status_mapping(store):
    from fastapi import HTTPException

    from server.app import post_open_site

    cid = _cred(store)

    # 404：凭据不存在（路由先查 store）
    with pytest.raises(HTTPException) as info:
        post_open_site(999, _fake_request(SimpleNamespace(), store))
    assert info.value.status_code == 404
    assert info.value.detail == "凭据不存在"

    # 404：manager 抛 ValueError
    manager = SimpleNamespace(open_site=lambda cred_id, headless=False: (_ for _ in ()).throw(ValueError("凭据不存在")))
    with pytest.raises(HTTPException) as info:
        post_open_site(cid, _fake_request(manager, store))
    assert info.value.status_code == 404

    # 502：上游错误（error 前缀 UPSTREAM_ERROR_TAG）
    from server.login import UPSTREAM_ERROR_TAG

    manager = SimpleNamespace(open_site=lambda cred_id, headless=False: {
        "ok": False, "error": f"{UPSTREAM_ERROR_TAG}（HTTP 502）",
        "_upstream_error": True})
    with pytest.raises(HTTPException) as info:
        post_open_site(cid, _fake_request(manager, store))
    assert info.value.status_code == 502
    assert UPSTREAM_ERROR_TAG in info.value.detail

    # 503：本地失败（浏览器起不来等）
    manager = SimpleNamespace(open_site=lambda cred_id, headless=False: {
        "ok": False, "error": "无法启动浏览器（Playwright/Edge 不可用）：x"})
    with pytest.raises(HTTPException) as info:
        post_open_site(cid, _fake_request(manager, store))
    assert info.value.status_code == 503

    # 200：成功
    manager = SimpleNamespace(open_site=lambda cred_id, headless=False: {
        "ok": True, "state": "opened", "url": BASE + LANDING_SUFFIX,
        "refreshed": False, "message": "ok"})
    payload = post_open_site(cid, _fake_request(manager, store))
    assert payload["ok"] is True
    assert payload["state"] == "opened"


def test_route_open_site_forwards_headless(store):
    from server.app import post_open_site

    cid = _cred(store)
    seen: list = []

    def fake_open_site(cred_id, headless=False):
        seen.append((cred_id, headless))
        return {"ok": True, "state": "opened", "url": BASE + LANDING_SUFFIX}

    manager = SimpleNamespace(open_site=fake_open_site)
    post_open_site(cid, _fake_request(manager, store), headless=True)
    assert seen == [(cid, True)]


# ===================================================================
# 9) shutdown 会关掉站点窗口并让线程退出
# ===================================================================
def test_shutdown_closes_site_windows(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    result = manager.open_site(cid)
    assert result["state"] == "opened", result
    with manager._lock:                          # noqa: SLF001
        site = manager._sites[cid]
    thread = site["thread"]
    assert thread.is_alive()

    manager.shutdown()

    thread.join(timeout=5.0)
    assert not thread.is_alive(), "shutdown 后站点线程必须退出"
    assert fake_env.browser.closed is True
    assert fake_env.pw.stopped is True
    with manager._lock:                          # noqa: SLF001
        assert manager._sites == {}


def test_reap_stray_keeps_open_site_pids(store, fake_env, monkeypatch):
    """取消登录会话（cancel/_reap_stray）绝不能杀掉正开着的站点窗口。"""
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    # 让 worker 记到一个假的 driver pid（真进程不存在，只验证集合语义）
    monkeypatch.setattr(login_mod, "_driver_pid", lambda pw: 4321)

    try:
        result = manager.open_site(cid)
        assert result["state"] == "opened", result
        with manager._lock:                      # noqa: SLF001
            assert 4321 in manager._pids

        killed_pids: list = []
        monkeypatch.setattr(login_mod, "kill_process_trees",
                            lambda pids: killed_pids.extend(pids) or [])
        manager._reap_stray()                    # noqa: SLF001 - 直接验语义

        assert 4321 not in killed_pids, "存活的站点窗口 pid 被误杀了"
        with manager._lock:                      # noqa: SLF001
            assert 4321 in manager._pids          # 留给 shutdown 兜底
    finally:
        _stop_sites(manager)


# ===================================================================
# 10) 回归：浏览器被**外部**杀掉时，本地标志仍在说谎，只有真实往返能发现
# ===================================================================
def test_worker_detects_external_kill_via_roundtrip(store, fake_env):
    """回归 BUG：外部杀进程后 is_closed()=False / is_connected()=True 依旧成立。

    真实往返抛 ``TargetClosedError`` → worker 必须退出、``_sites`` 必须摘除，
    且下一次点击必须重新打开（而不是永远 already_open）。
    """
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    try:
        first = manager.open_site(cid)
        assert first["state"] == "opened", first

        page, browser = fake_env.page, fake_env.browser
        # 复现环境事实：杀进程后这两个本地标志**都在说谎**
        assert page.is_closed() is False
        assert browser.is_connected() is True
        page.probe_exc = TargetClosedError(
            "Target page, context or browser has been closed")

        # 1~2s 内 worker 应感知死亡并走原有收尾
        assert _wait_until(lambda: _sites_empty(manager)), \
            "外部杀进程后 _sites 条目没有被摘除（窗口永久卡死）"
        assert browser.closed is True, "收尾必须关浏览器"
        assert fake_env.pw.stopped is True, "收尾必须停 playwright"
        assert any("站点窗口已关闭" in msg for _, msg in engine.logs), engine.logs
        # 探测确实用了带 timeout 的真实往返
        assert page.probe_calls, "存活探测没有发真实往返"
        assert page.probe_calls[-1]["timeout"] == \
            login_mod._SITE_PROBE_TIMEOUT_MS          # noqa: SLF001

        # 关键链路：窗口死后必须能重新打开，而不是又 already_open
        _reset_env(fake_env)
        second = manager.open_site(cid)
        assert second["ok"] is True, second
        assert second["state"] == "opened", second
    finally:
        _stop_sites(manager)


# ===================================================================
# 11) 往返探测超时（页面繁忙）= 存活，绝不误关窗口
# ===================================================================
def test_worker_keeps_window_when_probe_times_out(store, fake_env):
    from server import login as login_mod

    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    try:
        assert manager.open_site(cid)["state"] == "opened"
        fake_env.page.probe_exc = PlaywrightTimeoutError(
            "Timeout 1000ms exceeded.")            # 页面繁忙，浏览器还活着

        time.sleep(1.6)                            # 至少跑完一轮探测 + 1s 等待
        with manager._lock:                        # noqa: SLF001
            site = manager._sites.get(cid)
        assert site is not None, "页面繁忙被误判成死亡：_sites 被摘除了"
        assert site["thread"].is_alive(), "页面繁忙时 worker 不该退出"
        assert fake_env.browser.closed is False, "页面繁忙时不该关窗口"
        assert fake_env.pw.stopped is False
        # 探测确实带客户端侧超时（否则页面 JS 死循环会永久阻塞 worker）
        assert fake_env.page.probe_calls
        assert fake_env.page.probe_calls[-1]["timeout"] == \
            login_mod._SITE_PROBE_TIMEOUT_MS       # noqa: SLF001
    finally:
        fake_env.page.probe_exc = None
        _stop_sites(manager)


# ===================================================================
# 12) 窗口正常关闭 → 条目摘除 → 下次点击是 opened（不是 already_open）
# ===================================================================
def test_reopen_after_window_closed_normally(store, fake_env):
    engine = FakeEngine()
    manager = _make_manager(engine, store)
    cid = _cred(store)

    try:
        assert manager.open_site(cid)["state"] == "opened"
        # 已打开时重复点击就是 already_open
        assert manager.open_site(cid)["state"] == "already_open"

        fake_env.page.close()                      # 等价于用户手动关窗
        assert _wait_until(lambda: _sites_empty(manager)), \
            "窗口关闭后 1~2s 内 _sites 条目必须被摘除"

        _reset_env(fake_env)
        result = manager.open_site(cid)
        assert result["ok"] is True, result
        assert result["state"] == "opened", result
    finally:
        _stop_sites(manager)


# ===================================================================
# 13) 异常分类器：区分「目标已关闭」与「页面繁忙超时」
# ===================================================================
@pytest.mark.parametrize("exc,expect_closed", [
    (TargetClosedError("Target page, context or browser has been closed"), True),
    (Exception("Target page, context or browser has been closed"), True),
    (Exception("Browser has been closed"), True),
    (Exception("Connection closed"), True),
    (Exception("Target crashed"), True),
    (PlaywrightTimeoutError("Timeout 1000ms exceeded."), False),
    (TimeoutError("Timeout 1000ms exceeded."), False),
    (Exception("Frame was detached"), False),        # 无法确认 → 保守判存活
])
def test_is_target_closed_error_classification(exc, expect_closed):
    from server import login as login_mod

    assert login_mod._is_target_closed_error(exc) is expect_closed   # noqa: SLF001


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))