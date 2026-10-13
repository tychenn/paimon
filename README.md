# Volition

Volition 探索一个拥有独立桌面、持续状态、自主目标和学习能力的 Agent。

**当前阶段：重新实现前的开发环境。** 旧代码和运行数据已从当前分支移除，历史提交保留。当前提供 Python 包、环境检查命令、配置模板和持续集成；桌面操作、模型调用、自主循环、记忆和参数训练将逐步实现。

## 开始开发

使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```sh
git clone https://github.com/tychenn/volition.git
cd volition
uv python install 3.12
uv sync --locked
uv run volition doctor --config configs/local.example.toml
```

仓库提供 `./dev` 作为 uv 入口：优先使用 PATH 中的 uv，也支持项目内 `.tools/bin/uv`。例如：

```sh
./dev sync --locked
./dev run volition doctor
./dev run ruff check .
./dev run ruff format --check .
./dev build --no-sources
```

`doctor` 检查本地 Python、SQLite 和可选 TOML 文件的语法。退出成功表示开发基础可用；它会明确报告尚未实现的连接与训练功能。

## 运行位置规划

| 位置 | 后续职责 |
| --- | --- |
| 本地 Mac 宿主 | 运行调度、用户交流、持续状态与经历记录 |
| 本地 macOS 虚拟机 | Volition 自己的桌面、结构化界面观测与动作执行 |
| 远程 GPU 服务器 | 模型推理、参数训练与版本产物 |

本地核心包暂时只依赖 Python 标准库。GPU 推理与训练依赖会在接入时单独定义。

## 配置与数据

- `configs/local.example.toml`：未来连接方式的占位模板。
- `configs/local.toml`：个人配置，可从模板复制，已被 Git 忽略。
- `runtime/`：预留运行数据目录，已被 Git 忽略。
- API key 通过环境变量接入，模板仅保存变量名；当前尚未实现连接器或自动读取 `.env`。

请将真实凭据、对话经历、截图、模型权重和虚拟机磁盘保存在 Git 之外。运行时记忆与开发说明分别管理。

参见 [开发约定与下一步](docs/development.md)。
