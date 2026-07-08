# cleanup.ps1 - reclaim disk from KNOWN-SAFE caches only. Reports first; deletes only with -Go.
# Safe = regenerable caches (npm/npx/pip/vercel/temp/playwright screenshots). NEVER touches
# projects, .claude assets, models, containers, or anything you can't rebuild automatically.
param([switch]$Go)
$ErrorActionPreference = 'SilentlyContinue'
function SizeGB($p){ if(Test-Path $p){ [math]::Round(((Get-ChildItem $p -Recurse -File -Force | Measure-Object Length -Sum).Sum)/1GB,2) } else { 0 } }

$free0 = [math]::Round((Get-PSDrive C).Free/1GB,2)
Write-Host "Disk C: free before: $free0 GB`n" -ForegroundColor Cyan

$targets = @(
  @{ n="npm cache";      p="$env:LOCALAPPDATA\npm-cache";                  cmd={ npm cache clean --force 2>$null } },
  @{ n="npx _npx cache"; p="$env:LOCALAPPDATA\npm-cache\_npx";             cmd={ Remove-Item "$env:LOCALAPPDATA\npm-cache\_npx" -Recurse -Force } },
  @{ n="pip cache";      p="$env:LOCALAPPDATA\pip\Cache";                  cmd={ python -m pip cache purge 2>$null } },
  @{ n="Vercel cache";   p="$env:LOCALAPPDATA\com.vercel.cli";            cmd={ Remove-Item "$env:LOCALAPPDATA\com.vercel.cli" -Recurse -Force } },
  @{ n="User TEMP";      p="$env:TEMP";                                    cmd={ Get-ChildItem $env:TEMP -Force | Where-Object { $_.Name -ne 'claude' } | Remove-Item -Recurse -Force } },  # 'claude' dir = live Claude Code session artifacts
  @{ n="Playwright shots";p="C:\.playwright-mcp";                          cmd={ Remove-Item "C:\.playwright-mcp\*" -Recurse -Force } },
  @{ n="Windows TEMP";   p="C:\Windows\Temp";                             cmd={ Get-ChildItem "C:\Windows\Temp" -Force | Remove-Item -Recurse -Force } },
  @{ n="Thumbnail cache";p="$env:LOCALAPPDATA\Microsoft\Windows\Explorer"; cmd={ Get-ChildItem "$env:LOCALAPPDATA\Microsoft\Windows\Explorer\thumbcache_*.db" | Remove-Item -Force } }
)

$total = 0
foreach ($t in $targets) {
  $gb = SizeGB $t.p
  $total += $gb
  "{0,-18} {1,7} GB   {2}" -f $t.n, $gb, $t.p | Write-Host
  if ($Go -and $gb -gt 0) { & $t.cmd; Write-Host "   -> cleared" -ForegroundColor Green }
}
Write-Host "`nReclaimable (safe caches): ~$([math]::Round($total,2)) GB" -ForegroundColor Yellow

if (-not $Go) {
  Write-Host "`nThis was a DRY RUN. To actually clear these safe caches, run:" -ForegroundColor Cyan
  Write-Host "  & `"$PSCommandPath`" -Go" -ForegroundColor White
  Write-Host "`nBIG non-cache items to review MANUALLY (not touched by this script):" -ForegroundColor Cyan
  Write-Host "  - Docker WSL disk: %LOCALAPPDATA%\Docker\wsl (can be huge; 'docker system prune' after Docker starts)"
  Write-Host "  - Ollama models: %USERPROFILE%\.ollama\models  (gemma3:4b ~3.3GB - keep, it's your menial-task model)"
  Write-Host "  - PlatformIO: %USERPROFILE%\.platformio  (toolchains; reinstallable)"
  Write-Host "  - node_modules across projects: reinstallable with npm i"
} else {
  $free1 = [math]::Round((Get-PSDrive C).Free/1GB,2)
  Write-Host "`nDisk C: free after: $free1 GB  (reclaimed ~$([math]::Round($free1-$free0,2)) GB)" -ForegroundColor Green
}
