"""Apple Accessibility adapter, restricted to an Apple Virtualization macOS guest."""

import json
import platform
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

from paimon.contracts import Observation, PaimonError


def require_virtual_mac():
    if platform.system() != "Darwin":
        raise PaimonError("桥接服务需要在 macOS 虚拟机内运行。")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    if not model.startswith("VirtualMac"):
        raise PaimonError("检测到宿主或未验证的机型，拒绝访问桌面。")


class MacOSAdapter:
    def __init__(self, environment_id):
        require_virtual_mac()
        import AppKit
        import ApplicationServices as AX
        from Foundation import NSURL

        self.AX, self.AppKit, self.NSURL = AX, AppKit, NSURL
        if not AX.AXIsProcessTrusted():
            raise PaimonError("请先在虚拟机系统设置中为运行终端授予辅助功能权限。")
        self.environment_id = environment_id
        self.elements = {}
        self.snapshot_id = None
        self.observed_at = 0
        self.frontmost_pid = None

    def attribute(self, element, name, default=None):
        error, value = self.AX.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == 0 else default

    def observe(self):
        app = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            raise PaimonError("当前没有可访问的前台应用。")
        pid = app.processIdentifier()
        root = self.AX.AXUIElementCreateApplication(pid)
        self.AX.AXUIElementSetMessagingTimeout(root, 1.0)
        elements, nodes = {}, []
        queue = [(root, None, 0)]
        started = time.monotonic()
        while (
            queue
            and len(nodes) < 120
            and time.monotonic() - started < 8
            and len(json.dumps(nodes, ensure_ascii=False)) < 10000
        ):
            element, parent, depth = queue.pop(0)
            node_id = str(len(nodes))
            role = str(self.attribute(element, "AXRole", "unknown"))
            secure = self.attribute(element, "AXSubrole", "") == "AXSecureTextField"
            error, actions = self.AX.AXUIElementCopyActionNames(element, None)
            error_set, settable = self.AX.AXUIElementIsAttributeSettable(element, "AXValue", None)
            node = {
                "id": node_id,
                "parent": parent,
                "role": role,
                "title": str(self.attribute(element, "AXTitle", ""))[:300],
                "description": str(self.attribute(element, "AXDescription", ""))[:300],
                "enabled": bool(self.attribute(element, "AXEnabled", False)),
                "pressable": error == 0 and "AXPress" in (actions or []),
                "editable": error_set == 0 and bool(settable) and not secure,
            }
            value = self.attribute(element, "AXValue", "") if not secure else "[secure]"
            if isinstance(value, (str, int, float, bool)):
                node["value"] = str(value)[:1500]
            nodes.append(node)
            elements[node_id] = (element, node)
            if depth < 10 and not secure:
                children = self.attribute(element, "AXChildren", []) or []
                queue.extend((child, node_id, depth + 1) for child in list(children)[:120])
        self.elements = elements
        self.snapshot_id = str(uuid4())
        self.observed_at = time.monotonic()
        self.frontmost_pid = pid
        return Observation(
            id=self.snapshot_id,
            environment_id=self.environment_id,
            kind="macos_vm",
            capabilities=["click", "type_text", "open_url"],
            content={
                "application": str(app.localizedName()),
                "pid": pid,
                "nodes": nodes,
                "truncated": bool(queue),
                "action_semantics": "click=AXPress; type_text replaces AXValue; "
                "open_url opens an HTTP(S) URL in the default browser",
            },
        )

    def validate(self, request):
        if request.observation_id != self.snapshot_id or time.monotonic() - self.observed_at > 180:
            raise PaimonError("观察已过期，请重新观察。")
        app = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None or app.processIdentifier() != self.frontmost_pid:
            raise PaimonError("前台应用已改变，请重新观察。")
        action = request.action
        if action.name == "open_url":
            url = urlsplit(action.target)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
            ):
                raise PaimonError("仅支持不含认证信息的 HTTP(S) URL。")
            return
        if action.name not in {"click", "type_text"} or action.target not in self.elements:
            raise PaimonError("动作或目标不在当前观察中。")
        element, node = self.elements[action.target]
        if not self.attribute(element, "AXEnabled", False):
            raise PaimonError("目标控件当前不可用。")
        if action.name == "click" and not node["pressable"]:
            raise PaimonError("目标控件不支持 AXPress。")
        if action.name == "type_text" and not node["editable"]:
            raise PaimonError("目标控件不可编辑。")

    def execute(self, request):
        action = request.action
        self.snapshot_id = None  # Every mutation consumes its observation.
        if action.name == "open_url":
            url = self.NSURL.URLWithString_(action.target)
            if url is None or not self.AppKit.NSWorkspace.sharedWorkspace().openURL_(url):
                raise PaimonError("系统未接受打开 URL 的请求。")
        else:
            element, _ = self.elements[action.target]
            if action.name == "click":
                error = self.AX.AXUIElementPerformAction(element, "AXPress")
            else:
                error = self.AX.AXUIElementSetAttributeValue(element, "AXValue", action.text)
            if error != 0:
                raise PaimonError(f"辅助功能动作失败（AXError {error}）；请重新观察确认结果。")
        return {
            "accepted_by_os": True,
            "verified_effect": False,
            "next_step": "observe to verify the visible result",
        }
