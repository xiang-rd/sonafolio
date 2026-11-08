import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from sonafolio.desktop_settings import get_settings, save_settings, scene_catalog

ROOT=Path(__file__).resolve().parents[1]
class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(self.root)});env.start();self.addCleanup(env.stop)
    def test_configuration_persists_and_credentials_never_return(self):
        key='test-only-secret-sentinel-not-real'
        (self.root/'sonafolio.toml').write_text('[other]\nvalue="preserve"\n[llm]\nmodel="old"\n')
        result=save_settings(llm={'model':'new-model'},api_key=key,data_dir='recordings',appearance='light')
        self.assertTrue(result['api_key_configured']);self.assertNotIn(key,json.dumps(result))
        values=tomllib.loads((self.root/'sonafolio.toml').read_text())
        self.assertEqual(values['other']['value'],'preserve');self.assertEqual(values['llm']['model'],'new-model')
        self.assertEqual(get_settings()['storage']['data_dir'],'recordings')
        self.assertEqual(get_settings()['appearance'],'light')
        path=self.root/'.runtime/secrets/deepseek-api-key'
        self.assertEqual(path.read_text(),key)
        self.assertNotIn(key,(self.root/'sonafolio.toml').read_text())
        if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_invalid_directory_or_key_does_not_replace_existing_configuration(self):
        save_settings(llm={'model':'preserved'})
        before=(self.root/'sonafolio.toml').read_bytes()
        for values in [{'data_dir':'../outside'},{'data_dir':'.models/data'},{'api_key':'short'}]:
            with self.subTest(values=values),self.assertRaises(ValueError):save_settings(**values)
            self.assertEqual((self.root/'sonafolio.toml').read_bytes(),before)
    def test_scene_catalog_is_resolved_and_read_only(self):
        result=scene_catalog();ids={s['id'] for s in result['items']}
        self.assertIn('interview.technical',ids)
        self.assertTrue(all(s['prompt'] and s['template'] for s in result['items']))
        self.assertFalse((self.root/'skills').exists())
