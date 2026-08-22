#!/usr/bin/env bash
# Merge installer — Linux & macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/Tabulanis/Merge/master/install.sh -o install.sh
#   bash install.sh
#
# Design rules, in order:
#   1. UNFUCKUPABLE — no sudo, everything under one folder (~/merge),
#      re-running is always safe and just resumes/updates.
#   2. INTELLIGENT — picks a model your RAM can actually run; finds a free
#      port; never clobbers an existing config, only adds what's missing.
#   3. HONEST — every failure says what broke and the one thing to try next.
#
# Env overrides (all optional):
#   MERGE_HOME       install prefix          (default ~/merge)
#   MERGE_MODEL_B    model size in billions: 1.5 / 3 / 7 / 14  (default: by RAM)
#   MERGE_MODEL_URL  full .gguf URL, overrides the size table
#   MERGE_GPU        "vulkan" on Linux to try the GPU build     (default: cpu)
#   MERGE_SKIP_MODEL 1 = don't download weights (you'll wire your own)
set -euo pipefail

MERGE_HOME="${MERGE_HOME:-$HOME/merge}"
REPO_URL="https://github.com/Tabulanis/Merge"
LLAMA_API="https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓ \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗ %s\033[0m\n' "$*" >&2; printf '   %s\n' "${2:-}" >&2; exit 1; }

# ---------- preflight ------------------------------------------------------
say "Checking this machine"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in
  Linux)  PLAT=linux ;;
  Darwin) PLAT=mac ;;
  *) die "Unsupported OS: $OS" "Windows: use install.ps1, or WSL + this script." ;;
esac
case "$ARCH" in
  x86_64|amd64)  CPU=x64 ;;
  arm64|aarch64) CPU=arm64 ;;
  *) die "Unsupported CPU: $ARCH" ;;
esac
command -v curl >/dev/null || die "curl is required" \
  "$([ $PLAT = mac ] && echo 'It ships with macOS — this should not happen.' || echo 'Install it: sudo apt install curl  (or your distro equivalent)')"
command -v tar  >/dev/null || die "tar is required"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  fi
done
[ -n "$PY" ] || die "Python 3.10+ not found" \
  "$([ $PLAT = mac ] && echo 'Install it:  brew install python  (or from python.org)' || echo 'Install it:  sudo apt install python3 python3-venv  (or your distro equivalent)')"
ok "$OS/$ARCH, $($PY --version 2>&1)"

# RAM in GB (drives the model choice)
if [ $PLAT = linux ]; then
  RAM_GB=$(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1024 / 1024 ))
else
  RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
fi
ok "${RAM_GB}GB RAM"

# free disk where we'll install (model alone can be 9GB)
mkdir -p "$MERGE_HOME"
FREE_GB=$(df -Pk "$MERGE_HOME" | awk 'NR==2 {print int($4/1024/1024)}')
[ "$FREE_GB" -ge 15 ] || die "Only ${FREE_GB}GB free at $MERGE_HOME — need ~15GB" \
  "Free some space, or point MERGE_HOME at a bigger drive."

# ---------- the code -------------------------------------------------------
say "Getting Merge"
APP="$MERGE_HOME/app"
if [ -d "$APP/.git" ] && command -v git >/dev/null; then
  git -C "$APP" pull -q --ff-only || true   # offline re-run still works
  ok "updated existing checkout"
elif command -v git >/dev/null; then
  git clone -q --depth 1 "$REPO_URL" "$APP"
  ok "cloned"
else
  curl -fsSL "$REPO_URL/archive/refs/heads/master.tar.gz" -o "$MERGE_HOME/src.tgz" \
    || die "Couldn't download the code" "Is the network up? Re-run to retry."
  rm -rf "$APP"; mkdir -p "$APP"
  tar xzf "$MERGE_HOME/src.tgz" -C "$APP" --strip-components=1 && rm "$MERGE_HOME/src.tgz"
  ok "downloaded (no git found — re-runs re-download; installing git enables updates)"
