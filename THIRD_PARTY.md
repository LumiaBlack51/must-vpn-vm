# Third-party components

The MIT license applies only to this project's original code. It does not cover
Sangfor's aTrust, Ubuntu, QEMU, the Python runtime, or any dependencies.

* The original MUST DEB is imported locally, verified by SHA-256 and never
  included in Git or public build artifacts. No vendor executables are patched.
* QEMU Windows runtime: https://qemu.weilnetz.de/w64/ . The local installer selects
  x86_64 executables, their dependency DLLs and firmware; COPYING, COPYING.LIB and
  the supplied documentation are retained. QEMU source/build instructions:
  https://qemu.weilnetz.de/ and https://github.com/stweil/qemu . QEMU is GPL-2.0;
  libraries and firmware have their own licenses. A distributor must arrange
  corresponding source and notices for the exact redistributed build.
* Ubuntu Minimal 24.04 amd64: https://cloud-images.ubuntu.com/minimal/releases/noble/ .
  Package copyrights remain inside `/usr/share/doc/*/copyright` in the image;
  source packages: https://archive.ubuntu.com/ubuntu/ .
* gVisor netstack: Apache-2.0, https://github.com/google/gvisor . The exact module
  revision and checksums are pinned in gateway/go.mod and gateway/go.sum.
* Python: PSF license, https://www.python.org/ . Paramiko: LGPL-2.1,
  https://github.com/paramiko/paramiko . pycdlib: LGPL-2.1,
  https://github.com/clalancette/pycdlib . Cryptography: Apache-2.0/BSD-3-Clause,
  https://github.com/pyca/cryptography . PyInstaller's bootloader exception permits
  distributing frozen applications: https://pyinstaller.org/en/stable/license.html .

Local MSI files are development artifacts, not signed public releases. The
repository contains source and build recipes, not a redistribution of the VPN.
