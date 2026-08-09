$ErrorActionPreference = "Stop"

$python = (Get-Command python -ErrorAction Stop).Source
$taskName = "IvoireData Updater"
$taskCommand = '"' + $python + '" -m ivoiredata.cli sync --due'

Write-Host "Creation de la tache Windows: $taskName"
schtasks.exe /Create /SC HOURLY /MO 1 /TN $taskName /TR $taskCommand /F | Out-Host

Write-Host "Tache creee. Test manuel:"
Write-Host "  schtasks /Run /TN `"$taskName`""
Write-Host "Etat:"
schtasks.exe /Query /TN $taskName /V /FO LIST | Out-Host
