# -*- coding: utf-8 -*-
"""EngineHub —— 多登录账号并发引擎管理。

一个登录凭据（``CredentialStore`` 中的一条记录）= 一个独立的 ``SniperEngine``：

- 独立 token（各自登录 / 自动续登，互不影响）；
- 独立任务快照、去重集合、调度队列、限速器、领取线程；
- 独立数据目录 ``<data_dir>/accounts/<cred_id>/``（token/config/history/log 隔离）；
- 独立平台子账号集合（上游按 token 隔离，天然不会串号）。

没有任何凭据时退化为单个 legacy 引擎（数据目录=根目录），行为与旧版完全一致。
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import persist
from .core import DEFAULT_CONFIG, EngineConfig, SniperEngine
from .credentials import mask_phone
from .login import LoginBusy, LoginManager

LEGACY_KEY = 0             # 无凭据时的匿名引擎
RENEW_INTERVAL = 8.0       # 自动续登巡检间隔（秒）
RENEW_COOLDOWN = 30.0      # 同一账号两次登录尝试的最小间隔（秒）


class EngineHub:
    """聚合多个 ``SniperEngine``，对外暴露与单引擎兼容的接口。"""

    def __init__(self, publish, store, data_dir, on_log=None) -> None:
        self._publish = publish
        self._store = store
        self._root = Path(data_dir)
        self._on_log = on_log
        self._lock = threading.RLock()
        self._agg_lock = threading.Lock()   # 聚合状态串行化，避免多引擎交叉加锁
        self._engines: dict = {}      # key -> SniperEngine
        self._managers: dict = {}     # key -> LoginManager
        self._meta: dict = {}         # key -> {cred_id, phone, name, label}
        self._last_login: dict = {}   # key -> monotonic 上次登录尝试
        self._desired_accounts: dict = {}   # key -> 用户是否希望该账号运行
        self._interactive_key: Optional[int] = None
        self._session_key: Optional[int] = None
        self._stop = threading.Event()
        self._renew_thread: Optional[threading.Thread] = None
        self._cfg_template: Optional[dict] = None

        # 只在启动时做一次旧版根 token 迁移（不能放在每次 sync 里，
        # 否则运行期新增账号可能把 root token 错配给新账号）
        records = self._cred_records()
        if len(records) > 1:
            self._adopt_root_token(records)
        self.sync_accounts()
        self._start_renew_loop()

    # ========================== 账号 / 引擎 ==========================
    def _engine_dir(self, key: int, cred_count: Optional[int] = None) -> Path:
        """数据目录：legacy/单账号沿用根目录（兼容旧数据），多账号才分目录。"""
        if key == LEGACY_KEY:
            return self._root
        if cred_count is None:
            cred_count = len(self._cred_ids())
        if cred_count <= 1:
            return self._root
        return self._root / "accounts" / str(key)

    def _cred_ids(self) -> set:
        ids = set()
        for rec in self._store.list_all() or []:
            try:
                ids.add(int(rec.get("id")))
            except Exception:
                continue
        return ids

    def _cred_records(self) -> dict:
        """凭据记录快照：``{cred_id: record}``；非法记录直接忽略。"""
        out: dict = {}
        for rec in self._store.list_all() or []:
            try:
                out[int(rec.get("id"))] = rec
            except Exception:
                continue
        return out

    def _snapshot_engines(self) -> list:
        with self._lock:
            return list(self._engines.items())

    def _meta_of(self, key: int) -> dict:
        with self._lock:
            return dict(self._meta.get(key) or {})

    def sync_accounts(self) -> None:
        """与凭据存储对齐：新增建引擎、删除停引擎、无凭据时建 legacy。

        注意：旧版根 token 的归属迁移只在 ``__init__`` 做一次；运行期新增
        账号时不能重复迁移，否则会把根目录里的 token 错配给新账号。
        """
        creds = self._cred_records()

        with self._lock:
            for key in list(self._engines):
                if key != LEGACY_KEY and key not in creds:
                    engine = self._engines.pop(key)
                    self._managers.pop(key, None)
                    self._meta.pop(key, None)
                    self._last_login.pop(key, None)
                    self._desired_accounts.pop(key, None)
                    try:
                        engine.stop()
                    except Exception:
                        pass
            if creds and LEGACY_KEY in self._engines:
                engine = self._engines.pop(LEGACY_KEY)
                self._managers.pop(LEGACY_KEY, None)
                self._meta.pop(LEGACY_KEY, None)
                self._desired_accounts.pop(LEGACY_KEY, None)
                try:
                    engine.stop()
                except Exception:
                    pass

        # 账号数量变化时（1↔N）调整数据目录并迁移 token/config
        self._migrate_dirs(creds)

        with self._lock:
            if creds:
                for cid, rec in creds.items():
                    self._ensure_engine(cid, rec)
            else:
                self._ensure_engine(LEGACY_KEY, {})

    def _adopt_root_token(self, creds: dict) -> None:
        """升级迁移：把旧版根目录 token.json 交给最近成功登录的那个账号。"""
        try:
            root_token = persist.load_token(self._root)
            if not root_token:
                return
            for cid in creds:
                if persist.load_token(self._engine_dir(int(cid))):
                    return  # 已经有账号 token，不迁移
            best = max(creds.values(), key=lambda r: r.get("last_ok_at") or 0)
            persist.save_token(root_token, self._engine_dir(int(best["id"])))
        except Exception:
            pass

    def _migrate_dirs(self, creds: dict) -> None:
        """账号数 1↔N 变化时把引擎迁到正确目录，并带上 token/config。"""
        count = len(creds)
        for key, engine in list(self._snapshot_engines()):
            target = self._engine_dir(key, count)
            try:
                current = Path(engine.data_dir())
                if current.resolve() == target.resolve():
                    continue
            except Exception:
                continue
            if key != LEGACY_KEY and key not in creds:
                continue
            was_running = bool(engine.status().get("running"))
            try:
                engine.stop()
            except Exception:
                pass
            try:
                persist.ensure_dir(target)
            except Exception:
                pass
            for name in ("token.json", "config.json"):
                src, dst = current / name, target / name
                if not src.exists():
                    continue
                # 迁回根目录时，账号自己的 token 必须覆盖根目录里的旧 token
                overwrite = (name == "token.json" and target == self._root)
                if dst.exists() and not overwrite:
                    continue
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
            with self._lock:
                self._engines.pop(key, None)
                self._managers.pop(key, None)
                rec = creds.get(key) or {}
                new_engine = self._ensure_engine(key, rec)
            if was_running and new_engine is not None:
                try:
                    new_engine.start()
                except Exception:
                    pass

    def _ensure_engine(self, key: int, rec: dict) -> Optional[SniperEngine]:
        with self._lock:
            phone = str(rec.get("phone") or "")
            label = mask_phone(phone) or (rec.get("name") or "")
            self._meta[key] = {
                "cred_id": int(rec.get("id") or 0) if key != LEGACY_KEY else 0,
                "phone": phone,
                "name": (rec.get("name") or ""),
                "label": label,
            }
            existing = self._engines.get(key)
            if existing is not None:
                return existing

        directory = self._engine_dir(key)
        try:
            persist.ensure_dir(directory)
        except Exception:
            pass

        engine = SniperEngine(lambda ev, k=key: self._on_engine_event(k, ev),
                              data_dir=directory)
        manager = LoginManager(engine, bus=None, store=self._store)

        # 恢复 token / 配置 / 历史
        try:
            token = persist.load_token(directory)
            if token:
                engine.set_token(token)
        except Exception:
            pass
        try:
            cfg = persist.load_config(directory)
            if not cfg:
                cfg = dict(self._cfg_template or {})
                # 新账号不继承全局配置里的 platform_account_id：
                # 那是别的账号名下的子账号，拿来领取必然失败。
                if key != LEGACY_KEY:
                    cfg["platform_account_id"] = None
            if cfg:
                engine.update_config(cfg)
        except Exception:
            pass
        try:
            engine.load_history(persist.load_history(directory, 200))
        except Exception:
            pass

        with self._lock:
            self._engines[key] = engine
            self._managers[key] = manager
            if self._interactive_key is None:
                self._interactive_key = key
        return engine

    # ============================ 事件 ============================
    def _on_engine_event(self, key: int, event: dict) -> None:
        """给事件打上账号标签后广播；status 统一发聚合状态。"""
        try:
            ev = dict(event)
            meta = self._meta.get(key) or {}
            ev["cred_id"] = meta.get("cred_id", 0)
            ev["account"] = meta.get("label", "")
            kind = ev.get("type")
            if kind == "tasks":
                ev["items"] = [self._tag_item(it, meta)
                               for it in (ev.get("items") or [])]
            elif kind == "status":
                # 串行聚合：拿不到锁就跳过本次（下一次事件仍会广播），
                # 避免两个引擎线程互相等待对方的引擎锁造成死锁。
                if not self._agg_lock.acquire(blocking=False):
                    return
                try:
                    ev["status"] = self.status()
                finally:
                    self._agg_lock.release()
            self._publish(ev)
        except Exception:
            pass

    @staticmethod
    def _tag_item(item: dict, meta: dict) -> dict:
        out = dict(item) if isinstance(item, dict) else {}
        out["_cred_id"] = meta.get("cred_id", 0)
        out["_account"] = meta.get("label", "")
        return out

    # ============================ 状态 ============================
    def status(self) -> dict:
        engines = self._snapshot_engines()
        accounts: list = []
        stats_total: dict = {}
        running = False
        states: list = []
        paused_reason = ""
        started_at = None
        last_poll_at = None
        base = None
        queue_size = 0
        workers = 0
        rtt = 0.0
        offset = 0.0
        token_present = False
        token_valid = False
        token_exp: Optional[str] = None

        for key, engine in engines:
            st = engine.status()
            meta = self._meta.get(key) or {}
            cfg = engine.snapshot()
            accounts.append({
                "cred_id": meta.get("cred_id", 0),
                "key": key,
                "phone": meta.get("phone", ""),
                "name": meta.get("name", ""),
                "label": meta.get("label", ""),
                "running": bool(st.get("running")),
                "state": st.get("state"),
                "state_reason": st.get("state_reason") or "",
                "token": st.get("token") or {},
                "stats": st.get("stats") or {},
                "queue": st.get("queue", 0),
                "workers": st.get("workers", 0),
                "last_poll_at": st.get("last_poll_at"),
                "started_at": st.get("started_at"),
                "rtt_ms": st.get("rtt_ms", 0),
                "platform_account_id": cfg.get("platform_account_id"),
                "base": cfg.get("base"),
            })

            for k, v in (st.get("stats") or {}).items():
                if isinstance(v, (int, float)):
                    stats_total[k] = stats_total.get(k, 0) + v
            states.append(st.get("state") or "idle")
            if st.get("running"):
                running = True
            if st.get("state") == "paused" and not paused_reason:
                paused_reason = st.get("state_reason") or ""
            if st.get("started_at"):
                started_at = min(started_at or st["started_at"], st["started_at"])
            if st.get("last_poll_at"):
                last_poll_at = max(last_poll_at or 0, st["last_poll_at"])
            base = base or st.get("upstream_base")
            queue_size += int(st.get("queue") or 0)
            workers += int(st.get("workers") or 0)
            rtt = max(rtt, float(st.get("rtt_ms") or 0))
            offset = offset or float(st.get("clock_offset_ms") or 0)
            tok = st.get("token") or {}
            if tok.get("present"):
                token_present = True
                if tok.get("valid"):
                    token_valid = True
                    exp = tok.get("exp")
                    if exp and (token_exp is None or str(exp) < str(token_exp)):
                        token_exp = exp

        if running:
            state, reason = "running", ""
        elif "paused" in states:
            state, reason = "paused", paused_reason
        elif "error" in states:
            state, reason = "error", ""
        else:
            state, reason = "idle", ""

        return {
            "running": running,
            "state": state,
            "state_reason": reason,
            "token": {"present": token_present, "exp": token_exp,
                      "valid": token_valid},
            "started_at": started_at,
            "last_poll_at": last_poll_at,
            "upstream_base": base,
            "stats": stats_total,
            "queue": queue_size,
            "workers": workers,
            "rtt_ms": round(rtt, 1),
            "clock_offset_ms": round(offset, 1),
            "accounts": accounts,
        }

    def accounts(self) -> dict:
        return {"items": self.status().get("accounts", [])}

    def tasks(self) -> dict:
        """合并各账号任务快照：同一 task id 去重，可领账号取并集。"""
        items: list = []
        index: dict = {}
        ts = None
        for key, engine in self._snapshot_engines():
            meta = self._meta.get(key) or {}
            data = engine.tasks()
            if data.get("ts"):
                ts = max(ts or 0, data["ts"])
            for raw in data.get("items") or []:
                if not isinstance(raw, dict):
                    continue
                tid = raw.get("id")
                item = self._tag_item(raw, meta)
                item["_cred_ids"] = [meta.get("cred_id", 0)]
                if tid in index:
                    self._merge_task(index[tid], item)
                    continue
                index[tid] = item
                items.append(item)
        return {"ts": ts, "items": items}

    @staticmethod
    def _merge_task(cur: dict, new: dict) -> None:
        """同任务合并：账号并集 + 可领状态取或 + 配额取大。"""
        cur.setdefault("_cred_ids", [])
        for cid in new.get("_cred_ids") or []:
            if cid not in cur["_cred_ids"]:
                cur["_cred_ids"].append(cid)
        cur_elig = cur.setdefault("claimEligibility", {})
        new_elig = new.get("claimEligibility") or {}
        accounts = list(cur_elig.get("accounts") or [])
        known = {a.get("id") if isinstance(a, dict) else a for a in accounts}
        for acc in new_elig.get("accounts") or []:
            aid = acc.get("id") if isinstance(acc, dict) else acc
            if aid not in known:
                accounts.append(acc)
                known.add(aid)
        cur_elig["accounts"] = accounts
        if new_elig.get("canClaim"):
            cur_elig["canClaim"] = True
        if cur_elig.get("canClaim") and not cur_elig.get("reason"):
            cur_elig["reason"] = new_elig.get("reason")
        try:
            cur["remainingQuota"] = max(float(cur.get("remainingQuota") or 0),
                                        float(new.get("remainingQuota") or 0))
        except Exception:
            pass

    def platform_accounts(self, platform: Optional[str] = None,
                          cred_id: Optional[int] = None) -> dict:
        """平台子账号：指定 cred_id 时只返回该登录账号名下的子账号。"""
        if cred_id is not None:
            key = int(cred_id)
            engine = self._engines.get(key)
            if engine is None:
                return {"items": [], "fallback": False, "cred_id": key}
            data = engine.platform_accounts(platform)
            meta = self._meta.get(key) or {}
            items = []
            for raw in data.get("items") or []:
                rec = dict(raw) if isinstance(raw, dict) else {}
                rec["cred_id"] = meta.get("cred_id", 0)
                rec["account"] = meta.get("label", "")
                items.append(rec)
            return {"items": items, "fallback": bool(data.get("fallback")),
                    "cred_id": key}

        merged: list = []
        seen: set = set()
        fallback = False
        for key, engine in self._snapshot_engines():
            meta = self._meta.get(key) or {}
            data = engine.platform_accounts(platform)
            fallback = fallback or bool(data.get("fallback"))
            for raw in data.get("items") or []:
                rec = dict(raw) if isinstance(raw, dict) else {}
                rec["cred_id"] = meta.get("cred_id", 0)
                rec["account"] = meta.get("label", "")
                sig = (rec["cred_id"], rec.get("id"))
                if sig in seen:
                    continue
                seen.add(sig)
                merged.append(rec)
        return {"items": merged, "fallback": fallback}

    def history(self) -> list:
        items: list = []
        for key, engine in self._snapshot_engines():
            meta = self._meta.get(key) or {}
            for rec in engine.history():
                if not isinstance(rec, dict):
                    continue
                item = dict(rec)
                item["cred_id"] = meta.get("cred_id", 0)
                item["account"] = meta.get("label", "")
                items.append(item)
        items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
        return items[:300]

    def logs(self) -> list:
        items: list = []
        for key, engine in self._snapshot_engines():
            meta = self._meta.get(key) or {}
            for rec in engine.logs():
                item = dict(rec)
                item["cred_id"] = meta.get("cred_id", 0)
                item["account"] = meta.get("label", "")
                items.append(item)
        items.sort(key=lambda x: str(x.get("ts") or ""))
        return items[-500:]

    def load_history(self, items: list) -> None:
        engines = self._snapshot_engines()
        if engines:
            engines[0][1].load_history(items)

    # ============================ 配置 ============================
    def snapshot(self) -> dict:
        engines = self._snapshot_engines()
        if engines:
            return engines[0][1].snapshot()
        return dict(DEFAULT_CONFIG.to_dict())

    def update_config(self, partial: Optional[dict]) -> dict:
        patch = dict(partial or {})
        if patch:
            EngineConfig.validate_patch(patch)   # 非法值直接抛 ValueError
        engines = self._snapshot_engines()
        multi = len(engines) > 1
        result = dict(self.snapshot())
        for key, engine in engines:
            sub = patch
            # 多账号时 platform_account_id 是「每个账号自己的默认子账号」，
            # 全局保存不能广播给所有账号，否则会把 A 的子账号塞给 B。
            if multi and key != LEGACY_KEY and "platform_account_id" in patch:
                sub = {k: v for k, v in patch.items() if k != "platform_account_id"}
            if not sub:
                continue
            try:
                result = engine.update_config(sub)
            except ValueError:
                raise
        try:
            persist.save_config(result, self._root)
        except Exception:
            pass
        self._cfg_template = result
        return result

    def update_account_config(self, cred_id: int, partial: dict) -> dict:
        engine = self._engines.get(int(cred_id))
        if engine is None:
            raise KeyError("账号不存在")
        result = engine.update_config(partial)
        persist.save_config(result, self._engine_dir(int(cred_id)))
        return result

    # ============================ token ===========================
    def set_token(self, token: Optional[str]) -> dict:
        """旧接口：无凭据时作用于 legacy；仅有一个账号时作用于该账号。"""
        engines = self._snapshot_engines()
        if LEGACY_KEY in self._engines:
            return self._engines[LEGACY_KEY].set_token(token)
        if len(engines) == 1:
            return engines[0][1].set_token(token)
        raise ValueError("当前有多个登录账号，请在账号卡片里单独设置 Token")

    def set_account_token(self, cred_id: int, token: Optional[str]) -> dict:
        engine = self._engines.get(int(cred_id))
        if engine is None:
            raise KeyError("账号不存在")
        return engine.set_token(token)

    def has_token(self) -> bool:
        for _, engine in self._snapshot_engines():
            if engine.has_token():
                return True
        return False

    def token_status(self) -> dict:
        return self.status().get("token") or {"present": False, "exp": None,
                                              "valid": False}

    # ========================= 启停 / 登录 =========================
    def start(self) -> None:
        """启动全部账号：有可用 token 立即启动，缺 token 的后台取 Token。"""
        engines = self._snapshot_engines()
        if not engines:
            raise ValueError("未配置账号")
        started = 0
        pending: list = []
        for key, engine in engines:
            self._desired_accounts[key] = True
            if engine.token_status().get("valid"):
                try:
                    engine.start()
                    started += 1
                    continue
                except ValueError:
                    pass
            pending.append(key)

        can_login = False
        for key in pending:
            if (self._meta.get(key) or {}).get("cred_id"):
                can_login = True
                threading.Thread(target=self._login_and_start, args=(key,),
                                 daemon=True).start()
        if started == 0 and not can_login:
            raise ValueError("未配置 token，无法启动")
        self._publish({"type": "status", "status": self.status(),
                       "cred_id": 0, "account": ""})

    def start_account(self, cred_id: int) -> dict:
        key = int(cred_id)
        engine = self._engines.get(key)
        if engine is None:
            raise KeyError("账号不存在")
        self._desired_accounts[key] = True
        if engine.token_status().get("valid"):
            try:
                engine.start()
            except ValueError:
                threading.Thread(target=self._login_and_start, args=(key,),
                                 daemon=True).start()
        else:
            threading.Thread(target=self._login_and_start, args=(key,),
                             daemon=True).start()
        return engine.status()

    def stop_account(self, cred_id: int) -> dict:
        key = int(cred_id)
        engine = self._engines.get(key)
        if engine is None:
            raise KeyError("账号不存在")
        self._desired_accounts[key] = False
        engine.stop()
        self._publish({"type": "status", "status": self.status(),
                       "cred_id": 0, "account": ""})
        return engine.status()

    def stop(self) -> None:
        for key in list(self._engines):
            self._desired_accounts[key] = False
        for _, engine in self._snapshot_engines():
            try:
                engine.stop()
            except Exception:
                pass

    def _login_and_start(self, key: int) -> None:
        engine = self._engines.get(key)
        meta = self._meta_of(key)
        cred_id = meta.get("cred_id")
        if engine is None:
            return
        if not self._desired_accounts.get(key):
            return
        if not cred_id:
            engine.log("未配置 token，且该账号没有登录凭据，无法自动登录", "WARN")
            return
        self._last_login[key] = time.monotonic()
        try:
            result = self._managers[key].fetch_one(cred_id)
        except Exception as exc:
            engine.log(f"自动登录异常：{exc}", "ERR")
            return
        if result.get("ok"):
            if not self._desired_accounts.get(key):
                return
            try:
                engine.start()
            except ValueError as exc:
                engine.log(f"登录成功但启动失败：{exc}", "ERR")
            return
        engine.log(f"自动登录失败：{result.get('error') or '未知错误'}", "ERR")

    def _start_renew_loop(self) -> None:
        thread = threading.Thread(target=self._renew_loop, name="hub-renew",
                                  daemon=True)
        self._renew_thread = thread
        thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if any(self._desired_accounts.values()):
                    self._renew_pass()
            except Exception:
                pass
            self._stop.wait(RENEW_INTERVAL)

    def _renew_pass(self) -> None:
        now = time.monotonic()
        for key, engine in self._snapshot_engines():
            if not self._desired_accounts.get(key):
                continue
            st = engine.status()
            tok = st.get("token") or {}
            reason = str(st.get("state_reason") or "")
            token_broken = st.get("state") == "paused" and "token" in reason.lower()
            if tok.get("valid") and not token_broken:
                if not st.get("running") and st.get("state") != "paused":
                    try:
                        engine.start()
                    except ValueError:
                        pass
                continue
            cred_id = (self._meta.get(key) or {}).get("cred_id")
            if not cred_id:
                continue
            if now - self._last_login.get(key, 0.0) < RENEW_COOLDOWN:
                continue
            self._last_login[key] = now
            try:
                result = self._managers[key].fetch_one(cred_id)
            except Exception:
                continue
            if result.get("ok"):
                try:
                    engine.start()
                except ValueError:
                    pass
                engine.log("自动续登成功，已换用新 token 并恢复狙击", "OK")
                self._last_login[key] = 0.0

    # ======================= 登录 / 会话接口 =======================
    def fetch_token(self, cred_id: int) -> dict:
        key = int(cred_id)
        manager = self._managers.get(key)
        if manager is None:
            raise KeyError("账号不存在")
        return manager.fetch_one(key)

    def fetch_any(self) -> dict:
        attempts: list = []
        for key, manager in list(self._managers.items()):
            cred_id = (self._meta.get(key) or {}).get("cred_id")
            if not cred_id:
                continue
            result = manager.fetch_one(cred_id)
            if result.get("ok"):
                return {**result, "attempts": attempts}
            attempts.append({
                "phone": (self._meta.get(key) or {}).get("phone", ""),
                "error": result.get("error") or "登录失败",
            })
        return {"ok": False, "error": "所有账号登录失败", "attempts": attempts}

    def _manager_for(self, cred_id: Optional[int]) -> tuple:
        with self._lock:
            key = int(cred_id) if cred_id else (self._interactive_key
                                                if self._interactive_key is not None
                                                else LEGACY_KEY)
            manager = self._managers.get(key)
            if manager is None:
                snapshot = list(self._managers.items())
                if not snapshot:
                    raise KeyError("账号不存在")
                key, manager = snapshot[0]
            return key, manager

    def interactive_start(self, target_url: str, debug: bool = False,
                          cred_id: Optional[int] = None) -> dict:
        for key, manager in self._snapshot_managers():
            status = manager.status()
            if status.get("active"):
                raise LoginBusy("已有登录会话进行中，请先完成或取消")
        key, manager = self._manager_for(cred_id)
        result = manager.start_interactive(target_url, debug=debug)
        with self._lock:
            self._session_key = key
            self._interactive_key = key
        return result

    def _snapshot_managers(self) -> list:
        with self._lock:
            return list(self._managers.items())

    def interactive_status(self) -> dict:
        with self._lock:
            key = self._session_key
        if key is None:
            with self._lock:
                key = self._interactive_key
        manager = self._managers.get(key) if key is not None else None
        if manager is None:
            return {"active": False, "phase": "idle", "message": "",
                    "captured_phone": "", "started_at": None, "elapsed": 0.0,
                    "session_id": ""}
        return manager.status()

    def interactive_cancel(self) -> dict:
        key = self._session_key
        manager = self._managers.get(key) if key is not None else None
        if manager is None:
            return self.interactive_status()
        return manager.cancel()

    # ============================ 其它 ============================
    def log(self, msg: str, level: str = "INFO") -> None:
        engines = self._snapshot_engines()
        if engines:
            engines[0][1].log(msg, level)
            return
        if self._on_log is not None:
            self._on_log(msg, level)

    def threads_alive(self) -> bool:
        return any(engine.threads_alive() for _, engine in self._snapshot_engines())

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._renew_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=2)
            except Exception:
                pass
        for _, manager in self._snapshot_managers():
            try:
                manager.shutdown()
            except Exception:
                pass
        for _, engine in self._snapshot_engines():
            try:
                engine.stop()
            except Exception:
                pass
