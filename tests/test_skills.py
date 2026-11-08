import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sonafolio.analysis import analyze_recording, choose_category, make_chunks, source_records, validate_references
from sonafolio.skills import SkillRegistry, merge_schema, render_summary

ROOT = Path(__file__).resolve().parents[1]


class SkillTests(unittest.TestCase):
    def test_inheritance_keeps_common_and_specialized_fields(self):
        skill = SkillRegistry().get('interview.technical')
        self.assertIn('participants',skill.schema['properties'])
        self.assertIn('qa',skill.schema['properties'])
        self.assertIn('technical_topics',skill.schema['properties'])
        self.assertIn('从候选人的角度',skill.prompt)

    def test_conflicting_inherited_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            merge_schema({'properties':{'x':{'type':'string'}}},{'properties':{'x':{'type':'number'}}})

    def test_category_vote_can_use_a_shared_parent(self):
        results=[{'category':'interview.technical'},{'category':'interview.hr'},{'category':'interview'}]
        self.assertEqual(choose_category(results,{'unknown','interview','interview.hr','interview.technical'}),'interview')

    def test_reference_validation_rejects_fabricated_evidence(self):
        with self.assertRaises(ValueError):
            validate_references({'facts':[{'segment_ids':['invented']}]},{'real'},{'SPEAKER_00'})

    def test_grouping_preserves_uncertain_text(self):
        records=source_records([{'id':'a.1','source_segment_id':'a','start':0,'end':1,'text':'甲','speaker_id':'SPEAKER_00'},
                                {'id':'a.2','source_segment_id':'a','start':1,'end':2,'text':'B','speaker_id':None}])
        self.assertEqual(''.join(p['text'] for p in records[0]['parts']),'甲B')
        self.assertEqual(records[0]['parts'][1]['speaker'],'待确认')
        self.assertEqual(len(make_chunks(records)),1)

    def test_analysis_caches_calls_and_indexes_output(self):
        (ROOT/'.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as temporary:
            root=Path(temporary)
            path=root/'data/runs/example/refined/example/transcript.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'source_sha256':'test-source','diarization':{'state':'completed'},'segments':[
                {'id':'seg-000000','start':0,'end':1,'text':'面试中的技术问题','speaker_id':'SPEAKER_00'}]}),encoding='utf-8')
            calls=[]
            def fake(instructions,content,schema,**kwargs):
                calls.append(instructions)
                if '"chunk_notes"' in content:
                    self.assertIn('ASR 误识',instructions)
                    self.assertIn('不等于原录音没说',instructions)
                if 'category' in schema['properties']:
                    return {'category':'interview.technical','tags':['技术'],'reason':'技术面试','segment_ids':['seg-000000']},{'usage':{}}
                return {'title':'测试总结','overview':'概览','participants':[],
                        'facts':[{'text':'事实','segment_ids':['seg-000000']}],
                        'analysis':[],'actions':[],'uncertainties':[],'qa':[],'technical_topics':[]},{'usage':{}}
            with patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(root)}):
                result=analyze_recording(path,call=fake)
                count=len(calls)
                again=analyze_recording(path,call=fake)
                self.assertEqual(len(calls),count)
                self.assertEqual(result['output_directory'],again['output_directory'])
                adjusted=analyze_recording(path,call=fake,merge_max_tokens=20000)
                self.assertEqual(len(calls),count+1)
                self.assertNotEqual(result['output_directory'],adjusted['output_directory'])
                output=Path(result['output_directory'])
                self.assertIn('sources.md#seg-000000',(output/'summary.md').read_text())
                with sqlite3.connect(root/'data/index.sqlite') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM analyses').fetchone()[0],2)
                before=(output/'summary.json').read_bytes()
                forced=analyze_recording(path,call=fake,force=True)
                self.assertNotEqual(forced['output_directory'],result['output_directory'])
                self.assertEqual((output/'summary.json').read_bytes(),before)
                self.assertEqual(len(calls),count+4)
                self.assertTrue((Path(forced['output_directory'])/'scene.json').is_file())
                route=next((output/'checkpoints').glob('route-*.json'))
                corrupted=json.loads(route.read_text());corrupted['result']['reason']='格式合法但已损坏'
                route.write_text(json.dumps(corrupted))
                count_before=len(calls)
                analyze_recording(path,call=fake)
                self.assertEqual(len(calls),count_before+1)
                self.assertEqual(json.loads(route.read_text())['result']['reason'],'技术面试')

    def test_unknown_batch_route_waits_for_confirmation_without_extracting(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as temporary:
            root=Path(temporary);path=root/'data/runs/example/alignment/example/transcript.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'source_sha256':'test-source','diarization':{'state':'completed'},'segments':[
                {'id':'seg-1','start':0,'end':1,'text':'无法确定的混合场景','speaker_id':'SPEAKER_00'}]}))
            calls=[]
            def fake(instructions,content,schema,**kwargs):
                calls.append(schema)
                self.assertIn('category',schema['properties'],'Ambiguous routing must not trigger extraction')
                return {'category':'unknown','tags':[],'reason':'无法确定','segment_ids':['seg-1']},{'usage':{}}
            with patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(root)}):result=analyze_recording(path,call=fake,review_unknown=True)
            self.assertEqual(result['state'],'needs_review');self.assertEqual(len(calls),1)
            self.assertFalse((Path(result['output_directory'])/'summary.json').exists())
            self.assertTrue((Path(result['output_directory'])/'checkpoints/classification.json').is_file())

    def test_cyclic_custom_skills_are_rejected(self):
        (ROOT/'.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as temporary:
            root=Path(temporary)
            for name,parent in [('test.a','test.b'),('test.b','test.a')]:
                directory=root/name; directory.mkdir()
                (directory/'skill.toml').write_text(f'protocol_version=1\nid="{name}"\nversion="1"\ndescription="test"\nparent="{parent}"\n')
            with self.assertRaisesRegex(ValueError,'Cyclic'):
                SkillRegistry(root)

    def test_dry_run_never_calls_model_or_creates_summary(self):
        (ROOT/'.tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as temporary:
            root=Path(temporary)
            path=root/'data/runs/example/refined/example/transcript.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'source_sha256':'test-source','diarization':{'state':'completed'},'segments':[
                {'id':'seg-1','start':0,'end':1,'text':'本地预览内容','speaker_id':'SPEAKER_00'}]}),encoding='utf-8')
            def forbidden(*args,**kwargs):
                raise AssertionError('Dry run attempted an API call')
            with patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(root)}):
                result=analyze_recording(path,dry_run=True,call=forbidden)
            self.assertEqual(result['api_requests_made'],0)
            self.assertIn('本地预览内容',Path(result['preview']).read_text())
            self.assertFalse((root/'data/output').exists())


if __name__=='__main__':
    unittest.main()
