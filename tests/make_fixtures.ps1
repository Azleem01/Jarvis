# Regenerate the speech fixtures used by tests/bench.py.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\make_fixtures.ps1
#
# Uses Windows' built-in SAPI voice, so no downloads and no recording session.
# It emits whatever rate the voice prefers (usually 24 kHz); bench.py resamples
# to 16 kHz on load. Real speech matters here: random noise makes Whisper
# hallucinate long transcripts, and decode timings then track output length
# rather than the setting being measured.

$phrases = @{
    "open_youtube_chrome" = "open youtube on my chrome browser"
    "open_notepad"        = "open notepad"
    "set_alarm"           = "set an alarm for seven thirty in the morning"
}

$dir = Join-Path $PSScriptRoot "fixtures"
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

foreach ($name in $phrases.Keys) {
    $voice  = New-Object -ComObject SAPI.SpVoice
    $stream = New-Object -ComObject SAPI.SpFileStream
    $format = New-Object -ComObject SAPI.SpAudioFormat
    $format.Type = 26                      # SAFT16kHz16BitMono (advisory)
    $stream.Format = $format
    $stream.Open((Join-Path $dir "$name.wav"), 3, $false)
    $voice.AudioOutputStream = $stream
    $voice.Speak($phrases[$name]) | Out-Null
    $stream.Close()
    Write-Host "wrote $name.wav"
}
