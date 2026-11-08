"""Desktop-side contract checks and local process transport without real models."""
import base64
import io
import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from jsonschema import Draft202012Validator

from sonafolio.audio import record_input
from sonafolio.ipc import DesktopServer, MAX_LINE_BYTES, protocol_schema
from sonafolio.processes import CancelledError

ROOT = Path(__file__).resolve().parents[1]


class IPCTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.tmp').mkdir(exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=ROOT/'.tmp')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        env=patch.dict(os.environ,{'SONAFOLIO_WORKSPACE':str(self.root)})
        env.start();self.addCleanup(env.stop)
        self.audio=self.root/'synthetic.wav'
        with wave.open(str(self.audio),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\x00\x00'*800)
        self.output=io.StringIO();self.diagnostics=io.StringIO()
        self.started=threading.Event();self.release=threading.Event()
        self.received=[]
        def run(audio, *, options, cancelled, on_event, **kwargs):
            self.received.append(options)
            self.started.set()
            on_event({'task_id':'a'*64,'stage':'asr','state':'running'})
            while not self.release.wait(.01):
                if cancelled():
                    raise CancelledError('cancelled')
            return {'task_id':'a'*64,'state':'preview_only','output_directory':'synthetic'}
        self.server=DesktopServer(self.output,run=run,diagnostics=self.diagnostics)
        self.addCleanup(self.server.close)
        self.serial=0
        self.validator=Draft202012Validator(protocol_schema()['outbound'])

    def messages(self):
        with self.server._write_lock:
            lines=self.output.getvalue().splitlines()
        values=[json.loads(line) for line in lines]
        for value in values:
            self.validator.validate(value)
        self.assertEqual([v['seq'] for v in values],list(range(1,len(values)+1)))
        return values

    def request(self, method, params=None, **overrides):
        self.serial+=1
        value={'protocol_version':1,'id':f'client-{self.serial}','method':method,'params':params or {}}
        value.update(overrides)
        self.server.process_line(json.dumps(value))
        return next(message for message in reversed(self.messages()) if message.get('id')==value['id'] and message['type']=='response')

    def wait_finished(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            values=self.messages()
            if any(v.get('event')=='operation.finished' for v in values):
                return next(v for v in reversed(values) if v.get('event')=='operation.finished')
            time.sleep(.01)
        self.fail('No terminal operation event')

    def test_handshake_schema_and_bad_requests_are_recoverable_and_do_not_echo_input(self):
        self.server.ready()
        response=self.request('hello')
        self.assertEqual(response['result']['default_analysis'],'preview')
        self.assertIn('tasks.run',response['result']['methods'])
        for raw in ['not json','{"protocol_version":1,"id":"bad","method":"hello","params":{"key":"SECRET_SENTINEL"}}',
                    '{"protocol_version":1,"id":"bad2","method":"hello","params":{"value":NaN}}']:
            self.server.process_line(raw)
        self.assertNotIn('SECRET_SENTINEL',self.output.getvalue()+self.diagnostics.getvalue())
        self.assertEqual(self.request('hello',protocol_version=2)['error']['code'],'UNSUPPORTED_VERSION')
        self.assertEqual(self.request('missing')['error']['code'],'METHOD_NOT_FOUND')
        self.assertFalse(self.request('tasks.run',{'audio':str(self.audio),'options':{'refine':'false'}})['ok'])
        self.assertTrue(self.request('hello')['ok'])

    def test_async_run_cancel_busy_and_request_replay_contract(self):
        response=self.request('tasks.run',{'audio':str(self.audio)})
        operation=response['result']['operation_id']
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.received[0].analysis,'preview')
        duplicate=self.request('tasks.run',{'audio':str(self.audio)},id=response['id'])
        self.assertEqual(duplicate['error']['code'],'DUPLICATE_REQUEST')
        self.assertEqual(self.request('tasks.run',{'audio':str(self.audio)})['error']['code'],'BUSY')
        self.assertTrue(self.request('hello')['ok'],'control loop was blocked by inference')
        cancelled=self.request('operations.cancel',{'operation_id':operation})
        self.assertIn(cancelled['result']['state'],{'cancelling','cancelled'})
        finished=self.wait_finished()
        self.assertEqual(finished['data']['state'],'cancelled')
        self.assertEqual(finished['operation_id'],operation)
        # Acceptance must precede every progress event for that operation.
        values=self.messages()
        accepted=next(v for v in values if v.get('id')==response['id'])
        progress=next(v for v in values if v.get('event')=='task.progress')
        self.assertLess(accepted['seq'],progress['seq'])
        self.assertEqual(self.request('operations.get',{'operation_id':operation})['result']['state'],'cancelled')

    def test_success_failure_and_cloud_is_explicit(self):
        self.release.set()
        response=self.request('tasks.run',{'audio':str(self.audio),'options':{'analysis':'cloud'}})
        self.wait_finished()
        self.assertEqual(self.received[0].analysis,'cloud')
        self.assertEqual(self.request('operations.get',{'operation_id':response['result']['operation_id']})['result']['state'],'preview_only')
        def fail(*args,**kwargs):
            raise RuntimeError('PRIVATE_DIAGNOSTIC_SENTINEL')
        self.server.run=fail
        second=self.request('tasks.run',{'audio':str(self.audio)})
        job=self.server._jobs[second['result']['operation_id']]
        job['thread'].join(2)
        final=self.request('operations.get',{'operation_id':job['operation_id']})['result']
        self.assertEqual(final['error']['code'],'PROCESSING_FAILED')
        self.assertNotIn('PRIVATE_DIAGNOSTIC_SENTINEL',self.output.getvalue()+self.diagnostics.getvalue())

    def test_task_views_rename_and_bounded_artifact_reads(self):
        _,task=record_input(self.audio)
        artifact=task/'analysis/v1/summary.md';artifact.parent.mkdir(parents=True)
        artifact.write_text('中文内容🙂'*12,encoding='utf-8')
        broken=self.root/'data/runs/broken/manifest.json'
        broken.parent.mkdir(parents=True);broken.write_text('[]')
        listed=self.request('tasks.list')['result']
        self.assertEqual(listed['unreadable'],1)
        self.assertEqual(listed['total'],1)
        self.assertNotIn('source',listed['items'][0]['input'])
        self.assertEqual(self.request('tasks.rename',{'task_id':task.name,'title':'新标题'})['result']['title'],'新标题')
        self.assertEqual(self.request('tasks.get',{'task_id':task.name})['result']['title'],'新标题')
        chunks=[];offset=0
        while True:
            result=self.request('artifacts.read',{'task_id':task.name,'path':'analysis/v1/summary.md','offset':offset,'limit':7})['result']
            chunks.append(base64.b64decode(result['data']))
            offset=result['next_offset']
            if result['eof']:break
        self.assertEqual(b''.join(chunks),artifact.read_bytes())
        for path in ['../../.runtime/secrets/deepseek-api-key','input/recording.wav','/etc/passwd']:
            self.assertEqual(self.request('artifacts.read',{'task_id':task.name,'path':path})['error']['code'],'INVALID_PATH')
        other=self.root/'private.json';other.write_text('DO_NOT_READ')
        try:
            (task/'analysis/leak.json').symlink_to(other)
        except OSError:
            return
        blocked=self.request('artifacts.read',{'task_id':task.name,'path':'analysis/leak.json'})
        self.assertEqual(blocked['error']['code'],'INVALID_PATH')
        self.assertNotIn('DO_NOT_READ',self.output.getvalue())

    def test_resume_delegates_saved_options_and_close_cancels_active_work(self):
        _,task=record_input(self.audio)
        def resume(task_id, *, analysis, cancelled, on_event):
            self.assertEqual(task_id,task.name);self.assertIsNone(analysis)
            return self.server.run(self.audio,options=type('Options',(),{'analysis':'preview'})(),cancelled=cancelled,on_event=on_event)
        self.server.resume=resume
        response=self.request('tasks.resume',{'task_id':task.name})
        self.assertTrue(self.started.wait(2))
        self.server.close()
        self.assertEqual(self.wait_finished()['data']['state'],'cancelled')
        self.assertFalse(self.server._jobs[response['result']['operation_id']]['thread'].is_alive())

    @unittest.skipIf(os.name=='nt','macOS/POSIX shutdown baseline; Windows real-machine validation is on hold')
    def test_real_host_eof_and_sigterm_cancel_work_and_keep_stdout_clean(self):
        host=self.root/'fake_ipc_host.py'
        host.write_text("""
import time
import sonafolio.ipc as ipc
from sonafolio.processes import CancelledError
Server=ipc.DesktopServer

def run(audio, *, cancelled, on_event, **kwargs):
    print('adapter diagnostic sentinel',flush=True)
    on_event({'task_id':'a'*64,'stage':'asr','state':'running'})
    while not cancelled():time.sleep(.01)
    raise CancelledError('stopped')

ipc.DesktopServer=lambda output:Server(output,run=run)
ipc.serve_stdio()
""")
        for shutdown in ('eof','sigterm'):
            with self.subTest(shutdown=shutdown):
                process=subprocess.Popen([sys.executable,'-B',str(host)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                messages=queue.Queue()
                def read():
                    for line in process.stdout:messages.put(json.loads(line))
                reader=threading.Thread(target=read);reader.start()
                initial=messages.get(timeout=5)
                self.assertEqual(initial['event'],'server.ready')
                request={'protocol_version':1,'id':'host-run','method':'tasks.run','params':{'audio':str(self.audio)}}
                process.stdin.write(json.dumps(request)+'\n');process.stdin.flush()
                seen=[initial]
                while not any(m.get('event')=='task.progress' for m in seen):
                    seen.append(messages.get(timeout=5))
                if shutdown=='eof':process.stdin.close()
                else:process.terminate()
                process.wait(timeout=5)
                reader.join(2)
                while not messages.empty():seen.append(messages.get_nowait())
                for message in seen:self.validator.validate(message)
                self.assertEqual(next(m for m in seen if m.get('event')=='operation.finished')['data']['state'],'cancelled')
                self.assertIn('adapter diagnostic sentinel',process.stderr.read())
                self.assertEqual(process.returncode,0)
                if not process.stdin.closed:process.stdin.close()
                process.stdout.close();process.stderr.close()

    def test_real_process_jsonl_framing_size_limit_eof_and_diagnostics(self):
        def request(identifier,method='hello',**params):
            return json.dumps({'protocol_version':1,'id':identifier,'method':method,'params':params}).encode()+b'\n'
        payload=b'bad json\n'+request('before')+b'x'*(MAX_LINE_BYTES+10)+b'\n'+request('after')+request('list','tasks.list')
        completed=subprocess.run([sys.executable,'-B','-m','sonafolio','ipc'],input=payload,capture_output=True,timeout=10)
        self.assertEqual(completed.returncode,0,completed.stderr.decode())
        messages=[json.loads(line) for line in completed.stdout.splitlines()]
        for message in messages:self.validator.validate(message)
        self.assertEqual(messages[0]['event'],'server.ready')
        self.assertTrue(next(m for m in messages if m.get('id')=='after')['ok'])
        self.assertTrue(any(m.get('error',{}).get('code')=='REQUEST_TOO_LARGE' for m in messages))
        self.assertEqual(next(m for m in messages if m.get('id')=='list')['result']['total'],0)


if __name__=='__main__':
    unittest.main()
