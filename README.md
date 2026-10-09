# astrbot_plugin_bridge_server

在聊天里遥控远端 Windows 机器上 CLI agent 的 AstrBot 插件，服务器端那一半。

## 背景

CLI agent 通常只跑在本机的终端里，人一走开就看不见进度，也没法中途喊停。
本插件把远端机器上的 agent 接进聊天，用几条指令就能提交任务、看进度、取消，
执行过程的进度还会被轮询回来主动播报。

## 架构

```
聊天端  ->  AstrBot（本插件，跑在 Linux 服务器）
              |  SSH
              v
           Windows 端 runner.py
              |  子进程
              v
           dsh / codex 适配器
```

服务器端负责指令解析、SSH 调度、任务表和播报；Windows 端负责真正把任务起起来，
并把事件写进可轮询的落盘文件。两边通过 SSH 加命令行调用通信，不需要长连接。

## 依赖

- `paramiko`（见 requirements.txt），SSH 是阻塞 IO，统一用 `asyncio.to_thread` 包出去。
- Windows 端需要配套的 `runner.py`，本仓库不含，属于另一半。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/wg <任务>` | 提交任务给 dsh |
| `/wc <任务>` | 提交任务给 codex |
| `/wl` | 列出远端任务 |
| `/ww <task_id>` | 查看任务进度 |
| `/wx <task_id>` | 取消任务 |
| `/wd` | 任务仪表盘，纯本地读快照，不发起 SSH |

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ssh` | object | None | SSH 连接参数（目标 Windows 桥） |
| `poll_interval` | int | 15 | 轮询间隔（秒） |
| `announce_interval` | int | 30 | 播报限频间隔（秒） |
| `default_permission` | string | workspace-write | 默认权限档 |
| `default_timeout_s` | int | 600 | 任务默认超时（秒） |
| `max_text_len` | int | 1500 | 单条播报最大字符数 |
| `allowed_sessions` | list | [] | 允许使用插件的会话（unified_msg_origin，做包含匹配），留空表示不限制 |
| `ssh_timeout_s` | int | 30 | 单次 SSH 调用超时（秒） |
| `verbose` | bool | False | 是否额外播报思考过程等细节事件 |
| `snapshot_path` | string |  | 任务仪表盘快照文件路径（由 collect_dashboard.py 生成），留空则使用插件目录下的 dashboard_snapshot.json |
| `ssh.host` | string | 192.168.1.100 | 目标主机地址 |
| `ssh.port` | int | 22 | SSH 端口 |
| `ssh.user` | string | your_user | SSH 用户名 |
| `ssh.work_dir` | string | D:\path\to\agent | 远端工作目录（runner.py 所在目录） |
| `ssh.python` | string | python | 远端 Python 解释器命令 |
| `ssh` | object | - | 以下为其子项 |

## 安全

- 用 `allowed_sessions` 限定哪些会话能下指令，留空表示不限制。
- 连接远端走公钥认证，不使用明文口令。
- 远端返回的内容只当数据用，不当作命令执行。
- 已知待处理：远端 runner 侧对任务标识的校验还不够严，建议只在受控内网里暴露远端 SSH。

## 安装

1. 把本插件目录放进 AstrBot 的 `data/plugins/`。
2. 装依赖：`pip install -r requirements.txt`。
3. 在管理面板里填好远端主机、端口、用户名、工作目录和私钥路径。
4. 重载插件。

## 注意事项

- 只调度远端，不监控远端以外的东西。
- 进度靠轮询，实时性受 `poll_interval` 限制。
- 远端机器必须开着，且 SSH 可达；隧道或代理断了插件就联系不上。
- 任务表落在本地，进程重启后靠落盘状态恢复。

## 兼容性

支持 AstrBot v4.x，已在 aiocqhttp 平台验证。

## 许可

MIT
