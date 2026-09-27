"""Build a per-user MSI with no custom actions, service install, or network changes."""
import argparse
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as X

p=argparse.ArgumentParser();p.add_argument('--wix',required=True,type=Path);p.add_argument('--stage',required=True,type=Path);p.add_argument('--out',required=True,type=Path);a=p.parse_args()
ns='http://schemas.microsoft.com/wix/2006/wi';X.register_namespace('',ns)
def node(parent,tag,**attrs):return X.SubElement(parent,'{'+ns+'}'+tag,attrs)
root=X.Element('{'+ns+'}Wix');product=node(root,'Product',Id='*',Name='MUST VPN VM (Preview)',Language='1033',Version='0.2.4',Manufacturer='MUST VM Project',UpgradeCode='71F289BB-C190-4937-BEB7-0F541BC89105')
node(product,'Package',InstallerVersion='500',Compressed='yes',InstallScope='perUser',Platform='x64')
node(product,'MajorUpgrade',DowngradeErrorMessage='A newer MUST VPN VM is installed.')
node(product,'MediaTemplate',EmbedCab='yes',CompressionLevel='high')
node(product,'Condition',Message='64-bit Windows is required.').text='VersionNT64'
target=node(product,'Directory',Id='TARGETDIR',Name='SourceDir');node(target,'Directory',Id='DesktopFolder',Name='Desktop');local=node(target,'Directory',Id='LocalAppDataFolder');install=node(local,'Directory',Id='INSTALLDIR',Name='MUST VPN VM App')
feature=node(product,'Feature',Id='Main',Title='MUST VPN VM',Level='1')
dirs={Path('.'):install}
for index,path in enumerate(sorted(a.stage.rglob('*'))):
    relative=path.relative_to(a.stage)
    if path.is_dir():
        dirs[relative]=node(dirs[relative.parent],'Directory',Id='D'+str(index),Name=path.name);continue
    component=node(dirs[relative.parent],'Component',Id='C'+str(index),Guid=str(uuid.uuid5(uuid.NAMESPACE_URL,'must-vm/'+relative.as_posix())),Win64='yes')
    file=node(component,'File',Id='F'+str(index),Source=str(path.resolve()),KeyPath='yes')
    node(feature,'ComponentRef',Id='C'+str(index))
    if relative.as_posix()=='must-vm.exe':
        node(component,'Environment',Id='AddPath',Name='PATH',Value='[INSTALLDIR]',Action='set',Part='last',System='no')
shortcut=node(install,'Component',Id='DesktopShortcutComponent',
              Guid=str(uuid.uuid5(uuid.NAMESPACE_URL,'must-vm/desktop-terminal')),Win64='yes')
node(shortcut,'Shortcut',Id='DesktopTerminal',Directory='DesktopFolder',Name='MUST VPN Terminal',
     Description='Open a terminal inside the isolated school VPN',Target='[INSTALLDIR]terminal.cmd',
     WorkingDirectory='INSTALLDIR',Advertise='no')
node(shortcut,'RegistryValue',Root='HKCU',Key='Software\\MUSTVPNVM',Name='DesktopTerminal',
     Type='integer',Value='1',KeyPath='yes')
node(feature,'ComponentRef',Id='DesktopShortcutComponent')
a.out.parent.mkdir(parents=True,exist_ok=True)
wxs=a.out.with_suffix('.wxs');wixobj=a.out.with_suffix('.wixobj')
X.ElementTree(root).write(wxs,encoding='utf-8',xml_declaration=True)
subprocess.run([str(a.wix/'candle.exe'),'-nologo','-out',str(wixobj),str(wxs)],check=True)
# Per-user components deliberately have file key paths. Do not suppress other ICE errors.
subprocess.run([str(a.wix/'light.exe'),'-nologo','-sice:ICE38','-sice:ICE64','-out',str(a.out),str(wixobj)],check=True)
print(a.out)
