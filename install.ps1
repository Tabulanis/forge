# Merge installer — Windows 10/11 (PowerShell).
#
#   Right-click > Run with PowerShell, or from a terminal:
#     powershell -ExecutionPolicy Bypass -File install.ps1
#
# Same rules as install.sh: no admin rights, everything under one folder
# ($env:USERPROFILE\merge), re-running is always safe and just resumes.
#
# Honesty note: the Linux/macOS installer is the battle-tested one. If this
# script fights you, the reliable route is WSL:  wsl --install , then run
# install.sh inside it.
#
# Overrides:  $env:MERGE_HOME, $env:MERGE_MODEL_B (1.5|3|7|14), $env:MERGE_MODEL_URL
$ErrorActionPreference = "Stop"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host " OK $m" -ForegroundColor Green }
function Die($m, $fix) {
    Write-Host " X  $m" -ForegroundColor Red
    if ($fix) { Write-Host "    $fix" }
    exit 1
}

$MergeHome = if ($env:MERGE_HOME) { $env:MERGE_HOME } else { Join-Path $env:USERPROFILE "merge" }
$RepoUrl   = "https://github.com/Tabulanis/Merge"

# ---------- preflight ------------------------------------------------------
Say "Checking this machine"
$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "py -3", "python")) {
    try {
        $v = Invoke-Expression "$c -c `"import sys; print(sys.version_info>=(3,10))`"" 2>$null
        if ("$v" -match "True") { $py = $c; break }
    } catch {}
}
if (-not $py) { Die "Python 3.10+ not found" "Install from python.org or the Microsoft Store, then re-run." }
Ok "Python found ($py)"

$ramGB = [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Ok "$ramGB GB RAM"
New-Item -ItemType Directory -Force -Path $MergeHome | Out-Null
$freeGB = [int]((Get-PSDrive (Split-Path $MergeHome -Qualifier).TrimEnd(':')).Free / 1GB)
if ($freeGB -lt 15) { Die "Only $freeGB GB free — need ~15GB" "Free space or set MERGE_HOME to another drive." }

# ---------- the code -------------------------------------------------------
Say "Getting Merge"
$App = Join-Path $MergeHome "app"
if ((Test-Path (Join-Path $App ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    git -C $App pull --ff-only 2>$null | Out-Null; Ok "updated existing checkout"
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    git clone --depth 1 $RepoUrl $App | Out-Null; Ok "cloned"
} else {
    $zip = Join-Path $MergeHome "src.zip"
    curl.exe -fsSL "$RepoUrl/archive/refs/heads/master.zip" -o $zip
    if (Test-Path $App) { Remove-Item -Recurse -Force $App }
    Expand-Archive $zip -DestinationPath $MergeHome -Force
    Move-Item (Join-Path $MergeHome "Merge-master") $App
    Remove-Item $zip
    Ok "downloaded (installing git enables updates on re-run)"
}

Say "Setting up Python (first run takes a few minutes — the science libraries are big)"
$Venv = Join-Path $MergeHome "venv"
$vpy  = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) { Invoke-Expression "$py -m venv `"$Venv`"" }
& $vpy -m pip -q install --upgrade pip
& $vpy -m pip -q install -e $App
if ($LASTEXITCODE -ne 0) { Die "Python dependencies failed" "Usually network. Re-run — it resumes." }
Ok "forge installed into its own venv"

# ---------- llama-server ---------------------------------------------------
Say "Getting the model server (prebuilt — nothing to compile)"
$LlamaDir = Join-Path $MergeHome "llama"
$Bin = Get-ChildItem -Path $LlamaDir -Filter "llama-server.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Bin) {
    $rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
    $tag = ($rel | Where-Object { $_.assets.Count -gt 3 } | Select-Object -First 1).tag_name
    $asset = "llama-$tag-bin-win-cpu-x64.zip"
    $zip = Join-Path $MergeHome "llama.zip"
    curl.exe -fL --retry 3 -o $zip "https://github.com/ggml-org/llama.cpp/releases/download/$tag/$asset"
    if ($LASTEXITCODE -ne 0) { Die "Couldn't download $asset" "Re-run to retry." }
    if (Test-Path $LlamaDir) { Remove-Item -Recurse -Force $LlamaDir }
    Expand-Archive $zip -DestinationPath $LlamaDir -Force
    Remove-Item $zip
    $Bin = Get-ChildItem -Path $LlamaDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
    if (-not $Bin) { Die "llama-server.exe missing from the archive" "Release layout changed — file an issue naming tag $tag." }
    Ok "llama-server $tag (CPU build; NVIDIA users can swap in the win-cuda build later)"
} else { Ok "llama-server already present" }

# ---------- the model ------------------------------------------------------
$ModelUrl = $env:MERGE_MODEL_URL
if (-not $ModelUrl) {
    $B = $env:MERGE_MODEL_B
    if (-not $B) {
        if     ($ramGB -ge 30) { $B = "14" }
        elseif ($ramGB -ge 14) { $B = "7" }
        elseif ($ramGB -ge 7)  { $B = "3" }
        else                   { $B = "1.5" }
    }
    $ModelUrl = "https://huggingface.co/bartowski/Qwen2.5-${B}B-Instruct-GGUF/resolve/main/Qwen2.5-${B}B-Instruct-Q4_K_M.gguf"
    Say "Model for $ramGB GB RAM: Qwen2.5-${B}B (override with `$env:MERGE_MODEL_B)"
}
$ModelsDir = Join-Path $MergeHome "models"
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
$ModelFile = Join-Path $ModelsDir (Split-Path $ModelUrl -Leaf)
Say "Downloading the model (resumes if interrupted)"
curl.exe -fL --retry 3 -C - -o $ModelFile $ModelUrl
if ($LASTEXITCODE -ne 0) { Die "Model download failed" "Just re-run — it continues where it stopped." }
Ok "model downloaded"

# ---------- config (merge, never clobber) ----------------------------------
Say "Wiring the config"
$env:MODEL_BASENAME = Split-Path $ModelUrl -Leaf
$env:MERGE_PORT = "8080"
@'
import os, pathlib, socket, yaml
port = None
for p in range(8080, 8100):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); s.close(); port = p; break
    except OSError:
        pass
