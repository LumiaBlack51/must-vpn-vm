"""Select the x86_64 emulator and its transitive PE dependencies from verified QEMU."""
import argparse
from pathlib import Path
import shutil
import pefile

p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args()
a.destination.mkdir(parents=True,exist_ok=True)
pending=['qemu-system-x86_64.exe','qemu-img.exe'];seen=set()
while pending:
    name=pending.pop();path=a.source/name
    if name.lower() in seen or not path.exists():continue
    seen.add(name.lower());shutil.copy2(path,a.destination/name)
    pe=pefile.PE(str(path),fast_load=True);pe.parse_data_directories(directories=[1,13])
    for e in getattr(pe,'DIRECTORY_ENTRY_IMPORT',[])+getattr(pe,'DIRECTORY_ENTRY_DELAY_IMPORT',[]):pending.append(e.dll.decode())
    pe.close()
share=a.destination/'share';share.mkdir(exist_ok=True)
for name in ['bios-256k.bin','bios.bin','kvmvapic.bin','linuxboot_dma.bin','multiboot_dma.bin','pvh.bin','efi-virtio.rom','vgabios-stdvga.bin']:
    shutil.copy2(a.source/'share'/name,share/name)
for name in ['COPYING','COPYING.LIB']:
    shutil.copy2(a.source/name,a.destination/name)
if (a.source/'share/doc').exists():shutil.copytree(a.source/'share/doc',share/'doc',dirs_exist_ok=True)
print(f'Staged {len(seen)} PE files and x86 firmware.')
