param([Parameter(Mandatory)][string]$QemuSource,[Parameter(Mandatory)][string]$Wix,[string]$Go='go')
$ErrorActionPreference='Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m pip install -r requirements.txt pyinstaller==6.22.3 pefile
if ($LASTEXITCODE) {throw 'Python dependencies failed'}
Push-Location gateway
& $Go test ./...
if ($LASTEXITCODE) {throw 'Gateway tests failed'}
& $Go build -trimpath -ldflags '-s -w' -o ../dist/must-gateway.exe .
if ($LASTEXITCODE) {throw 'Gateway build failed'}
Pop-Location
python -m unittest discover -s tests -v
if ($LASTEXITCODE) {throw 'Launcher tests failed'}
python -m PyInstaller --noconfirm --clean --onedir --distpath dist/router-package --name must-router --add-data 'guest:guest' --add-data 'runtime-lock.json:.' --add-data 'router_settings.html:.' router_app.py
if ($LASTEXITCODE) {throw 'Router build failed'}
python -m PyInstaller --noconfirm --clean --onefile --windowed --name must-router-settings --add-data 'router_settings.html:.' router_settings_entry.py
if ($LASTEXITCODE) {throw 'Settings launcher build failed'}
Copy-Item dist\must-router-settings.exe dist\router-package\must-router\
Copy-Item dist\must-gateway.exe,packaging\router.cmd,README.md,ROUTER.md,LICENSE,THIRD_PARTY.md dist\router-package\must-router\
python packaging\stage_runtime.py $QemuSource dist\router-package\must-router\runtime\qemu
if ($LASTEXITCODE) {throw 'Runtime staging failed'}
Copy-Item runtime\base.qcow2 dist\router-package\must-router\runtime\base.qcow2
python packaging\build_msi.py --router --wix $Wix --stage dist\router-package\must-router --out dist\MUST-VPN-Router-0.3.1-x64.msi
if ($LASTEXITCODE) {throw 'MSI build failed'}
