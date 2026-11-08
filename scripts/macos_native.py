"""Make TorchCodec use the FFmpeg ABI already vendored by PyAV."""
from pathlib import Path
import re
import subprocess


def vendor_codec(site: Path):
    codec=site/'torchcodec'
    vendor=site/'av/.dylibs'
    for path in codec.glob('libtorchcodec_*.dylib'):
        match=re.fullmatch(r'libtorchcodec_(?:core|custom_ops)(\d+)\.dylib',path.name)
        if match and match[1]!='8':
            path.unlink()
    for path in [codec/'libtorchcodec_core8.dylib',codec/'libtorchcodec_custom_ops8.dylib']:
        output=subprocess.check_output(['otool','-L',str(path)],text=True)
        for line in output.splitlines()[1:]:
            dependency=line.strip().split(' ',1)[0]
            name=Path(dependency).name
            if not name.startswith(('libav','libsw')):continue
            pattern=name.removesuffix('.dylib')+'.*.dylib'
            choices=sorted(vendor.glob(pattern))
            if len(choices)!=1:raise RuntimeError('Missing matching bundled FFmpeg ABI: '+name)
            replacement='@loader_path/../av/.dylibs/'+choices[0].name
            subprocess.run(['install_name_tool','-change',dependency,replacement,str(path)],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['codesign','--force','--sign','-',str(path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    import sys
    vendor_codec(Path(sys.argv[1]))
