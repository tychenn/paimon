# Paimon

Paimon 探索一个拥有独立桌面、持续状态、自主目标和学习能力的 Agent。

当前实现了第一版运行内核：观测环境、调用模型选择行动/发言/等待、执行动作、保存经历。**主动发言后，所有新的自主活动持续暂停，直到收到对应回复；重启后仍然等待。**

## 当前能力

| 模块 | 实现与验证范围 |
| --- | --- |
| 运行循环与恢复 | 已实现；测试覆盖发言等待、回复关联、中断恢复和防止重复运行 |
| 经历、反馈、目标 | SQLite 事务保存原始记录；长期目标生成可读 Markdown；JSONL 完整导出 |
| 远程模型 | 支持 JSON Schema 输出、认证、超时、有限重试和无效输出暂停 |
| 独立文件环境 | 实际读取和写入工作区笔记；限制目录和文件类型 |
| macOS 虚拟机桥接 | 客户端及 guest 服务已实现；协议测试通过，真实桌面验收仍待完成 |
| 离线演示 | 固定脚本，只用于检查运行流程 |
| 参数训练、评价与情绪网络 | 尚未接入，配置不会将其报告为已实现 |

Qwen3.8-27B 是首个部署目标。模型下载、服务启动和真实推理分别验收，代码测试通过不代表远程模型已经可用。

## 快速体验运行流程

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```sh
git clone https://github.com/tychenn/paimon.git
cd paimon
uv python install 3.12
uv sync --locked
uv run paimon demo
```

演示会实际写下一条笔记，随后发言并等待输入。输入 `/exit` 或按 Ctrl+C 退出，等待状态保留。再次运行会继续等待同一条消息。使用新的 `--state-dir runtime/another-demo` 可建立另一段演示记录。

`./dev` 是 uv 的便捷入口，支持项目内 `.tools/bin/uv`。

## 使用真实模型

```sh
cp configs/local.example.toml configs/local.toml
# 按 docs/deployment.md 部署服务、建立 SSH 隧道并填写认证文件路径。
uv run paimon doctor --connect
uv run paimon init --goal "探索自己的环境，记录观察，并在有值得交流的内容时与我讨论。"
uv run paimon run
```

示例配置使用独立笔记工作区。接入虚拟机后，改用 `desktop.backend = "macos_vm"`，填写桥接身份和认证变量。路径统一相对于配置文件解析。

交互终端在 Agent 发言后接受普通文本回复；也可以在另一个终端操作：

```sh
uv run paimon status
uv run paimon reply "用户的原始回复" --to '<pending_message_id>'
uv run paimon history --limit 20
uv run paimon goals
uv run paimon export runtime/experiences.jsonl
```

`run --non-interactive` 用于独立进程，等待时只轮询回复状态。`step` 最多执行一轮。`run --max-steps 2` 限制推进次数，不能越过反馈等待。

## 中断与恢复

模型错误、非法动作和执行异常会进入 `PAUSED`。若程序在动作过程中崩溃，重启后保留“结果未知”，请先检查虚拟机或文件结果，再使用：

```sh
uv run paimon resume --note "已检查上次动作结果，允许重新观察。"
uv run paimon run
```

恢复后重新观察环境，旧动作不会自动重放。`resume` 无法跳过等待用户回复的状态。

## 数据和运行位置

| 位置 | 职责 |
| --- | --- |
| Mac 宿主 | 运行循环、终端交流、经历和目标 |
| 本地 macOS 虚拟机 | 自己的应用、文件、结构化控件观测与操作 |
| 远程 GPU 服务器 | 模型推理；后续加入真实参数更新 |

默认记录在 `runtime/live/paimon.sqlite3`，目标文档在 `runtime/live/long_term_goals.md`。目标文档由数据库生成，更新目标请使用命令或模型决策。原始反馈完整保存；送入模型的历史有长度限制。

个人配置、密钥、经历、截图、权重、虚拟机磁盘均被 Git 忽略。配置不会自动加载 `.env`。模型密钥可使用环境变量或仅当前用户可读的文件。

详见 [开发说明](docs/development.md)、[模型部署](docs/deployment.md) 和 [macOS 虚拟机桥接](docs/macos-guest.md)。
