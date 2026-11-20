# -*- coding: utf-8 -*-
"""数据目录与持久化辅助。

数据目录由环境变量 ``MATERIALS_DATA_DIR`` 覆盖，默认=项目根目录。
目录内的文件：
- ``token.json``   : {"token": "eyJ..."}（**永不**出现在日志/事件里）
- ``tokens.json``  : 每个登录账号各自的 token（按 cred_id 归档，**永不**进日志）
- ``platform_accounts.json``: 每个登录账号名下的平台子账号缓存
- ``cooldowns.json``: 请求冷却 / 任务冷却 / 待提交（与任务列表解耦）
- ``config.json``  : Config 全量快照
- ``history.jsonl``: 每次领取尝试一行 JSON（超过 5MB 轮转成 ``history.jsonl.1``，只留 1 份）
- ``credentials.json``: 登录凭据（密码为 DPAPI 密文；非 Windows 降级明文）
- ``sniper.log``   : RotatingFileHandler 纯文本日志
"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOKEN_FILE = "token.json"
TOKENS_FILE = "tokens.json"
PLATFORM_CACHE_FILE = "platform_accounts.json"
COOLDOWN_FILE = "cooldowns.json"
CONFIG_FILE = "config.json"
HISTORY_FILE = "history.jsonl"
CREDENTIALS_FILE = "credentials.json"
LOG_FILE = "sniper.log"

#: ``history.jsonl`` 轮转阈值（写前检查，超过就把当前文件改名成 ``.1``）
HISTORY_MAX_BYTES = 5 * 1024 * 1024
#: 历史备份文件名（最多保留 1 份；清空数据时也在白名单里）
HISTORY_BACKUP_FILE = HISTORY_FILE + ".1"

_LOG = logging.getLogger("snipy.persist")

_LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


def data_dir() -> Path:
    """当前数据目录（env MATERIALS_DATA_DIR，默认项目根）。"""
    raw = os.environ.get("MATERIALS_DATA_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return PROJECT_ROOT


def ensure_dir(directory: Optional[Path | str] = None) -> Path:
    d = Path(directory) if directory is not None else data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def token_path(directory: Optional[Path | str] = None) -> Path:
    return Path(directory) / TOKEN_FILE if directory is not None else data_dir() / TOKEN_FILE


def config_path(directory: Optional[Path | str] = None) -> Path:
    return Path(directory) / CONFIG_FILE if directory is not None else data_dir() / CONFIG_FILE


def history_path(directory: Optional[Path | str] = None) -> Path:
    return Path(directory) / HISTORY_FILE if directory is not None else data_dir() / HISTORY_FILE


def log_path(directory: Optional[Path | str] = None) -> Path:
    return Path(directory) / LOG_FILE if directory is not None else data_dir() / LOG_FILE


def credentials_path(directory: Optional[Path | str] = None) -> Path:
    return (Path(directory) / CREDENTIALS_FILE if directory is not None
            else data_dir() / CREDENTIALS_FILE)


def tokens_path(directory: Optional[Path | str] = None) -> Path:
    return (Path(directory) / TOKENS_FILE if directory is not None
            else data_dir() / TOKENS_FILE)


def platform_cache_path(directory: Optional[Path | str] = None) -> Path:
    return (Path(directory) / PLATFORM_CACHE_FILE if directory is not None
            else data_dir() / PLATFORM_CACHE_FILE)


def cooldown_path(directory: Optional[Path | str] = None) -> Path:
    return (Path(directory) / COOLDOWN_FILE if directory is not None
            else data_dir() / COOLDOWN_FILE)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:  # token 文件尽量收紧权限（Windows 上可能无效，忽略）
        os.chmod(path, 0o600)
    except Exception:
        pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ------------------------------- token -------------------------------
def load_token(directory: Optional[Path | str] = None) -> Optional[str]:
    obj = _read_json(token_path(directory))
    token = None
    if isinstance(obj, dict):
        token = obj.get("token")
    elif isinstance(obj, str):
        token = obj
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def save_token(token: str, directory: Optional[Path | str] = None) -> None:
    _atomic_write(token_path(directory), json.dumps({"token": token}))


# --------------------------- 每个账号的 token -------------------------
#: 文件结构：``{"version":1,"items":{"<cred_id>":{"token":..,"exp":..,
#: "saved_at":..}}}``。``load_tokens()`` 返回 **int 键** 的 dict（写回时统一
#: 转成字符串键），读取失败一律返回 ``{}``。
def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def load_tokens(directory: Optional[Path | str] = None) -> dict:
    """读 ``tokens.json``；缺失/损坏 → ``{}``（记 WARN，不抛）。"""
    path = tokens_path(directory)
    if not path.exists():
        return {}
    obj = _read_json(path)
    if not isinstance(obj, dict):
        _LOG.warning("tokens.json 结构异常（损坏或非对象），按空处理：%s", path)
        return {}
    raw_items = obj.get("items")
    if not isinstance(raw_items, dict):
        return {}
    out: dict = {}
    for key, value in raw_items.items():
        cid = _as_int(key)
        if cid is None or not isinstance(value, dict):
            continue
        token = value.get("token")
        if not isinstance(token, str) or not token.strip():
            continue
        exp = value.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            exp = None
        else:
            exp = int(exp)
        saved_at = value.get("saved_at")
        if isinstance(saved_at, bool) or not isinstance(saved_at, (int, float)):
            saved_at = None
        else:
            saved_at = float(saved_at)
        entry = {"token": token.strip(), "exp": exp, "saved_at": saved_at}
        if value.get("adopted"):
            entry["adopted"] = True
        out[cid] = entry
    return out


def save_tokens(items: dict, directory: Optional[Path | str] = None) -> None:
    """原子写 ``tokens.json``；键统一转字符串（JSON 对象键）。"""
    normalized: dict = {}
    for key, value in (items or {}).items():
        cid = _as_int(key)
        if cid is None or not isinstance(value, dict):
            continue
        token = value.get("token")
        if not isinstance(token, str) or not token.strip():
            continue
        exp = value.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            exp = None
        else:
            exp = int(exp)
        saved_at = value.get("saved_at")
        if isinstance(saved_at, bool) or not isinstance(saved_at, (int, float)):
            saved_at = time.time()
        else:
            saved_at = float(saved_at)
        entry = {"token": token.strip(), "exp": exp, "saved_at": saved_at}
        if value.get("adopted"):
            entry["adopted"] = True
        normalized[str(cid)] = entry
    _atomic_write(tokens_path(directory),
                  json.dumps({"version": 1, "items": normalized}, ensure_ascii=False))


# ----------------------- 平台子账号缓存（按账号） ----------------------
#: 文件结构：``{"version":1,"items":{"<cred_id>":{"fetched_at":..,
#: "fallback":bool,"items":[..]}}}``；``load_platform_cache()`` 返回 int 键 dict。
def load_platform_cache(directory: Optional[Path | str] = None) -> dict:
    """读 ``platform_accounts.json``；缺失/损坏 → ``{}``。"""
    path = platform_cache_path(directory)
    if not path.exists():
        return {}
    obj = _read_json(path)
    if not isinstance(obj, dict):
        _LOG.warning("platform_accounts.json 结构异常（损坏或非对象），按空处理：%s", path)
        return {}
    raw_items = obj.get("items")
    if not isinstance(raw_items, dict):
        return {}
    out: dict = {}
    for key, value in raw_items.items():
        cid = _as_int(key)
        if cid is None or not isinstance(value, dict):
            continue
        items = value.get("items")
        if not isinstance(items, list):
            items = []
        fetched_at = value.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            fetched_at = None
        else:
            fetched_at = float(fetched_at)
        out[cid] = {
            "fetched_at": fetched_at,
            "fallback": bool(value.get("fallback")),
            "items": [i for i in items if isinstance(i, dict)],
        }
    return out


def save_platform_cache(items: dict, directory: Optional[Path | str] = None) -> None:
    normalized: dict = {}
    for key, value in (items or {}).items():
        cid = _as_int(key)
        if cid is None or not isinstance(value, dict):
            continue
        fetched_at = value.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            fetched_at = time.time()
        else:
            fetched_at = float(fetched_at)
        normalized[str(cid)] = {
            "fetched_at": fetched_at,
            "fallback": bool(value.get("fallback")),
            "items": [i for i in (value.get("items") or []) if isinstance(i, dict)],
        }
    _atomic_write(platform_cache_path(directory),
                  json.dumps({"version": 1, "items": normalized}, ensure_ascii=False))


# --------------------------- 冷却 / 待提交 ---------------------------
#: 文件结构：``{"version":1,
#:   "request": {"<cred_id>": {...}},
#:   "tasks": {"<aid>:<task_id>": {...}},
#:   "pending": {"<aid>:<task_id>": {...}}}``。
#: 与任务列表解耦：没任务下发时仍能恢复状态。
def load_cooldowns(directory: Optional[Path | str] = None) -> dict:
    """读 ``cooldowns.json``；缺失/损坏 → 空结构。"""
    empty = {"request": {}, "tasks": {}, "pending": {}}
    path = cooldown_path(directory)
    if not path.exists():
        return empty
    obj = _read_json(path)
    if not isinstance(obj, dict):
        _LOG.warning("cooldowns.json 结构异常（损坏或非对象），按空处理：%s", path)
        return empty
    out = {"request": {}, "tasks": {}, "pending": {}}
    for section in ("request", "tasks", "pending"):
        raw = obj.get(section)
        if isinstance(raw, dict):
            out[section] = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    return out


def save_cooldowns(data: dict, directory: Optional[Path | str] = None) -> None:
    """原子写 ``cooldowns.json``（键统一字符串）。"""
    normalized = {"version": 1, "request": {}, "tasks": {}, "pending": {}}
    for section in ("request", "tasks", "pending"):
        raw = (data or {}).get(section)
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            normalized[section][str(key)] = dict(value)
    _atomic_write(cooldown_path(directory),
                  json.dumps(normalized, ensure_ascii=False, indent=2))


# ------------------------------- config ------------------------------
def load_config(directory: Optional[Path | str] = None) -> Optional[dict]:
    obj = _read_json(config_path(directory))
    return obj if isinstance(obj, dict) else None


def save_config(cfg: dict, directory: Optional[Path | str] = None) -> None:
    _atomic_write(config_path(directory), json.dumps(cfg, ensure_ascii=False, indent=2))


# ---------------------------- credentials ----------------------------
def load_credentials(directory: Optional[Path | str] = None) -> Optional[dict]:
    """原始结构：{"version":1,"next_id":N,"items":[{...password_enc...}]}。

    注意：里面的 ``password_enc`` 可能是 DPAPI 密文，也可能（非 Windows 降级）
    是 ``plain:v1:<明文>`` —— 该文件**永不**进日志、**永不**直接回前端。
    """
    obj = _read_json(credentials_path(directory))
    return obj if isinstance(obj, dict) else None


def save_credentials(data: dict, directory: Optional[Path | str] = None) -> None:
    _atomic_write(credentials_path(directory),
                  json.dumps(data, ensure_ascii=False, indent=2))


# ------------------------------ history ------------------------------
def append_history(record: dict, directory: Optional[Path | str] = None) -> None:
    p = history_path(directory)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 轮转：超过阈值就把 history.jsonl 改名成 history.jsonl.1（只留 1 份）。
        # 为什么不用 RotatingFileHandler：这里每来一次领取尝试追加一行，用 logging
        # 会带入格式化/handler 生命周期噪音；st_size + rename 最简单。任何异常都
        # 吞掉 —— 领取线程绝不能因为历史落盘失败而中断。
        try:
            if p.stat().st_size >= HISTORY_MAX_BYTES:
                backup = p.with_name(HISTORY_BACKUP_FILE)
                if backup.exists():
                    backup.unlink()
                os.replace(p, backup)
        except Exception:
            pass
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 落盘失败不能影响领取线程


def load_history(directory: Optional[Path | str] = None, limit: int = 200) -> list[dict]:
    p = history_path(directory)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    items: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


# -------------------------------- log --------------------------------
_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(directory: Optional[Path | str] = None) -> Optional[logging.Logger]:
    """返回写 ``sniper.log`` 的 logger（同一路径只装配一次）。"""
    path = log_path(directory)
    key = str(path)
    if key in _LOGGERS:
        return _LOGGERS[key]
    logger = logging.getLogger(f"snipy.web.{len(_LOGGERS)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(path), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8", errors="replace"
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE))
        logger.addHandler(handler)
    except Exception:
        _LOGGERS[key] = logger
        return None
    _LOGGERS[key] = logger
    return logger


# -------------------------- 一键清空（恢复出厂） --------------------------
#: 清空时允许删除的**全部**文件名（白名单，绝不 rmtree）。
_CLEAR_FILES: tuple[str, ...] = (
    TOKEN_FILE, TOKENS_FILE, PLATFORM_CACHE_FILE, COOLDOWN_FILE, CONFIG_FILE,
    HISTORY_FILE, CREDENTIALS_FILE, LOG_FILE,
)
#: 日志轮转备份（RotatingFileHandler(backupCount=3) 产出）。
_LOG_BACKUPS: tuple[str, ...] = tuple(f"{LOG_FILE}.{i}" for i in (1, 2, 3))


def _handler_file(handler: Any) -> str:
    """handler 指向的绝对路径（取不到时返回空串）。"""
    try:
        name = getattr(handler, "baseFilename", None)
    except Exception:
        return ""
    return os.path.abspath(str(name)) if name else ""


def close_log_handlers(directory: Optional[Path | str] = None) -> int:
    """关闭并移除 ``sniper.log`` 上的全部 handler，返回关闭的个数。

    Windows 下 handler 不 close 就删文件会抛 ``PermissionError``，所以清空前必须
    先走这里。所有异常一律吞掉：此刻日志文件即将被删除，不能往里写任何东西。
    """
    target_path = os.path.abspath(str(log_path(directory)))
    closed = 0

    try:
        cached = _LOGGERS.pop(str(log_path(directory)), None)
    except Exception:
        cached = None

    loggers: list = []
    if cached is not None:
        loggers.append(cached)
    # 兜底：其它 logger 上挂着的、指向同一文件的 handler（例如手工加的）
    try:
        for obj in list(logging.Logger.manager.loggerDict.values()):
            if not isinstance(obj, logging.Logger) or obj is cached:
                continue
            for handler in list(getattr(obj, "handlers", ()) or ()):
                if _handler_file(handler) == target_path:
                    loggers.append(obj)
                    break
    except Exception:
        pass

    for logger in loggers:
        try:
            handlers = list(logger.handlers)
        except Exception:
            handlers = []
        for handler in handlers:
            # 只动指向该日志文件的 handler；同一个 logger 上别的 handler 不碰
            if logger is not cached and _handler_file(handler) != target_path:
                continue
            try:
                handler.close()
                closed += 1
            except Exception:
                pass
            try:
                logger.removeHandler(handler)
            except Exception:
                pass
        if logger is cached:
            # 顺带摘掉 logging 模块里的同名缓存项：避免之后按同一个名字重建时
            # 拿回这个刚被摘空 handler 的旧 logger 对象。
            try:
                name = getattr(logger, "name", "")
                if name and logging.Logger.manager.loggerDict.get(name) is logger:
                    logging.Logger.manager.loggerDict.pop(name, None)
            except Exception:
                pass
    return closed


def clear_all(directory: Optional[Path | str] = None) -> dict:
    """删除全部数据文件（彻底恢复出厂），返回删除统计。

    返回：``{"credentials": int, "tokens": int, "platform_cache": int,
    "history": int, "logs": int, "config_reset": True,
    "files": [已删除的文件名...], "failed": [删除失败的文件名...]}``。

    删除范围**仅限** 8 个数据文件 + 各自的 ``.tmp`` 残留 + 日志轮转备份 +
