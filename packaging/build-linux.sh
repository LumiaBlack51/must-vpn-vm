#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# Build on Ubuntu 24.04 amd64. The host never installs the proprietary VPN.
python3 -m venv "${BUILD_VENV:-build/venv}"
venv=${BUILD_VENV:-build/venv}
"$venv/bin/pip" install -r requirements.txt pyinstaller==6.22.3
if [ -n "${GATEWAY_BINARY:-}" ]; then
  "$GATEWAY_BINARY" -h
  if [ "$(readlink -f "$GATEWAY_BINARY")" != "$(readlink -f dist/must-gateway)" ]; then
    cp "$GATEWAY_BINARY" dist/must-gateway
  fi
else
  (cd gateway && go test ./... && go build -trimpath -ldflags '-s -w' -o ../dist/must-gateway .)
fi
"$venv/bin/python" -m unittest discover -s tests -v
"$venv/bin/pyinstaller" --noconfirm --onedir --distpath dist/linux --workpath build/linux --specpath build/linux --name must-vm --add-data "$PWD/guest:guest" --add-data "$PWD/runtime-lock.json:." "$PWD/app.py"
# Native filesystem staging also supports a source checkout on WSL /mnt/c or /mnt/e.
stage=$(mktemp -d -t must-vm-deb.XXXXXXXX)
mkdir -p "$stage/opt/must-vm" "$stage/usr/bin" "$stage/DEBIAN"
cp -r dist/linux/must-vm/. "$stage/opt/must-vm/"
cp dist/must-gateway "$stage/opt/must-vm/"
cp LICENSE THIRD_PARTY.md README.md "$stage/opt/must-vm/"
printf '#!/bin/sh\nexec /opt/must-vm/must-vm "$@"\n' > "$stage/usr/bin/must-vm"
chmod -R u=rwX,go=rX "$stage"
chmod 755 "$stage" "$stage/DEBIAN" "$stage/usr/bin/must-vm" "$stage/opt/must-vm/must-vm" "$stage/opt/must-vm/must-gateway"
cat > "$stage/DEBIAN/control" <<'EOF'
Package: must-vpn-vm
Version: 0.1.1
Section: net
Priority: optional
Architecture: amd64
Maintainer: MUST VM Project
Depends: qemu-system-x86, qemu-utils, iproute2
Description: Isolated command-line Ubuntu VM for MUST VPN (preview)
 Does not install or execute the vendor VPN on the host.
EOF
chmod 644 "$stage/DEBIAN/control"
dpkg-deb --build --root-owner-group "$stage" dist/must-vpn-vm_0.1.1_amd64.deb
echo "Build staging retained at $stage"
