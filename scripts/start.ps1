param([switch]$Demo)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required. Create .venv with your Python installation.' }
    & $taskPython -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $taskPython -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
}
& $taskPython -m farstar setup
if ($Demo) { & $taskPython -m farstar demo } else { & $taskPython -m farstar run }
