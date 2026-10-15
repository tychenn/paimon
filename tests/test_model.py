import json

import httpx
import pytest

from paimon.config import Config, DesktopConfig, ModelConfig
from paimon.contracts import Action, Observation, PaimonError
from paimon.environment import MacOSBridge
from paimon.model import ModelError, RemoteModel


def response(content='{"kind":"wait"}', finish="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "model": "test-model",
            "usage": {"completion_tokens": 10},
        },
    )


def test_structured_request_and_transient_retry(monkeypatch):
    requests = []
    monkeypatch.setattr("paimon.model.time.sleep", lambda _: None)
    monkeypatch.setenv("PAIMON_MODEL_API_KEY", "fake-test-token")

    def handle(request):
        requests.append(request)
        return httpx.Response(503) if len(requests) == 1 else response()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = RemoteModel(ModelConfig(model_id="test-model", enable_thinking=False), client)
        result = model.decide({"observation": {"kind": "workspace"}})
    assert result.attempts == 2
    assert requests[0].headers["Authorization"] == "Bearer fake-test-token"
    body = json.loads(requests[-1].content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "content,finish",
    [
        ("not-json", "stop"),
        ('{"kind":"wait","extra":true}', "stop"),
        ('{"kind":"speak","message":"hello","action":{"name":"click"}}', "stop"),
        ('{"kind":"wait"}', "length"),
        (None, "stop"),
    ],
)
def test_rejects_malformed_contradictory_or_truncated_outputs(content, finish):
    with httpx.Client(transport=httpx.MockTransport(lambda _: response(content, finish))) as client:
        with pytest.raises(ModelError):
            RemoteModel(ModelConfig(model_id="test"), client).decide({})


def test_non_retryable_auth_error_does_not_leak_server_message():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, text="secret-token-from-server")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ModelError) as error:
            RemoteModel(ModelConfig(model_id="test"), client).decide({})
    assert len(calls) == 1
    assert "secret-token" not in str(error.value)


def test_config_paths_and_validation_do_not_expose_input(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[runtime]\nstate_dir = "state"\n[model]\napi_key_file = "key"\n')
    config = Config.load(path)
    assert config.runtime.state_dir == str(tmp_path / "state")
    assert config.model.api_key_file == str(tmp_path / "key")
    path.write_text('[model]\nbase_url = "https://user:secret@example.com"\n')
    with pytest.raises(PaimonError) as error:
        Config.load(path)
    assert "secret" not in str(error.value)


def test_bridge_rejects_wrong_environment(monkeypatch):
    monkeypatch.setenv("PAIMON_DESKTOP_API_KEY", "fake-test-token")
    observation = Observation(
        id="frame", kind="macos_vm", environment_id="wrong-vm", capabilities=["click"], content={}
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=observation.model_dump()))
    ) as client:
        with pytest.raises(PaimonError, match="身份不匹配"):
            MacOSBridge(DesktopConfig(environment_id="expected-vm"), client).observe()


def test_bridge_never_retries_uncertain_action(monkeypatch):
    monkeypatch.setenv("PAIMON_DESKTOP_API_KEY", "fake-test-token")
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    observation = Observation(
        id="frame", kind="macos_vm", environment_id="test-vm", capabilities=["click"], content={}
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        bridge = MacOSBridge(DesktopConfig(environment_id="test-vm"), client)
        with pytest.raises(PaimonError, match="结果未知"):
            bridge.execute(Action(name="click", target="button-1"), observation, "operation-1")
    assert len(requests) == 1
    assert json.loads(requests[0].content)["observation_id"] == "frame"
