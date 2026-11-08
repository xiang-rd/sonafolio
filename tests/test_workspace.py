"""Verify environment boundaries without installing packages or models."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workspace", PROJECT / "scripts/workspace.py")
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_overrides_parent_paths_without_mutating_parent(self):
        parent = {
            "HOME": "/existing-home", "CODEX_HOME": "/existing-codex",
            "HF_HOME": "/external-models", "UV_CACHE_DIR": "/external-cache",
            "PYTHONPATH": "/external-packages", "PIP_TARGET": "/external-target",
        }
        before = parent.copy()
        env = workspace.build_environment(self.root, parent)
        self.assertEqual(parent, before)
        self.assertEqual(env["HOME"], parent["HOME"])
        self.assertEqual(env["CODEX_HOME"], parent["CODEX_HOME"])
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PIP_TARGET", env)
        for key in workspace.DIRECTORIES:
            self.assertTrue(Path(env[key]).is_relative_to(self.root), key)

    def test_configured_model_and_cache_roots_are_applied_before_child_imports(self):
        (self.root/'sonafolio.toml').write_text('[storage]\nmodels_dir="assets/models"\ncache_dir="assets/cache"\ndata_dir="records"\n')
        env=workspace.build_environment(self.root,{})
        self.assertEqual(env['HF_HOME'],str(self.root/'assets/cache/huggingface'))
        self.assertEqual(env['TORCH_HOME'],str(self.root/'assets/cache/torch'))
        self.assertEqual(env['SONAFOLIO_MODELS_DIR'],str(self.root/'assets/models'))
        self.assertEqual(env['SONAFOLIO_DATA_DIR'],str(self.root/'records'))
        self.assertTrue((self.root/'assets/cache/.gitignore').exists())

    def test_token_and_temp_are_local(self):
        env = workspace.build_environment(self.root, {"HF_TOKEN_PATH": "/external-token"})
        self.assertTrue(Path(env["HF_TOKEN_PATH"]).is_relative_to(self.root))
        self.assertEqual(env["TMPDIR"], env["TEMP"])
        self.assertEqual(env["TEMP"], env["TMP"])
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["UV_NO_CONFIG"], "1")
        self.assertEqual(env["PYANNOTE_METRICS_ENABLED"], "0")

    def test_rejects_symlink_escape_before_creating_runtime(self):
        outside = self.root / "outside"
        outside.mkdir()
        project = self.root / "project"
        project.mkdir()
        try:
            (project / ".cache").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this platform")
        with self.assertRaises(ValueError):
            workspace.build_environment(project, {})
        self.assertFalse((project / ".runtime").exists())

    def test_uv_and_python_do_not_fall_back_to_global_tools(self):
        for command in (["uv", "--version"], ["python", "--version"], ["pip", "--version"]):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "not installed"):
                    workspace.resolve_command(self.root, command)

    def test_child_observes_workspace_temp_and_cache(self):
        env = workspace.build_environment(self.root, dict(os.environ))
        code = (
            "import json, os, tempfile; "
            "print(json.dumps([tempfile.gettempdir(), os.environ['HF_HOME']]))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], env=env, cwd=self.root,
            check=True, capture_output=True, text=True,
        )
        for path in json.loads(result.stdout):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root))

    @unittest.skipIf(os.name == "nt", "POSIX virtualenv symlink layout")
    def test_python_keeps_virtualenv_invocation_path(self):
        base = self.root / ".runtime/python/bin/python"
        base.parent.mkdir(parents=True)
        base.touch()
        venv = self.root / ".venv/bin/python"
        venv.parent.mkdir(parents=True)
        venv.symlink_to(base)
        command = workspace.resolve_command(self.root, ["python", "-V"])
        self.assertEqual(command, [str(venv), "-V"])

    def test_launcher_uses_script_location_and_hides_secrets(self):
        env = dict(os.environ, HF_TOKEN="sentinel-secret-must-not-print")
        result = subprocess.run(
            [sys.executable, "-B", str(PROJECT / "scripts/workspace.py"), "doctor"],
            cwd=self.root, env=env, capture_output=True, text=True, check=True,
        )
        self.assertNotIn(env["HF_TOKEN"], result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["paths"]["SONAFOLIO_WORKSPACE"], str(PROJECT))

    def test_launcher_preserves_child_failure_status(self):
        result = subprocess.run(
            [sys.executable, "-B", str(PROJECT / "scripts/workspace.py"),
             "run", "--", sys.executable, "-B", "-c", "raise SystemExit(7)"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
