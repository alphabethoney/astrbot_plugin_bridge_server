# -*- coding: utf-8 -*-
"""任务仪表盘渲染器：纯本地读快照、拼清单文本，不发起任何 SSH。

快照由 collect_dashboard.py 生成，格式约定：

    {
      "as_of": "2026-10-09 23:45:00",
      "local":    {"ok": true, "error": null,
                   "processes": [{"name","pid","status","started","output"}...]},
      "bridge":   {"ok": true, "error": null,
                   "tasks": [{"task_id","agent","cwd","status","prompt"}...]},
      "resident": {"ok": true, "error": null,
                   "crontab": {"count"}, "inspection": {"ok","detail"},
                   "tunnel": {"ok","detail"}, "plugins": {"count","detail"}}
    }

这里只做「读文件 + 格式化」，保证 /wd 命令零远端开销。
"""
import json
import os
import time

MAX_ITEMS = 6        # 组内超过 6 条折叠成汇总
STALE_S = 600        # 快照超过 10 分钟提示可能过期

BRIDGE_LABEL = {
    "queued": "排队", "running": "运行中", "succeeded": "成功",
    "failed": "失败", "timed_out": "超时", "cancelled": "已取消",
}
BRIDGE_ABNORMAL = {"failed", "timed_out", "cancelled"}
LOCAL_LABEL = {
    "running": "运行中", "zombie": "僵尸", "stopped": "已停止", "failed": "失败",
}
LOCAL_ABNORMAL = {"zombie", "stopped", "failed"}


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _s(v):
    return "" if v is None else str(v)


def _truncate(v, n):
    v = _s(v).replace("\r", " ").replace("\n", " ").strip()
    return v[:n] + "…" if len(v) > n else v


class Row:
    __slots__ = ("text", "abnormal", "bucket")

    def __init__(self, text, abnormal=False, bucket=""):
        self.text = text
        self.abnormal = abnormal
        self.bucket = bucket


def _build_local(local):
    if not local.get("ok"):
        return [Row("采集失败：%s" % (local.get("error") or "未知错误"), True, "错误")]
    rows = []
    for p in local.get("processes") or []:
        st = str(p.get("status") or "running")
        label = LOCAL_LABEL.get(st, st)
        name = p.get("name") or "?"
        pid = p.get("pid")
        started = p.get("started") or "?"
        out = _truncate(p.get("output"), 32) or "—"
        pid_s = "pid %s" % pid if pid is not None else "pid ?"
        rows.append(
            Row(
                "[%s] %s（%s） 启动 %s 输出 %s" % (label, name, pid_s, started, out),
                st in LOCAL_ABNORMAL,
                label,
            )
        )
    return rows


def _build_bridge(bridge):
    if not bridge.get("ok"):
        return [Row("采集失败：%s" % (bridge.get("error") or "未知错误"), True, "错误")]
    rows = []
    for t in bridge.get("tasks") or []:
        st = str(t.get("status") or "未知")
        label = BRIDGE_LABEL.get(st, st)
        tid = t.get("task_id") or "?"
        agent = t.get("agent") or "?"
        cwd = t.get("cwd") or "?"
        prompt = _truncate(t.get("prompt"), 20)
        text = "[%s] %s [%s] %s" % (label, tid, agent, cwd)
        if prompt:
            text += " 「%s」" % prompt
        rows.append(Row(text, st in BRIDGE_ABNORMAL, label))
    return rows


def _build_resident(res):
    if not res.get("ok"):
        return [Row("采集失败：%s" % (res.get("error") or "未知错误"), True, "错误")]
    rows = []

    crontab = res.get("crontab") or {}
    cc = crontab.get("count")
    rows.append(
        Row("crontab 条目 %s 条" % (cc if cc is not None else "未知"), False, "crontab")
    )

    insp = res.get("inspection") or {}
    insp_ok = bool(insp.get("ok"))
    rows.append(
        Row("巡检 %s（%s）" % ("正常" if insp_ok else "异常",
                              _s(insp.get("detail")) or "—"),
            not insp_ok, "巡检")
    )

    tun = res.get("tunnel") or {}
    tun_ok = bool(tun.get("ok"))
    rows.append(
        Row("隧道 %s（%s）" % ("连通" if tun_ok else "断开",
                              _s(tun.get("detail")) or "—"),
            not tun_ok, "隧道")
    )

    pl = res.get("plugins") or {}
    pc = pl.get("count")
    if pc is None:
        rows.append(Row("插件 数量未知（%s）" % (_s(pl.get("detail")) or "未配置目录"),
                        False, "插件"))
    else:
        rows.append(Row("插件 %s 个" % _num(pc), False, "插件"))

    return rows


def _group(title, rows, empty):
    if not rows:
        return ["%s 空（%s）" % (title, empty)]
    rows = sorted(rows, key=lambda r: (not r.abnormal))
    if len(rows) <= MAX_ITEMS:
        return ["%s 共 %d 条" % (title, len(rows))] + [
            "%d. %s" % (i, r.text) for i, r in enumerate(rows, 1)
        ]
    ab = [r for r in rows if r.abnormal]
    norm = [r for r in rows if not r.abnormal]
    counts = {}
    for r in rows:
        b = r.bucket or "其他"
        counts[b] = counts.get(b, 0) + 1
    summary = "、".join(
        "%s %d" % (b, c) for b, c in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    lines = ["%s 共 %d 条（%s）" % (title, len(rows), summary)]
    for i, r in enumerate(ab[:3], 1):
        lines.append("%d. %s" % (i, r.text))
    if len(ab) > 3:
        lines.append("… 其余 %d 条异常项已折叠" % (len(ab) - 3))
    if norm:
        lines.append("… 其余 %d 条正常项已折叠" % len(norm))
    return lines


def render(snap, stale=False):
    as_of = _s(snap.get("as_of")) or "未知"
    header = "任务仪表盘 · 数据截至 %s" % as_of
    if stale:
        header += "（快照可能过期，请运行采集脚本）"

    local_rows = _build_local(snap.get("local") or {})
    bridge_rows = _build_bridge(snap.get("bridge") or {})
    resident_rows = _build_resident(snap.get("resident") or {})

    abnormal_total = sum(
        1 for r in local_rows + bridge_rows + resident_rows if r.abnormal
    )

    parts = [header]
    if abnormal_total:
        parts.append("异常 %d 条（已置顶于各分组）" % abnormal_total)
    parts += _group("【本地任务】", local_rows, "无在跑任务")
    parts += _group("【远端桥】", bridge_rows, "远端无任务记录")
    parts += _group("【常驻项】", resident_rows, "无数据")
    return "\n".join(parts)


class Dashboard:
    def __init__(self, snapshot_path=None, stale_seconds=STALE_S):
        self.stale_seconds = stale_seconds
        if snapshot_path:
            self.path = snapshot_path
        else:
            self.path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "dashboard_snapshot.json"
            )

    def render(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                snap = json.load(f)
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            return "任务仪表盘：暂无快照数据（请先运行 collect_dashboard.py）"
        except Exception as e:
            return "任务仪表盘：读取快照失败：%s" % e
        return render(snap, stale=time.time() - mtime > self.stale_seconds)
