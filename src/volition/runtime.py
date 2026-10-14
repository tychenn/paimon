"""One durable autonomous turn at a time, with strict feedback waiting."""

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

from volition.contracts import VolitionError
from volition.environment import Environment
from volition.model import Model, ModelError
from volition.store import Store


@contextmanager
def runner_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "runner.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VolitionError("已有运行进程使用这个状态目录。") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Runtime:
    """Caller holds runner_lock for the entire run/step/recovery operation."""

    def __init__(
        self, store: Store, model: Model, environment: Environment, history_limit: int = 20
    ):
        self.store = store
        self.model = model
        self.environment = environment
        self.history_limit = history_limit

    def step(self) -> dict:
        self.store.recover()
        state = self.store.state()
        if state["phase"] in {"AWAITING_FEEDBACK", "PAUSED"}:
            return state
        if state["phase"] == "PROCESSING_FEEDBACK":
            self.store.process_feedback()
            return self.store.state()
        if time.time() < state["next_tick_at"]:
            return state
        operation_id = self.store.begin()
        try:
            observation = self.environment.observe()
            self.store.stage(
                operation_id, "thinking", "environment.observed", observation.model_dump()
            )
            context = {
                **self.store.context(self.history_limit),
                "observation": observation.model_dump(),
            }
            reply = self.model.decide(context)
            decision = reply.decision
            self.store.stage(operation_id, "decided", "model.decision", reply.model_dump())
            self.store.validate_goal(decision.goal)
            result = None
            if decision.kind == "act":
                if decision.action.name not in observation.capabilities:
                    raise VolitionError("模型选择了当前环境未提供的动作。")
                self.store.stage(
                    operation_id,
                    "executing",
                    "action.requested",
                    {
                        "action": decision.action.model_dump(),
                        "observation_id": observation.id,
                        "environment_id": observation.environment_id,
                    },
                )
                result = self.environment.execute(decision.action, observation, operation_id)
            self.store.finish(operation_id, decision, result)
        except ModelError as exc:
            if exc.raw is not None:
                self.store.event("model.invalid_output", {"raw": exc.raw}, operation_id)
            self.store.fail(operation_id, str(exc))
        except VolitionError as exc:
            self.store.fail(operation_id, str(exc))
        except Exception as exc:
            # Do not serialize arbitrary exception strings, URLs or authorization values.
            self.store.fail(operation_id, f"执行失败（{type(exc).__name__}）；请检查经历记录。")
        except BaseException:
            self.store.fail(operation_id, "运行被中断；请检查未完成操作后恢复。")
            raise
        return self.store.state()
