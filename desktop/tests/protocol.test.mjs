import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import readline from 'node:readline';
const root=fileURLToPath(new URL('../../',import.meta.url));
const runtime=path.join(root,'build/macos-runtime');

test('desktop client talks to bundled Python without system Python, models or API calls',async()=>{
 const workspace=await fs.mkdtemp(path.join(root,'.tmp/desktop-client-'));
 const wav=Buffer.alloc(44+3200);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(16000,24);wav.writeUInt32LE(32000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(3200,40);
 const audio=path.join(workspace,'中文合成录音.wav');await fs.writeFile(audio,wav);
 const child=spawn(path.join(runtime,'python/bin/python3'),['-I','-B',path.join(runtime,'launch.py')],{cwd:workspace,env:{...process.env,PATH:'/usr/bin:/bin',SONAFOLIO_WORKSPACE:workspace,PYTHONPATH:'/nonexistent'}});
 const exited=new Promise(resolve=>child.once('close',resolve));
 child.once('error',error=>{diagnostics+=error.message;});
 const pending=new Map(),events=[],seen=[];let serial=0,diagnostics='';
 child.stderr.on('data',data=>diagnostics+=data.toString());
 readline.createInterface({input:child.stdout}).on('line',line=>{const frame=JSON.parse(line);seen.push(frame);assert.equal(frame.protocol_version,1);if(frame.type==='response'){const p=pending.get(frame.id);pending.delete(frame.id);frame.ok?p?.resolve(frame.result):p?.reject(new Error(frame.error.code));}else events.push(frame);});
 const request=(method,params={})=>new Promise((resolve,reject)=>{const id=`node-${++serial}`;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Request timeout: '+method+' '+diagnostics));},10000);pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});child.stdin.write(JSON.stringify({protocol_version:1,id,method,params})+'\n');});
 const until=async predicate=>{const start=Date.now();while(!predicate()){if(child.exitCode!==null)throw Error('Runtime exited early '+diagnostics);if(Date.now()-start>15000)throw Error('IPC event timeout '+diagnostics);await new Promise(r=>setTimeout(r,20));}};
 try{
  await until(()=>events.some(e=>e.event==='server.ready'));
  const hello=await request('hello');assert.equal(hello.default_analysis,'preview');assert.ok(hello.methods.includes('settings.save'));
  const key='synthetic-client-key-not-a-real-secret';await request('settings.save',{llm:{model:'desktop-test'},api_key:key});
  const config=await request('settings.get');assert.equal(config.api_key_configured,true);assert.ok(!JSON.stringify(config).includes(key));
  const accepted=await request('tasks.run',{audio,options:{analysis:'none',refine:false}});
  await until(()=>events.some(e=>e.event==='operation.finished'&&e.operation_id===accepted.operation_id));
  const finished=events.find(e=>e.event==='operation.finished');assert.equal(finished.data.state,'failed'); // empty workspace deliberately has no models
  const tasks=await request('tasks.list');assert.equal(tasks.total,1);const task=tasks.items[0];assert.equal(task.state,'failed');
  await request('tasks.rename',{task_id:task.task_id,title:'桌面客户端已验证'});
  assert.equal((await request('tasks.get',{task_id:task.task_id})).title,'桌面客户端已验证');
  const artifact=await request('artifacts.read',{task_id:task.task_id,path:'manifest.json',offset:0,limit:131072});
  const manifest=JSON.parse(Buffer.from(artifact.data,'base64').toString());assert.equal(manifest.title,'桌面客户端已验证');
  const audioInfo=await request('audio.info',{task_id:task.task_id});assert.ok(audioInfo.path.endsWith('/input/recording.wav'));
  const scene=(await request('scenes.list')).items.find(s=>s.id==='interview.technical');
  const {version,digest,...definition}=scene;definition.name='客户端编辑验证';
  const saved=await request('scenes.save',{scene:definition,expected_version:version});assert.equal(saved.version,'1.0.1');
  assert.ok((await request('scenes.preview',{scene:definition})).preview);
  assert.equal((await request('scenes.history',{scene_id:scene.id})).items.length,2);
  const batch=await request('batches.create',{files:[audio],options:{analysis:'none',refine:false}});
  await until(()=>events.some(e=>e.event==='operation.finished'&&e.operation_id===batch.operation_id));
  const batchEvent=events.find(e=>e.event==='operation.finished'&&e.operation_id===batch.operation_id);
  const batchId=batchEvent.data.result.batch_id;
  assert.equal((await request('batches.get',{batch_id:batchId})).items[0].state,'failed');
  assert.equal((await request('batches.list')).items[0].batch_id,batchId);
  assert.ok(!JSON.stringify(seen).includes(key));assert.ok(!diagnostics.includes(key));
  assert.deepEqual(seen.map(f=>f.seq),seen.map((_,i)=>i+1));
 }finally{
  child.stdin.end();const timer=setTimeout(()=>child.kill('SIGKILL'),5000);await exited;clearTimeout(timer);await fs.rm(workspace,{recursive:true,force:true});
 }
});
