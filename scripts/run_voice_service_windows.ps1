param(
    [int]$Timeout = 4,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path

& "$PSScriptRoot\setup_windows_runtime.ps1"

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$servicePath = Join-Path $projectRoot "voice_command_service.py"

$argsList = @($servicePath, "--timeout", "$Timeout")
if ($Once) {
    $argsList += "--once"
}

& $venvPython @argsList
