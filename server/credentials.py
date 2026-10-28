# -*- coding: utf-8 -*-
"""凭据存储（``credentials.json``）。

字段：``{id, name, phone, password_enc, last_ok_at, last_error}``（自增 id）。

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
    def _load(self) -> dict:
        obj = persist.load_credentials(self._dir)
        items: list = []
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            items = [i for i in obj["items"]
                     if isinstance(i, dict) and isinstance(i.get("id"), int)]
        next_id = obj.get("next_id") if isinstance(obj, dict) else None
        max_id = max([int(i["id"]) for i in items] or [0])
        if not isinstance(next_id, int) or isinstance(next_id, bool) or next_id <= max_id:
            next_id = max_id + 1
        return {"version": 1, "next_id": next_id, "items": items}

    def _save(self, data: dict) -> None:
        persist.save_credentials(data, self._dir)

    def _encrypt(self, password: str) -> str:
        stored = dpapi_protect(password)
        if stored.startswith(ENC_PLAIN) and not self._warned_plain:
            self._warned_plain = True
            self._log("DPAPI 不可用：凭据密码降级为明文存储（不安全，请限制数据目录权限）",
                      "WARN")
        return stored

    # ---- 展示形态（脱敏）----
    @staticmethod
    def public(item: dict) -> dict:
        return {
            "id": item.get("id"),
            "name": item.get("name") or "",
            "phone": mask_phone(item.get("phone")),
            "has_password": bool(item.get("password_enc")),
            "last_ok_at": item.get("last_ok_at"),
            "last_error": item.get("last_error") or "",
        }

    # ---- 查询 ----
    def list_public(self) -> list[dict]:
        with self._lock:
            items = self._load()["items"]
        return [self.public(i) for i in items]

    def list_all(self) -> list[dict]:
        """含 ``password_enc`` 的原始记录（仅内部使用，勿直接回给前端）。"""
        with self._lock:
            return list(self._load()["items"])

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
            }
            data["next_id"] = item["id"] + 1
            data["items"].append(item)
            self._save(data)
        return self.public(item)

    def delete(self, cred_id: Any) -> bool:
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
        return True

    # ---- 结果回写 ----
    def mark_ok(self, cred_id: Any, ts: Optional[float] = None) -> None:
        self._update(cred_id, {"last_ok_at": float(ts if ts is not None else time.time()),
                               "last_error": ""})

    def mark_error(self, cred_id: Any, message: str) -> None:
        self._update(cred_id, {"last_error": str(message or "")[:400]})

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
