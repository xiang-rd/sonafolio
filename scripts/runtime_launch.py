"""Bundle entry point; require an explicitly selected writable workspace."""
from pathlib import Path
import importlib.util
import json
import os
import sys


def main():
    root_value=os.environ.get('SONAFOLIO_WORKSPACE')
    if not root_value or not Path(root_value).is_absolute():
        raise SystemExit('A workspace must be selected before starting the runtime')
    workspace=Path(root_value).resolve(strict=True)
    runtime=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location('workspace_env',runtime/'workspace_env.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env=module.build_environment(workspace,os.environ,include_venv=False)
    os.environ.clear();os.environ.update(env)
    os.chdir(workspace)
    if sys.argv[1:]==['--check']:
        import av,numpy,torch,transformers,pyannote.audio
        from pywhispercpp.model import Model
        import tempfile,wave,ctypes
        with tempfile.TemporaryDirectory(dir=workspace/'.tmp') as directory:
            audio=Path(directory)/'synthetic.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\x00\x00'*1600)
            from torchcodec.decoders import AudioDecoder
            decoded=AudioDecoder(str(audio)).get_all_samples()
            if decoded.data.shape[-1]!=1600:raise RuntimeError('Bundled audio decoding failed')
        dyld=ctypes.CDLL(None)
        dyld._dyld_image_count.restype=ctypes.c_uint32
        dyld._dyld_get_image_name.argtypes=[ctypes.c_uint32]
        dyld._dyld_get_image_name.restype=ctypes.c_char_p
        loaded=[dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]
        if any(path.startswith(('/opt/homebrew/','/usr/local/')) for path in loaded):
            raise RuntimeError('Runtime borrowed non-system libraries outside its bundle')
        print(json.dumps({'python':sys.version.split()[0],'executable':sys.executable,
            'av':av.__version__,'numpy':numpy.__version__,'torch':torch.__version__,
            'transformers':transformers.__version__,'mps':torch.backends.mps.is_available(),
            'workspace':str(workspace),'runtime':str(runtime),'native_backends':'imported','torchcodec_decode':'passed','external_homebrew_libraries':False}))
        return
    sys.argv=['sonafolio',*(sys.argv[1:] or ['ipc'])]
    from sonafolio.cli import main as cli_main
    cli_main()


if __name__=='__main__':main()
