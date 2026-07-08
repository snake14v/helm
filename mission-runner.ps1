# mission-runner.ps1 - phase-2 unattended runner.
# Polls the board for queued missions and launches a HEADLESS Claude session (/mission) on each.
# One mission at a time. Skips blocked/attempt-exhausted missions. Logs everything.
# Usage:  .\mission-runner.ps1            (watch loop, poll every 60s)
#         .\mission-runner.ps1 -Once      (single pass)
#         .\mission-runner.ps1 -Once -DryRun   (detect + report, launch nothing)
param([switch]$Once, [switch]$DryRun, [int]$PollSeconds = 60)
$ErrorActionPreference = 'Stop'
$api = "http://localhost:8799"
$logDir = Join-Path $PSScriptRoot "runner-logs"; New-Item -ItemType Directory -Force $logDir | Out-Null
function Log($m){ $line = "$(Get-Date -Format 'HH:mm:ss') $m"; Write-Host $line; Add-Content (Join-Path $logDir "runner-$(Get-Date -Format yyyyMMdd).log") $line }

$claude = (Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1).FullName
if (-not $claude) { Log "FATAL: claude.exe not found"; return }

# Headless auth: push the OAuth token from the User env into THIS process env so the child
# claude.exe inherits it. Without this the runner launches claude "Not logged in" and every
# mission fails exit 1 (root cause of the stuck-forever loop). Mirrors agent-failover.ps1.
$env:CLAUDE_CODE_OAUTH_TOKEN = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN) { Log "WARN: no CLAUDE_CODE_OAUTH_TOKEN in User env - headless Claude will be 'Not logged in'. Fix: run 'claude setup-token' then setx." }

# Human-in-the-loop gate (added per Vaishak's request 2026-07-07): a mission does NOT execute
# until he clicks Approve on the dashboard. This is the ONLY thing that changed his experience -
# previously the runner silently ran with --permission-mode acceptEdits and he had no visibility
# or control. Default DENY on timeout/no-response - never silently proceeds.
function Wait-Approval($title, $detail, [int]$timeoutSec = 1800) {
  try {
    $req = Invoke-RestMethod "$api/api/approvals" -Method Post -TimeoutSec 10 -Body (@{title=$title;detail=$detail;who='mission-runner'}|ConvertTo-Json)
  } catch { Log "approval request failed to post - treating as denied (fail-safe)"; return $false }
  Log "PAUSED - waiting for you to click Approve/Deny on the dashboard (id=$($req.id), up to $($timeoutSec)s)"
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 4
    try { $st = (Invoke-RestMethod "$api/api/approvals/status?id=$($req.id)" -TimeoutSec 10).status } catch { continue }
    if ($st -eq 'approved') { Log "APPROVED - proceeding."; return $true }
    if ($st -eq 'denied')   { Log "DENIED by you - holding this mission."; return $false }
  }
  Log "approval TIMED OUT after $($timeoutSec)s - treating as denied (safe default)."
  return $false
}

