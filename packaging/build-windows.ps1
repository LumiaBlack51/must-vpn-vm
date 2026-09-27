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
python -m PyInstaller --noconfirm --clean --onedir --name must-vm --add-data 'guest:guest' --add-data 'runtime-lock.json:.' app.py
if ($LASTEXITCODE) {throw 'Launcher build failed'}
Copy-Item dist\must-gateway.exe dist\must-vm\
Copy-Item README.md,LICENSE,THIRD_PARTY.md dist\must-vm\
python packaging\stage_runtime.py $QemuSource dist\must-vm\runtime\qemu
if ($LASTEXITCODE) {throw 'Runtime staging failed'}
Copy-Item runtime\base.qcow2 dist\must-vm\runtime\base.qcow2
python packaging\build_msi.py --wix $Wix --stage dist\must-vm --out dist\MUST-VPN-VM-0.2.1-x64.msi
if ($LASTEXITCODE) {throw 'MSI build failed'}
