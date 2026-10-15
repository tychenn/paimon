#!/usr/bin/env bash
# Run on the selected GPU node, inside the dedicated inference environment.
set -euo pipefail
: "${PAIMON_MODEL_DIR:?Set the pinned model snapshot directory}"
: "${PAIMON_MODEL_KEY_FILE:?Set the private API key file}"
: "${CUDA_VISIBLE_DEVICES:?Select the allocated GPUs explicitly}"
export VLLM_API_KEY
VLLM_API_KEY="$(cat "$PAIMON_MODEL_KEY_FILE")"
test -n "$VLLM_API_KEY"
# The native sampler avoids FlashInfer's extra CUDA compiler requirement.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
exec python -m vllm.entrypoints.openai.api_server \
  --model "$PAIMON_MODEL_DIR" \
  --served-model-name paimon-base \
  --host "${PAIMON_BIND_HOST:-127.0.0.1}" --port "${PAIMON_PORT:-18000}" \
  --tensor-parallel-size 2 --dtype bfloat16 \
  --max-model-len 32768 --max-num-seqs 1 \
  --gpu-memory-utilization 0.85 --enforce-eager \
  --reasoning-parser qwen3 --no-enable-log-requests --no-enable-log-outputs
