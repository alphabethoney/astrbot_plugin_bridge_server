#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务仪表盘采集脚本（本地跑，产出快照 JSON，供插件 /wd 纯本地读）。

覆盖三类来源：
  1. 服务器本地在跑的 agent 进程（进程名、pid、启动时间、状态、输出文件）；
  2. 远端桥任务（一次 ssh 跑 runner.py list，取 task_id/agent/cwd/status/prompt）；
  3. 常驻项（crontab 条目数、巡检状态、隧道连通性、已装插件数量）。

用法：
  python3 collect_dashboard.py [--config collect_config.json] [--out dashboard_snapshot.json]

配置缺失时使用内置默认值；任意一项采集失败只影响该项，不拖垮整份快照。
建议用 crontab 定时运行，例如每 5 分钟一次。
"""
import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time

try:
    import paramiko
except ImportError:
    paramiko = None

# 同 ssh_dispatch：把 paramiko 包级 logger 压到 WARNING，避免 DEBUG 连接细节刷屏。
# WARNING 及以上的认证失败 / 连接异常仍会保留，不影响排查真问题。
logging.getLogger("paramiko").setLevel(logging.WARNING)

DEFAULTS = {
    "out": "dashboard_snapshot.json",
    "ssh": {"host": "", "port": 22, "user": "", "work_dir": "", "python": "python"},
    "local_agents": ["dsh", "codex", "astrbot"],
    "inspection": {"marker": "", "fresh_hours": 24},
    "tunnel": {"host": "", "port": 22},
    "plugins_dir": "",
}


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(dict(base[k]), v)
        else:
            base[k] = v
    return base


def read_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = _merge(cfg, json.load(f))
        except Exception as e:
            sys.stderr.write("配置读取失败：%s\n" % e)
    return cfg


# ---------------------------------------------------------------- 来源 1：本地进程
def _btime():
    try:
        for line in open("/proc/stat", encoding="utf-8"):
            if line.startswith("btime "):
                return int(line.split()[1])
    except Exception:
        return 0
    return 0


def _proc_output(pid):
    for fd in ("1", "2"):
        try:
            target = os.readlink("/proc/%d/fd/%s" % (pid, fd))
        except Exception:
            continue
        if target and not target.startswith(
            ("pipe:", "socket:", "anon_inode:", "/dev/pts", "/dev/null", "tty")
        ):
            return target
    return ""


def _proc_info(pid):
    info = {"name": "", "pid": pid, "status": "running", "started": "", "output": ""}
    try:
        data = open("/proc/%d/stat" % pid, encoding="utf-8").read()
    except Exception:
        info["status"] = "failed"
        return info
    lp = data.find("(")
    rp = data.rfind(")")
    info["name"] = data[lp + 1:rp] if lp >= 0 and rp > lp else ""
    tail = data[rp + 2:].split() if rp >= 0 else []
    state = tail[0] if tail else "?"
    stmap = {
        "R": "running", "S": "running", "D": "running", "I": "running",
        "T": "stopped", "t": "stopped", "Z": "zombie", "X": "failed",
    }
    info["status"] = stmap.get(state, state or "running")
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        start_ticks = int(tail[19])
        info["started"] = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(_btime() + start_ticks / ticks),
        )
    except Exception:
        info["started"] = ""
    info["output"] = _proc_output(pid)
    return info


def collect_local(patterns):
    try:
        env = dict(os.environ, LC_ALL="C")
        out = subprocess.run(
            ["ps", "-eo", "pid=,args=", "--no-headers"],
            capture_output=True, text=True, env=env, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": "ps 失败：%s" % e, "processes": []}
    procs = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, args = line.split(None, 1)
            pid = int(pid_s)
        except ValueError:
            continue
        low = args.lower()
        if not any(p.lower() in low for p in patterns):
            continue
        info = _proc_info(pid)
        info["args"] = args
        procs.append(info)
    procs.sort(key=lambda p: p.get("pid", 0))
    return {"ok": True, "processes": procs}


# ---------------------------------------------------------------- 来源 2：远端桥
def _q(s):
    return '"' + str(s).replace('"', "") + '"'


def _list_command(ssh):
    parts = []
    if ssh.get("work_dir"):
        parts.append("cd /d " + _q(ssh["work_dir"]) + " &&")
    parts.append(_q(ssh.get("python") or "python"))
    parts.append("runner.py list")
    return " ".join(parts)


def _ssh_run(ssh, cmd):
    host = ssh["host"]
    port = int(ssh.get("port") or 22)
    user = ssh["user"]
    if paramiko is not None:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            hostname=host, port=port, username=user,
            look_for_keys=True, allow_agent=True,
            timeout=30, banner_timeout=30, auth_timeout=30,
        )
        try:
            _, stdout, stderr = c.exec_command(cmd, timeout=30)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
        finally:
            c.close()
        if code != 0:
            raise RuntimeError("远端退出码 %d：%s" % (code, (err or out).strip()[:200]))
        return out
    r = subprocess.run(
        ["ssh", "%s@%s" % (user, host), "-p", str(port), cmd],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("退出码 %d：%s" % (r.returncode, (r.stderr or r.stdout).strip()[:200]))
    return r.stdout


def collect_bridge(ssh):
    if not ssh.get("host") or not ssh.get("user"):
        return {"ok": False, "error": "未配置远端桥 SSH", "tasks": []}
    try:
        out = _ssh_run(ssh, _list_command(ssh))
    except Exception as e:
        return {"ok": False, "error": "SSH 失败：%s" % e, "tasks": []}
    tasks = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("ok") and isinstance(rec.get("tasks"), list):
            tasks = rec["tasks"]
            break
    return {"ok": True, "tasks": tasks}


# ---------------------------------------------------------------- 来源 3：常驻项
def _crontab_count():
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    n = 0
    for line in out.stdout.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def _inspection(cfg):
    marker = cfg.get("marker")
    fresh_h = max(0, int(cfg.get("fresh_hours") or 24))
    if not marker:
        return {"ok": False, "detail": "未配置巡检标记文件"}
    try:
        mt = os.path.getmtime(marker)
    except Exception:
        return {"ok": False, "detail": "找不到标记文件 %s" % marker}
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))
    if time.time() - mt > fresh_h * 3600:
        return {"ok": False, "detail": "最近 %s，超过 %d 小时" % (t, fresh_h)}
    return {"ok": True, "detail": "最近 %s" % t}


def _tunnel(cfg):
    host = cfg.get("host")
    port = int(cfg.get("port") or 22)
    if not host:
        return {"ok": False, "detail": "未配置隧道探测目标"}
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return {"ok": True, "detail": "%s:%d 连通" % (host, port)}
    except Exception:
        return {"ok": False, "detail": "%s:%d 断开" % (host, port)}


def _plugins(plugins_dir):
    if not plugins_dir:
        return {"count": None, "detail": "未配置插件目录"}
    if not os.path.isdir(plugins_dir):
        return {"count": None, "detail": "目录不存在 %s" % plugins_dir}
    n = 0
    for entry in os.listdir(plugins_dir):
        p = os.path.join(plugins_dir, entry)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "metadata.yaml")):
            n += 1
    return {"count": n, "detail": plugins_dir}


def collect_resident(cfg):
    return {
        "ok": True,
        "crontab": {"count": _crontab_count()},
        "inspection": _inspection(cfg.get("inspection") or {}),
        "tunnel": _tunnel(cfg.get("tunnel") or {}),
        "plugins": _plugins(cfg.get("plugins_dir")),
    }


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="采集任务仪表盘快照")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config or os.path.join(script_dir, "collect_config.json")
    cfg = read_config(config_path)
    out_path = args.out or cfg.get("out") or os.path.join(script_dir, "dashboard_snapshot.json")
    if not os.path.isabs(out_path):
        out_path = os.path.join(script_dir, out_path)

    snapshot = {
        "as_of": now_str(),
        "local": collect_local(cfg.get("local_agents") or []),
        "bridge": collect_bridge(cfg.get("ssh") or {}),
        "resident": collect_resident(cfg),
    }

    # 归档旧快照：每次采集前把上一份拷进 snapshots/ 子目录，保留最近 20 份历史
    if os.path.exists(out_path):
        archive_dir = os.path.join(os.path.dirname(out_path), "snapshots")
        os.makedirs(archive_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(out_path, os.path.join(archive_dir, "dashboard_snapshot_%s.json" % ts))
        archived = sorted(fn for fn in os.listdir(archive_dir) if fn.endswith(".json"))
        for old in archived[:-20]:
            try:
                os.remove(os.path.join(archive_dir, old))
            except OSError:
                pass

    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    print("快照已写入 %s（截至 %s）" % (out_path, snapshot["as_of"]))


if __name__ == "__main__":
    main()
