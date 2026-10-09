# -*- coding: utf-8 -*-
"""播报器：把远端事件翻译成人话，并按任务限频，避免刷屏。

规则：
  - started / permission / tool / text / error 这类事件值得单独播报；
  - thinking / tool_result / usage / exit 默认不播（exit 交给 _state 汇总）；
  - verbose 打开时会额外播报 thinking；
  - 普通事件按 announce_interval 限频，error 事件始终播报。
"""
import time


class Announcer:
    def __init__(self, interval=30, max_len=1500, verbose=False):
        self.interval = interval
        self.max_len = max_len
        self.verbose = verbose

    def _truncate(self, text):
        text = (text or "").strip()
        if len(text) > self.max_len:
            return text[: self.max_len] + "…"
        return text

    def translate(self, ev: dict):
        """把一条事件翻译成人话，返回 None 表示无需播报。"""
        etype = ev.get("type") or ""
        text = self._truncate(str(ev.get("text") or ""))
        meta = ev.get("meta") or {}

        if etype == "started":
            return f"任务开始：{text}" if text else "任务开始"
        if etype == "permission":
            return f"权限档：{text}" if text else None
        if etype == "text":
            return text or None
        if etype == "tool":
            name = meta.get("name") or meta.get("tool") or text or "工具"
            return f"调用工具：{name}"
        if etype == "error":
            return f"出错：{text}" if text else "出错"
        if etype == "thinking":
            return f"思考：{text}" if (self.verbose and text) else None
        if etype in ("tool_result", "usage", "exit"):
            return None
        # 未知类型：有文本才播
        return text or None

    def translate_state(self, state: dict):
        """把 _state 里的状态翻译成一句总结，返回 None 表示不需要播报。"""
        status = state.get("status") or ""
        final = self._truncate(str(state.get("final") or ""))
        code = state.get("exit_code")

        if status == "succeeded":
            return f"任务完成：{final}" if final else "任务完成"
        if status == "failed":
            return f"任务失败（退出码 {code}）"
        if status == "timed_out":
            return "任务超时被终止"
        if status == "cancelled":
            return "任务已取消"
        if status in ("queued", "running"):
            return None
        return None

    def should_announce(self, rec, ev: dict) -> bool:
        """限频判断：error 始终播报，普通事件按间隔合并。"""
        if (ev.get("type") or "") == "error":
            return True
        return time.time() - rec.last_announce >= self.interval
