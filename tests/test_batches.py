import json,os,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch
from sonafolio.batches import create_batch,run_batch,load_batch,batch_view,resolve_item,list_batches
from sonafolio.pipeline import RunOptions
from sonafolio.tasks import managed_input,task_directory
from sonafolio.skills import SkillRegistry
from sonafolio.scenes import save_scene,as_definition
ROOT=Path(__file__).resolve().parents[1]
class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(self.root)});env.start();self.addCleanup(env.stop)
        self.files=[]
        for i in range(3):
            path=self.root/f'recording-{i}.wav'
            with wave.open(str(path),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes((i+1).to_bytes(2,'little')*800)
            self.files.append(str(path))
    def test_restart_failure_isolation_and_original_files_are_not_needed_after_import(self):
        batch=create_batch(self.files,RunOptions(analysis='none',refine=False));calls=[]
        def fake(audio,*,options,**kwargs):
            calls.append(audio);self.assertTrue(audio.parent.name=='input')
            if len(calls)==1:raise RuntimeError('synthetic failure')
            return {'state':'completed','task_id':audio.parent.parent.name}
        result=run_batch(batch['batch_id'],runner=fake)
        self.assertEqual(result['state'],'completed_with_errors');self.assertEqual(len(calls),3)
        stored=load_batch(batch['batch_id']);self.assertEqual([i['state'] for i in stored['items']],['failed','completed','completed'])
        for file in self.files:Path(file).unlink()
        resumed=[]
        def success(audio,**kwargs):resumed.append(audio);return {'state':'completed','task_id':audio.parent.parent.name}
        self.assertEqual(run_batch(batch['batch_id'],runner=success)['state'],'completed');self.assertEqual(len(resumed),1)
        self.assertEqual(list_batches()['items'][0]['total'],3)
        self.assertNotIn('source',batch_view(batch['batch_id'])['items'][0])
    def test_pause_preserves_queue_and_catalog_is_frozen_across_edits(self):
        batch=create_batch(self.files,RunOptions(analysis='none'));original=(Path(batch['options']['catalog'])).read_bytes();calls=[]
        def fake(audio,*,options,**kwargs):calls.append(audio);return {'state':'completed','task_id':audio.parent.parent.name}
        result=run_batch(batch['batch_id'],runner=fake,cancelled=lambda:len(calls)>=1)
        self.assertEqual(result['state'],'paused');self.assertEqual(len(calls),1)
        value=as_definition(SkillRegistry().get('interview.technical'));value['prompt']='new prompt'
        save_scene(value,expected_version='1.0.0')
        frozen=[]
        def inspect(audio,*,options,**kwargs):
            registry=SkillRegistry.from_snapshot(json.loads(Path(options.catalog).read_text()))
            frozen.append(registry.get(value['id']).prompt);return {'state':'completed'}
        run_batch(batch['batch_id'],runner=inspect)
        self.assertTrue(all(prompt!='new prompt' for prompt in frozen));self.assertEqual(original,Path(batch['options']['catalog']).read_bytes())
    def test_ambiguity_does_not_block_other_items_and_manual_confirmation_resumes_one(self):
        batch=create_batch(self.files,RunOptions(analysis='cloud'));count=[]
        def fake(audio,*,options,**kwargs):
            count.append(options.skill)
            return {'state':'needs_review' if len(count)==1 else 'completed'}
        self.assertEqual(run_batch(batch['batch_id'],runner=fake)['state'],'needs_review')
        view=batch_view(batch['batch_id']);item=view['items'][0]
        resolve_item(batch['batch_id'],item['item_id'],'interview.technical')
        chosen=[]
        def finish(audio,*,options,**kwargs):chosen.append(options.skill);return {'state':'completed'}
        self.assertEqual(run_batch(batch['batch_id'],runner=finish)['state'],'completed');self.assertEqual(chosen,['interview.technical'])
    def test_selected_preview_cloud_mode_does_not_change_other_items(self):
        options=RunOptions(analysis='preview')
        batch=create_batch(self.files,options)
        self.assertIsNone(options.catalog)
        run_batch(batch['batch_id'],runner=lambda *a,**k:{'state':'preview_only'})
        item=load_batch(batch['batch_id'])['items'][1]
        calls=[]
        def finish(audio,*,options,**kwargs):calls.append(options.analysis);return {'state':'completed'}
        run_batch(batch['batch_id'],item_id=item['item_id'],analysis='cloud',speaker_device='mps',runner=finish)
        value=load_batch(batch['batch_id'])
        self.assertEqual(value['items'][1]['speaker_device_override'],'mps')
        self.assertEqual(value['options']['speaker_device'],'auto')
        self.assertEqual(calls,['cloud']);self.assertEqual(value['options']['analysis'],'preview')
        self.assertEqual([i['state'] for i in value['items']],['preview_only','completed','preview_only'])

    def test_duplicate_content_and_corrupted_snapshot(self):
        batch=create_batch([self.files[0],self.files[0],self.files[1]],RunOptions(analysis='none'))
        run_batch(batch['batch_id'],runner=lambda *a,**k:{'state':'completed'})
        self.assertEqual(len(load_batch(batch['batch_id'])['items']),2)
        Path(batch['options']['catalog']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'snapshot changed'):run_batch(batch['batch_id'])
