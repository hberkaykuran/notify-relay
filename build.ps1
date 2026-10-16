# Build dist\notify-relay-setup.exe: tests, then PyInstaller (onedir), then Inno Setup.
# Onedir rather than onefile: faster start and fewer antivirus false positives.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

py -3.12 test_notify_relay.py
if ($LASTEXITCODE) { throw 'tests failed' }

py -3.12 -m PyInstaller --noconfirm --clean --windowed --name notify-relay `
    --icon icon.ico --add-data "icon.ico;." --collect-all winrt launcher.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" /Q installer.iss
if ($LASTEXITCODE) { throw 'Inno Setup failed' }

Get-Item dist\notify-relay-setup.exe | Select-Object Name, Length, LastWriteTime
