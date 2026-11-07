$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voices = @()
  foreach ($v in $synth.GetInstalledVoices()) {
    $voices += [pscustomobject]@{
      name = $v.VoiceInfo.Name
      culture = $v.VoiceInfo.Culture.Name
      gender = $v.VoiceInfo.Gender.ToString()
      enabled = $v.Enabled
    }
  }
  $voices | ConvertTo-Json -Compress
} finally {
  $synth.Dispose()
}