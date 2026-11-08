"""Storage and downloads use disposable bytes and mocked network only."""
import hashlib,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sonafolio.paths import model_path
from sonafolio.storage_config import storage_roots
from sonafolio.storage import plan_storage,apply_storage
from sonafolio.desktop_settings import save_settings,get_settings
from sonafolio.model_manager import fetch_file,verify_model,download_in_worker,ModelError,CATALOG,model_catalog
from sonafolio.processes import CancelledError
ROOT=Path(__file__).resolve().parents[1]

class StorageModelTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(self.root)});env.start();self.addCleanup(env.stop)
    def test_paths_are_disjoint_and_cannot_alias_source_or_escape(self):
        for value in ['../out','/outside','desktop/files','data/models','.runtime/models','skills/models']:
            with self.subTest(value=value),self.assertRaises(ValueError):storage_roots(self.root,{'models_dir':value})
        with self.assertRaises(ValueError):storage_roots(self.root,{'models_dir':'assets','cache_dir':'assets/cache'})
        (self.root/'linked').symlink_to(self.root.parent,target_is_directory=True)
        with self.assertRaises(ValueError):storage_roots(self.root,{'models_dir':'linked'})
        save_settings(models_dir='assets/models',cache_dir='assets/cache')
        self.assertEqual(model_path('.models/whisper.cpp/model.bin'),self.root/'assets/models/whisper.cpp/model.bin')
        self.assertEqual(os.environ['HF_HOME'],str(self.root/'assets/cache/huggingface'))
        self.assertTrue((self.root/'assets/models/.gitignore').is_file())
    def test_copy_preserves_original_and_configuration_switches_after_verification(self):
        source=self.root/'.models/asr';source.mkdir(parents=True);(source/'weight.bin').write_bytes(b'original weights')
        target={'models_dir':'assets/models','cache_dir':'assets/cache'}
        plan=plan_storage(target,'copy');self.assertEqual(plan['bytes'],16)
        result=apply_storage(target,'copy');self.assertEqual(result['state'],'completed')
        self.assertEqual((source/'weight.bin').read_bytes(),b'original weights')
        self.assertEqual((self.root/'assets/models/asr/weight.bin').read_bytes(),b'original weights')
        self.assertEqual(get_settings()['storage']['models_dir'],'assets/models')
    def test_cancel_failure_conflicts_and_symlinks_preserve_original_config(self):
        save_settings(llm={'model':'preserved'});before=(self.root/'sonafolio.toml').read_bytes()
        source=self.root/'.models';source.mkdir(exist_ok=True);(source/'a.bin').write_bytes(b'a'*100)
        with self.assertRaises(CancelledError):apply_storage({'models_dir':'new-models'},'copy',cancelled=lambda:True)
        with patch('sonafolio.desktop_settings.save_settings',side_effect=RuntimeError('synthetic failure')),self.assertRaises(RuntimeError):apply_storage({'models_dir':'new-models'},'copy')
        self.assertEqual((self.root/'sonafolio.toml').read_bytes(),before);self.assertFalse((self.root/'new-models').exists())
        (self.root/'existing').mkdir();(self.root/'existing/file').write_bytes(b'existing')
        with self.assertRaises(ValueError):plan_storage({'models_dir':'existing'},'copy')
        (source/'linked').symlink_to(source/'a.bin')
        with self.assertRaises(ValueError):plan_storage({'models_dir':'new-models'},'copy')
    def test_existing_directory_switch_does_not_copy_or_delete(self):
        target=self.root/'already-there';target.mkdir();(target/'custom').write_bytes(b'keep')
        apply_storage({'models_dir':'already-there'},'existing')
        self.assertEqual((target/'custom').read_bytes(),b'keep')
        self.assertEqual(get_settings()['paths']['models'],str(target))
    def test_hf_token_is_private_write_only_and_invalid_updates_are_atomic(self):
        token='hf_synthetic_token_only_123456';result=save_settings(hf_token=token)
        self.assertTrue(result['hf_token_configured']);self.assertNotIn(token,json.dumps(result))
        file=self.root/'.runtime/secrets/huggingface-token';self.assertEqual(file.stat().st_mode&0o777,0o600)
        before=(self.root/'sonafolio.toml').read_bytes()
        with self.assertRaises(ValueError):save_settings(hf_token='bad',llm={'model':'changed'})
        self.assertEqual(before,(self.root/'sonafolio.toml').read_bytes())
    def test_range_resume_and_ignored_range_never_duplicate_bytes(self):
        class Response:
            status_code=206;headers={'Content-Range':'bytes 3-5/6'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def raise_for_status(self):pass
            def iter_bytes(self,*args):yield b'def'
        entry={'size':6,'sha256':hashlib.sha256(b'abcdef').hexdigest()}
        target=self.root/'partial';target.write_bytes(b'abc')
        with patch('httpx.stream',return_value=Response()) as request:
            fetch_file('https://example.invalid/file',target,entry)
            self.assertEqual(request.call_args.kwargs['headers']['Range'],'bytes=3-')
        self.assertEqual(target.read_bytes(),b'abcdef')
        class Full(Response):
            status_code=200
            def iter_bytes(self,*args):yield b'abcdef'
        target.write_bytes(b'ab')
        with patch('httpx.stream',return_value=Full()):fetch_file('https://example.invalid/file',target,entry)
        self.assertEqual(target.read_bytes(),b'abcdef')
        class Wrong(Full):
            def iter_bytes(self,*args):yield b'xxxxxx'
        target.write_bytes(b'ab')
        with patch('httpx.stream',return_value=Wrong()),self.assertRaises(ModelError):fetch_file('https://example.invalid/file',target,entry)
        self.assertFalse(target.exists())
    def test_verified_installation_retains_previous_model_and_checks_all_files(self):
        identifier='vad';value=CATALOG[identifier];directory=self.root/'.models'/value['directory'];directory.mkdir(parents=True)
        (directory/'keep.bin').write_bytes(b'previous model')
        content=b'synthetic model';entry=SimpleNamespace(rfilename=value['files'][0],lfs=SimpleNamespace(sha256=hashlib.sha256(content).hexdigest()),blob_id=None,size=len(content))
        info=SimpleNamespace(sha='a'*40,siblings=[entry]);status=self.root/'status.json'
        def fake(url,target,entry,**kwargs):target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content);kwargs['progress'](len(content))
        with patch('huggingface_hub.HfApi') as api,patch('sonafolio.model_manager.fetch_file',side_effect=fake):
            api.return_value.model_info.return_value=info;download_in_worker(identifier,status)
        self.assertEqual(verify_model(identifier)['state'],'completed')
        self.assertTrue(any((self.root/'.models/.history').glob('*/keep.bin')))
        self.assertTrue(next(i for i in model_catalog()['items'] if i['id']==identifier)['verified'])
        (directory/value['files'][0]).write_bytes(b'bad')
        with self.assertRaises(ModelError):verify_model(identifier)
        self.assertFalse(next(i for i in model_catalog()['items'] if i['id']==identifier)['verified'])
    def test_failed_download_leaves_installed_model_untouched(self):
        value=CATALOG['vad'];directory=self.root/'.models'/value['directory'];directory.mkdir(parents=True)
        original=directory/value['files'][0];original.write_bytes(b'old')
        info=SimpleNamespace(sha='a'*40,siblings=[SimpleNamespace(rfilename=value['files'][0],lfs=SimpleNamespace(sha256='0'*64),size=1)])
        with patch('huggingface_hub.HfApi') as api,patch('sonafolio.model_manager.fetch_file',side_effect=ModelError('NETWORK_ERROR')):
            api.return_value.model_info.return_value=info
            with self.assertRaises(ModelError):download_in_worker('vad',self.root/'status.json')
        self.assertEqual(original.read_bytes(),b'old')
