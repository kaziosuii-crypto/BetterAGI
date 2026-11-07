$ErrorActionPreference = 'Stop'
& 'E:\BetterAGI\tools\dotnet-install.ps1' -Runtime aspnetcore -Version 10.0.14 -InstallDir 'C:\Program Files\dotnet' -NoPath -Verbose 2>&1 | Out-File -FilePath 'E:\BetterAGI\tools\aspnet-install.log' -Encoding utf8