cfgp = pathlib.Path.home() / ".forge" / "config.yaml"
cfgp.parent.mkdir(exist_ok=True)
cfg = yaml.safe_load(cfgp.read_text()) if cfgp.exists() else {}
cfg = cfg or {}
models = cfg.setdefault("models", {})
if "merge" not in models:
    models["merge"] = {"provider": "openai-compat", "model": os.environ["MODEL_BASENAME"],
                       "base_url": f"http://127.0.0.1:{port}/v1", "max_tokens": 4096}
cfg.setdefault("active_model", "merge")
cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
open(pathlib.Path(os.environ["MERGE_HOME_OUT"]) / "run_port.txt", "w").write(str(port))
print(f"   config: {cfgp}  (port {port})")
'@ | Set-Content (Join-Path $MergeHome "wire_config.py")
$env:MERGE_HOME_OUT = $MergeHome
& $vpy (Join-Path $MergeHome "wire_config.py")
$Port = Get-Content (Join-Path $MergeHome "run_port.txt")

# ---------- start / stop ---------------------------------------------------
@"
# Start Merge: model server + web dashboard.
`$ErrorActionPreference = "Stop"
Set-Location "$MergeHome"
New-Item -ItemType Directory -Force -Path run | Out-Null
try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null }
catch {
    `$p = Start-Process -PassThru -WindowStyle Hidden "$($Bin.FullName)" -ArgumentList "-m","$ModelFile","--port","$Port","--host","127.0.0.1","--jinja","-c","8192"
    `$p.Id | Set-Content run\llama.pid
    Write-Host "model server starting on :$Port (first load takes ~a minute)"
}
& "$Venv\Scripts\forge-dash.exe"
"@ | Set-Content (Join-Path $MergeHome "start-merge.ps1")
@"
Set-Location "$MergeHome"
if (Test-Path run\llama.pid) { Stop-Process -Id (Get-Content run\llama.pid) -ErrorAction SilentlyContinue; Remove-Item run\llama.pid; Write-Host "model server stopped" }
"@ | Set-Content (Join-Path $MergeHome "stop-merge.ps1")

Say "Done."
Write-Host ""
Write-Host "  Start Merge:   powershell -ExecutionPolicy Bypass -File $MergeHome\start-merge.ps1"
Write-Host "  Stop:          powershell -ExecutionPolicy Bypass -File $MergeHome\stop-merge.ps1"
Write-Host "  Everything lives in $MergeHome — delete that folder to uninstall."
Write-Host "  Re-run this installer any time to update; it never breaks an existing setup."
