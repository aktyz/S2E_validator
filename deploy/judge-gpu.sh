#!/usr/bin/env bash
# Serve the judge model on a GPU machine, reachable from this machine only.
# Run as a sudo-capable user on Ubuntu with NVIDIA drivers installed.
#
#   bash deploy/judge-gpu.sh
#
# Then, from the laptop:  ssh -N -L 8001:127.0.0.1:8001 emmanuel@<ip>
# and the judge URL is   http://localhost:8001/v1
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-VL-7B-Instruct}"
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
sudo docker rm -f judge 2>/dev/null || true
# 127.0.0.1 only: the port is never on the public interface.
sudo docker run -d --name judge --restart unless-stopped --gpus all \
  -p 127.0.0.1:8001:8000 \
  -v /opt/judge/hf-cache:/root/.cache/huggingface \
  --ipc=host \
  "$VLLM_IMAGE" \
  --model "$MODEL" \
  --served-model-name judge \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --limit-mm-per-prompt '{"image": 1}' \
  --gpu-memory-utilization 0.9

echo "waiting for the model to load (first run downloads ~16 GB)..."
for _ in $(seq 1 120); do
  if curl -fs http://127.0.0.1:8001/v1/models >/dev/null; then
    curl -s http://127.0.0.1:8001/v1/models; echo
    sudo docker inspect --format '{{.Config.Image}} {{.Image}}' judge
    exit 0
  fi
  sleep 10
done
sudo docker logs --tail 50 judge
exit 1
