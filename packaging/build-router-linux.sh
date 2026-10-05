#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m venv "${BUILD_VENV:-build/router-venv}"
venv=${BUILD_VENV:-build/router-venv}
"$venv/bin/pip" install -r requirements.txt pyinstaller==6.22.3
if [ -n "${GATEWAY_BINARY:-}" ]; then
  "$GATEWAY_BINARY" -h
  if [ "$(readlink -f "$GATEWAY_BINARY")" != "$(readlink -f dist/must-gateway)" ]; then cp "$GATEWAY_BINARY" dist/must-gateway; fi
else
  (cd gateway && go test ./... && go build -trimpath -ldflags '-s -w' -o ../dist/must-gateway .)
fi
"$venv/bin/python" -m unittest discover -s tests -v
"$venv/bin/pyinstaller" --noconfirm --onedir --distpath dist/linux --workpath build/router-linux --specpath build/router-linux --name must-router --add-data "$PWD/guest:guest" --add-data "$PWD/runtime-lock.json:." --add-data "$PWD/router_settings.html:." "$PWD/router_app.py"
stage=$(mktemp -d -t must-router-deb.XXXXXXXX)
mkdir -p "$stage/opt/must-router" "$stage/usr/bin" "$stage/DEBIAN"
mkdir -p "$stage/usr/share/applications"
cp packaging/must-router.desktop "$stage/usr/share/applications/"
cp -r dist/linux/must-router/. "$stage/opt/must-router/"
cp dist/must-gateway LICENSE THIRD_PARTY.md README.md ROUTER.md "$stage/opt/must-router/"
printf '#!/bin/sh\nexec /opt/must-router/must-router "$@"\n' > "$stage/usr/bin/must-router"
chmod -R u=rwX,go=rX "$stage"
chmod 755 "$stage" "$stage/DEBIAN" "$stage/usr/bin/must-router" "$stage/opt/must-router/must-router" "$stage/opt/must-router/must-gateway"
cat > "$stage/DEBIAN/control" <<'EOF'
Package: must-vpn-router
Version: 0.3.2
Section: net
Priority: optional
Architecture: amd64
Maintainer: MUST VM Project
Depends: qemu-system-x86, qemu-utils, iproute2, openssh-client
Description: Independent AISC SSH and Web VPN router (preview)
 Selective forwarding through an isolated VM; coexists with must-vpn-vm.
EOF
chmod 644 "$stage/DEBIAN/control"
dpkg-deb --build --root-owner-group "$stage" dist/must-vpn-router_0.3.2_amd64.deb
echo "Build staging retained at $stage"
