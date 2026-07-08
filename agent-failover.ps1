# agent-failover.ps1 - run a task through an AGENT CHAIN, auto-switching when one is exhausted.
# Chain: Claude (headless) -> Codex -> Gemma(local, advisory). When Claude hits "out of usage
# credits" / rate-limit, the SAME task is handed to the next agent with a context handoff file
# (all agents share the Windows filesystem, so "file transfer" = same cwd + handoff note).
# Returns JSON: {winner, exitCode, outputFile, switched:[...], handoff}.
#
#   .\agent-failover.ps1 -Prompt "..." -Cwd "C:\repo" [-Mission mission_id] [-TestMode]
# -TestMode: makes the Claude step EMIT a fake credit error (no real claude call) so you can
#            verify the switchover to Codex live, without actually exhausting your quota.
param(
  [Parameter(Mandatory)] [string]$Prompt,
  [string]$Cwd = $PWD.Path,
  [string]$Mission = "",
  [switch]$TestMode
)
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$logDir = Join-Path $root "failover-logs"; New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$api = "http://localhost:8799"
function Log($m){ Write-Host "$(Get-Date -Format HH:mm:ss) $m"; Add-Content (Join-Path $logDir "failover-$stamp.log") "$(Get-Date -Format HH:mm:ss) $m" }
function BoardStatus($s){ if($Mission){ try{ Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$Mission;status=$s}|ConvertTo-Json) -TimeoutSec 6 | Out-Null }catch{} } }

# Gate every provider SWITCH behind a manual click (Vaishak, 2026-07-07: "I want to approve
# every switchover myself, one by one"). Timeout/no-response = stay put, do NOT auto-switch.
function AutoFailoverOn() {
  # Fully automatic switchover (Vaishak 2026-07-08: "if Fable/Opus runs out, switch to whatever's
  # available, automatic fully"). Default ON. Flip runtime.json {"autoFailover": false} for the
  # old manual approve-each-switch behaviour.
  try { $rt = Get-Content (Join-Path $root 'runtime.json') -Raw -EA Stop | ConvertFrom-Json
        if ($null -ne $rt.autoFailover) { return [bool]$rt.autoFailover } } catch {}
  return $true
}
function Wait-Switch($fromAgent, $toAgent, $why, [int]$timeoutSec = 900) {
  if (AutoFailoverOn) { Log "AUTO-FAILOVER on -> switching $fromAgent -> $toAgent immediately (no approval)"; return $true }
  try { $req = Invoke-RestMethod "$api/api/approvals" -Method Post -TimeoutSec 10 -Body (@{title="Switch $fromAgent -> $toAgent?";detail=$why;who='agent-failover'}|ConvertTo-Json) }
  catch { Log "approval post failed - NOT switching (fail-safe)"; return $false }
  Log "PAUSED - asking you to approve switching $fromAgent -> $toAgent (id=$($req.id))"
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 4
    try { $st = (Invoke-RestMethod "$api/api/approvals/status?id=$($req.id)" -TimeoutSec 10).status } catch { continue }
    if ($st -eq 'approved') { Log "approved - switching to $toAgent."; return $true }
    if ($st -eq 'denied')   { Log "denied - staying on $fromAgent (task held)."; return $false }
  }
  Log "switch approval timed out - NOT switching (safe default)."; return $false
}

# --- exhaustion / rate-limit detection (the trigger for switchover) ---
$EXHAUST = 'out of usage credits|usage-credits|rate.?limit|rate.?limited|\b429\b|quota (?:exceeded|reached)|insufficient (?:credit|quota)|credit balance|overloaded_error|too many requests'
function IsExhausted($text){ return ($text -match $EXHAUST) }

# --- context handoff: what the next agent needs to continue ---
function WriteHandoff($fromAgent, $priorLog){
  $h = Join-Path $logDir "handoff-$stamp.md"
  $tail = ""
  if ($priorLog -and (Test-Path $priorLog)) { $tail = (Get-Content $priorLog -Tail 25 -EA SilentlyContinue) -join "`n" }
  @"
# Agent handoff ($stamp)
**$fromAgent ran out of capacity mid-task. You are the failover executor.**

## Original task
$Prompt

## Working directory (files are here - same filesystem, nothing to copy)
$Cwd

## What the previous agent got through (tail of its output)
$tail

## Your job
Continue/complete the task from here. Write any deliverable into this working dir. If a
Mission id is set ($Mission), POST the final status to $api/api/job when done.
"@ | Set-Content $h -Encoding UTF8
  return $h
}

