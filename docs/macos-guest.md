# macOS 虚拟机与桌面桥接

## 虚拟机准备

使用官方 [UTM](https://mac.getutm.app/) 的 Apple Virtualization，选择 macOS 12+，从 Apple 下载恢复镜像。参见 [UTM macOS 客体说明](https://docs.getutm.app/guest-support/macos/)。16GB 宿主的初始配置为 6GB 内存、4 核、64GB 虚拟磁盘；实际磁盘占用会随安装和使用增长。

关闭宿主目录共享，不登录个人 Apple ID。系统许可、账户凭据和权限由用户处理。完成安装后给虚拟机建立独立用户，再安装 Python 3.12 和 uv。

## 在虚拟机内安装

```sh
git clone https://github.com/tychenn/volition.git
cd volition
uv sync --locked --extra guest
```

创建随机 bridge key 文件并设为权限 `600`。在虚拟机的“系统设置 → 隐私与安全性 → 辅助功能”中，为运行服务的终端授予权限，再启动：

```sh
uv run --extra guest volition-guest \
  --environment-id volition-macos-vm \
  --key-file /absolute/path/to/guest-api.key
```

服务先检查 `hw.model` 是否为 `VirtualMac`，通过后才加载桌面接口。在宿主上运行会拒绝启动。此检查用于防止部署位置出错；身份仍应结合实际虚拟机和 SSH 连接检查。

服务仅监听虚拟机内部 `127.0.0.1:18765`。通过用户授权的 SSH 转发连接，宿主端设置 `VOLITION_DESKTOP_API_KEY`，配置：

```toml
[desktop]
backend = "macos_vm"
environment_id = "volition-macos-vm"
bridge_url = "http://127.0.0.1:18765"
api_key_env = "VOLITION_DESKTOP_API_KEY"
```

## 当前原生动作

通过 [Apple 辅助功能接口](https://developer.apple.com/documentation/applicationservices) 和 [PyObjC](https://pyobjc.readthedocs.io/en/latest/apinotes/ApplicationServices.html) 读取当前应用的控件角色、标题、文本、可用性和支持的动作。当前支持：

- `click`：对本次观察中的控件执行 `AXPress`。
- `type_text`：设置可编辑控件的 `AXValue`，替换其当前值。
- `open_url`：让系统浏览器打开 HTTP(S) URL。

暂未接入截图、坐标点击、快捷键和滚动；不可访问的控件不会假定可操作。密码控件不读取值，不接受文本填写。观察有数量、深度、文本和时间上限；前台应用改变或观察过期时拒绝使用旧目标。

`accepted_by_os` 表示系统接口接受了动作。下一轮观察才用于确认实际界面效果。截图、结构化控件和动作结果在未来可以共同进入观察协议。

## 端点与恢复

所有请求必须带 Bearer key，不启用 CORS。

- `GET /v1/observation` 返回 `Observation`。
- `POST /v1/actions` 接收操作 ID、环境 ID、观察 ID 和 `Action`。

动作执行前先将操作 ID 和请求摘要写入 SQLite。同一请求重复到达时返回已保存结果；服务在动作中崩溃时保留未知状态，拒绝再次执行。恢复主循环前需要检查实际界面。

当前协议、恢复和宿主拒绝逻辑已自动测试。真实 VM 的安装、权限、控件读取与动作效果需要单独完成现场验收。
