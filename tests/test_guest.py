from types import SimpleNamespace

import pytest

from volition.contracts import Action, VolitionError
from volition.guest import ActionRequest, GuestService
from volition.guest_macos import require_virtual_mac


class Adapter:
    environment_id = "vm-test"
    validations = 0
    executions = 0
    fail = False

    def validate(self, request):
        self.validations += 1
        if request.observation_id != "frame-1":
            raise VolitionError("stale")

    def execute(self, request):
        self.executions += 1
        if self.fail:
            raise TimeoutError("after side effect")
        return {"accepted_by_os": True}


def request(**overrides):
    return ActionRequest.model_validate(
        {
            "operation_id": "one",
            "environment_id": "vm-test",
            "observation_id": "frame-1",
            "action": Action(name="click", target="1"),
            **overrides,
        }
    )


def test_guest_deduplicates_action_across_restart(tmp_path):
    adapter = Adapter()
    service = GuestService(adapter, tmp_path)
    first = service.execute(request())
    service.close()
    service = GuestService(adapter, tmp_path)
    try:
        assert service.execute(request()) == first
        assert adapter.executions == 1
        with pytest.raises(VolitionError):
            service.execute(request(action=Action(name="click", target="2")))
        assert adapter.executions == 1
    finally:
        service.close()


def test_guest_ambiguous_action_persists_and_is_not_replayed(tmp_path):
    adapter = Adapter()
    adapter.fail = True
    service = GuestService(adapter, tmp_path)
    with pytest.raises(TimeoutError):
        service.execute(request())
    service.close()
    service = GuestService(adapter, tmp_path)
    try:
        with pytest.raises(VolitionError, match="未知"):
            service.execute(request())
        assert adapter.executions == 1
    finally:
        service.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_id": "old-frame"},
        {"environment_id": "wrong-environment"},
    ],
)
def test_guest_rejects_stale_or_foreign_request(tmp_path, changes):
    adapter = Adapter()
    service = GuestService(adapter, tmp_path)
    try:
        with pytest.raises(VolitionError):
            service.execute(request(**changes))
        assert adapter.executions == 0
        assert service.db.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
    finally:
        service.close()


def test_host_guard_runs_without_accessing_desktop(monkeypatch):
    monkeypatch.setattr("volition.guest_macos.platform.system", lambda: "Darwin")
    monkeypatch.setattr("volition.guest_macos.subprocess.check_output", lambda *a, **kw: "Mac16,13")
    with pytest.raises(VolitionError, match="宿主"):
        require_virtual_mac()


def test_native_adapter_rejects_stale_frame_and_app_switch():
    from volition.guest_macos import MacOSAdapter

    adapter = object.__new__(MacOSAdapter)
    adapter.snapshot_id = "other-frame"
    with pytest.raises(VolitionError, match="过期"):
        adapter.validate(request())
    adapter.snapshot_id = "frame-1"
    adapter.observed_at = __import__("time").monotonic()
    adapter.frontmost_pid = 1
    app = SimpleNamespace(processIdentifier=lambda: 2)
    adapter.AppKit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(
            sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: app)
        )
    )
    with pytest.raises(VolitionError, match="前台应用"):
        adapter.validate(request())


def test_http_bridge_requires_auth_and_matches_client_contract(tmp_path):
    from http.server import HTTPServer
    from queue import Queue
    from threading import Thread

    import httpx

    from volition.config import DesktopConfig
    from volition.environment import MacOSBridge
    from volition.guest import handler_for

    servers = Queue()
    adapter = Adapter()

    def serve():
        service = GuestService(adapter, tmp_path)
        try:
            with HTTPServer(("127.0.0.1", 0), handler_for(service, "fake-test-token")) as server:
                servers.put(server)
                server.serve_forever(poll_interval=0.01)
        finally:
            service.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = servers.get(timeout=5)
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.post("/v1/actions", json=request().model_dump()).status_code == 401
            assert adapter.executions == 0
            headers = {"Authorization": "Bearer fake-test-token"}
            assert client.post("/v1/actions", headers=headers, json={}).status_code == 400
            first = client.post("/v1/actions", headers=headers, json=request().model_dump())
            assert first.status_code == 200
            second = client.post("/v1/actions", headers=headers, json=request().model_dump())
            assert second.json() == first.json()
            assert adapter.executions == 1
            # The shared contract is also parsed by the real host-side HTTP client.
            bridge = MacOSBridge(DesktopConfig(environment_id="vm-test", bridge_url=url), client)
            bridge.headers = lambda: headers
            observation = SimpleNamespace(environment_id="vm-test", id="frame-1")
            assert bridge.execute(request().action, observation, "one") == first.json()
            assert adapter.executions == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert not thread.is_alive()
