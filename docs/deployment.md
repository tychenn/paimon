# 远程模型部署

## 固定模型与独立环境

首个部署目标为官方 [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)，固定版本：

```text
1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
```

官方 [vLLM 部署指南](https://recipes.vllm.ai/Qwen/Qwen3.8-27B) 描述了推理及 thinking 配置。这里选择 BF16、两张 A40、32K 上下文、单并发作为初始方案；实际可用性和延迟需要在目标节点测试。

以下均在用户获准使用的远程环境执行。不要改动其他项目的 Python 环境或占用已有工作负载的 GPU。

```sh
mkdir -p ~/volition
python3.12 -m venv ~/volition/.venv-inference
~/volition/.venv-inference/bin/python -m pip install uv
~/volition/.venv-inference/bin/uv pip install \
  --python ~/volition/.venv-inference/bin/python 'vllm==0.31.0'
source ~/volition/.venv-inference/bin/activate
python scripts/download_model.py --directory ~/volition/models/Qwen3.8-27B
```

受网络限制时可使用 `scripts/download_verified_mirror.py`：它只接受固定版本的官方 Hugging Face 文件清单，并将官方 ModelScope 权重的大小和 SHA-256 与其比对，下载后再次校验。配置、模板和 tokenizer 仍从固定的 Hugging Face 版本获取。两个 API 清单分别来自：

- `https://huggingface.co/api/models/Qwen/Qwen3.8-27B/revision/<上述版本>?blobs=true`
- `https://modelscope.cn/api/v1/models/Qwen/Qwen3.8-27B/repo/files?Revision=master&Recursive=true`

请保留下载清单和最终的 `volition-manifest.json`。

## 启动和认证

将随机生成的 API key 放入仅当前用户可读的文件，避免写进版本库或命令行参数。服务脚本通过 `VLLM_API_KEY` 环境变量传给 vLLM，并关闭请求和输出日志。

```sh
export CUDA_VISIBLE_DEVICES=0,1
export VOLITION_MODEL_DIR="$HOME/volition/models/Qwen3.8-27B"
export VOLITION_MODEL_KEY_FILE="$HOME/volition/runtime/model-api.key"
bash scripts/serve_model.sh
```

脚本默认监听节点回环地址。推荐 SSH 转发到该节点的回环端口；如果从登录节点转发到计算节点的私网端口，需显式指定 `VOLITION_BIND_HOST` 为计算节点地址，继续保留 API 认证。

本地配置的 `model.base_url` 指向 SSH 隧道，例如 `http://127.0.0.1:18000/v1`，`model.model_id = "volition-base"`。将本地认证文件路径填入 `api_key_file`。`doctor --connect` 检查服务列表和模型 ID，随后用 `step` 验证真正的推理调用。

默认集成配置为 `enable_thinking = false`、最大输出 2048 token，便于先检查结构化决策链路。评估需要深度推理的行为时可启用 thinking，并增加输出预算与超时；此配置选择不代表对模型能力的评价。服务启用 `qwen3` reasoning parser，避免把 thinking 内容混入决策 JSON。

服务通过 `Ctrl+C` 或其独立会话停止。恢复后重用同一模型目录和认证文件。不要将下载完成、服务进程存在或 `/models` 可访问等同于真实推理已经验证。
