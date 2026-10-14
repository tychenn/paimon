import json

import pytest

from volition.contracts import Action, Decision, GoalChange, ModelReply, VolitionError
from volition.environment import WorkspaceEnvironment
from volition.model import ModelError
from volition.runtime import Runtime, runner_lock
from volition.store import Store


class CountingModel:
    def __init__(self, *decisions):
        self.decisions = iter(decisions)
        self.calls = 0
        self.contexts = []

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        decision = next(self.decisions)
        if isinstance(decision, BaseException):
            raise decision
        return ModelReply(
            decision=decision, raw=decision.model_dump_json(), model="test", latency_ms=0
        )


class CountingEnvironment(WorkspaceEnvironment):
    observations = 0
    actions = 0

    def observe(self):
        self.observations += 1
        return super().observe()

    def execute(self, *args):
        self.actions += 1
        return super().execute(*args)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state")
    yield value
    value.close()


@pytest.fixture
def environment(tmp_path):
    return CountingEnvironment(tmp_path / "notes", "test-environment")


def test_speech_survives_restart_and_blocks_all_activity(tmp_path, environment):
    model = CountingModel(Decision(kind="speak", message="继续吗？"))
    with runner_lock(tmp_path / "state"):
        first = Store(tmp_path / "state")
        state = Runtime(first, model, environment).step()
        pending = state["pending_message_id"]
        event_count = len(first.events(100))
        first.close()
    with runner_lock(tmp_path / "state"):
        restored = Store(tmp_path / "state")
        try:
            runtime = Runtime(restored, model, environment)
            for _ in range(3):
                assert runtime.step()["phase"] == "AWAITING_FEEDBACK"
            assert model.calls == environment.observations == 1
            assert environment.actions == 0
            assert len(restored.events(100)) == event_count
            with pytest.raises(VolitionError):
                restored.resume("不能绕过反馈")
            with pytest.raises(VolitionError):
                restored.reply("stale-message", "yes")
            reply = "  我有不同意见。\n请先说明理由。  "
            feedback_id = restored.reply(pending, reply)
            with pytest.raises(VolitionError):
                restored.reply(pending, "duplicate")
            assert runtime.step()["phase"] == "RUNNING"
            assert restored.message(feedback_id)["content"] == reply
            assert model.calls == environment.observations == 1
        finally:
            restored.close()


def test_real_action_and_feedback_reach_next_model_turn(store, environment):
    model = CountingModel(
        Decision(kind="act", action=Action(name="write_note", target="first.md", text="hello")),
        Decision(kind="speak", message="写好了。", memory_note="文件来自本轮测试。"),
        Decision(kind="wait", wait_seconds=60),
    )
    runtime = Runtime(store, model, environment)
    assert runtime.step()["phase"] == "RUNNING"
    assert (environment.directory / "first.md").read_text() == "hello"
    state = runtime.step()
    store.reply(state["pending_message_id"], "测试反馈：先等待。")
    runtime.step()
    runtime.step()
    assert model.contexts[-1]["latest_feedback"]["content"] == "测试反馈：先等待。"
    assert environment.actions == 1
    assert model.calls == 3
    runtime.step()  # A scheduled wait does not poll either model or desktop.
    assert model.calls == 3


@pytest.mark.parametrize("stage", ["observing", "thinking", "executing"])
def test_interrupted_operation_pauses_without_replay(store, environment, stage):
    operation = store.begin()
    store.stage(operation, stage, "test.interruption", {})
    model = CountingModel()
    runtime = Runtime(store, model, environment)
    assert runtime.step()["phase"] == "PAUSED"
    assert model.calls == environment.observations == environment.actions == 0
    failed = [event for event in store.events() if event["kind"] == "turn.failed"]
    assert failed[-1]["payload"]["action_outcome"] == (
        "unknown" if stage == "executing" else "not_started"
    )
    store.resume("测试中已检查环境，允许重新观察。")
    assert store.state()["active_operation"] is None
    assert environment.actions == 0


def test_ambiguous_action_failure_is_never_retried(store, environment):
    class LostResponse(CountingEnvironment):
        def execute(self, *args):
            super().execute(*args)
            raise TimeoutError("test response lost after actual write")

    environment = LostResponse(environment.directory, environment.environment_id)
    model = CountingModel(
        Decision(kind="act", action=Action(name="write_note", target="once.md", text="once"))
    )
    runtime = Runtime(store, model, environment)
    assert runtime.step()["phase"] == "PAUSED"
    runtime.step()
    assert (environment.directory / "once.md").read_text() == "once"
    assert environment.actions == model.calls == 1


def test_invalid_goal_does_not_execute_side_effect(store, environment):
    decision = Decision(
        kind="act",
        action=Action(name="write_note", target="bad.md", text="bad"),
        goal=GoalChange(id="missing", title="not created"),
    )
    assert Runtime(store, CountingModel(decision), environment).step()["phase"] == "PAUSED"
    assert environment.actions == 0
    assert not (environment.directory / "bad.md").exists()


def test_model_failure_preserves_raw_and_does_not_act(store, environment):
    model = CountingModel(ModelError("invalid test output", raw="broken-json"))
    assert Runtime(store, model, environment).step()["phase"] == "PAUSED"
    assert environment.actions == 0
    assert any(event["payload"] == {"raw": "broken-json"} for event in store.events())


def test_context_is_bounded_and_keeps_old_memory_and_feedback(store, environment, tmp_path):
    model = CountingModel(Decision(kind="speak", message="test", memory_note="durable"))
    state = Runtime(store, model, environment).step()
    store.reply(state["pending_message_id"], "original feedback")
    store.process_feedback()
    for _ in range(30):
        store.event("test.noise", {"large": "x" * 10000})
    context = store.context(100)
    assert sum(len(event["payload"]) for event in context["recent_events"]) <= 14000
    assert context["memories"][0]["text"] == "durable"
    assert context["latest_feedback"]["content"] == "original feedback"
    export = tmp_path / "experience.jsonl"
    store.export(export)
    assert len(export.read_text().splitlines()) > 30
    assert any(
        len(json.loads(line)["payload"].get("large", "")) == 10000
        for line in export.read_text().splitlines()
    )
    assert export.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        store.export(export)


def test_runner_lock_excludes_second_runner(tmp_path):
    with runner_lock(tmp_path):
        with pytest.raises(VolitionError), runner_lock(tmp_path):
            pytest.fail("second runner entered")


@pytest.mark.parametrize("target", ["../outside.md", "/tmp/outside.md", ".hidden.md", "x.py"])
def test_workspace_rejects_escape_and_unsupported_files(environment, target):
    with pytest.raises(VolitionError):
        environment.execute(
            Action(name="write_note", target=target, text="unsafe"), environment.observe(), "test"
        )


def test_workspace_rejects_symlink(environment, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("keep")
    (environment.directory / "link.md").symlink_to(outside)
    for name in ("write_note", "read_note"):
        with pytest.raises(VolitionError):
            environment.execute(
                Action(name=name, target="link.md", text="change"), environment.observe(), "test"
            )
    assert outside.read_text() == "keep"
