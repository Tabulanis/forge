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
    MODEL=~/aidojo/models/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf
    PORT=8081; CTX=4096; NGL=99 ;;
  coder14)
    MODEL=~/llmmodels/Qwen2.5-Coder-14B-Instruct-abliterated-Q8_0.gguf
    PORT=8082; CTX=8192; NGL=99 ;;
  *) echo "unknown model: $1  (try: big, tiny, coder14)"; exit 1 ;;
esac

echo "starting $(basename "$MODEL") on port $PORT ..."
# --jinja is what makes tool calling work: it uses the model's own chat
# template, which is where the tool-call format lives.
exec "$LLAMA" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
  -ngl "$NGL" -c "$CTX" --jinja
