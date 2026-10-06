# Build the Windows package: build\stzb-warroom-windows.zip (a folder with stzb-warroom.exe).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server ... -Include D:\Wireshark-4.6.9-x64.exe
#
# -Server (default: ST_SERVER) is the server the package reports to and -Site (default: ST_SITE) the
# website its window links to; both go into config.json beside the exe, not into the source.
#
# Needs Python 3.11+ (python.org, with tcl/tk); installs PyInstaller with pip (build only).
# -Include copies files into the package folder (e.g. the Wireshark installer, which the
# window offers to run when Wireshark is missing).
param([string]$Server = $env:ST_SERVER, [string]$Site = $env:ST_SITE, [string[]]$Include = @())
$ErrorActionPreference = "Stop"
if (-not $Server) { throw "no server: pass -Server or set ST_SERVER" }
$root = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $root

python -m pip install --upgrade "pyinstaller==6.22.3"
if ($LASTEXITCODE) { throw "pip failed" }
# The window's page (stzb_warroom\ui) goes along as data; tkinter is not used.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name stzb-warroom `
    --distpath build\dist --workpath build\work --specpath build `
    --icon "$root\packaging\windows\icon.ico" --exclude-module tkinter `
    --add-data "$root\stzb_warroom\ui;stzb_warroom\ui" `
    --paths $root "$root\packaging\windows\launcher.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$package = "build\dist\stzb-warroom"
Copy-Item "packaging\windows\*.txt" $package  # the players' guide
$config = @{ server = $Server }
if ($Site) { $config.site = $Site }
# Without a BOM: Windows PowerShell 5's UTF8 encoding would write one.
[IO.File]::WriteAllText("$root\$package\config.json", (ConvertTo-Json $config), (New-Object Text.UTF8Encoding $false))
foreach ($file in $Include) { Copy-Item $file $package }
# zipfile writes the Chinese file names with the UTF-8 flag, which every unzip tool reads.
python -c "import shutil; shutil.make_archive('build/stzb-warroom-windows', 'zip', 'build/dist', 'stzb-warroom')"
if ($LASTEXITCODE) { throw "zip failed" }
Write-Host "built build\stzb-warroom-windows.zip"
