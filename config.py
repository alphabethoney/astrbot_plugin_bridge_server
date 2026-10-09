# -*- coding: utf-8 -*-
"""配置表。

把 `_conf_schema.json` 解析后的配置统一成带类型兜底的访问器，所有可调项都在这里
取值，其他模块不写死任何具体值（尤其不写死任何 QQ 号）。

类型约定：只使用 int / float / bool / string / text / list / file / object / dict，
注意是 `bool` 而不是 `boolean`。下面用到 string / int / bool / list / object。
"""


class BridgeConfig:
    """桥服务器端插件的配置访问器。

    AstrBot 会把 _conf_schema.json 解析出的配置以 dict 形式传给插件的 __init__。
    """

    # 兜底默认值，实际以 _conf_schema.json + 面板配置为准
    DEFAULTS = {
        "host": "",
        "port": 22,
        "user": "",
        "work_dir": "",
        "python": "python",
        "poll_interval": 15,
        "announce_interval": 30,
        "default_permission": "workspace-write",
        "default_timeout_s": 600,
        "max_text_len": 1500,
        "allowed_sessions": [],
        "ssh_timeout_s": 30,
        "verbose": False,
        "snapshot_path": "",
    }

    def __init__(self, config=None):
        # AstrBotConfig 继承自 dict，这里兼容普通 dict 与 None
        self._cfg = config if isinstance(config, dict) else {}

    def _raw(self, key, default=None):
        """从顶层读一个配置项，空值（None/""）视为未配置。"""
        try:
            val = self._cfg.get(key)
            return default if val in (None, "") else val
        except Exception:
            return default

    @staticmethod
    def _from(container, key):
        try:
            val = container.get(key)
            return val if val not in (None, "") else None
        except Exception:
            return None

    def _ssh(self, key):
        """读 ssh 连接参数。优先从嵌套的 `ssh` object 读，兼容被铺平到顶层的情况。"""
        nested = self._raw("ssh", None)
        if isinstance(nested, dict):
            return self._from(nested, key)
        return self._raw(key, None)

    # ---- ssh 连接参数 ----
    @property
    def ssh_host(self):
        return str(self._ssh("host") or self.DEFAULTS["host"])

    @property
    def ssh_port(self):
        try:
            return int(self._ssh("port") or self.DEFAULTS["port"])
        except Exception:
            return self.DEFAULTS["port"]

    @property
    def ssh_user(self):
        return str(self._ssh("user") or self.DEFAULTS["user"])

    @property
    def work_dir(self):
        return str(self._ssh("work_dir") or self.DEFAULTS["work_dir"])

    @property
    def python(self):
        return str(self._ssh("python") or self.DEFAULTS["python"])

    # ---- 顶层标量 ----
    def _int(self, key, minimum=None):
        try:
            v = int(self._raw(key, self.DEFAULTS[key]))
        except Exception:
            v = self.DEFAULTS[key]
        return max(minimum, v) if minimum is not None else v

    @property
    def poll_interval(self):
        return self._int("poll_interval", minimum=3)

    @property
    def announce_interval(self):
        return self._int("announce_interval", minimum=5)

    @property
    def default_permission(self):
        return str(self._raw("default_permission", self.DEFAULTS["default_permission"]))

    @property
    def default_timeout_s(self):
        return self._int("default_timeout_s", minimum=1)

    @property
    def max_text_len(self):
        return self._int("max_text_len", minimum=50)

    @property
    def ssh_timeout_s(self):
        return self._int("ssh_timeout_s", minimum=5)

    @property
    def snapshot_path(self):
        """任务仪表盘快照文件路径（由 collect_dashboard.py 生成）。留空用插件目录默认值。"""
        return str(self._raw("snapshot_path", self.DEFAULTS["snapshot_path"]) or "")

    @property
    def verbose(self):
        try:
            return bool(self._raw("verbose", self.DEFAULTS["verbose"]))
        except Exception:
            return self.DEFAULTS["verbose"]

    @property
    def allowed_sessions(self):
        """允许使用插件的会话（unified_msg_origin）列表。"""
        val = self._raw("allowed_sessions", self.DEFAULTS["allowed_sessions"])
        if isinstance(val, str):
            return [s.strip() for s in val.splitlines() if s.strip()]
        if isinstance(val, (list, tuple)):
            return [str(s).strip() for s in val if str(s).strip()]
        return []

    def session_allowed(self, session: str) -> bool:
        """判断某会话是否允许使用。空列表 = 不限制；否则做包含匹配。"""
        allowed = self.allowed_sessions
        if not allowed:
            return True
        session = str(session or "")
        return any(a and (a == session or a in session) for a in allowed)
