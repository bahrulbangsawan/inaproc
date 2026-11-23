# Install the `inaproc` skill on Windows (PowerShell 5.1+ or PowerShell 7).
#   .\install.ps1 claude | project | codex | zip | all | uninstall
# One-line install without cloning:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/bahrulbangsawan/inaproc/main/install.ps1))) claude
param([Parameter(Position = 0)][string]$Target = "")
$ErrorActionPreference = "Stop"
$Repo = "bahrulbangsawan/inaproc"
$Branch = if ($env:INAPROC_BRANCH) { $env:INAPROC_BRANCH } else { "main" }
$Here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Src = Join-Path $Here "skills\inaproc"
$DistDir = Join-Path $Here "dist"
$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
$CodexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }

# Run from the web (no local checkout)? Download the repository first.
if (-not (Test-Path (Join-Path $Src "SKILL.md")) -and $Target -and $Target -ne "uninstall") {
  $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("inaproc-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Path $Tmp | Out-Null
  Write-Host "Downloading $Repo ($Branch)..."
  $ZipFile = Join-Path $Tmp "repo.zip"
  Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $ZipFile
  Expand-Archive $ZipFile -DestinationPath $Tmp
  $Here = (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName
  $Src = Join-Path $Here "skills\inaproc"
  $DistDir = Join-Path (Get-Location).Path "dist"
}

function Get-Python {
  foreach ($c in @("py -3", "python", "python3")) {
    try {
      $v = & ([scriptblock]::Create("$c --version")) 2>$null
      if ($v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 8) { return $c }
    } catch {}
  }
  throw "Python 3.8+ is required. Install it with: winget install Python.Python.3.12  (or https://www.python.org/downloads/)"
}

function Copy-Skill($Dest) {
  if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
  New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
  Copy-Item -Recurse $Src $Dest
}

function Test-Live($Dir) {
  $py = Get-Python
  & ([scriptblock]::Create("$py `"$Dir\scripts\inaproc.py`" ppn")) *> $null
  if ($LASTEXITCODE -eq 0) { Write-Host "  OK: live check passed (katalog.inaproc.id reachable)" }
  else { Write-Host "  ! installed, but the live check failed (offline or rate-limited?)" }
}

function Install-Claude {
  $d = Join-Path $ClaudeDir "skills\inaproc"; Copy-Skill $d
  Write-Host "Claude Code: installed to $d  (use /inaproc)"; Test-Live $d
}
function Install-Project {
  $d = Join-Path (Get-Location).Path ".claude\skills\inaproc"; Copy-Skill $d
  Write-Host "Claude Code (project): installed to $d"; Test-Live $d
}
function Install-Codex {
  $d = Join-Path $CodexDir "skills\inaproc"; Copy-Skill $d
  New-Item -ItemType Directory -Force -Path (Join-Path $CodexDir "prompts") | Out-Null
  Copy-Item (Join-Path $Here "codex\prompts\inaproc.md") (Join-Path $CodexDir "prompts\inaproc.md") -Force
  Write-Host "Codex: installed to $d  (use /prompts:inaproc; restart Codex)"; Test-Live $d
}
function New-Zip {
  New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
  $z = Join-Path $DistDir "inaproc.zip"
  if (Test-Path $z) { Remove-Item $z }
  Compress-Archive -Path $Src -DestinationPath $z
  Write-Host "Zip: $z  (claude.ai / Claude Desktop: Settings > Capabilities > Skills > Upload skill)"
}
function Remove-All {
  foreach ($p in @((Join-Path $ClaudeDir "skills\inaproc"), (Join-Path $CodexDir "skills\inaproc"), (Join-Path $CodexDir "prompts\inaproc.md"))) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
  }
  Write-Host "Removed inaproc from Claude Code and Codex."
}

switch ($Target) {
  "claude"    { Install-Claude }
  "project"   { Install-Project }
  "codex"     { Install-Codex }
  "zip"       { New-Zip }
  "all"       { Install-Claude; Install-Codex; New-Zip }
  "uninstall" { Remove-All }
  default     { Write-Host "Usage: .\install.ps1 claude | project | codex | zip | all | uninstall" }
}
