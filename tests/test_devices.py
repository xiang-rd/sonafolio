import os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sonafolio.devices import mps_available,speaker_device
ROOT=Path(__file__).resolve().parents[1]
class DeviceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.addCleanup(temporary.cleanup)
        env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(Path(temporary.name).resolve()),'HF_TOKEN':'PRIVATE','DEEPSEEK_API_KEY':'PRIVATE'});env.start();self.addCleanup(env.stop)
        mps_available.cache_clear();self.addCleanup(mps_available.cache_clear)
    def test_mac_auto_uses_available_mps_and_probe_never_receives_credentials(self):
        with patch('sonafolio.devices.sys.platform','darwin'),patch('sonafolio.devices.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"mps":true}')) as process:
            self.assertEqual(speaker_device('auto'),'mps');self.assertEqual(speaker_device('auto'),'mps');self.assertEqual(process.call_count,1)
            self.assertNotIn('HF_TOKEN',process.call_args.kwargs['env']);self.assertNotIn('DEEPSEEK_API_KEY',process.call_args.kwargs['env'])
    def test_auto_falls_back_when_unavailable_or_probe_fails(self):
        for response in [SimpleNamespace(returncode=0,stdout='{"mps":false}'),SimpleNamespace(returncode=1,stdout=''),SimpleNamespace(returncode=0,stdout='bad')]:
            mps_available.cache_clear()
            with patch('sonafolio.devices.sys.platform','darwin'),patch('sonafolio.devices.subprocess.run',return_value=response):self.assertEqual(speaker_device('auto'),'cpu')
        with patch('sonafolio.devices.sys.platform','win32'):self.assertEqual(speaker_device('auto'),'cpu')
    def test_explicit_cpu_does_not_probe_and_preferences_persist(self):
        with patch('sonafolio.devices.subprocess.run') as process:
            self.assertEqual(speaker_device('cpu'),'cpu');process.assert_not_called()
        from sonafolio.desktop_settings import save_settings,get_settings
        from sonafolio.devices import configured_speaker_device
        save_settings(speaker_device='cpu');self.assertEqual(configured_speaker_device(),'cpu');self.assertEqual(get_settings()['processing']['speaker_device'],'cpu')
        with self.assertRaises(ValueError):save_settings(speaker_device='invalid')