while ($true) {
  try {
    $state = Invoke-RestMethod "$api/api/state" -TimeoutSec 10
    # Per-task control: a mission runs only if its own settings.mode is 'auto', OR it has no
    # explicit mode and the GLOBAL switch (runtime.json mode) is 'auto'. Explicit 'manual' = never.
    $globalAuto = $false
    try { $rtg = Get-Content (Join-Path $PSScriptRoot 'runtime.json') -Raw -EA Stop | ConvertFrom-Json; if ($rtg.mode -eq 'auto') { $globalAuto = $true } } catch {}
    $eligible = @($state.tasks | Where-Object {
      $tm = if ($_.settings -and $_.settings.mode) { "$($_.settings.mode)".ToLower() } else { '' }
      $maxAtt = if ($_.settings -and $_.settings.maxAttempts) { [int]$_.settings.maxAttempts } else { 2 }
      $_.mission -and $_.col -eq 'backlog' -and
      ($_.status -notmatch 'blocked|dispatched|partial') -and
      (($tm -eq 'auto') -or ($tm -eq '' -and $globalAuto)) -and
      (-not $_.runnerAttempts -or $_.runnerAttempts -lt $maxAtt)
    })
    if (-not $eligible) {
      $qn = @($state.tasks | Where-Object {$_.mission -and $_.col -eq 'backlog'}).Count
      Log "no eligible missions (queued=$qn)"
    } else {
      $m = $eligible[0]
      $prev = 0; if ($m.PSObject.Properties['runnerAttempts'] -and $m.runnerAttempts) { $prev = [int]$m.runnerAttempts }
      $att = $prev + 1
      $maxAttM = if ($m.settings -and $m.settings.maxAttempts) { [int]$m.settings.maxAttempts } else { 2 }  # per-task cap
      Log "MISSION: '$($m.text)' (id=$($m.id), attempt $att)"
      if ($DryRun) { Log "dry-run: would request approval, then launch headless /mission" }
      else {
        Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$m.id;status="awaiting your approval - see Agents tab";runnerAttempts=$att}|ConvertTo-Json) | Out-Null
        $approved = Wait-Approval "Run mission: $($m.text)" "Executor: headless Claude. Mission id: $($m.id). Attempt $att of 2. Will run in $PSScriptRoot with acceptEdits once you approve."
        if (-not $approved) {
          Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$m.id;status="held - not approved (attempt $att used)"}|ConvertTo-Json) | Out-Null
          Log "mission held - not approved. Re-queue or approve next time it comes up."
          if ($Once) { break }
          Start-Sleep -Seconds $PollSeconds
          continue
        }
        Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$m.id;status="dispatched to runner (attempt $att)"}|ConvertTo-Json) | Out-Null
        # Per-task prompt add-on: extra operator instructions injected into THIS mission's run.
        # SANITIZE: strip double-quotes (they'd close the hand-quoted -p arg and let trailing text
        # become separate argv flags to claude.exe) and collapse newlines. (Review 2026-07-08.)
        $addon = ''
        if ($m.settings -and $m.settings.promptAddon) {
          $clean = ("$($m.settings.promptAddon)" -replace '"', "'" -replace '[\r\n]+', ' ').Trim()
          if ($clean) { $addon = " -- OPERATOR ADD-ON INSTRUCTIONS: $clean"; Log "prompt add-on present ($($clean.Length) chars, sanitized)" }
        }
        # Executor: per-task settings.executor wins; else the global runtime.json executor.
        # 'failover' routes through the Claude->Codex->cloud->Gemma chain; default = headless Claude.
        # SECURITY (Review 2026-07-08): mission text is attacker/LLM-controllable (queue_mission stores
        # it raw). Neutralize the double-quote + newlines BEFORE it enters a child-process argv on the
        # failover path (agent-failover hand-quotes it into claude -p / codex exec). Same rule as $addon.
        $safeText = ("$($m.text)" -replace '"', "'" -replace '[\r\n]+', ' ').Trim()
        $executor = 'claude'
        if ($m.settings -and $m.settings.executor) { $executor = "$($m.settings.executor)".ToLower() }
        else { try { $rt = Get-Content (Join-Path $PSScriptRoot 'runtime.json') -Raw -EA Stop | ConvertFrom-Json; if ($rt.executor) { $executor = "$($rt.executor)".ToLower() } } catch {} }
        if ($executor -eq 'failover') {
          Log "executor=failover -> routing mission to agent-failover chain (Claude->Codex->Gemma)"
          $foPrompt = "Execute mission '$safeText'.$addon When done, write a report to reports\$($m.id).md and POST {id:'$($m.id)',col:'done',report:'/reports/$($m.id).md'} to $api/api/job."
          $fo = & "$PSScriptRoot\agent-failover.ps1" -Prompt $foPrompt -Cwd $PSScriptRoot -Mission $m.id 2>&1 | Select-Object -Last 1
          Log "failover result: $fo"
          if ($Once) { break }
          Start-Sleep -Seconds $PollSeconds
          continue
        }
        $out = Join-Path $logDir "mission-$($m.id).log"
        Log "launching headless claude -> $out"
        # Start-Process (not `&`): a native stderr WARNING must not become a terminating error
        # under ErrorActionPreference=Stop, and claude wants explicit stdin (else 3s stall+warn).
        # -p: non-interactive; permission behavior comes from user settings allowlist + acceptEdits
        $stdinFile = Join-Path $logDir "empty-stdin.txt"
        if (-not (Test-Path $stdinFile)) { Set-Content $stdinFile "" -NoNewline }
        $proc = Start-Process -FilePath $claude -PassThru -Wait -WindowStyle Hidden `
          -WorkingDirectory $PSScriptRoot `
          -ArgumentList @('-p', "`"/mission run the queued mission with id $($m.id)$addon`"", '--permission-mode', 'acceptEdits') `
          -RedirectStandardInput $stdinFile -RedirectStandardOutput $out -RedirectStandardError "$out.err"
        $code = $proc.ExitCode
        # FAILOVER: if Claude hit credit/rate-limit exhaustion, auto-switch this mission to Codex->Gemma
        $claudeText = (Get-Content $out,"$out.err" -Raw -EA SilentlyContinue) -join "`n"
        if ($claudeText -match 'out of usage credits|usage-credits|rate.?limit|\b429\b|quota (?:exceeded|reached)|overloaded_error') {
          Log "Claude EXHAUSTED on this mission - invoking agent-failover chain (Codex -> Gemma)"
          $fo = & "$PSScriptRoot\agent-failover.ps1" -Mission $m.id -Cwd $PSScriptRoot `
                -Prompt "Execute mission '$safeText'. When done, write a report to reports\$($m.id).md and POST {id:'$($m.id)',col:'done',report:'/reports/$($m.id).md'} to $api/api/job." 2>&1 | Select-Object -Last 1
          Log "failover result: $fo"
          $code = 0  # failover owns the outcome + board status from here
        }
        if ($code -eq 0) {
          Log "headless run finished OK (exit 0) - board updated by the session itself"
        } elseif ($att -lt $maxAttM) {
          # failed fast (auth/startup) - make the card eligible again for another attempt
          Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$m.id;status="queued (retry after exit $code)"}|ConvertTo-Json) | Out-Null
          Log "headless run FAILED (exit $code) - card re-queued (attempt $att of $maxAttM); see $out"
        } else {
          Invoke-RestMethod "$api/api/job" -Method Post -Body (@{id=$m.id;status="runner-failed after $maxAttM attempts (exit $code) - needs human"}|ConvertTo-Json) | Out-Null
          Log "headless run FAILED (exit $code) - attempts exhausted, marked for human; see $out"
        }
      }
    }
  } catch { Log "ERROR: $($_.Exception.Message)" }
  if ($Once) { break }
  Start-Sleep -Seconds $PollSeconds
}
