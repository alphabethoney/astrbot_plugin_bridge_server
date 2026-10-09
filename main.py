# -*- coding: utf-8 -*-
"""桥服务器端插件主入口。

把聊天指令接到远端 Windows 桥（runner.py）：
  /wg <描述>    提交给 dsh
  /wc <描述>    提交给 codex
  /wl           列出远端任务
  /ww [task_id] 查看进度（缺省取本会话最近一个任务）
  /wx <task_id> 取消任务

指令会做会话白名单校验；任务提交后由后台轮询远端事件，翻译成人话并按限频播报。
"""
import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

from .announcer import Announcer
from .config import BridgeConfig
from .dashboard import Dashboard
from .ssh_dispatch import SshDispatcher, SshError
from .task_table import TaskTable, TERMINAL


class BridgeServer(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._cfg = BridgeConfig(config)
        self.dispatch = SshDispatcher(
            host=self._cfg.ssh_host,
            port=self._cfg.ssh_port,
            user=self._cfg.ssh_user,
            work_dir=self._cfg.work_dir,
            python=self._cfg.python,
            timeout=self._cfg.ssh_timeout_s,
        )
        self.table = TaskTable()
        self.dashboard = Dashboard(self._cfg.snapshot_path)
        self.announcer = Announcer(
            interval=self._cfg.announce_interval,
            max_len=self._cfg.max_text_len,
            verbose=self._cfg.verbose,
        )
        self._poll_task = None

    # ---------------------------------------------------------------- 生命周期
    async def initialize(self):
        if not self._cfg.ssh_host:
            logger.warning("[桥服务] 未配置目标主机，轮询不会工作")
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[桥服务] 已启动后台轮询，间隔 %d 秒", self._cfg.poll_interval)

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    # ---------------------------------------------------------------- 指令
    @filter.command("wg")
    async def wg(self, event: AstrMessageEvent, message: str = ""):
        """提交任务给 dsh。"""
        yield event.plain_result(await self._submit(event, "dsh", message))

    @filter.command("wc")
    async def wc(self, event: AstrMessageEvent, message: str = ""):
        """提交任务给 codex。"""
        yield event.plain_result(await self._submit(event, "codex", message))

    @filter.command("wl")
    async def wl(self, event: AstrMessageEvent):
        """列出远端任务。"""
        yield event.plain_result(await self._list_tasks())

    @filter.command("ww")
    async def ww(self, event: AstrMessageEvent, message: str = ""):
        """查看任务进度。"""
        yield event.plain_result(await self._watch_progress(event, message))

    @filter.command("wx")
    async def wx(self, event: AstrMessageEvent, message: str = ""):
        """取消任务。"""
        yield event.plain_result(await self._cancel(event, message))

    @filter.command("wd")
    async def wd(self, event: AstrMessageEvent):
        """任务仪表盘（纯本地读快照，不发起 SSH）。"""
        yield event.plain_result(self.dashboard.render())

    # ---------------------------------------------------------------- 实现
    @staticmethod
    def _session(event) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    async def _submit(self, event, agent, message):
        session = self._session(event)
        if not self._cfg.session_allowed(session):
            return "该会话未被授权使用桥"
        prompt = (message or "").strip()
        if not prompt:
            return f"用法：/{'wg' if agent == 'dsh' else 'wc'} <任务描述>"
        req = {
            "agent": agent,
            "cwd": self._cfg.work_dir,
            "prompt": prompt,
            "permission": self._cfg.default_permission,
            "timeout_s": self._cfg.default_timeout_s,
        }
        try:
            task_id = await self.dispatch.submit(req)
        except SshError as e:
            logger.warning("[桥服务] submit 失败：%s", e)
            return f"提交失败：{e}"
        except Exception as e:
            logger.warning("[桥服务] submit 异常", exc_info=True)
            return f"提交异常：{e}"
        if not task_id:
            return "提交失败：未拿到 task_id"
        self.table.add(task_id, agent, prompt, session)
        return f"已提交 [{agent}] {task_id}，稍后播报进度"

    async def _list_tasks(self):
        try:
            tasks = await self.dispatch.list_tasks()
        except Exception as e:
            logger.warning("[桥服务] list 失败：%s", e)
            return f"获取任务列表失败：{e}"
        if not tasks:
            return "远端暂无任务记录"
        lines = []
        for t in tasks[:20]:
            lines.append(
                f"{t.get('task_id')} [{t.get('agent')}] {t.get('status')} "
                f"{(t.get('prompt') or '')[:40]}"
            )
        return "任务列表：\n" + "\n".join(lines)

    async def _watch_progress(self, event, message):
        session = self._session(event)
        tid = (message or "").strip()
        rec = self.table.get(tid) if tid else self.table.recent_for(session)
        if rec is None:
            return "找不到任务，先 /wl 看看 task_id"
        await self._poll_one(rec)
        return (
            f"任务 {rec.task_id} [{rec.agent}] 状态：{rec.status}，"
            f"已读到 seq {rec.last_seq}。新的进度事件会直接发到本会话。"
        )

    async def _cancel(self, event, message):
        tid = (message or "").strip()
        if not tid:
            return "用法：/wx <task_id>"
        try:
            await self.dispatch.cancel(tid)
        except Exception as e:
            logger.warning("[桥服务] cancel 失败：%s", e)
            return f"取消失败：{e}"
        rec = self.table.get(tid)
        if rec is not None:
            rec.status = "cancelled"
        return f"已请求取消 {tid}"

    # ---------------------------------------------------------------- 轮询与播报
    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(self._cfg.poll_interval)
                await self._poll_active()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[桥服务] 轮询出错", exc_info=True)

    async def _poll_active(self):
        for rec in list(self.table.active()):
            try:
                await self._poll_one(rec)
            except Exception:
                logger.warning("[桥服务] 轮询任务 %s 出错", rec.task_id, exc_info=True)

    @staticmethod
    def _unpack_watch(result):
        """把 watch 的返回值统一解包成 (events, state)。

        远端 watch 通道当前返回 (events, state) 二元组；这里同时兼容日后
        改成 {"events": [...], "state": {...}} 这类字典的情况，形状不认识
        时退回空结果，避免调用方对二元组按字典取键抛 AttributeError。
        """
        if isinstance(result, dict):
            events = result.get("events")
            state = result.get("state")
        elif isinstance(result, (tuple, list)) and len(result) == 2:
            events, state = result
        else:
            events, state = None, None
        if not isinstance(events, (list, tuple)):
            events = []
        if not isinstance(state, dict):
            state = {}
        return list(events), state

    async def _poll_one(self, rec):
        """对单个任务做一次增量 watch，翻译并限频播报，最后应用 _state。"""
        try:
            result = await self.dispatch.watch(rec.task_id, rec.last_seq)
        except SshError as e:
            logger.warning("[桥服务] watch %s 失败：%s", rec.task_id, e)
            return
        except Exception as e:
            logger.warning("[桥服务] watch %s 异常：%s", rec.task_id, e)
            return

        events, state = self._unpack_watch(result)
        rec.last_poll = time.time()
        for ev in events:
            seq = ev.get("seq", 0)
            if isinstance(seq, (int, float)):
                rec.last_seq = max(rec.last_seq, int(seq))
            text = self.announcer.translate(ev)
            if text and self.announcer.should_announce(rec, ev):
                rec.last_announce = time.time()
                await self._say(rec, text)

        if state:
            summary = self._apply_state(rec, state)
            if summary:
                await self._say(rec, summary)

    def _apply_state(self, rec, state):
        """把 _state 应用到任务记录，状态切到终态时返回一句总结。"""
        status = state.get("status")
        if not status:
            return None
        old = rec.status
        rec.status = status
        if status in TERMINAL and old != status:
            return self.announcer.translate_state(state)
        return None

    async def _say(self, rec, text):
        """把播报内容发回触发任务的那个会话。"""
        if not rec.session:
            logger.info("[桥服务] 任务 %s 无会话信息，内容：%s", rec.task_id, text)
            return
        try:
            await self.context.send_message(rec.session, MessageChain().message(text))
        except Exception:
            logger.warning(
                "[桥服务] 播报失败（目标 %s）: %s", rec.session, text, exc_info=True
            )
