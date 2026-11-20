# -*- coding: utf-8 -*-
"""启动入口：python run.py [--reload] [--host H] [--port P] [--log-level L]

等价于用 uvicorn 跑 ``server.app:app``，并提供开发态热重载（``--reload``）。
依赖：uvicorn（建议 ``pip install "uvicorn[standard]"``，reload 需要 watchfiles）。

启动前会**自动释放端口**：若端口已被占用，会终止占用该端口的进程后再启动，
避免 "WinError 10013/10048 address already in use"（Windows 上端口被活跃监听时
uvicorn 常报 10013，看起来像权限问题，其实只是上一次的实例还在跑）。

相关开关：
  --no-free-port     关闭自动释放，端口冲突时按原样交给 uvicorn 报错
  --force-free-port  占用进程"看起来不像本项目的服务"时也强制终止
  --free-port-timeout N  等待端口释放的秒数（默认 10）
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

# Windows 控制台常为 GBK，emoji/中文替换字符会炸日志 -> 降级为替换
# line_buffering=True：启动器提示要即时可见（输出被重定向到文件时默认是块缓冲）
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass

# 保证无论从哪个 cwd 启动都能 import 到 server 包
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
try:
    DEFAULT_PORT = int(os.environ.get("PORT", "8787"))
except ValueError:
    DEFAULT_PORT = 8787

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


# ────────────────────────── 端口释放 ──────────────────────────
def _can_bind(host: str, port: int) -> bool:
    """尝试真实 bind 判断端口是否可用。

    注意：这里**故意不设 SO_REUSEADDR**。Windows 下设了它反而允许"抢占"
    已处于监听状态的端口，会让检测误判为可用。
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return True  # 解析不了地址就交给 uvicorn 自己报错
    if not infos:
        return True
    family, socktype, proto, _canon, sockaddr = infos[0]
    s = socket.socket(family, socktype, proto)
    try:
        s.bind(sockaddr)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _local_matches_port(local: str, port: int) -> bool:
    """netstat 的本地地址形如 ``127.0.0.1:8787`` / ``[::1]:8787`` / ``0.0.0.0:8787``。"""
    try:
        return int(local.rsplit(":", 1)[-1]) == port
    except (ValueError, IndexError):
        return False


def _port_owner_pids(port: int) -> list[int]:
    """找出正在 LISTEN 该端口的进程 PID 列表。"""
    pids: list[int] = []
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
                errors="replace", creationflags=_NO_WINDOW,
            ).stdout or ""
        except Exception:
            return []
        for line in out.splitlines():
            parts = line.split()
            # TCP   本地地址   外部地址   LISTENING   PID
            if len(parts) < 5 or parts[0].upper() not in ("TCP", "TCPV6"):
                continue
            local, state, pid_s = parts[1], parts[3], parts[4]
            if state.upper() != "LISTENING":
                continue
            if not _local_matches_port(local, port):
                continue
            try:
                pids.append(int(pid_s))
            except ValueError:
                continue
        return sorted(set(pids))

    # 非 Windows：优先 ss，其次 lsof / fuser
    try:
        out = subprocess.run(
            ["ss", "-ltnp"], capture_output=True, text=True, timeout=8, errors="replace",
        ).stdout or ""
        for line in out.splitlines():
            if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
                continue
            for chunk in line.split("pid=")[1:]:
                try:
                    pids.append(int(chunk.split(",")[0]))
                except ValueError:
                    continue
    except Exception:
        pass
    if not pids:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=8, errors="replace",
            ).stdout or ""
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
        except Exception:
            pass
    return sorted(set(pids))


def _describe_process(pid: int) -> tuple[str, str]:
    """返回 (进程名, 命令行)；取不到就给空串。"""
    if IS_WINDOWS:
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
            "if($p){Write-Output ($p.Name + \"`t\" + $p.CommandLine)}"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8,
                errors="replace", creationflags=_NO_WINDOW,
            ).stdout or ""
            if "\t" in out:
                name, _, cmd = out.partition("\t")
                return name.strip(), cmd.strip()
            return out.strip(), ""
        except Exception:
            return "", ""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm=,args="],
            capture_output=True, text=True, timeout=8, errors="replace",
        ).stdout or ""
        line = out.strip()
        if not line:
            return "", ""
        name, _, cmd = line.partition(" ")
        return name.strip(), cmd.strip()
    except Exception:
        return "", ""


def _looks_like_our_server(name: str, cmdline: str) -> bool:
    """判断占用进程是否像"本项目的服务实例"，用于决定是否安全自动终止。"""
    name_l = (name or "").lower()
    cmd_l = (cmdline or "").lower()
    interpreter = any(k in name_l for k in ("python", "pythonw", "pypy", "uvicorn"))
    if not interpreter:
        return False
    return any(k in cmd_l for k in ("run.py", "uvicorn", "server.app", "task_sniper"))


