#!/usr/bin/env bash
# Start a local model for Forge to talk to.
#   ./start-model.sh          -> the 30B (main workhorse, port 8080)
#   ./start-model.sh tiny     -> TinyLlama (small + fast, port 8081)
set -e
LLAMA=~/llama.cpp/build/bin/llama-server

case "${1:-big}" in
  big)
    MODEL=~/llama-agent/Huihui-Qwen3-30B-A3B-Instruct-2507-abliterated-Q4_K_M.gguf
    PORT=8080; CTX=16384; NGL=99 ;;
  tiny)
    # -ngl 0 keeps this one entirely in system RAM on the CPU, leaving the
    # whole GPU for the big model. That's the point of a small router model:
    # it costs nothing the big one needs.
    MODEL=~/aidojo/models/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf
    PORT=8081; CTX=4096; NGL=0 ;;
  coder14)
    MODEL=~/llmmodels/Qwen2.5-Coder-14B-Instruct-abliterated-Q8_0.gguf
    PORT=8082; CTX=8192; NGL=99 ;;
  vision)
    # The eyes. Needs BOTH files: the language model and the mmproj, which
    # is the part that turns pixels into something the model can read.
    # A few GPU layers only — the big coder model owns most of the VRAM.
    MODEL=~/forge/models/qwen2.5-vl-7b-q4.gguf
    MMPROJ=~/forge/models/qwen2.5-vl-7b-mmproj.gguf
    PORT=8090; CTX=8192; NGL=12 ;;
  *) echo "unknown model: $1  (try: big, tiny, coder14, vision)"; exit 1 ;;
esac

if [ -n "${MMPROJ:-}" ]; then
  echo "starting $(basename "$MODEL") with vision on port $PORT ..."
  exec "$LLAMA" -m "$MODEL" --mmproj "$MMPROJ" --host 127.0.0.1 --port "$PORT" \
    -ngl "$NGL" -c "$CTX" --jinja
fi

echo "starting $(basename "$MODEL") on port $PORT ..."
# --jinja is what makes tool calling work: it uses the model's own chat
# template, which is where the tool-call format lives.
exec "$LLAMA" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
  -ngl "$NGL" -c "$CTX" --jinja
