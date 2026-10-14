import json
import os
import subprocess
import sys

from volition.store import Store


def invoke(*args):
    # Exercise the actual console entry function in a fresh process.
    return subprocess.run(
        [sys.executable, "-c", "from volition.cli import main; raise SystemExit(main())", *args],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "VOLITION_MODEL_API_KEY": ""},
    )


def test_cli_restart_reply_and_full_export(tmp_path):
    config = tmp_path / "config.toml"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            [
                {"kind": "speak", "message": "CLI integration test", "goal": {"title": "测试目标"}},
                {"kind": "wait", "wait_seconds": 600},
            ]
        )
    )
    config.write_text(
        '[runtime]\nstate_dir = "state"\n[model]\nbackend = "scripted"\n'
        'script_path = "decisions.json"\n[desktop]\nworkspace_dir = "notes"\n'
    )
    common = ["--config", str(config)]
    first = invoke("step", *common)
    assert first.returncode == 0, first.stderr
    pending = json.loads(first.stdout)["pending_message_id"]
    second = invoke("step", *common)
    assert json.loads(second.stdout)["pending_message_id"] == pending
    store = Store(tmp_path / "state")
    try:
        assert store.context(20)["decision_count"] == 1
    finally:
        store.close()
    assert "测试目标" in (tmp_path / "state/long_term_goals.md").read_text()
    reply = invoke("reply", "测试反馈原文", "--to", pending, *common)
    assert reply.returncode == 0, reply.stderr
    assert json.loads(reply.stdout)["phase"] == "PROCESSING_FEEDBACK"
    assert json.loads(invoke("step", *common).stdout)["phase"] == "RUNNING"
    assert invoke("step", *common).returncode == 0
    export = tmp_path / "all.jsonl"
    assert invoke("export", str(export), *common).returncode == 0
    events = [json.loads(line) for line in export.read_text().splitlines()]
    assert any(
        event["kind"] == "feedback.received" and event["payload"]["text"] == "测试反馈原文"
        for event in events
    )
