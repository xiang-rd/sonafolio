"""Distribution audit must catch private data and links before signing a copy."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('package_macos', Path(__file__).resolve().parents[1]/'scripts/package_macos.py')
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


class MacPackageAuditTests(unittest.TestCase):
    def test_rejects_external_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root/'Test.app'
            app.mkdir()
            outside = root/'private.txt'
            outside.write_text('fixture')
            (app/'link').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'External symlink'):
                packaging.audit(app)

    def test_rejects_private_config_and_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            for name in ['sonafolio.toml', '.env.local', 'recording.m4a', '.models/model', 'secrets/token']:
                with self.subTest(name=name):
                    target = app/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b'fixture')
                    with self.assertRaisesRegex(ValueError, 'Unexpected private/runtime data'):
                        packaging.audit(app)
                    target.unlink()

    def test_identifies_nested_macho_and_allows_internal_link(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            binary = app/'Contents/Resources/runtime/lib.dylib'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(bytes.fromhex('cffaedfe')+b'fixture')
            (binary.parent/'alias').symlink_to('lib.dylib')
            # A dependency's public sample is allowed; user M4A files are rejected.
            (binary.parent/'sample.wav').write_bytes(b'fixture')
            self.assertEqual(packaging.audit(app), [binary])
