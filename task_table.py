# -*- coding: utf-8 -*-
"""任务表：维护服务端视角下的任务状态机与增量游标，并持久化到 JSON。

重启后自动加载，避免重复播报与游标丢失；每次变更即时落盘。
"""
import json
import os
import time
from dataclasses import dataclass, field, asdict

# 还在跑、需要继续轮询的状态
ACTIVE = {"queued", "running"}
# 终态：不再轮询
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}


@dataclass
class TaskRecord:
    task_id: str
    agent: str = ""
    prompt: str = ""
    session: str = ""          # 触发该任务的聊天会话（unified_msg_origin）
    conversation_id: str = ""  # 归属的任务对话（conversation_id）
    status: str = "queued"     # 服务端已知的远端状态
    last_seq: int = 0          # 最后读到的远端事件 seq（增量游标）
    created: float = field(default_factory=time.time)
    last_poll: float = 0.0     # 最近一次 watch 时间
    last_announce: float = 0.0  # 最近一次播报时间

    @property
    def is_terminal(self):
        return self.status in TERMINAL


class TaskTable:
    def __init__(self, path=None):
        self._tasks = {}          # task_id -> TaskRecord
        self._session_tasks = {}  # session -> [task_id...]，最新在前
        self._conversation_tasks = {}  # conversation_id -> [task_id...]，最新在前
        if path is None:
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "task_table.json"
            )
        self._path = path
        self.load()

    # ---------------------------------------------------------------- 持久化
    def _save(self):
        try:
            data = {
                "tasks": {tid: asdict(r) for tid, r in self._tasks.items()},
                "session_tasks": self._session_tasks,
                "conversation_tasks": self._conversation_tasks,
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            # 落盘失败不应影响任务调度本身
            pass

    def save(self):
        """显式落盘（供调用方在直接改字段后调用）。"""
        self._save()

    def load(self):
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._tasks = {
                tid: TaskRecord(**d) for tid, d in data.get("tasks", {}).items()
            }
            self._session_tasks = {
                k: list(v) for k, v in data.get("session_tasks", {}).items()
            }
            self._conversation_tasks = {
                k: list(v) for k, v in data.get("conversation_tasks", {}).items()
            }
        except Exception:
            self._tasks = {}
            self._session_tasks = {}
            self._conversation_tasks = {}

    # ---------------------------------------------------------------- 增改查
    def add(self, task_id, agent, prompt, session, conversation_id="") -> TaskRecord:
        rec = TaskRecord(task_id=task_id, agent=agent, prompt=prompt,
                         session=session, conversation_id=conversation_id)
        self._tasks[task_id] = rec
        self._session_tasks.setdefault(session, []).insert(0, task_id)
        self._conversation_tasks.setdefault(conversation_id, []).insert(0, task_id)
        self._save()
        return rec

    def get(self, task_id):
        return self._tasks.get(task_id)

    def all(self):
        return list(self._tasks.values())

    def active(self):
        """仍在跑、需要轮询的任务。"""
        return [r for r in self._tasks.values() if not r.is_terminal]

    def set_status(self, task_id, status):
        rec = self._tasks.get(task_id)
        if rec is not None:
            rec.status = status
            self._save()

    def bump_seq(self, task_id, seq):
        rec = self._tasks.get(task_id)
        if rec is not None and seq > rec.last_seq:
            rec.last_seq = seq
            self._save()

    def recent_for(self, session):
        """某会话最近提交的一个任务。"""
        for tid in self._session_tasks.get(session, []):
            rec = self._tasks.get(tid)
            if rec is not None:
                return rec
        return None

    def recent_for_conversation(self, conversation_id):
        """某任务对话最近提交的一个任务。"""
        for tid in self._conversation_tasks.get(conversation_id, []):
            rec = self._tasks.get(tid)
            if rec is not None:
                return rec
        return None

    def conversation_tasks(self, conversation_id):
        """某任务对话的全部任务（最新在前）。"""
        return [
            self._tasks[t]
            for t in self._conversation_tasks.get(conversation_id, [])
            if t in self._tasks
        ]

    def recent(self):
        """全局最近提交的一个任务。"""
        if not self._tasks:
            return None
        return max(self._tasks.values(), key=lambda r: r.created)
