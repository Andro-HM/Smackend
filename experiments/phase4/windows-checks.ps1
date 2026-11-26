param(
  [Parameter(Mandatory)] [string] $WheelPython,
  [Parameter(Mandatory)] [string] $SourcePython,
  [Parameter(Mandatory)] [string] $CorePython,
  [Parameter(Mandatory)] [string] $BaselinePython,
  [Parameter(Mandatory)] [string] $UpgradeDirectory,
  [Parameter(Mandatory)] [string] $OutputDirectory
)

$ErrorActionPreference = 'Stop'
if (-not $env:SMACKEND_SMOKE_DSN -or -not $env:SMACKEND_POSTGRES_BIN) {
  throw 'Set SMACKEND_SMOKE_DSN and SMACKEND_POSTGRES_BIN for a disposable local PostgreSQL cluster.'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$env:SMACKEND_SMOKE_PYTHON = (Resolve-Path -LiteralPath $CorePython).Path
$env:SMACKEND_AUTH_SMOKE_PYTHON = (Resolve-Path -LiteralPath $WheelPython).Path
$env:SMACKEND_MODULE_SMOKE_PYTHON = $env:SMACKEND_AUTH_SMOKE_PYTHON
$env:SMACKEND_PHASE3_SMOKE_PYTHON = $env:SMACKEND_AUTH_SMOKE_PYTHON
& $WheelPython -m pytest -q | Tee-Object -FilePath (Join-Path $OutputDirectory 'wheel-regression.txt')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:SMACKEND_MODULE_SMOKE_PYTHON = (Resolve-Path -LiteralPath $SourcePython).Path
$env:SMACKEND_PHASE3_SMOKE_PYTHON = $env:SMACKEND_MODULE_SMOKE_PYTHON
& $SourcePython -m pytest -q tests/test_modules.py tests/test_phase3.py tests/test_ai_adapters.py tests/test_voice_transport.py |
  Tee-Object -FilePath (Join-Path $OutputDirectory 'source-regression.txt')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $WheelPython experiments/phase4/upgrade.py $BaselinePython $WheelPython $UpgradeDirectory |
  Tee-Object -FilePath (Join-Path $OutputDirectory 'upgrade.json')
exit $LASTEXITCODE
