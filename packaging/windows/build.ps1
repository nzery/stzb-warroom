# Build the Windows package: build\stzb-warroom-windows.zip (a folder with stzb-warroom.exe).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server ... -Include D:\Wireshark-4.6.9-x64.exe
#
# -Version (default: the tag being built, GITHUB_REF_NAME, without its "v") is the version the
# program reports and compares with the newest release; any other build is 0.0.0.
# -Server (default: ST_SERVER) is the server the package reports to and -Site (default: ST_SITE) the
# website its window links to; both go into config.json beside the exe, not into the source.
#
# Needs Python 3.11+ (python.org, with tcl/tk); installs PyInstaller with pip (build only).
# Downloads the window, Electron (a fixed version, checked against its SHA-256), and rcedit, which
# gives its exe our icon and name; both are kept in build\downloads for the next build.
# -Include copies files into the package folder (e.g. the Wireshark installer, which the
# window offers to run when Wireshark is missing).
param([string]$Server = $env:ST_SERVER, [string]$Site = $env:ST_SITE, [string[]]$Include = @(),
      [string]$Version = $env:GITHUB_REF_NAME)
$ErrorActionPreference = "Stop"
if (-not $Server) { throw "no server: pass -Server or set ST_SERVER" }
$root = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $root
$Version = $Version -replace '^v', ''
if ($Version -notmatch '^\d+(\.\d+)*$') { $Version = "0.0.0" }
[IO.File]::WriteAllText("$root\stzb_warroom\_version.py", "__version__ = `"$Version`"`n")
Write-Host "version $Version"

python -m pip install --upgrade "pyinstaller==6.22.3"
if ($LASTEXITCODE) { throw "pip failed" }
# The window's page (stzb_warroom\ui) goes along as data; tkinter is not used.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name stzb-warroom `
    --distpath build\dist --workpath build\work --specpath build `
    --icon "$root\stzb_warroom\ui\icon.ico" --exclude-module tkinter `
    --add-data "$root\stzb_warroom\ui;stzb_warroom\ui" `
    --paths $root "$root\packaging\windows\launcher.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$package = "build\dist\stzb-warroom"

# The window: Electron in window\, its app (electron\) in window\resources\app.
$electronVersion = "44.5.1"
$downloads = @(
    @{ Name = "electron-v$electronVersion-win32-x64.zip"; Sha256 = "9b382492dcfee91f8f9e92c91f7972550a1b95d2299cac72279dab33a600d7db"
       Url = "https://github.com/electron/electron/releases/download/v$electronVersion/electron-v$electronVersion-win32-x64.zip" },
    @{ Name = "rcedit-x64.exe"; Sha256 = "3e7801db1a5edbec91b49a24a094aad776cb4515488ea5a4ca2289c400eade2a"
       Url = "https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe" })
New-Item -ItemType Directory -Force build\downloads | Out-Null
$ProgressPreference = "SilentlyContinue"  # Windows PowerShell 5 downloads slowly while drawing progress
foreach ($file in $downloads) {
    $path = "build\downloads\$($file.Name)"
    if (-not (Test-Path $path) -or (Get-FileHash $path -Algorithm SHA256).Hash -ne $file.Sha256) {
        Invoke-WebRequest -UseBasicParsing -Uri $file.Url -OutFile $path
        if ((Get-FileHash $path -Algorithm SHA256).Hash -ne $file.Sha256) { throw "$($file.Name): wrong SHA-256" }
    }
}
$window = "$package\window"
Expand-Archive "build\downloads\electron-v$electronVersion-win32-x64.zip" $window
Remove-Item "$window\resources\default_app.asar"
Get-ChildItem "$window\locales" -Exclude zh-CN.pak, en-US.pak | Remove-Item  # its own menus and dialogs: Chinese, else English
Rename-Item "$window\electron.exe" stzb-window.exe
$app = New-Item -ItemType Directory "$window\resources\app"
Copy-Item electron\main.js, electron\package.json, stzb_warroom\ui\icon.ico $app
$name = -join ([char[]](0x7387, 0x571F, 0x6218, 0x5C40))  # the program's name; Windows PowerShell 5 misreads a BOM-less script's Chinese
& build\downloads\rcedit-x64.exe "$window\stzb-window.exe" --set-icon stzb_warroom\ui\icon.ico `
    --set-version-string ProductName $name --set-version-string FileDescription $name `
    --set-version-string OriginalFilename stzb-window.exe --set-version-string InternalName stzb-window `
    --set-file-version $Version --set-product-version $Version
if ($LASTEXITCODE) { throw "rcedit failed" }

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