$switched = @()
$claudeToken = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')

# ---------- Tier 1: Claude (headless) ----------
Log "TIER 1 -> Claude"
BoardStatus "running on Claude"
$claudeOut = Join-Path $logDir "claude-$stamp.log"
if ($TestMode) {
  "You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch models." | Set-Content $claudeOut
  $c1 = 1
  Log "  [TestMode] simulated Claude credit-exhaustion"
} else {
  $claude = (Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -EA SilentlyContinue | Select-Object -Last 1).FullName
  $env:CLAUDE_CODE_OAUTH_TOKEN = $claudeToken
  $stdin = Join-Path $logDir "empty.txt"; if(-not(Test-Path $stdin)){ Set-Content $stdin "" -NoNewline }
  $p = Start-Process $claude -PassThru -Wait -WindowStyle Hidden -WorkingDirectory $Cwd `
        -ArgumentList @('-p', "`"$Prompt`"", '--permission-mode','acceptEdits') `
        -RedirectStandardInput $stdin -RedirectStandardOutput $claudeOut -RedirectStandardError "$claudeOut.err"
  $c1 = $p.ExitCode
}
$claudeText = (Get-Content $claudeOut,"$claudeOut.err" -Raw -EA SilentlyContinue) -join "`n"
if ($c1 -eq 0 -and -not (IsExhausted $claudeText)) {
  Log "Claude OK - no switchover needed."; BoardStatus "done (Claude)"
  @{winner='claude';exitCode=0;outputFile=$claudeOut;switched=$switched;handoff=$null} | ConvertTo-Json -Compress; return
}
if (IsExhausted $claudeText) { Log "[!] Claude EXHAUSTED (credit/rate-limit detected)." ; $switched += 'claude:exhausted' }
else { Log "Claude failed (exit $c1, not exhaustion)." ; $switched += "claude:exit$c1" }

if (-not (Wait-Switch 'Claude' 'Codex' "Claude was exhausted/failed on: $Prompt")) {
  BoardStatus "held - Claude exhausted, switch to Codex NOT approved"
  @{winner='none';exitCode=1;switched=$switched;handoff=$null;needsHuman=$true;reason='switch to Codex denied/timed out'} | ConvertTo-Json -Compress; return
}

# ---------- Tier 2: Codex ----------
$handoff = WriteHandoff 'Claude' $claudeOut
Log "TIER 2 -> Codex (handoff: $(Split-Path $handoff -Leaf))"
BoardStatus "failed over to Codex (Claude exhausted, switch approved)"
# Resolve the .cmd shim (Get-Command returns codex.ps1, which Start-Process can't launch)
$codex = if (Test-Path "$env:APPDATA\npm\codex.cmd") { "$env:APPDATA\npm\codex.cmd" }
         else { $s = (Get-Command codex -EA SilentlyContinue).Source; if ($s) { $s -replace '\.ps1$','.cmd' } }
