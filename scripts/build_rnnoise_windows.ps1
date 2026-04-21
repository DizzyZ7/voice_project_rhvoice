$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$srcDir = Join-Path $root "third_party\rnnoise-windows"
if (-not (Test-Path $srcDir)) {
  throw "RNNoise source not found: $srcDir"
}

$msbuildCandidates = @(
  "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
  "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
  "C:\Program Files\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe",
  "C:\Program Files\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
)

$msbuild = $msbuildCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $msbuild) {
  $found = & where.exe msbuild 2>$null
  if ($LASTEXITCODE -eq 0 -and $found) {
    $msbuild = ($found -split "`r?`n" | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1)
  }
}
if (-not $msbuild) {
  throw "MSBuild not found. Install Visual Studio Build Tools (Desktop development with C++)."
}

$sln = Join-Path $srcDir "Rnnoise-windows.sln"
if (-not (Test-Path $sln)) {
  throw "Solution not found: $sln"
}

Write-Host "Building rnnoise_share.dll..."
& $msbuild $sln /t:rnnoise_share /p:Configuration=Release /p:Platform=x64 /m
if ($LASTEXITCODE -ne 0) {
  throw "MSBuild failed with code $LASTEXITCODE"
}

$dll = Join-Path $srcDir "x64\Release\rnnoise_share.dll"
if (-not (Test-Path $dll)) {
  throw "Build completed but DLL not found: $dll"
}

Write-Host "Done. Set STT_RNNOISE_LIB to:"
Write-Host $dll
