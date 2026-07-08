# connect-model.ps1 - guided connect for open/free models. ASCII-only (PS 5.1 reads .ps1 as ANSI).
#   .\connect-model.ps1                 # show all providers + where to get a free key
#   .\connect-model.ps1 openrouter <KEY># set the key (persisted via setx) + live-test it
# One key per provider (their real free tier). OpenRouter is the best single move: Kimi + DeepSeek
# + Qwen behind one key. No multi-accounting - that violates ToS and risks bans.
param([string]$Provider = "", [string]$Key = "")

$PROVIDERS = [ordered]@{
  openrouter = @{ env="OPENROUTER_API_KEY"; get="https://openrouter.ai/keys"; note="ONE key -> Kimi K2, DeepSeek R1, Qwen, GLM (many :free)" }
  groq       = @{ env="GROQ_API_KEY";       get="https://console.groq.com/keys"; note="free, extremely fast; hosts Kimi K2, Llama, Qwen" }
  moonshot   = @{ env="MOONSHOT_API_KEY";   get="https://platform.moonshot.ai"; note="Kimi K2 direct (trial credits)" }
  cerebras   = @{ env="CEREBRAS_API_KEY";   get="https://cloud.cerebras.ai"; note="free, fast; Llama + Qwen" }
  gemini     = @{ env="GEMINI_API_KEY";     get="https://aistudio.google.com/apikey"; note="free Gemini Flash tier (one account)" }
}

if (-not $Provider) {
  Write-Host "`n  CONNECT AN OPEN MODEL - pick one, grab its FREE key, then run:" -ForegroundColor Cyan
  Write-Host "    .\connect-model.ps1 <provider> <YOUR_KEY>`n" -ForegroundColor Gray
  foreach ($k in $PROVIDERS.Keys) {
    $p = $PROVIDERS[$k]
    $set = [Environment]::GetEnvironmentVariable($p.env,'User')
    $mark = if ($set) { "[connected]" } else { "[ not set ]" }
    Write-Host ("  {0,-11} {1}" -f $k, $mark) -ForegroundColor $(if($set){'Green'}else{'Yellow'})
    Write-Host ("              key: {0}  ->  {1}" -f $p.env, $p.get) -ForegroundColor DarkGray
    Write-Host ("              {0}" -f $p.note) -ForegroundColor DarkGray
  }
  Write-Host "`n  Recommended first: openrouter (most models, one key).`n" -ForegroundColor Cyan
  return
}

$Provider = $Provider.ToLower()
if (-not $PROVIDERS.Contains($Provider)) { Write-Host "Unknown provider '$Provider'. Options: $($PROVIDERS.Keys -join ', ')" -ForegroundColor Red; return }
$env_ = $PROVIDERS[$Provider].env
if (-not $Key) { Write-Host "Get a free key at $($PROVIDERS[$Provider].get), then: .\connect-model.ps1 $Provider <YOUR_KEY>" -ForegroundColor Yellow; return }

# persist (User scope) - new processes + the dashboard server (via registry read) pick it up
setx $env_ "$Key" | Out-Null
[Environment]::SetEnvironmentVariable($env_, $Key, 'Process')  # also this session
Write-Host "Saved $env_ (User env). Testing the connection..." -ForegroundColor Gray

# live test via the dashboard's tester (or the python module directly)
try {
  $r = Invoke-RestMethod "http://localhost:8799/api/models/test?provider=$Provider" -TimeoutSec 40
  if ($r.ok) { Write-Host "CONNECTED: $($r.label) replied '$($r.reply)' in $($r.ms)ms (model $($r.model))." -ForegroundColor Green }
  else { Write-Host "Key saved but test FAILED: $($r.error)" -ForegroundColor Red; Write-Host "(free models can be rate-limited/busy - try again in a minute)" -ForegroundColor DarkGray }
} catch {
  Write-Host "Key saved. Dashboard not reachable to test - start it (DAY-START) then click 'test' on the model card." -ForegroundColor Yellow
}