$codexOut = Join-Path $logDir "codex-$stamp.log"
if ($codex -and (Test-Path $codex)) {
  $cxPrompt = "You are the FAILOVER executor (Claude ran out of credits). Read the handoff at `"$handoff`" then complete this task: $Prompt"
  $cxStdin = Join-Path $logDir "empty.txt"; if(-not(Test-Path $cxStdin)){ Set-Content $cxStdin "" -NoNewline }
  # workspace-write (not deprecated --full-auto); redirect empty stdin so codex doesn't hang reading it
  $p2 = Start-Process $codex -PassThru -Wait -WindowStyle Hidden -WorkingDirectory $Cwd `
        -ArgumentList @('exec','--sandbox','workspace-write','--skip-git-repo-check', "`"$cxPrompt`"") `
        -RedirectStandardInput $cxStdin -RedirectStandardOutput $codexOut -RedirectStandardError "$codexOut.err"
  $c2 = $p2.ExitCode
  $codexText = (Get-Content $codexOut,"$codexOut.err" -Raw -EA SilentlyContinue) -join "`n"
  if ($c2 -eq 0 -and -not (IsExhausted $codexText)) {
    Log "Codex COMPLETED the task (failover success)."; BoardStatus "done (Codex failover)"
    @{winner='codex';exitCode=0;outputFile=$codexOut;switched=$switched;handoff=$handoff} | ConvertTo-Json -Compress; return
  }
  if (IsExhausted $codexText) { Log "[!] Codex also exhausted - dropping to Gemma advisory." ; $switched += 'codex:exhausted' }
  else { Log "Codex failed (exit $c2) - dropping to Gemma advisory." ; $switched += "codex:exit$c2" }
} else { Log "Codex shim not found - skipping to Gemma." ; $switched += 'codex:missing' }

# ---------- Tier 3: Cloud free-tier APIs (Kimi / OpenRouter->MiMo,DeepSeek / Groq / Cerebras / Gemini) ----------
# Advisory tier (chat-only: strong reasoning + a full solution/plan, but no autonomous file edits).
# Tried BEFORE Gemma because these models (Kimi K2, Llama-70B, Gemini) are far stronger than gemma3:4b.
Log "TIER 3 -> cloud free-tier APIs (best available)"
BoardStatus "failing over to free-tier cloud APIs (Claude+Codex out)"
$py = (Get-Command python -EA SilentlyContinue).Source
if (-not $py) { $py = "python" }
$apiPromptFile = Join-Path $logDir "apiprompt-$stamp.txt"
"Both agentic coding agents (Claude, Codex) are out of quota. Produce the BEST complete solution and step-by-step plan a human (or the next session) can apply for this task. Be concrete and include any code in full. Task: $Prompt" | Set-Content $apiPromptFile -Encoding UTF8
$apiOut = Join-Path $logDir "cloudapi-$stamp.md"
$apiRaw = & $py "$root\llm_providers.py" auto-file $apiPromptFile 2>&1 | Select-Object -Last 1
$ap = $null; try { $ap = $apiRaw | ConvertFrom-Json } catch {}
if ($ap -and $ap.ok) {
  "# Cloud free-tier failover ($stamp) - $($ap.label)`n`nClaude + Codex were exhausted. Answered by **$($ap.provider)** ($($ap.model)). Advisory - no files were edited:`n`n$($ap.text)" | Set-Content $apiOut -Encoding UTF8
  Log "Cloud API [$($ap.provider)] answered ($($ap.ms)ms) -> $(Split-Path $apiOut -Leaf)"
  BoardStatus "advisory from $($ap.provider) (Claude+Codex out) - review needed"
  $switched += "cloudapi:$($ap.provider)"
  @{winner="cloudapi:$($ap.provider)";exitCode=0;outputFile=$apiOut;switched=$switched;handoff=$handoff;needsHuman=$true;provider=$ap.provider} | ConvertTo-Json -Compress; return
}
$apiErr = if ($ap) { $ap.error } else { "no python / bad response: $apiRaw" }
Log "no cloud free-tier API available ($apiErr) - dropping to Gemma."
$switched += 'cloudapi:none'

# ---------- Tier 4: Gemma (local, advisory only - can't edit files, never runs out) ----------
Log "TIER 4 -> Gemma (local advisory, last resort)"
BoardStatus "Gemma advisory (Claude+Codex+cloud exhausted) - needs human"
$gemOut = Join-Path $logDir "gemma-$stamp.md"
try {
  $body = @{ model='gemma3:4b'; stream=$false; prompt="Both cloud coding agents are out of quota. Give the BEST plain-text plan a human can execute for this task, step by step. Task: $Prompt" } | ConvertTo-Json
  $r = Invoke-RestMethod "http://localhost:11434/api/generate" -Method Post -Body $body -TimeoutSec 180
  "# Gemma advisory ($stamp)`n`nClaude + Codex were exhausted. Local Gemma cannot edit files, so here is a human-executable plan:`n`n$($r.response)" | Set-Content $gemOut -Encoding UTF8
  Log "Gemma wrote an advisory plan -> $(Split-Path $gemOut -Leaf) (human action needed)."
  @{winner='gemma-advisory';exitCode=0;outputFile=$gemOut;switched=$switched;handoff=$handoff;needsHuman=$true} | ConvertTo-Json -Compress
} catch {
  Log "Gemma unreachable - chain exhausted. Task needs a human when quota resets."
  BoardStatus "ALL AGENTS EXHAUSTED - needs human when quota resets"
  @{winner='none';exitCode=1;switched=$switched;handoff=$handoff;needsHuman=$true} | ConvertTo-Json -Compress
}