fi

say "Setting up Python (first run takes a few minutes — the science libraries are big)"
VENV="$MERGE_HOME/venv"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" \
  || die "Couldn't create a Python venv" "On Debian/Ubuntu:  sudo apt install python3-venv  — then re-run."
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install -e "$APP" \
  || die "Python dependencies failed to install" "Usually network. Re-run — it resumes where it stopped."
ok "forge installed into its own venv (nothing touched system Python)"

# ---------- llama-server ---------------------------------------------------
say "Getting the model server (llama.cpp prebuilt — nothing to compile)"
LLAMA_DIR="$MERGE_HOME/llama"
# release archives have shipped both bin/llama-server and a flat layout —
# look for the binary wherever it landed before deciding to download
BIN=$(find "$LLAMA_DIR" -name llama-server -type f 2>/dev/null | head -1)
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
  TAG=$(curl -fsSL "$LLAMA_API" | "$VENV/bin/python" -c '
import json,sys
for r in json.load(sys.stdin):
    if len(r.get("assets",[])) > 3: print(r["tag_name"]); break') \
    || die "Couldn't reach GitHub for llama.cpp" "Re-run when the network is back."
  case "$PLAT-$CPU-${MERGE_GPU:-cpu}" in
    mac-arm64-*)      ASSET="llama-$TAG-bin-macos-arm64.tar.gz" ;;   # Metal GPU comes free
    mac-x64-*)        ASSET="llama-$TAG-bin-macos-x64.tar.gz" ;;
    linux-x64-vulkan) ASSET="llama-$TAG-bin-ubuntu-vulkan-x64.tar.gz" ;;
    linux-x64-*)      ASSET="llama-$TAG-bin-ubuntu-x64.tar.gz" ;;
    linux-arm64-*)    ASSET="llama-$TAG-bin-ubuntu-arm64.tar.gz" ;;
  esac
  curl -fL --retry 3 --progress-bar -o "$MERGE_HOME/llama.tgz" \
    "https://github.com/ggml-org/llama.cpp/releases/download/$TAG/$ASSET" \
    || die "Couldn't download $ASSET" "Re-run to retry. If it keeps failing, file an issue with this asset name."
  rm -rf "$LLAMA_DIR"; mkdir -p "$LLAMA_DIR"
  tar xzf "$MERGE_HOME/llama.tgz" -C "$LLAMA_DIR" --strip-components=1 && rm "$MERGE_HOME/llama.tgz"
  [ -x "$BIN" ] || BIN=$(find "$LLAMA_DIR" -name llama-server -type f | head -1)
  [ -n "$BIN" ] && [ -x "$BIN" ] || die "llama-server missing from the archive" "The release layout changed — file an issue naming tag $TAG."
  ok "llama-server $TAG"
else
  ok "llama-server already present"
fi

# ---------- the model ------------------------------------------------------
if [ "${MERGE_SKIP_MODEL:-0}" != 1 ]; then
  if [ -z "${MERGE_MODEL_URL:-}" ]; then
    if   [ -n "${MERGE_MODEL_B:-}" ]; then B="$MERGE_MODEL_B"
    elif [ "$RAM_GB" -ge 30 ]; then B=14
    elif [ "$RAM_GB" -ge 14 ]; then B=7
    elif [ "$RAM_GB" -ge 7 ];  then B=3
    else B=1.5; fi
    MERGE_MODEL_URL="https://huggingface.co/bartowski/Qwen2.5-${B}B-Instruct-GGUF/resolve/main/Qwen2.5-${B}B-Instruct-Q4_K_M.gguf"
    if [ -n "${MERGE_MODEL_B:-}" ]; then say "Model (your choice): Qwen2.5-${B}B"
    else say "Model for ${RAM_GB}GB RAM: Qwen2.5-${B}B (override with MERGE_MODEL_B=1.5|3|7|14)"; fi
  fi
  MODEL_FILE="$MERGE_HOME/models/$(basename "$MERGE_MODEL_URL")"
  mkdir -p "$MERGE_HOME/models"
  WANT=$(curl -fsIL "$MERGE_MODEL_URL" | tr -d '\r' | awk 'tolower($1)=="content-length:" {n=$2} END {print n+0}')
  HAVE=$([ -f "$MODEL_FILE" ] && wc -c < "$MODEL_FILE" || echo 0)
  if [ "$WANT" -gt 0 ] && [ "$HAVE" -eq "$WANT" ]; then
    ok "model already downloaded ($(( WANT /1024/1024 ))MB, size verified)"
  else
    say "Downloading the model ($(( WANT /1024/1024 ))MB — resumes if interrupted)"
    curl -fL --retry 3 --progress-bar -C - -o "$MODEL_FILE" "$MERGE_MODEL_URL" \
      || die "Model download failed" "Just re-run — it continues from where it stopped."
    HAVE=$(wc -c < "$MODEL_FILE")
    [ "$HAVE" -eq "$WANT" ] || die "Model file is incomplete ($HAVE of $WANT bytes)" "Re-run to resume the download."
    ok "model verified byte-for-byte"
  fi
