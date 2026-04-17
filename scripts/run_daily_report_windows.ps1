param(
    [string]$Date = "",
    [string]$OutputDir = "",
    [string]$PythonBin = $env:JWDSAR_PYTHON_BIN
)

if ([string]::IsNullOrWhiteSpace($PythonBin)) {
    $PythonBin = "python"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$argsList = @("app_scheduled.py", "--generate-once")
if (-not [string]::IsNullOrWhiteSpace($Date)) {
    $argsList += @("--date", $Date)
}
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $argsList += @("--output-dir", $OutputDir)
}

& $PythonBin @argsList
exit $LASTEXITCODE
