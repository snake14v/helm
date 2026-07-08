# day-launcher.ps1 - start the day as ANY provider or combo. ASCII-only (PS 5.1 ANSI-reads .ps1).
# Boot tier (mission-control server) is always ensured; nothing heavy runs unless the profile asks.
#   Providers:  claude | codex | antigravity | gemma        (single-agent)
#   Combos:     trio (Claude + all consultants warm) | full (trio + Docker stack + mission runner)
# Called by DAY-START.bat (menu), the dashboard /api/launch buttons, or directly:
#   day-launcher.ps1 -Profile gemma
param(
  [ValidateSet('claude','codex','antigravity','gemma','trio','full')] [string]$Profile = 'claude',
  [switch]$FromDashboard
)
$ErrorActionPreference = 'Continue'
$log = "$PSScriptRoot\day-launcher.log"
function Say($m,$c='Gray'){ Write-Host $m -ForegroundColor $c; Add-Content $log "$(Get-Date -Format 'HH:mm:ss') $m" }
function PortUp($p){ (Test-NetConnection 127.0.0.1 -Port $p -WarningAction SilentlyContinue -InformationLevel Quiet) }
$HOMEDIR = $env:USERPROFILE
$CODEX = "$env:APPDATA\npm\codex.cmd"
$AGY   = "$env:LOCALAPPDATA\agy\bin\agy.exe"
Set-Content $log "=== day-launcher $Profile $(Get-Date) ==="
Say "DAY START -> profile: $Profile" 'Cyan'

# ---- boot tier: mission control server (every profile) ----
if (-not (PortUp 8799)) {
  Start-Process python -ArgumentList "`"$PSScriptRoot\server.py`"" -WindowStyle Hidden -WorkingDirectory $PSScriptRoot
  Start-Sleep -Seconds 2; Say "mission-control server: started" 'Green'
} else { Say "mission-control server: already up" 'Green' }
if (-not $FromDashboard) {
  $edge = "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe"
  if (-not (Test-Path $edge)) { $edge = "msedge" }
  Start-Process $edge -ArgumentList "--app=http://localhost:8799" -EA SilentlyContinue
}

# ---- shared helpers ----
function Ensure-Ollama {
  if (-not (PortUp 11434)) { Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden -EA SilentlyContinue; Start-Sleep -Seconds 3; Say "ollama: started" 'Green' }
  else { Say "ollama: already up" 'Green' }
  Start-Job { try { Invoke-RestMethod http://127.0.0.1:11434/api/generate -Method Post -TimeoutSec 120 -Body (@{model='gemma3:4b';prompt='hi';stream=$false;keep_alive='60m'}|ConvertTo-Json) | Out-Null } catch {} } | Out-Null
  Say "gemma3:4b: warming (keep_alive 60m)"
}
function Check-Codex { $ok=$false; try { & $CODEX login status 2>&1 | Out-Null; $ok=($LASTEXITCODE -eq 0) } catch {}; Say ("codex: " + $(if($ok){'logged in'}else{'NOT logged in - run: codex login'})) $(if($ok){'Green'}else{'Yellow'}) }
function Check-Agy { Say ("antigravity: " + $(if(Test-Path $AGY){'installed'}else{'missing - see /council skill'})) $(if(Test-Path $AGY){'Green'}else{'Yellow'}) }
function Open-Claude { $c=(Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -EA SilentlyContinue|Select-Object -Last 1).FullName; if($c){ Start-Process $c -WorkingDirectory $HOMEDIR; Say "Claude Code: terminal opened" 'Green' } else { Say "Claude Code: exe not found" 'Red' } }
function Open-Codex { if(Test-Path $CODEX){ Start-Process cmd -ArgumentList '/k', "`"$CODEX`"" -WorkingDirectory $HOMEDIR; Say "Codex: terminal opened" 'Green' } else { Say "Codex: codex.cmd not found (npm i -g @openai/codex)" 'Red' } }
function Open-Agy { if(Test-Path $AGY){ Start-Process cmd -ArgumentList '/k', "`"$AGY`"" -WorkingDirectory $HOMEDIR; Say "Antigravity: terminal opened" 'Green' } else { Say "Antigravity: agy.exe not found (see /council skill)" 'Red' } }
function Open-Gemma { Start-Process cmd -ArgumentList '/k', 'ollama run gemma3:4b' -WorkingDirectory $HOMEDIR; Say "Gemma: local chat terminal opened (offline, zero cloud tokens)" 'Green' }

# ---- profile composition ----
switch ($Profile) {
  'claude'      { Open-Claude }
  'codex'       { Check-Codex; Open-Codex }
  'antigravity' { Check-Agy; Open-Agy }
  'gemma'       { Ensure-Ollama; Open-Gemma }
  'trio'        { Ensure-Ollama; Check-Codex; Check-Agy; Open-Claude }
  'full' {
    Ensure-Ollama; Check-Codex; Check-Agy
    $env:PATH = "C:\Program Files\Docker\Docker\resources\bin;$env:PATH"
    $up=$false; $j=Start-Job { $env:PATH="C:\Program Files\Docker\Docker\resources\bin;$env:PATH"; docker info --format '{{.ServerVersion}}' 2>$null }
    if((Wait-Job $j -Timeout 8) -and (Receive-Job $j)){ $up=$true }; Remove-Job $j -Force
    if(-not $up){ Say "docker: starting Docker Desktop..." 'Yellow'; Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
      for($i=0;$i -lt 18;$i++){ Start-Sleep 5; $j=Start-Job { $env:PATH="C:\Program Files\Docker\Docker\resources\bin;$env:PATH"; docker info --format '{{.ServerVersion}}' 2>$null }; if((Wait-Job $j -Timeout 8) -and (Receive-Job $j)){ $up=$true; Remove-Job $j -Force; break }; Remove-Job $j -Force } }
    if($up){ Say "docker: engine up" 'Green'; foreach($c in 'n8n','crawl4ai-srv'){ docker start $c 2>$null|Out-Null }; Say "n8n + crawl4ai: started" 'Green' }
    else { Say "docker: did NOT come up - if crash dialog, run fix-docker-inference.ps1 (Quit first, never Reset)" 'Red' }
    $runner="$PSScriptRoot\MISSION-RUNNER.bat"; if(Test-Path $runner){ Start-Process cmd -ArgumentList '/c',$runner -WindowStyle Minimized; Say "mission runner: started (minimized)" 'Green' }
    Open-Claude
  }
}
Say "DAY START complete." 'Cyan'