def _kill_pid_tree(pid: int, force: bool = False) -> tuple[bool, str]:
    """终止进程（含子进程）。返回 (是否成功, 失败原因)。

    注意参数顺序：``/PID`` 的取值必须紧跟其后，``/F`` 不能插在两者之间，
    否则 taskkill 会报 ``Value expected for '/PID'``。
    """
    if IS_WINDOWS:
        cmd = ["taskkill"]
        if force:
            cmd.append("/F")
        cmd += ["/T", "/PID", str(pid)]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                errors="replace", creationflags=_NO_WINDOW,
            )
            if r.returncode == 0:
                return True, ""
            msg = ((r.stdout or "") + (r.stderr or "")).strip().replace("\r", " ").replace("\n", " ")
            return False, msg or f"taskkill 返回码 {r.returncode}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"SIGTERM 失败: {exc}"
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            return True, ""
    try:
        os.kill(pid, signal.SIGKILL)
        return True, ""
    except OSError as exc:
        return False, f"SIGKILL 失败: {exc}"


def _port_in_use(host: str, port: int) -> tuple[bool, list[int]]:
    """判断端口是否被占用，返回 (是否占用, 监听该端口的 PID 列表)。

    **以 netstat 监听表为主、bind 探测为辅**：只看 bind 会漏判——若占用方设了
    SO_REUSEADDR（如 Python 的 ``http.server``）并绑在 ``0.0.0.0``，本机再 bind
    ``127.0.0.1`` 会成功，但两个进程会"共存"，等于没检测到。
    """
    owners = _port_owner_pids(port)
    if owners:
        return True, owners
    return (not _can_bind(host, port)), []


def _wait_port_free(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if not _port_in_use(host, port)[0]:
            return True
        time.sleep(0.3)
    return not _port_in_use(host, port)[0]


def ensure_port_free(host: str, port: int, *, force: bool = False, timeout: float = 10.0) -> bool:
    """确保端口可用：被占用就终止占用进程。返回最终是否可用。"""
    busy, pids = _port_in_use(host, port)
    if not busy:
        print(f"[端口] {host}:{port} 可用，继续启动。")
        return True

    if not pids:
        print(
            f"[端口] {host}:{port} 无法 bind，但没找到正在监听的进程。\n"
            f"        可能是端口落在 Windows 保留端口段（netsh int ipv4 show excludedportrange protocol=tcp），\n"
            f"        或需要管理员权限。可换端口启动：python run.py --port 8788"
        )
        return False

    killed: list[int] = []
    self_pid = os.getpid()
    parent_pid = os.getppid()
    for pid in pids:
        if pid <= 4 or pid in (self_pid, parent_pid):
            continue
        name, cmdline = _describe_process(pid)
        desc = f"{name or '未知进程'} (PID {pid})"
        if cmdline:
            desc += f" 命令：{cmdline[:160]}"

        if not _looks_like_our_server(name, cmdline) and not force:
            print(
                f"[端口] {host}:{port} 被 {desc} 占用。\n"
                f"        该进程看起来不是本项目的服务，为安全起见未自动终止。\n"
                f"        确认可杀时用：python run.py --force-free-port\n"
                f"        或换端口：python run.py --port 8788"
            )
            return False

        print(f"[端口] 终止占用进程：{desc}")
        ok, why = _kill_pid_tree(pid, force=True)
        if ok:
            killed.append(pid)
        else:
            print(f"[端口] 终止 PID {pid} 失败：{why}（可尝试用管理员权限运行）")

    if not killed:
        print(f"[端口] 未能终止任何占用进程，{host}:{port} 仍不可用。")
        return False

    if _wait_port_free(host, port, timeout):
        print(f"[端口] 已释放 {host}:{port}（终止 PID {', '.join(str(p) for p in killed)}），继续启动。")
        return True

    print(
        f"[端口] 已终止 PID {', '.join(str(p) for p in killed)}，但 {host}:{port} 在 {timeout:g}s 内仍不可用。\n"
        f"        可稍等片刻后重试，或换端口：python run.py --port 8788"
    )
    return False


# ────────────────────────── 入口 ──────────────────────────
def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="任务狙击台后端启动器")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8787）")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发模式）")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    parser.add_argument(
        "--no-free-port", action="store_true",
        help="不自动释放端口（端口被占用时直接报错）",
    )
    parser.add_argument(
        "--force-free-port", action="store_true",
        help="端口被非本项目进程占用时也强制终止",
    )
    parser.add_argument(
        "--free-port-timeout", type=float, default=10.0,
        help="终止占用进程后等待端口释放的秒数（默认 10）",
    )
    args = parser.parse_args()

    if not args.no_free_port:
        if not ensure_port_free(
            args.host, args.port,
            force=args.force_free_port, timeout=args.free_port_timeout,
        ):
            return 1

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())