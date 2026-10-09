# -*- coding: utf-8 -*-
"""任务表：维护服务端视角下的任务状态机与增量游标。

目前只保存在内存里（插件进程生命周期内），重启即丢。重启后可以借助远端
list/watch 重建，但要保证「最后读到的 seq」不丢、避免重复播报，需要把游标
持久化到插件数据目录——TODO。
"""
import time
from dataclasses import dataclass, field

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
    status: str = "queued"     # 服务端已知的远端状态
    last_seq: int = 0          # 最后读到的远端事件 seq（增量游标）
    created: float = field(default_factory=time.time)
    last_poll: float = 0.0     # 最近一次 watch 时间
    last_announce: float = 0.0  # 最近一次播报时间

    @property
    def is_terminal(self):
        return self.status in TERMINAL


class TaskTable:
    def __init__(self):
        self._tasks = {}          # task_id -> TaskRecord
        self._session_tasks = {}  # session -> [task_id...]，最新在前

    def add(self, task_id, agent, prompt, session) -> TaskRecord:
        rec = TaskRecord(task_id=task_id, agent=agent, prompt=prompt, session=session)
        self._tasks[task_id] = rec
        self._session_tasks.setdefault(session, []).insert(0, task_id)
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

    def bump_seq(self, task_id, seq):
        rec = self._tasks.get(task_id)
        if rec is not None and seq > rec.last_seq:
            rec.last_seq = seq

    def recent_for(self, session):
        """某会话最近提交的一个任务。"""
        for tid in self._session_tasks.get(session, []):
            rec = self._tasks.get(tid)
            if rec is not None:
                return rec
        return None

    def recent(self):
        """全局最近提交的一个任务。"""
        if not self._tasks:
            return None
        return max(self._tasks.values(), key=lambda r: r.created)
