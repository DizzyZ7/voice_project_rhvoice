$ErrorActionPreference = "Stop"

$token = "change-me-in-prod"
$audioPath = Join-Path $PSScriptRoot "..\voice_test.wav"
if (-not (Test-Path $audioPath)) {
  $audioPath = Join-Path $PSScriptRoot "..\voice_test_long.wav"
}
if (-not (Test-Path $audioPath)) {
  throw "No demo audio found. Expected voice_test.wav or voice_test_long.wav in project root."
}

$headersJson = @{
  Authorization = "Bearer $token"
  "Content-Type" = "application/json"
}

Write-Host "TTS demo traffic..."
1..15 | ForEach-Object {
  $body = @{
    text = "Grafana demo request $_"
    save_to_file = "demo/tts_$_.wav"
    use_cache = $false
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "http://localhost:8001/tts/generate" -Headers $headersJson -Body $body | Out-Null
  Start-Sleep -Milliseconds 200
}

Write-Host "STT demo traffic..."
1..10 | ForEach-Object {
  curl.exe -s -X POST "http://localhost:8000/stt/recognize" `
    -H "Authorization: Bearer $token" `
    -F "file=@$audioPath;type=audio/wav" | Out-Null
  Start-Sleep -Milliseconds 250
}

Write-Host "Orchestrator demo traffic..."
1..10 | ForEach-Object {
  curl.exe -s -X POST "http://localhost:8002/process" `
    -H "Authorization: Bearer $token" `
    -F "file=@$audioPath;type=audio/wav" | Out-Null
  Start-Sleep -Milliseconds 300
}

Write-Host "Injecting small error burst for Errors/min panel..."
1..3 | ForEach-Object {
  try {
    $bad = @{ text = "" } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "http://localhost:8001/tts/generate" -Headers $headersJson -Body $bad | Out-Null
  } catch {
    # expected validation error
  }
}

Write-Host "Done. In Grafana set range to Last 15 minutes and click Refresh."
