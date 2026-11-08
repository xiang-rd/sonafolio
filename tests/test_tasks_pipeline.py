"""Synthetic recordings and fake stages only: no model downloads or cloud calls."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from typer.testing import CliRunner

from sonafolio.audio import fingerprint, record_input
from sonafolio.cli import app
from sonafolio.migration import migrate_legacy
from sonafolio.paths import write_json
from sonafolio.pipeline import RunOptions, run_pipeline, resume_pipeline
from sonafolio.processes import CancelledError, run_stage_process
from sonafolio.tasks import data_root, file_lock, load_manifest, managed_input, rebuild_index, rename_task

ROOT = Path(__file__).resolve().parents[1]


class TaskPipelineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {'SONAFOLIO_WORKSPACE':str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.audio = self.root / 'original.wav'
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\x00\x00' * 800)
        self.model = self.root / '.models/test.bin'
        self.model.parent.mkdir()
        self.model.write_bytes(b'fake model')
        self.options = RunOptions(model=str(self.model), vad_model=None, refine=False, analysis='none')
        # Actual signatures except unavailable model manifests; source/parameter hashes
        # remain real, so invalidation is tested across meaningful dependencies.
        from sonafolio.pipeline import stage_settings
        def settings(stage, options):
            if stage == 'diarization':
                return {'speakers':options.num_speakers, 'device':options.speaker_device, 'range':[options.start,options.duration]}
            return stage_settings(stage, options)
        adapter = patch('sonafolio.pipeline.stage_settings', side_effect=settings)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.calls = []

    def fake_stage(self, stage, inputs, options, output, cancelled, force=False):
        self.calls.append(stage)
        sha = fingerprint(self.audio)
        if stage == 'diarization':
            write_json(output / 'speakers.json', {'source_sha256':sha, 'state':'completed', 'speaker_ids':['A','B','C'],
                       'range':{'start':options.start,'end':.1}, 'turns':[]})
        elif stage in {'asr','annotation','refinement'}:
            write_json(output / 'transcript.json', {'source_sha256':sha, 'stage':stage, 'range':{'start':options.start,'end':.1},
                       'diarization':{'state':'completed' if stage!='asr' else 'pending'},
                       'segments':[{'id':'s1','start':0,'end':.1,'text':'测试','speaker_id':'A'}]})
        else:
            if options.analysis == 'preview':
                write_json(output / 'checkpoints/request-plan.json', {'api_requests_made':0})
                (output / 'checkpoints/outbound-text.md').write_text('local preview')
            else:
                write_json(output / 'summary.json', {'metadata':{'source_sha256':sha,'skill_id':'interview','input_transcript':Path(inputs['transcript']).relative_to(output.parents[1]).as_posix()},'result':{}})
                write_json(output / 'scene.json', {'state':'frozen'})
                (output / 'summary.md').write_text('[引用](sources.md#s1)')
                (output / 'sources.md').write_text('<a id="s1"></a>')
        return {'output_directory':str(output)}

    def test_import_title_and_paths_are_stable_and_originals_are_protected(self):
        before = fingerprint(self.audio)
        _, task = record_input(self.audio)
        renamed_file = self.root / 'renamed.wav'
        shutil.copy2(self.audio, renamed_file)
        _, same = record_input(renamed_file)
        self.assertEqual(task, same)
        self.assertEqual(task.name, before)
        rename_task(task.name, '新的标题')
        self.assertEqual(load_manifest(task)['title'], '新的标题')
        self.assertEqual(fingerprint(self.audio),before)
        with self.assertRaisesRegex(ValueError,'managed task inputs'):
            write_json(managed_input(task),{})
        self.assertEqual(len(list((task/'input').iterdir())),1)
        managed_input(task).write_bytes(b'corrupt fixture')
        with self.assertRaisesRegex(ValueError,'changed'):
            record_input(self.audio)

    def test_rename_missing_task_does_not_create_a_directory_that_blocks_import(self):
        identifier=fingerprint(self.audio)
        with self.assertRaises(FileNotFoundError):
            rename_task(identifier,'not imported')
        self.assertFalse((self.root/'data/runs'/identifier).exists())
        _,task=record_input(self.audio)
        self.assertTrue((task/'manifest.json').exists())

    def test_data_root_config_rejects_escape_protected_paths_and_symlinks(self):
        config = self.root / 'sonafolio.toml'
        for value in ['../escape','.models/data','src/data','/outside','.']:
            config.write_text('[storage]\ndata_dir=' + json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                data_root()
        config.write_text('[storage]\ndata_dir="records"')
        _, task = record_input(self.audio)
        self.assertEqual(task.parent.parent, self.root / 'records')
        self.assertIn('# Sonafolio private data', (self.root/'records/.gitignore').read_text())
        try:
            (self.root/'linked').symlink_to(self.root.parent,target_is_directory=True)
        except OSError:
            return
        config.write_text('[storage]\ndata_dir="linked"')
        with self.assertRaises(ValueError):
            data_root()

    def test_success_reuse_corruption_invalidation_and_force_preserves_history(self):
        first = run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls,['asr','diarization','annotation'])
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(len(self.calls),3)
        task = Path(first['run_directory'])
        manifest = load_manifest(task)
        artifact = task / manifest['stages']['asr'][0]['directory'] / 'transcript.json'
        artifact.write_text('{}')
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls.count('asr'),2)
        # Restored identical ASR content permits reusing downstream results.
        self.assertEqual(self.calls.count('annotation'),1)
        previous = list(task.glob('asr/*/transcript.json'))
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage,force=True)
        self.assertTrue(all(path.exists() for path in previous))
        self.assertEqual(len(list(task.glob('asr/*/transcript.json'))),3)
        self.options.num_speakers = 4
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls.count('asr'),3)
        self.assertEqual(self.calls.count('diarization'),3)

    def test_failure_is_durable_and_resume_skips_completed_stage(self):
        def fail(stage,*args):
            if stage == 'diarization':
                raise RuntimeError('synthetic failure')
            return self.fake_stage(stage,*args)
        with self.assertRaises(RuntimeError):
            run_pipeline(self.audio,options=self.options,executor=fail)
        _, task = record_input(self.audio)
        self.assertEqual(load_manifest(task)['state'],'failed')
        resumed = resume_pipeline(task.name,executor=self.fake_stage)
        self.assertEqual(resumed['state'],'completed')
        self.assertEqual(self.calls.count('asr'),1)
        self.assertEqual(load_manifest(task)['stages']['diarization'][0]['state'],'failed')

    def test_cancel_is_saved_and_locks_prevent_concurrent_writers(self):
        stop = threading.Event()
        def cancel_after_asr(stage,*args):
            result=self.fake_stage(stage,*args)
            stop.set()
            return result
        with self.assertRaises(CancelledError):
            run_pipeline(self.audio,options=self.options,executor=cancel_after_asr,cancelled=stop.is_set)
        _, task = record_input(self.audio)
        self.assertEqual(load_manifest(task)['state'],'cancelled')
        with file_lock(task/'.pipeline.lock'):
            with self.assertRaisesRegex(RuntimeError,'already in use'):
                rename_task(task.name,'blocked')
        resume_pipeline(task.name,executor=self.fake_stage)
        self.assertEqual(load_manifest(task)['state'],'completed')

    def test_changed_model_invalidates_asr_without_repeating_speakers(self):
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.model.write_bytes(b'new fake weights')
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls.count('asr'),2)
        self.assertEqual(self.calls.count('diarization'),1)

    def test_preview_cloud_and_prompt_changes_only_repeat_analysis(self):
        self.options.analysis='preview'
        first=run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(first['state'],'preview_only')
        self.assertFalse(list(Path(first['run_directory']).glob('analysis/*/summary.json')))
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls.count('analysis'),1)
        self.options.analysis='cloud'
        result=run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        task=Path(result['run_directory'])
        prompt=self.root/'prompt.md';prompt.write_text('a')
        self.options.prompt=str(prompt)
        run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        self.assertEqual(self.calls.count('asr'),1)
        self.assertEqual(self.calls.count('analysis'),3)
        self.assertEqual(len(load_manifest(task)['analysis_versions']),2)
        indexed=rebuild_index()
        self.assertEqual(indexed['analyses'],2)
        with sqlite3.connect(data_root()/'index.sqlite') as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],2)

    def test_partial_analysis_continues_same_checkpoint_directory(self):
        self.options.analysis='preview'
        failed=[]
        def partial(stage, inputs, options, output, cancelled, force=False):
            if stage=='analysis' and not failed:
                failed.append(output)
                write_json(output/'checkpoints/partial.json',{'saved':True})
                raise RuntimeError('interrupt after a paid chunk')
            if stage=='analysis':
                self.assertEqual(output,failed[0])
                self.assertTrue((output/'checkpoints/partial.json').exists())
            return self.fake_stage(stage,inputs,options,output,cancelled,force)
        with self.assertRaises(RuntimeError):
            run_pipeline(self.audio,options=self.options,executor=partial)
        run_pipeline(self.audio,options=self.options,executor=partial)
        self.assertEqual(self.calls.count('asr'),1)

    def test_real_stage_worker_annotation_and_preview_without_models_or_cloud(self):
        from sonafolio.pipeline import default_executor
        def hybrid(stage, inputs, options, output, cancelled, force=False):
            if stage in {'annotation','analysis'}:
                return default_executor(stage,inputs,options,output,cancelled,force)
            return self.fake_stage(stage,inputs,options,output,cancelled,force)
        self.options.analysis='preview'
        result=run_pipeline(self.audio,options=self.options,executor=hybrid)
        output=Path(result['output_directory'])
        self.assertTrue((output/'catalog.json').is_file())
        self.assertEqual(json.loads((output/'checkpoints/request-plan.json').read_text())['api_requests_made'],0)
        self.assertIn('测试',(output/'checkpoints/outbound-text.md').read_text())

    def legacy_fixture(self):
        legacy=self.root/'data/runs/old-name--123'
        legacy.mkdir(parents=True)
        sha=fingerprint(self.audio)
        write_json(legacy/'input.json',{'source':str(self.audio),'sha256':sha})
        write_json(legacy/'refined/v1/transcript.json',{'source_sha256':sha,'segments':[]})
        output=self.root/'data/output/interview'/f'{legacy.name}--abc-m16384-v3'
        write_json(output/'summary.json',{'metadata':{'source_sha256':sha,'skill_id':'interview','input_transcript':str(legacy/'refined/v1/transcript.json')},'result':{'participants':[],'facts':[{'text':'事实','segment_ids':['s1']}]}})
        (output/'summary.md').write_text('[引用](sources.md#s1)')
        (output/'sources.md').write_text('<a id="s1"></a>')
        write_json(legacy/'analysis/abc/notes-000.json',{'source_transcript':str(legacy/'refined/v1/transcript.json')})
        return legacy,output

    def test_copy_migration_keeps_old_data_and_rebuilds_index(self):
        legacy,oldoutput=self.legacy_fixture()
        before=fingerprint(oldoutput/'summary.json')
        preview=migrate_legacy(legacy)
        self.assertEqual(preview['state'],'preview_only')
        self.assertFalse((Path(preview['task_directory'])/'manifest.json').exists())
        result=migrate_legacy(legacy,apply=True)
        task=Path(result['task_directory'])
        self.assertEqual(result['state'],'migrated')
        self.assertEqual(fingerprint(oldoutput/'summary.json'),before)
        self.assertTrue((legacy/'refined/v1/transcript.json').is_file())
        summary=next(task.glob('analysis/*/summary.json'))
        self.assertTrue((task / json.loads(summary.read_text())['metadata']['input_transcript']).exists())
        self.assertTrue((summary.parent/'checkpoints/notes-000.json').is_file())
        self.assertEqual(json.loads((summary.parent/'scene.json').read_text())['state'],'legacy_snapshot_unavailable')
        self.assertEqual(rebuild_index()['analyses'],1)
        self.assertEqual(migrate_legacy(legacy,apply=True)['state'],'already_migrated')

    def test_failed_migration_rolls_back_publication_and_preserves_old_data(self):
        legacy,oldoutput=self.legacy_fixture()
        (oldoutput/'summary.md').write_text('[坏引用](sources.md#missing)')
        with self.assertRaisesRegex(ValueError,'broken source links'):
            migrate_legacy(legacy,apply=True)
        _, task=record_input(self.audio)
        self.assertFalse(list((task/'analysis').glob('*/summary.json')))
        self.assertEqual(load_manifest(task).get('migrations',{}),{})
        self.assertTrue((oldoutput/'summary.json').exists())
        self.assertFalse(list(task.glob('.migration-*')))

    def test_rebuild_rejects_broken_references_without_changing_existing_index(self):
        self.options.analysis='cloud'
        result=run_pipeline(self.audio,options=self.options,executor=self.fake_stage)
        rebuild_index()
        output=Path(result['output_directory'])
        (output/'summary.md').write_text('[坏引用](sources.md#missing)')
        with self.assertRaisesRegex(ValueError,'broken source links'):
            rebuild_index()
        with sqlite3.connect(data_root()/'index.sqlite') as db:
            self.assertEqual(db.execute('select count(*) from analyses').fetchone()[0],1)

    def test_index_failure_rolls_back_published_migration_directories(self):
        legacy,oldoutput=self.legacy_fixture()
        from sonafolio.tasks import rebuild_index as real_rebuild
        calls=[]
        def fail_once():
            calls.append(True)
            if len(calls)==1:
                raise RuntimeError('simulated index transaction failure')
            return real_rebuild()
        with patch('sonafolio.migration.rebuild_index',side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError,'transaction failure'):
                migrate_legacy(legacy,apply=True)
        _,task=record_input(self.audio)
        self.assertFalse(list(task.glob('analysis/*/summary.json')))
        self.assertFalse(list(task.glob('alignment/*/transcript.json')))
        self.assertTrue((oldoutput/'summary.json').exists())

    def test_cli_exposes_run_resume_and_explicit_cloud_mode(self):
        help_result=CliRunner().invoke(app,['run','--help'])
        self.assertEqual(help_result.exit_code,0,help_result.output)
        self.assertIn('preview',help_result.output)
        with patch('sonafolio.pipeline.run_pipeline',return_value={'state':'preview_only'}) as runner:
            result=CliRunner().invoke(app,['run',str(self.audio)])
            self.assertEqual(result.exit_code,0,result.output)
            self.assertEqual(runner.call_args.kwargs['options'].analysis,'preview')

    @unittest.skipIf(os.name=='nt','POSIX process-group assertion; Windows requires native validation')
    def test_cancellation_stops_native_child_process_tree(self):
        marker=self.root/'child-wrote'
        child='import time;from pathlib import Path;time.sleep(1);Path('+repr(str(marker))+').write_text("orphan")'
        parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(30)'
        stop=threading.Event()
        timer=threading.Timer(.2,stop.set);timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(CancelledError):
            run_stage_process([sys.executable,'-c',parent],self.root/'process.log',stop.is_set)
        time.sleep(1.1)
        self.assertFalse(marker.exists(),'native child survived cancellation')


if __name__ == '__main__':
    unittest.main()
