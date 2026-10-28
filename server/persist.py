# -*- coding: utf-8 -*-
"""数据目录与持久化辅助。

数据目录由环境变量 ``MATERIALS_DATA_DIR`` 覆盖，默认=项目根目录。
目录内的文件：
- ``token.json``   : {"token": "eyJ..."}（**永不**出现在日志/事件里）
- ``config.json``  : Config 全量快照
- ``history.jsonl``: 每次领取尝试一行 JSON
- ``credentials.json``: 登录凭据（密码为 DPAPI 密文；非 Windows 降级明文）
- ``sniper.log``   : RotatingFileHandler 纯文本日志
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOKEN_FILE = "token.json"
CONFIG_FILE = "config.json"
HISTORY_FILE = "history.jsonl"
CREDENTIALS_FILE = "credentials.json"
LOG_FILE = "sniper.log"

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