历史轮转备份（``history.jsonl.1``）；
    绝不递归删除目录，绝不碰 ``.har`` / ``web/`` / ``server/`` / ``tests/`` /
    ``__pycache__`` / ``.git`` 等任何其它文件。单个文件删除失败（如被占用）
    记进 ``failed`` 后继续，不抛异常。
    """
    d = Path(directory) if directory is not None else data_dir()

    # ---- 1) 先统计（删掉之后就数不到了）----
    credentials_count = 0
    try:
        obj = load_credentials(d)
        items = obj.get("items") if isinstance(obj, dict) else None
        if isinstance(items, list):
            credentials_count = len(items)
    except Exception:
        pass

    tokens_count = 0
    try:
        tokens_count = len(load_tokens(d))
    except Exception:
        pass

    platform_cache_count = 0
    try:
        platform_cache_count = len(load_platform_cache(d))
    except Exception:
        pass

    history_count = 0
    try:
        lines = history_path(d).read_text(encoding="utf-8", errors="replace").splitlines()
        history_count = len([line for line in lines if line.strip()])
    except Exception:
        pass

    # ---- 2) 先关日志 handler（Windows 下不关就删会 PermissionError）----
    close_log_handlers(d)

    # ---- 3) 按白名单逐个删除 ----
    names: list = []
    for name in _CLEAR_FILES:
        names.append(name)
        names.append(name + ".tmp")
    names.extend(_LOG_BACKUPS)
    # 历史轮转备份（append_history 产出的 history.jsonl.1）也是数据文件，
    # 单独补进名单：仍然「只删白名单文件、不递归」。
    names.append(HISTORY_BACKUP_FILE)

    files: list = []
    failed: list = []
    deleted_logs = 0
    for name in names:
        target = d / name
        try:
            existed = target.exists()
        except Exception:
            existed = False
        try:
            target.unlink(missing_ok=True)
        except Exception:
            failed.append(name)
            continue
        if existed:
            files.append(name)
            if name in _LOG_BACKUPS or name == LOG_FILE:
                deleted_logs += 1

    return {
        "credentials": credentials_count,
        "tokens": tokens_count,
        "platform_cache": platform_cache_count,
        "history": history_count,
        "logs": deleted_logs,
        "config_reset": True,
        "files": files,
        "failed": failed,
    }
