import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from sonafolio.scenes import as_definition,save_scene,preview_scene,draft_scene,list_scenes,scene_history
from sonafolio.skills import SkillRegistry
from sonafolio.processes import CancelledError
ROOT=Path(__file__).resolve().parents[1]
class SceneAuthoringTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(self.root)});env.start();self.addCleanup(env.stop)
        self.value=as_definition(SkillRegistry().get('interview.technical'))
        self.value.update(name='软件技术面试',keywords=['后端','候选人'],use_when='讨论软件工程经验',not_when='非招聘会议')
    def test_versioned_edits_override_builtin_without_changing_history(self):
        original=SkillRegistry().get('interview.technical')
        saved=save_scene(self.value,expected_version=original.version)
        self.assertEqual(saved['version'],'1.0.1')
        changed=SkillRegistry(self.root/'skills').get(original.id)
        self.assertEqual(changed.use_when,'讨论软件工程经验')
        self.assertEqual(changed.prompt,self.value['prompt'],'Resolved parent prompt must not be appended twice')
        first=(self.root/'skills/interview.technical/versions/1.0.1.json').read_bytes()
        self.value['enabled']=False
        save_scene(self.value,expected_version=saved['version'])
        registry=SkillRegistry(self.root/'skills')
        self.assertNotIn(original.id,{route['id'] for route in registry.routes()})
        self.assertEqual(first,(self.root/'skills/interview.technical/versions/1.0.1.json').read_bytes())
        self.assertEqual(len(scene_history(original.id)['items']),3)
        self.assertEqual(SkillRegistry().get(original.id).digest,original.digest)
        with self.assertRaisesRegex(ValueError,'version changed'):save_scene(self.value,expected_version=original.version)
    def test_templates_and_schema_are_checked_before_publishing(self):
        for update in [{'template':'{{ result.missing_field }}'},{'template':'{{ range(1000000) }}'},
                       {'schema':{'type':'object','$ref':'https://invalid.example/schema'}},{'parent':'interview.technical'}]:
            value={**self.value,**update}
            with self.subTest(update=update),self.assertRaises(Exception):save_scene(value,expected_version='1.0.0')
        self.assertFalse((self.root/'skills/interview.technical/scene.json').exists())
    def test_keyword_draft_is_persisted_but_not_routable_until_confirmed(self):
        calls=[]
        def fake(instructions,content,schema,**kwargs):
            calls.append(content)
            return {'id':'interview.custom','name':'研发复盘','description':'研发岗位招聘交流','use_when':'技术招聘',
                    'not_when':'日常例会','prompt':'总结事实并引用来源。','template':'# {{ result.title }}\n{{ result.overview }}',
                    'base_scene':'interview'},{'usage':{}}
        drafted=draft_scene(['研发','面试'],call=fake)
        self.assertFalse(drafted['scene']['enabled']);self.assertEqual(len(list_scenes()['drafts']),1)
        self.assertNotIn('interview.custom',SkillRegistry(self.root/'skills').loaded)
        self.assertIn('研发',calls[0])
        saved=save_scene({**drafted['scene'],'enabled':True})
        self.assertEqual(list_scenes()['drafts'],[])
        self.assertIn(saved['id'],{route['id'] for route in SkillRegistry(self.root/'skills').routes()})
        snapshot={'skills':{key:__import__('dataclasses').asdict(value) for key,value in SkillRegistry(self.root/'skills').loaded.items()}}
        self.assertEqual(SkillRegistry.from_snapshot(snapshot).get(saved['id']).prompt,'总结事实并引用来源。')
        with self.assertRaises(CancelledError):draft_scene(['x'],call=fake,cancelled=lambda:True)
    def test_worker_failure_and_midflight_cancel_never_publish_a_scene(self):
        def fail(*args,**kwargs):raise RuntimeError('synthetic worker failure')
        with patch('sonafolio.scenes.run_stage_process',side_effect=fail),self.assertRaises(RuntimeError):draft_scene(['测试'])
        with patch('sonafolio.scenes.run_stage_process',side_effect=CancelledError('cancelled')),self.assertRaises(CancelledError):draft_scene(['测试'])
        self.assertEqual(list_scenes()['drafts'],[])
        self.assertNotIn('test.custom',SkillRegistry(self.root/'skills').loaded)

    def test_explicit_schema_example_survives_reload_and_resave(self):
        value={**self.value,'id':'test.custom','parent':None,'schema':{'type':'object','properties':{'title':{'type':'string','pattern':'^ok$'}},'required':['title']},'template':'{{ result.title }}','example':{'title':'ok'}}
        saved=save_scene(value)
        loaded=next(s for s in list_scenes()['items'] if s['id']=='test.custom')
        self.assertEqual(loaded['example'],{'title':'ok'})
        loaded.pop('digest');loaded.pop('version')
        self.assertEqual(save_scene(loaded,expected_version=saved['version'])['version'],'1.0.1')

    def test_drafts_and_disabled_versions_do_not_affect_routing(self):
        self.value.update(id='interview.private',enabled=False)
        save_scene(self.value)
        routes=SkillRegistry(self.root/'skills').routes()
        self.assertNotIn('interview.private',{r['id'] for r in routes})
        self.assertTrue((self.root/'skills/.gitignore').exists())
        self.assertIn('preview',preview_scene(self.value))
