# trust-workspaces.ps1 - one-time: let the HEADLESS mission runner load its permission
# allowlist by trusting the forward-slash workspace keys that `claude -p` uses.
# (Interactive Claude creates backslash keys, e.g. "C:\..."; headless normalizes to "C:/..."
#  which are separate + untrusted. This sets the forward-slash keys.)
# SAFE: backs up .claude.json first, validates the result, auto-restores on any problem.
$ErrorActionPreference = 'Stop'
$path = "$env:USERPROFILE\.claude.json"
$bak  = "$path.bak-trustfix-$(Get-Date -Format yyyyMMddHHmmss)"
Copy-Item $path $bak -Force
Write-Host "backup: $bak" -ForegroundColor Gray

$c = Get-Content $path -Raw | ConvertFrom-Json
function New-TrustedEntry { [PSCustomObject]@{
  allowedTools=@(); mcpContextUris=@(); enabledMcpjsonServers=@(); disabledMcpjsonServers=@();
  hasTrustDialogAccepted=$true; projectOnboardingSeenCount=0;
  hasClaudeMdExternalIncludesApproved=$false; hasClaudeMdExternalIncludesWarningShown=$false } }

# The runner starts headless Claude in the mission-control dir; the drive root covers ad-hoc runs.
$keys = @('C:/', 'C:/Users/VAISHAK/Desktop/automation/mission-control')
foreach ($k in $keys) {
  if ($c.projects.PSObject.Properties.Name -contains $k) { $c.projects.$k.hasTrustDialogAccepted = $true; Write-Host "trusted (existing): $k" -ForegroundColor Green }
  else { $c.projects | Add-Member -NotePropertyName $k -NotePropertyValue (New-TrustedEntry); Write-Host "trusted (added): $k" -ForegroundColor Green }
}
$c | ConvertTo-Json -Depth 100 | Set-Content $path -Encoding UTF8

# validate or roll back
$v = Get-Content $path -Raw | ConvertFrom-Json
$ok = ($v.PSObject.Properties.Name.Count -ge 30) -and [bool]$v.oauthAccount -and [bool]$v.mcpServers -and
      $v.projects.'C:/'.hasTrustDialogAccepted -and
      $v.projects.'C:/Users/VAISHAK/Desktop/automation/mission-control'.hasTrustDialogAccepted
if ($ok) { Write-Host "`nOK - .claude.json valid, trust set, oauth+mcp intact. Runner can now execute headless." -ForegroundColor Cyan }
else { Copy-Item $bak $path -Force; Write-Host "`nVALIDATION FAILED - restored from backup. No changes made." -ForegroundColor Red }