fi

# ---------- config (merge, never clobber) ---------------------------------
say "Wiring the config"
PORT=$("$VENV/bin/python" - <<'PY'
import socket
for p in range(8080, 8100):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); s.close(); print(p); break
    except OSError:
        pass
PY
)
MODEL_BASENAME="$( [ "${MERGE_SKIP_MODEL:-0}" != 1 ] && basename "$MERGE_MODEL_URL" || echo "" )" \
PORT="$PORT" "$VENV/bin/python" - <<'PY'
import os, pathlib, yaml
cfgp = pathlib.Path.home() / ".forge" / "config.yaml"
cfgp.parent.mkdir(exist_ok=True)
cfg = yaml.safe_load(cfgp.read_text()) if cfgp.exists() else {}
cfg = cfg or {}
models = cfg.setdefault("models", {})
port, name = os.environ["PORT"], os.environ.get("MODEL_BASENAME", "")
if "merge" not in models:            # add, NEVER overwrite someone's entry
    models["merge"] = {"provider": "openai-compat", "model": name or "local-model",
                       "base_url": f"http://127.0.0.1:{port}/v1", "max_tokens": 4096}
cfg.setdefault("active_model", "merge")
cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
print(f"   config: {cfgp}  (model entry 'merge' -> port {port})")
PY

# ---------- start / stop ---------------------------------------------------
cat > "$MERGE_HOME/start-merge.sh" <<START
#!/usr/bin/env bash
# Start Merge: model server + web dashboard. Re-running is safe.
set -e
cd "$MERGE_HOME"
mkdir -p run
if ! curl -s -m 2 http://127.0.0.1:$PORT/health >/dev/null 2>&1; then
  nohup "$BIN" -m "models/$(basename "${MERGE_MODEL_URL:-none}")" \\
      --port $PORT --host 127.0.0.1 --jinja -c 8192 > run/llama.log 2>&1 &
  echo \$! > run/llama.pid
  echo "model server starting on :$PORT (first load takes ~a minute)"
fi
exec "$VENV/bin/forge-dash"
START
cat > "$MERGE_HOME/stop-merge.sh" <<'STOP'
#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f run/llama.pid ] && kill "$(cat run/llama.pid)" 2>/dev/null && rm run/llama.pid && echo "model server stopped"
echo "dashboard stops with Ctrl-C in its own window"
STOP
chmod 755 "$MERGE_HOME/start-merge.sh" "$MERGE_HOME/stop-merge.sh"

say "Done."
printf '\n  Start Merge:   %s\n  Stop:          %s\n  Everything lives in %s — delete that folder to uninstall.\n  Re-run this installer any time to update; it never breaks an existing setup.\n\n' \
  "$MERGE_HOME/start-merge.sh" "$MERGE_HOME/stop-merge.sh" "$MERGE_HOME"
