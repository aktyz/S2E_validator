#!/usr/bin/env bash
# Serve the judge models on a GPU machine, reachable from this machine only.
# Run as a sudo-capable user on Ubuntu with NVIDIA drivers installed.
#
#   bash deploy/judge-gpu.sh
#
# Then, from the laptop:  ssh -N -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 <user>@<ip>
# and the judge URLs are  http://localhost:8001/v1 and http://localhost:8002/v1
set -euo pipefail

# Two judges on one 48 GB L40S, each on its own port, 127.0.0.1 only. A
# submission is paid only if both would pay it (judge/unanimous.py):
#   S2E_JUDGE_URL=http://localhost:8001/v1         S2E_JUDGE_MODEL=<QWEN below>
#   S2E_JUDGE_SECOND_URL=http://localhost:8002/v1  S2E_JUDGE_SECOND_MODEL=<NEMOTRON below>
# Each server answers to its model's own name, so every verdict's provenance
# names the real models.
# The memory shares are what fits both: Qwen in BF16 (about 16.6 GB of
# weights) and Nemotron in NVIDIA's FP8 build (15.4 GB; the BF16 build, about
# 25 GB, leaves no room for the working memory of either). The L40S runs FP8
# natively.
QWEN="${QWEN:-Qwen/Qwen2.5-VL-7B-Instruct}"
NEMOTRON="${NEMOTRON:-nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-FP8}"
# The image the measurements in README.md ran on (vLLM 0.32.0), pinned by digest.
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34}"

nvidia-smi --query-gpu=name,memory.total --format=csv

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
if ! sudo docker info 2>/dev/null | grep -qi nvidia; then
  distribution=$(. /etc/os-release; echo "$ID$VERSION_ID")
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -q && sudo apt-get install -yq nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  echo "installed the NVIDIA container toolkit for $distribution"
fi

sudo mkdir -p /opt/judge/hf-cache
serve() {  # name port model memory-share [extra vLLM flags]
  local name=$1 port=$2 model=$3 share=$4; shift 4
  sudo docker rm -f "$name" 2>/dev/null || true
  # 127.0.0.1 only: the port is never on the public interface.
  sudo docker run -d --name "$name" --restart unless-stopped --gpus all \
    -p "127.0.0.1:$port:8000" \
    -v /opt/judge/hf-cache:/root/.cache/huggingface \
    --ipc=host \
    "$VLLM_IMAGE" \
    --model "$model" \
    --dtype bfloat16 \
    --max-model-len 8192 \
    --limit-mm-per-prompt '{"image": 1}' \
    --gpu-memory-utilization "$share" \
    "$@"
}

ready() {  # name port
  for _ in $(seq 1 180); do
    if curl -fs "http://127.0.0.1:$2/v1/models" >/dev/null; then
      echo "$1 ready on 127.0.0.1:$2"
      sudo docker inspect --format '{{.Config.Image}}' "$1"
      return 0
    fi
    if [ "$(sudo docker inspect -f '{{.State.Running}}' "$1")" != "true" ]; then break; fi
    sleep 10
  done
  sudo docker logs --tail 50 "$1"
  return 1
}

sudo docker rm -f judge 2>/dev/null || true  # the single-model container of earlier runs
echo "loading both models (the first run downloads about 32 GB)..."
# One after the other: each checks the GPU memory that is free when it starts.
serve judge-qwen 8001 "$QWEN" 0.47 --max-num-seqs 8
ready judge-qwen 8001 || exit 1
# Nemotron is a hybrid (Mamba) model: its state is reserved per concurrent
# request, and CUDA graphs need memory on top. The judge sends one request at
# a time, so few requests and no graphs fit it next to Qwen.
serve judge-nemotron 8002 "$NEMOTRON" 0.47 --trust-remote-code --quantization modelopt --max-num-seqs 8 --enforce-eager
ready judge-nemotron 8002 || exit 1
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
