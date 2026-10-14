"""Run Volition and inspect its persistent experiences."""

import argparse
import json
import platform
import sys
import time
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path

from volition.config import Config, DesktopConfig, ModelConfig, RuntimeConfig
from volition.contracts import VolitionError
from volition.environment import MacOSBridge, WorkspaceEnvironment
from volition.model import RemoteModel, ScriptedModel
from volition.runtime import Runtime, runner_lock
from volition.store import Store


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def parser():
    root = argparse.ArgumentParser(prog="volition", description=__doc__)
    root.add_argument("--version", action="version", version=version("volition"))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("configs/local.toml"))
    common.add_argument("--state-dir", type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    items = {
        name: commands.add_parser(name, parents=[common])
        for name in (
            "doctor",
            "init",
            "run",
            "step",
            "status",
            "reply",
            "history",
            "goals",
            "export",
            "resume",
        )
    }
    items["doctor"].add_argument("--connect", action="store_true")
    items["init"].add_argument("--goal")
    items["run"].add_argument("--max-steps", type=int, default=0)
    items["run"].add_argument("--non-interactive", action="store_true")
    items["reply"].add_argument("text")
    items["reply"].add_argument("--to", required=True, help="Pending message ID from status.")
    items["history"].add_argument("--limit", type=int, default=20)
    items["goals"].add_argument("--add")
    items["export"].add_argument("path", type=Path)
    items["resume"].add_argument("--note", required=True)
    demo = commands.add_parser("demo", help="Explicit offline scripted demonstration.")
    demo.add_argument("--state-dir", type=Path, default=Path("runtime/demo"))
    demo.add_argument("--non-interactive", action="store_true")
    demo.add_argument("--max-steps", type=int, default=4)
    return root


def configuration(args):
    if args.command == "demo":
        directory = args.state_dir.resolve()
        return Config(
            runtime=RuntimeConfig(state_dir=str(directory)),
            model=ModelConfig(
                backend="scripted", script_path=str(Path(__file__).with_name("demo.json"))
            ),
            desktop=DesktopConfig(
                workspace_dir=str(directory / "workspace"), environment_id="volition-offline-demo"
            ),
        )
    config = Config.load(args.config)
    if args.state_dir:
        config.runtime.state_dir = str(args.state_dir.resolve())
    return config


def doctor(args):
    report = {
        "stage": "runtime_core",
        "version": version("volition"),
        "python": platform.python_version(),
        "platform": platform.system(),
        "parameter_training": "not_implemented",
    }
    if not args.config.exists():
        if args.connect:
            raise VolitionError("请先创建 configs/local.toml。")
        report["config"] = "not_found; copy configs/local.example.toml to configs/local.toml"
    else:
        config = configuration(args)
        report.update(
            config="valid",
            model_backend=config.model.backend,
            environment_backend=config.desktop.backend,
            connections="not_checked",
        )
        if args.connect:
            if config.model.backend != "remote":
                raise VolitionError("当前是离线演示配置，没有远程模型服务。")
            model = RemoteModel(config.model)
            try:
                report["model_service"] = model.probe()
            finally:
                model.close()
            output(report)
            return 0 if report["model_service"]["configured_model_available"] else 2
    output(report)
    return 0


def run_loop(runtime, config, args):
    store = runtime.store
    steps, announced = 0, None
    print("Volition 已启动。Ctrl+C 停止，经历和等待状态会保留。", flush=True)
    if config.model.backend == "scripted":
        print("当前是固定脚本的离线演示，没有调用神经模型或 macOS 桌面。", flush=True)
    while not args.max_steps or steps < args.max_steps:
        state = store.state()
        if state["phase"] == "PAUSED":
            print("运行已暂停：" + state["last_error"], flush=True)
            return 2
        if state["phase"] == "AWAITING_FEEDBACK":
            message = store.message(state["pending_message_id"])
            if announced != message["id"]:
                print("\nVolition：" + message["content"], flush=True)
                announced = message["id"]
            if args.non_interactive:
                time.sleep(config.runtime.tick_seconds)
                continue
            try:
                text = input("你（/exit 退出并保留等待状态）：")
            except EOFError:
                return 0
            if text == "/exit":
                return 0
            if text.strip():
                try:
                    store.reply(message["id"], text)
                except VolitionError as exc:
                    print(str(exc), flush=True)
            continue
        if time.time() < state["next_tick_at"]:
            time.sleep(
                min(config.runtime.tick_seconds, max(0.1, state["next_tick_at"] - time.time()))
            )
            continue
        runtime.step()
        steps += 1
        if store.state()["phase"] == "RUNNING":
            time.sleep(config.runtime.tick_seconds)
    state = store.state()
    if state["phase"] == "AWAITING_FEEDBACK":
        print("\nVolition：" + store.message(state["pending_message_id"])["content"], flush=True)
    output({"phase": state["phase"], "steps": steps})
    return 2 if state["phase"] == "PAUSED" else 0


def main():
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            return doctor(args)
        if getattr(args, "max_steps", 0) < 0 or getattr(args, "limit", 1) < 1:
            raise VolitionError("步数应大于等于零，记录数量应大于零。")
        config = configuration(args)
        with ExitStack() as stack:
            store = Store(Path(config.runtime.state_dir))
            stack.callback(store.close)
            if args.command == "init":
                if args.goal:
                    store.add_goal(args.goal)
                output({"state_dir": config.runtime.state_dir, "state": store.state()})
            elif args.command == "status":
                state = store.state()
                output(
                    {
                        **state,
                        "pending_message": store.message(state["pending_message_id"]),
                        "goals": store.goals(),
                    }
                )
            elif args.command == "reply":
                output(
                    {
                        "feedback_id": store.reply(args.to, args.text),
                        "phase": store.state()["phase"],
                    }
                )
            elif args.command == "history":
                output(store.events(args.limit))
            elif args.command == "goals":
                if args.add:
                    store.add_goal(args.add)
                output(store.goals())
            elif args.command == "export":
                store.export(args.path)
                output({"exported": str(args.path)})
            elif args.command == "resume":
                stack.enter_context(runner_lock(store.directory))
                store.recover()
                store.resume(args.note)
                output(store.state())
            else:
                stack.enter_context(runner_lock(store.directory))
                store.recover()
                if config.model.backend == "scripted":
                    model = ScriptedModel(Path(config.model.script_path))
                else:
                    model = RemoteModel(config.model)
                    stack.callback(model.close)
                if config.desktop.backend == "workspace":
                    environment = WorkspaceEnvironment(
                        Path(config.desktop.workspace_dir), config.desktop.environment_id
                    )
                else:
                    environment = MacOSBridge(config.desktop)
                    stack.callback(environment.close)
                runtime = Runtime(store, model, environment, config.runtime.history_limit)
                if args.command == "step":
                    state = runtime.step()
                    output({**state, "pending_message": store.message(state["pending_message_id"])})
                    return 2 if state["phase"] == "PAUSED" else 0
                return run_loop(runtime, config, args)
        return 0
    except VolitionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"文件或连接操作失败（{type(exc).__name__}）。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止。下次启动会读取持久状态。", file=sys.stderr)
        return 130
