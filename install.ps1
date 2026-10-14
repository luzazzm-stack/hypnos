# Hypnos v0.9 (memory architecture) installer for Windows (Windows 8.1, 10 and 11; Windows PowerShell 5.1 or PowerShell 7).
#
#   irm https://agent-memory-architecture-chi.vercel.app/install.ps1 | iex
#   (a coding agent installing it for you:  $env:HYPNOS_SETUP_YES=1; irm ... | iex)
#
# Installs uv (and through it Python 3.12) if missing, installs the memory, then runs `hypnos setup`, which connects
# every coding agent it finds and asks for a System One model. Everything runs in the background afterwards, with no
# windows. Options by environment variable: HYPNOS_SETUP_YES=1, HYPNOS_SETUP_SYSTEMONE=jev|laya|none,
# TYPESAFE_API_KEY=..., HYPNOS_SETUP_AGENTS=claude,opencode
# Uninstall:  hypnos setup --uninstall; uv tool uninstall hypnos

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'                              # the progress bar makes downloads very slow on PS 5
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}  # TLS 1.2 on old Windows

$repo = if ($env:HYPNOS_REPO) { $env:HYPNOS_REPO } else { 'luzazzm-stack/hypnos' }
$ref = if ($env:HYPNOS_REF) { $env:HYPNOS_REF } else { 'main' }
$source = if ($env:HYPNOS_SOURCE) { $env:HYPNOS_SOURCE } else { "https://codeload.github.com/$repo/zip/refs/heads/$ref" }

function Say($m) { Write-Host $m }
function Fail($m) { Write-Host "Install failed: $m" -ForegroundColor Red; throw $m }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Add-Path($d) { if ($d -and (Test-Path $d) -and ($env:Path -split ';' -notcontains $d)) { $env:Path = "$d;$env:Path" } }

Say 'Hypnos v0.9: installing...'
Add-Path "$HOME\.local\bin"; Add-Path "$HOME\.cargo\bin"

# 1. uv (installs Python for us; no system Python needed)
if (-not (Have 'uv')) {
    Say '- installing uv (Python package manager)'
    $uvScript = (New-Object Net.WebClient).DownloadString('https://astral.sh/uv/install.ps1')
    & ([scriptblock]::Create($uvScript)) | Out-Null
    Add-Path "$HOME\.local\bin"; Add-Path "$HOME\.cargo\bin"
    if (-not (Have 'uv')) { Fail 'uv did not install; see https://docs.astral.sh/uv/getting-started/installation/' }
}

# 2. the memory itself (from the public repo, or a local folder in HYPNOS_SOURCE for testing)
$work = Join-Path ([IO.Path]::GetTempPath()) ('hypnos-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    if (Test-Path -LiteralPath $source -PathType Container) {
        $src = $source
    } else {
        Say '- downloading'
        $zip = Join-Path $work 'src.zip'
        (New-Object Net.WebClient).DownloadFile($source, $zip)
        Add-Type -AssemblyName System.IO.Compression.FileSystem              # works on PowerShell 3+ (no Expand-Archive needed)
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, (Join-Path $work 'src'))
        $src = Get-ChildItem -Path (Join-Path $work 'src') | Where-Object { Test-Path (Join-Path $_.FullName 'hypnos\pyproject.toml') } |
               Select-Object -First 1 -ExpandProperty FullName
        if (-not $src) { Fail "package not found in $source" }
    }
    if (-not (Test-Path (Join-Path $src 'hypnos\pyproject.toml'))) { Fail "package not found in $source" }

    Say '- installing the memory (Python 3.12 is downloaded by uv if needed)'
    $ErrorActionPreference = 'Continue'
    & uv tool install --quiet --force --python 3.12 "$src\hypnos[mcp,vectors]" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say '  (meaning search is not available on this machine; installing without it)'
        & uv tool install --quiet --force --python 3.12 "$src\hypnos[mcp]"
        if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = 'Stop'; Fail 'uv tool install failed' }
    }
    & uv tool update-shell 2>$null | Out-Null
    $ErrorActionPreference = 'Stop'
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
Add-Path ((& uv tool dir --bin) 2>$null)
if (-not (Have 'hypnos')) { Fail 'hypnos is not on PATH; open a new terminal and run: hypnos setup' }

# 3. connect the agents (asks questions when a person is at the keyboard)
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and $env:HYPNOS_SETUP_YES -ne '1'
if ($interactive) { & hypnos setup } else { & hypnos setup --yes }
