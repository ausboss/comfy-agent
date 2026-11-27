#!/usr/bin/env bash
# First real-hardware check on one machine: build the pinned llama.cpp, fetch the pinned model, start the
# router and run probe_router.py. Everything goes under ~/pi-local-test and ~/models/pi-local; no sudo.
# Writes a report to ~/pi-local-test/first-check-<host>.txt. Usage (from anywhere):
#   bash first-check.sh [COMFY_URL]        default COMFY_URL http://127.0.0.1:8199
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
COMFY="${1:-http://127.0.0.1:8199}"
T=~/pi-local-test; M=~/models/pi-local/Qwen3.8-27B-UD-Q3_K_XL; mkdir -p "$T" "$M"
R="$T/first-check-$(hostname).txt"
exec > >(tee "$R") 2>&1
say() { echo; echo "== $*"; }

say "machine"; hostname; uname -sr; nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv
nvcc --version 2>/dev/null | tail -1 || echo "nvcc not on PATH"
df -h ~ | tail -1

say "is the GPU free?"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
curl -s -m 5 "$COMFY/queue" || echo "ComfyUI not answering at $COMFY"
echo
read -r -p "Continue only if nothing above is someone else's GPU work. Continue? [y/N] " ok
[ "$ok" = y ] || exit 1

say "llama.cpp b12808"
if [ ! -x "$T/llama.cpp/build/bin/llama-server" ]; then
  git clone --depth 1 --branch b12808 https://github.com/ggml-org/llama.cpp "$T/llama.cpp" &&
  cmake -S "$T/llama.cpp" -B "$T/llama.cpp/build" -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release &&
  cmake --build "$T/llama.cpp/build" -j"$(nproc)" --target llama-server || { echo "BUILD FAILED"; exit 1; }
fi
"$T/llama.cpp/build/bin/llama-server" --version

say "model"
base=https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/4af51b3542f775b0a8e4b310bfb3f17e01c306c9
for f in Qwen3.8-27B-UD-Q3_K_XL.gguf mmproj-F16.gguf; do
  [ -s "$M/$f" ] || curl -L --fail -C - -o "$M/$f" "$base/$f" || { echo "DOWNLOAD FAILED: $f"; exit 1; }
done
echo "7b20f0679da6f5c6ec7971e241630f44fe1e123e5c99bfea90c433268b355723  $M/Qwen3.8-27B-UD-Q3_K_XL.gguf" | sha256sum -c

say "router"
PORT=8080; curl -s -m 2 http://127.0.0.1:8080/health >/dev/null && PORT=8091
MODELS=~/models/pi-local LLAMA_SERVER="$T/llama.cpp/build/bin/llama-server" PORT=$PORT \
  nohup "$here/start-router.sh" > "$T/router.log" 2>&1 &
rp=$!
for i in $(seq 60); do curl -s -m 2 "http://127.0.0.1:$PORT/models" >/dev/null && break; sleep 1; done
echo "router pid $rp on :$PORT"

say "probe"
python3 "$here/probes/probe_router.py" --llm "http://127.0.0.1:$PORT" --model qwen38-27b --comfy "$COMFY" --rounds 3

say "clean up"
curl -s -X POST "http://127.0.0.1:$PORT/models/unload" -H 'Content-Type: application/json' -d '{"model":"qwen38-27b"}'; echo
sleep 3; kill "$rp" 2>/dev/null; sleep 3
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
echo; echo "Report saved to $R"
