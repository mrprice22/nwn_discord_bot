# Start the local LLM server (llama.cpp) that nwnbot asks about duplicates.
#
# This box is the bot box: the model, the bot and the sync all live here, so the
# server binds to 127.0.0.1 and nothing is exposed to the LAN. (The original
# hand-written launcher in "LLM CONFIG" binds 0.0.0.0 because the roadmap editor
# used to call it across the network from 10.42.0.83. It no longer does.)
#
# Tuning is lifted from that launcher, which was measured on this hardware:
#   -ngl 99            every layer on the GPU, then
#   --n-cpu-moe 44     push the expert FFN tensors of the first 44 layers back
#                      to system RAM. Attention stays on the card (small and
#                      latency-critical); the bulky experts stream from RAM.
#                      LOWER this if VRAM has room; RAISE it if you OOM.
#   -t 8               8 PHYSICAL cores. Not 16 - SMT hurts llama.cpp throughput.
#
# Qwen3.6 is a *reasoning* model. The bot disables thinking per request, which
# both halves latency and stops the answer landing in `reasoning_content` with
# an empty `content` - measured at ~2.0s per duplicate judgement with it off.

[CmdletBinding()]
param(
    [string]$Model = "D:\models\Qwen3.6-35B-A3B-Q4_K_M.gguf",
    [int]$Port = 8080,
    [int]$CpuMoe = 44,
    [int]$Ctx = 16384
)

$ErrorActionPreference = "Stop"

$llama = Join-Path $env:LOCALAPPDATA ("Microsoft\WinGet\Packages\" +
    "ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe")

if (-not (Test-Path $llama)) {
    throw "llama-server.exe not found at $llama. Reinstall with: winget install ggml.llamacpp"
}
if (-not (Test-Path $Model)) {
    throw "Model not found at $Model."
}

& $llama `
    -m $Model `
    -ngl 99 --n-cpu-moe $CpuMoe `
    -c $Ctx -fa on -ctk q8_0 -ctv q8_0 `
    -t 8 --jinja `
    --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
