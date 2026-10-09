# -*- coding: utf-8 -*-
"""SSH 调度层：全插件唯一接触 SSH 的模块。

职责：
  1. 把动作拼成远端 `python runner.py <sub> ...` 命令；
  2. 收 stdout、逐行解析 JSON；
  3. submit 的任务文本一律走 stdin（UTF-8 JSON），绝不拼进命令行，规避转义与注入。

远端 Windows 的默认 shell 是 cmd，命令用 cmd 语法拼接（cd /d + && 串联）。
依赖 paramiko（见 requirements.txt），SSH 是阻塞 IO，统一用 asyncio.to_thread
丢到线程池执行，避免卡住 AstrBot 的事件循环。
"""
import asyncio
import json
import logging

try:
    import paramiko
except ImportError:  # pragma: no cover - 依赖缺失时在运行期给出明确报错
    paramiko = None

from astrbot.api import logger

# paramiko 会把每次连接的握手、密钥协商、通道收发等细节按 DEBUG 级别打出来，
# 实测能把 AstrBot 的日志刷出上百条。这里把 paramiko 包级 logger 压到 WARNING：
# DEBUG/INFO 不再输出，但认证失败、连接超时、通道异常等 WARNING 及以上照常
# 记录，日常排障不受影响；真要深挖协议细节，临时把级别调回 DEBUG 即可。
logging.getLogger("paramiko").setLevel(logging.WARNING)


class SshError(Exception):
    """SSH 调用失败或远端 runner 返回错误的统一异常。"""


def _quote(s: str) -> str:
    """给 cmd 参数加双引号，降低含空格路径出错的概率。"""
    return '"' + str(s).replace('"', "") + '"'


class SshDispatcher:
    def __init__(self, host, port, user, work_dir, python="python", timeout=30):
        self.host = host
        self.port = int(port)
        self.user = user
        self.work_dir = work_dir
        self.python = python
        self.timeout = timeout

    # ---------------------------------------------------------------- 底层
    def _client(self):
        """新建一个 SSH 客户端。目前走免密（密钥/agent）；密码或私钥路径 TODO 走配置。"""
        if paramiko is None:
            raise SshError("未安装 paramiko，无法发起 SSH 连接")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            look_for_keys=True,
            allow_agent=True,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        return client

    def _build_command(self, subcommand, *args):
        """拼接远端命令：cd 到工作目录后调用 python runner.py <sub> <args...>。"""
        parts = []
        if self.work_dir:
            parts.append("cd /d " + _quote(self.work_dir) + " &&")
        parts.append(_quote(self.python))
        parts.append("runner.py")
        parts.append(subcommand)
        for a in args:
            parts.append(_quote(str(a)))
        return " ".join(parts)

    async def _exec(self, command, stdin_data=None):
        """在后台线程执行一次 SSH 命令，返回 (returncode, stdout_text, stderr_text)。"""

        def _run():
            client = self._client()
            try:
                stdin, stdout, stderr = client.exec_command(command, timeout=self.timeout)
                if stdin_data is not None:
                    stdin.write(stdin_data)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                out = stdout.read().decode("utf-8", "replace")
                err = stderr.read().decode("utf-8", "replace")
                code = stdout.channel.recv_exit_status()
                return code, out, err
            finally:
                client.close()

        return await asyncio.to_thread(_run)

    @staticmethod
    def _parse_lines(out_text):
        """把输出按行解析成 JSON 对象列表，坏行跳过并告警。"""
        records = []
        for line in out_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                logger.warning("[SSH] 忽略无法解析的输出行：%s", line[:200])
        return records

    async def _run_and_parse(self, command, stdin_obj=None):
        """执行命令，若 stdin_obj 非空则以 UTF-8 JSON 灌入 stdin，返回解析结果列表。"""
        stdin_data = None
        if stdin_obj is not None:
            stdin_data = (json.dumps(stdin_obj, ensure_ascii=False) + "\n").encode("utf-8")
        code, out, err = await self._exec(command, stdin_data)
        records = self._parse_lines(out)
        if code != 0:
            raise SshError(f"远端命令退出码 {code}：{(err or out).strip()[:300]}")
        return records

    # ---------------------------------------------------------------- 四个动作
    async def submit(self, req: dict) -> str:
        """提交任务，返回 task_id。req 含 agent/cwd/prompt/permission/timeout_s 等。"""
        records = await self._run_and_parse(self._build_command("submit"), stdin_obj=req)
        if not records:
            raise SshError("submit 未返回任何结果")
        resp = records[0]
        if not resp.get("ok"):
            raise SshError(f"submit 失败：{resp.get('error', resp)}")
        return str(resp.get("task_id") or "")

    async def watch(self, task_id, after=0):
        """增量取事件，统一返回 (events, state) 二元组，events 不含 _state 哨兵。

        为兼容远端将来可能直接返回 {"events": [...], "state": {...}} 的聚合
        字典形态，这里也一并处理；非字典的坏记录直接跳过，避免 .get 抛错。
        """
        command = self._build_command("watch", task_id, "--after", str(int(after)))
        records = await self._run_and_parse(command)
        events, state = [], {}
        for rec in records:
            if not isinstance(rec, dict):
                logger.warning("[SSH] watch 收到非字典记录，已跳过：%r", rec)
                continue
            # 聚合字典形态：没有 type 字段，但直接带 events/state
            if "type" not in rec and ("events" in rec or "state" in rec):
                agg_events = rec.get("events")
                if isinstance(agg_events, (list, tuple)):
                    events.extend(agg_events)
                agg_state = rec.get("state")
                if isinstance(agg_state, dict) and agg_state:
                    state = agg_state
                continue
            if rec.get("type") == "_state":
                state = rec.get("state") or {}
            else:
                events.append(rec)
        return events, state

    async def cancel(self, task_id) -> None:
        records = await self._run_and_parse(self._build_command("cancel", task_id))
        if not records or not records[0].get("ok"):
            raise SshError(f"cancel 失败：{records}")

    async def list_tasks(self):
        records = await self._run_and_parse(self._build_command("list"))
        if not records:
            return []
        resp = records[0]
        if not resp.get("ok"):
            raise SshError(f"list 失败：{resp.get('error', resp)}")
        return resp.get("tasks") or []
