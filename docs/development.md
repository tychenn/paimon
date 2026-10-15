# 开发起点

## 当前交付

- Python 3.12 包结构与命令行入口。
- uv 依赖锁、Ruff 格式与静态检查。
- Linux 和 macOS 的持续集成配置。
- 本地 Mac、macOS 虚拟机、远程模型服务的配置占位。

本轮尚未启动任何虚拟机、下载模型、连接 GPU 服务器或实现自主行为。

## 目录

```text
src/paimon/           Python 包与开发检查命令
configs/               可公开的配置示例
docs/                  开发说明
.github/workflows/     自动检查
runtime/               本地运行数据（后续创建，Git 忽略）
```

后续按实际功能增加模块，避免先堆叠空实现。

## 下一步顺序

1. 接通一个远程模型端点，记录模型版本、延迟和调用失败。
2. 在本地 macOS 虚拟机中验证结构化界面观测和一个可复现的桌面动作；截图作为补充。
3. 实现持久化经历、用户消息与运行状态，让一次观测、选择、动作和反馈能够回放。
4. 实现主动发言后的等待状态：持续等待用户回复，重启后保持等待；收到回复后再处理反馈与恢复。
5. 在这条闭环上增加自主目标、外部记忆和真实参数更新。训练、评测、版本记录和加载均需要实际验证。

评价与情绪模块的网络结构、模块间通信、基础模型和训练方法继续保持开放。分别记录外部记忆变化、参数更新和运行机制改动。

## 本地检查

```sh
./dev sync --locked
./dev run ruff check .
./dev run ruff format --check .
./dev run python -m compileall -q src
./dev run paimon doctor --config configs/local.example.toml
./dev build --no-sources
```

当前只有环境骨架。实现持久化状态、模型连接和桌面动作时，再增加对应的行为测试与集成测试。

## 迁移到另一台 Mac

安装 Git 与 uv，重新克隆仓库后，按照 README 创建 Python 环境。不要复制 `.venv` 或 `.tools`；在新机器上重新生成。个人配置与将来的 `runtime/` 数据单独迁移，远程服务地址在本地配置中填写。

若系统 Git 或 Python 提示 Xcode 许可尚未接受，可以先使用已有的独立 Git/Python 发行版。后续开发原生 macOS 桌面桥接程序之前，需要在本机完成 Xcode 工具链配置。
