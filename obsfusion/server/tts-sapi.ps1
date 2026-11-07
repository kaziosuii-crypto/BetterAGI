param(
  [Parameter(Mandatory=$true)][string]$TextFile,
  [Parameter(Mandatory=$true)][string]$OutFile,
  [string]$Voice = '',
  [int]$Rate = 0,
  [int]$Volume = 100
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
if ([string]::IsNullOrWhiteSpace($text)) { throw 'empty text' }
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($Voice -and $Voice.Trim().Length -gt 0) { $synth.SelectVoice($Voice) }
  $synth.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
  $synth.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))
  $synth.SetOutputToWaveFile($OutFile)
  $synth.Speak($text)
} finally {
  $synth.Dispose()
}
Write-Output ('OK ' + $OutFile)