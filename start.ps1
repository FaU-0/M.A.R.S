$ErrorActionPreference = "Stop"
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
        }
    }
}
if ($env:GIGACHAT_CA_FILE -and -not [IO.Path]::IsPathRooted($env:GIGACHAT_CA_FILE)) {
    $env:GIGACHAT_CA_FILE = Join-Path $PSScriptRoot $env:GIGACHAT_CA_FILE
}
if (-not $env:NO_PROXY) {
    $env:NO_PROXY = "ngw.devices.sberbank.ru,gigachat.devices.sberbank.ru,127.0.0.1,localhost"
}
python (Join-Path $PSScriptRoot "run_all.py") @args
