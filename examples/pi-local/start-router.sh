#!/usr/bin/env bash
# Start llama.cpp's server in router mode for Pi on a GPU shared with ComfyUI.
#   MODELS=~/models/pi-local LLAMA_SERVER=~/llama.cpp/build/bin/llama-server ./start-router.sh
# --no-models-autoload: the server never loads a model by itself, so a stray chat request during a render
#   gets "400 model is not loaded" instead of loading into a full card. comfy-agent loads it back.
# --models-max 1: never two models on the card.
# Deliberately not used: --sleep-idle-seconds (a sleeping model wakes on any request, bypassing the
#   lease) and --models-memory-margin (the router would make its own unload decisions).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
MODELS="${MODELS:-$HOME/models/pi-local}"
LLAMA_SERVER="${LLAMA_SERVER:-$(command -v llama-server || echo "$HOME/llama.cpp/build/bin/llama-server")}"
PORT="${PORT:-8080}"
preset="$(mktemp --suffix .ini)"
sed "s#\${MODELS}#$MODELS#g" "$here/router.ini" > "$preset"
exec "$LLAMA_SERVER" --host 127.0.0.1 --port "$PORT" \
  --models-preset "$preset" --no-models-autoload --models-max 1 "$@"
