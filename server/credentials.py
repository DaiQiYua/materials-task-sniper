# -*- coding: utf-8 -*-
"""凭据存储（``credentials.json``）。

字段：``{id, name, phone, password_enc, last_ok_at, last_error,
last_platform_error}``（自增 id）。

每个账号的 token **不在** ``credentials.json`` 里，而是单独落在
``tokens.json``（见 ``persist.load_tokens``）：token 与密码同级别敏感，
分文件便于整文件收紧权限，也避免把 JWT 混进凭据列表接口。

密码加密
--------
Windows 上用 **DPAPI**（ctypes 直接调 ``CryptProtectData`` /
``CryptUnprotectData``，crypt32.dll）。作用域选 **CURRENT_USER**（不传
``CRYPTPROTECT_LOCAL_MACHINE`` 标志，原因见下），并带
``CRYPTPROTECT_UI_FORBIDDEN``（服务/无界面进程里弹不出 UI）：

- CURRENT_USER（本实现选用）：密文只有**当前 Windows 用户**能解开。凭据属于
  交互式使用本工具的那个人，语义最贴合。
- LOCAL_MACHINE：同机**任何**用户都能解开，安全性更差，故不选。

代价：换用户 / 以服务身份运行时解密会失败 —— 此时只把错误写进
``last_error``（不崩、不落密码），用户重新录入即可。

非 Windows（或 DPAPI 调用失败）时**降级为明文**存储并打 WARN 日志；口令文件
仍写成 ``plain:v1:<明文>`` 以便读回时区分格式。

``credentials.json`` 里**永远不会出现** ``password`` 明文字段：DPAPI 可用时
只有密文（base64），测试会断言文件文本里 grep 不到密码明文。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import persist

# --------------------------- 手机号 / 脱敏 ---------------------------
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")   # 与前端一致；不符 → 422

_IS_WINDOWS = sys.platform.startswith("win")

ENC_DPAPI = "dpapi:v1:"
ENC_PLAIN = "plain:v1:"

CRYPTPROTECT_UI_FORBIDDEN = 0x1
# 注：故意不加 CRYPTPROTECT_LOCAL_MACHINE（0x4），走「当前用户」作用域。


def normalize_phone(phone: Any) -> str:
    return str(phone or "").strip()


def phone_valid(phone: Any) -> bool:
    return bool(PHONE_RE.match(normalize_phone(phone)))


def mask_phone(phone: Any) -> str:
    """138****5678：中间四位打码（展示/日志用）。"""
    p = normalize_phone(phone)
    if len(p) == 11 and PHONE_RE.match(p):
        return f"{p[:3]}****{p[7:]}"
    if len(p) > 7:
        return f"{p[:3]}****{p[-4:]}"
    return "***" if p else ""


# ------------------------------- DPAPI ------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_DPAPI_LOCK = threading.Lock()
_DPAPI: dict = {}


def _dpapi() -> dict:
    """装配 crypt32/kernel32 函数指针（进程内只做一次）。"""
    if _DPAPI:
        return _DPAPI
    with _DPAPI_LOCK:
        if _DPAPI:
            return _DPAPI
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_ptr = ctypes.POINTER(_DataBlob)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            func = getattr(crypt32, name)
            func.argtypes = [blob_ptr, ctypes.c_wchar_p, blob_ptr,
                             ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_uint32, blob_ptr]
            func.restype = ctypes.c_int
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        _DPAPI.update({
            "protect": crypt32.CryptProtectData,
            "unprotect": crypt32.CryptUnprotectData,
            "free": local_free,
        })
    return _DPAPI


def _dpapi_call(func, data: bytes, flags: int) -> bytes:
    """调用一次 DPAPI；失败抛 OSError。"""
    buffers = []
    for arg in (data,):
        buffers.append(ctypes.create_string_buffer(arg, len(arg) + 1))
    blob_in = _DataBlob(len(data), ctypes.cast(buffers[0], ctypes.POINTER(ctypes.c_char)))
    entropy_buf = ctypes.create_string_buffer(b"materials-task-sniper", 22)
    blob_entropy = _DataBlob(21, ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = func(ctypes.byref(blob_in), None, ctypes.byref(blob_entropy),
              None, None, flags, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI 调用失败（WinError {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        try:
            _dpapi()["free"](ctypes.cast(blob_out.pbData, ctypes.c_void_p))
        except Exception:
            pass


def dpapi_protect(plain: str) -> str:
    """加密 → ``dpapi:v1:<base64>``；DPAPI 不可用/失败 → 明文降级。"""
    raw = plain.encode("utf-8")
    if _IS_WINDOWS:
        try:
            blob = _dpapi_call(_dpapi()["protect"], raw, CRYPTPROTECT_UI_FORBIDDEN)
            return ENC_DPAPI + base64.b64encode(blob).decode("ascii")
        except Exception:
            pass   # 降级明文（调用方负责 WARN）
    return ENC_PLAIN + plain


def dpapi_unprotect(stored: str) -> str:
    """解密 ``password_enc``；格式不认识按明文处理（向后兼容）。"""
    stored = stored if isinstance(stored, str) else str(stored or "")
    if stored.startswith(ENC_DPAPI):
        blob = base64.b64decode(stored[len(ENC_DPAPI):].encode("ascii"))
        raw = _dpapi_call(_dpapi()["unprotect"], blob, CRYPTPROTECT_UI_FORBIDDEN)
        return raw.decode("utf-8")
    if stored.startswith(ENC_PLAIN):
        return stored[len(ENC_PLAIN):]
    return stored


def verify_dpapi() -> dict:
    """启动探测：真跑一次加密→解密往返，结果给 UI 展示加密状态。

    返回 ``{"available":bool,"mode":"dpapi"|"plain","platform":...,"scope":...,
    "detail":str}``。
    """
    info = {
        "available": False,
        "mode": "plain",
        "platform": sys.platform,
        "scope": "current_user" if _IS_WINDOWS else "none",
        "detail": "",
    }
    if not _IS_WINDOWS:
        info["detail"] = "非 Windows 平台：DPAPI 不可用，密码降级为明文存储（不安全）"
        return info
    probe = "dpapi-self-check-9f3c1a"
    try:
        blob = _dpapi_call(_dpapi()["protect"], probe.encode("utf-8"),
                           CRYPTPROTECT_UI_FORBIDDEN)
        back = _dpapi_call(_dpapi()["unprotect"], blob, CRYPTPROTECT_UI_FORBIDDEN)
        if back.decode("utf-8") != probe:
            raise OSError("往返校验不一致")
    except Exception as exc:
        info["detail"] = f"DPAPI 自检失败（{exc}）：密码降级为明文存储（不安全）"
        return info
    info.update({"available": True, "mode": "dpapi",
                 "detail": "Windows DPAPI（当前用户作用域）加密"})
    return info


# --------------------------- 凭据存储 -------------------------------
class CredentialStore:
    """``credentials.json`` 的增删列查（线程安全，原子写）。

    ``on_log(msg, level)`` 可选：由 app 注入 ``engine.log``，让 WARN 也进
    SSE/sniper.log。**绝不**把密码或密码密文塞进日志。
    """

    def __init__(self, directory: Optional[Path | str] = None,
                 on_log: Optional[Callable[..., None]] = None) -> None:
        self._dir = Path(directory) if directory is not None else None
        self._lock = threading.RLock()
        self.on_log = on_log
        self._warned_plain = False
        # ---- 内存快照缓存 ----
        # 为什么需要：引擎每轮轮询 / 每次领取都会调 get_token() 与 list_all()，
        # 每次都 json.loads 读盘；_limits_loop 每 0.5s 又遍历一遍，读盘被放大到
        # 每秒数十次。命中缓存时直接返回**副本**（避免调用方改到缓存对象），
        # 2s 的 TTL 足以吃掉这些重复读，又不会让外部改文件后「太久看不到」。
        self._items_cache: Optional[dict] = None
        self._items_at = 0.0
        self._tokens_cache: Optional[dict] = None
        self._tokens_at = 0.0

    #: 缓存有效期（秒）；任何写路径都会立即失效对应缓存
    _CACHE_TTL = 2.0

    # ---- 日志 ----
    def _log(self, msg: str, level: str = "WARN") -> None:
        if self.on_log is not None:
            try:
                self.on_log(msg, level)
                return
            except Exception:
                pass
        lvl = logging.WARNING if level in ("WARN", "ERR") else logging.INFO
        logging.getLogger("snipy.credentials").log(lvl, msg)

    # ---- 读写 ----
    def _invalidate_items(self) -> None:
        """失效凭据列表缓存（任何写 ``credentials.json`` 之后必须调）。"""
        self._items_cache = None
        self._items_at = 0.0

    def _invalidate_tokens(self) -> None:
        """失效 token 缓存（任何写 ``tokens.json`` 之后必须调）。"""
        self._tokens_cache = None
        self._tokens_at = 0.0

    def _load(self) -> dict:
        now = time.monotonic()
        cached = self._items_cache
        if cached is not None and (now - self._items_at) <= self._CACHE_TTL:
            # 缓存还新鲜，但仍要探一次文件是否存在：文件被外部**整体删除**
            # （例如「一键清空」删掉 credentials.json）时必须立刻看到空表，
            # 不能被缓存挡住；纯粹的内容变化则容忍 TTL 内的滞后。
            present = True
            if cached["items"]:
                try:
                    present = persist.credentials_path(self._dir).exists()
                except Exception:
                    present = True
            if present:
                # 返回深一层的副本，调用方随便改都不会污染缓存
                return {"version": cached["version"], "next_id": cached["next_id"],
                        "items": [dict(i) for i in cached["items"]]}
        obj = persist.load_credentials(self._dir)
        items: list = []
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            items = [i for i in obj["items"]
                     if isinstance(i, dict) and isinstance(i.get("id"), int)]
        next_id = obj.get("next_id") if isinstance(obj, dict) else None
        max_id = max([int(i["id"]) for i in items] or [0])
        if not isinstance(next_id, int) or isinstance(next_id, bool) or next_id <= max_id:
            next_id = max_id + 1
        self._items_cache = {"version": 1, "next_id": next_id,
                             "items": [dict(i) for i in items]}
        self._items_at = now
        return {"version": 1, "next_id": next_id,
                "items": [dict(i) for i in items]}

    def _save(self, data: dict) -> None:
        persist.save_credentials(data, self._dir)
        self._invalidate_items()

    def _encrypt(self, password: str) -> str:
        stored = dpapi_protect(password)
        if stored.startswith(ENC_PLAIN) and not self._warned_plain:
            self._warned_plain = True
            self._log("DPAPI 不可用：凭据密码降级为明文存储（不安全，请限制数据目录权限）",
                      "WARN")
        return stored

    # ---- 展示形态（脱敏）----
    @staticmethod
    def public(item: dict, token_info: Optional[dict] = None) -> dict:
        """对外展示形态：**永不**含 ``password_enc`` / token 本体。

        ``token_info`` 为 ``get_token_info()`` 的结果（可选），只用来派生
        ``has_token`` / ``token_exp`` / ``adopted`` 三个布尔/时间戳字段。
        """
        info = token_info if isinstance(token_info, dict) else {}
        exp = info.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            exp = None
        return {
            "id": item.get("id"),
            "name": item.get("name") or "",
            "phone": mask_phone(item.get("phone")),
            "phone_full": normalize_phone(item.get("phone")),
            "has_password": bool(item.get("password_enc")),
            "last_ok_at": item.get("last_ok_at"),
            "last_error": item.get("last_error") or "",
            "has_token": bool(info.get("token")),
            "token_exp": int(exp) if exp is not None else None,
            "last_platform_error": item.get("last_platform_error") or "",
            "adopted": bool(info.get("adopted")),
        }

    # ---- 查询 ----
    def list_public(self) -> list[dict]:
        with self._lock:
            items = self._load()["items"]
            tokens = self._load_tokens()
        return [self.public(i, tokens.get(int(i["id"]))) for i in items]

    def list_all(self) -> list[dict]:
        """含 ``password_enc`` 的原始记录（仅内部使用，勿直接回给前端）。"""
        with self._lock:
            # 逐条复制：调用方若改动返回的条目，绝不能改到缓存里的对象
            return list(dict(i) for i in self._load()["items"])

    def get(self, cred_id: Any) -> Optional[dict]:
        try:
            cid = int(cred_id)
        except Exception:
            return None
        with self._lock:
            for item in self._load()["items"]:
                if item.get("id") == cid:
                    return dict(item)
        return None

    def password_for(self, cred_id: Any) -> Optional[str]:
        item = self.get(cred_id)
        if not item:
            return None
        stored = item.get("password_enc") or ""
        if not stored:
            return None
        return dpapi_unprotect(stored)

    def count(self) -> int:
        with self._lock:
            return len(self._load()["items"])

    # ---- 每个账号的 token（tokens.json，**不**写 token.json）----
    def _load_tokens(self) -> dict:
        now = time.monotonic()
        cached = self._tokens_cache
        if cached is not None and (now - self._tokens_at) <= self._CACHE_TTL:
            # 同 _load：tokens.json 被整体删除（「一键清空」）后必须立刻返回空，
            # 否则引擎还会拿缓存里的旧 token 去抢单。
            present = True
            if cached:
                try:
                    present = persist.tokens_path(self._dir).exists()
                except Exception:
                    present = True
            if present:
                # 每条也复制：调用方（set_token/clear_token）会就地改动返回的 dict
                return {cid: dict(info) for cid, info in cached.items()}
        try:
            items = persist.load_tokens(self._dir)
        except Exception:
            items = {}
        self._tokens_cache = {cid: dict(info) for cid, info in items.items()}
        self._tokens_at = now
        return {cid: dict(info) for cid, info in items.items()}

    def _save_tokens(self, items: dict) -> None:
        persist.save_tokens(items, self._dir)
        self._invalidate_tokens()

    def get_token(self, cred_id: Any) -> Optional[str]:
        """该账号自己的 token（无 → ``None``）。"""
        info = self.get_token_info(cred_id)
        return info.get("token") if info else None

    def get_token_info(self, cred_id: Any) -> Optional[dict]:
        """``{"token":.., "exp":.., "saved_at":.., "adopted": bool?}`` 或 None。"""
        try:
            cid = int(cred_id)
        except Exception:
            return None
        with self._lock:                 # 缓存读写统一在同一把锁内
            info = self._load_tokens().get(cid)
        return dict(info) if isinstance(info, dict) else None

    def set_token(self, cred_id: Any, token: Any, exp: Optional[int] = None,
                  adopted: bool = False) -> dict:
        """把 token 写进 ``tokens.json``（**绝不**改 ``token.json``）。

        ``adopted=True`` 表示这条 token 是由旧全局 Token 推断迁移来的，
        便于 UI/排查区分（幂等迁移不需要重跑）。
        """
        try:
            cid = int(cred_id)
        except Exception:
            raise ValueError("凭据 id 非法")
        text = token if isinstance(token, str) else str(token or "")
        text = text.strip()
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        if not text:
            raise ValueError("token 不能为空")
        if exp is not None:
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                exp = None
            else:
                exp = int(exp)
        with self._lock:
            items = self._load_tokens()
            entry = {
                "token": text,
                "exp": exp,
                "saved_at": time.time(),
            }
            if adopted:
                # 只有「启动时的旧 Token 推断迁移」才置位；显式重新取到的
                # token 会把它清掉（不再算推断）。
                entry["adopted"] = True
            items[cid] = entry
            self._save_tokens(items)
        return dict(entry)

    def clear_token(self, cred_id: Any) -> bool:
        """删掉该账号的 token（连带清平台缓存）；返回是否真的删了。"""
        try:
            cid = int(cred_id)
        except Exception:
            return False
        removed = False
        with self._lock:
            items = self._load_tokens()
            if cid in items:
                items.pop(cid, None)
                self._save_tokens(items)
                removed = True
            removed = self._drop_platform_cache(cid) or removed
        return removed

    def _drop_platform_cache(self, cred_id: int) -> bool:
        """清 ``platform_accounts.json`` 里该 cred 的缓存（调用方持锁）。"""
        try:
            cache = persist.load_platform_cache(self._dir)
        except Exception:
            return False
        if cred_id not in cache:
            return False
        cache.pop(cred_id, None)
        try:
            persist.save_platform_cache(cache, self._dir)
        except Exception:
            return False
        return True

    # ---- 增删 ----
    def add(self, name: Any, phone: Any, password: str) -> dict:
        phone = normalize_phone(phone)
        if not phone_valid(phone):
            raise ValueError("手机号格式不正确（应为 1[3-9] 开头的 11 位数字）")
        if not isinstance(password, str) or not password:
            raise ValueError("密码不能为空")
        encrypted = self._encrypt(password)
        with self._lock:
            data = self._load()
            item = {
                "id": data["next_id"],
                "name": str(name or "").strip()[:64],
                "phone": phone,
                "password_enc": encrypted,
                "last_ok_at": None,
                "last_error": "",
                "last_platform_error": "",
            }
            data["next_id"] = item["id"] + 1
            data["items"].append(item)
            self._save(data)
        return self.public(item)

    def delete(self, cred_id: Any) -> bool:
        """删除凭据，并**级联清理**该账号的 token 与平台子账号缓存。"""
        try:
            cid = int(cred_id)
        except Exception:
            return False
        with self._lock:
            data = self._load()
            kept = [i for i in data["items"] if i.get("id") != cid]
            if len(kept) == len(data["items"]):
                return False
            data["items"] = kept
            self._save(data)
            # 级联：tokens.json 条目 + platform_accounts.json 该 cred 缓存
            tokens = self._load_tokens()
            if cid in tokens:
                tokens.pop(cid, None)
                try:
                    self._save_tokens(tokens)
                except Exception:
                    pass
            self._drop_platform_cache(cid)
        return True

    # ---- 结果回写 ----
    def mark_ok(self, cred_id: Any, ts: Optional[float] = None) -> None:
        self._update(cred_id, {"last_ok_at": float(ts if ts is not None else time.time()),
                               "last_error": ""})

    def mark_error(self, cred_id: Any, message: str) -> None:
        self._update(cred_id, {"last_error": str(message or "")[:400]})

    def mark_platform_error(self, cred_id: Any, message: str) -> None:
        """平台子账号拉取失败（不影响登录结果）；成功时传空串清空。"""
        self._update(cred_id, {"last_platform_error": str(message or "")[:400]})

    def clear_platform_error(self, cred_id: Any) -> None:
        self.mark_platform_error(cred_id, "")

    def _update(self, cred_id: Any, patch: dict) -> None:
        try:
            cid = int(cred_id)
        except Exception:
            return
        with self._lock:
            data = self._load()
            changed = False
            for item in data["items"]:
                if item.get("id") == cid:
                    item.update(patch)
                    changed = True
                    break
            if changed:
                self._save(data)
