import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import Markdown from 'react-markdown';
import {open} from '@tauri-apps/plugin-dialog';
import {invoke} from '@tauri-apps/api/core';
import {connect,rpc,subscribe,readArtifact,safeSourcePath,type Frame} from './bridge';
import {SceneManager,sceneLabel} from './SceneManager';
import {BatchManager} from './BatchManager';
import {AudioPlayer} from './AudioPlayer';
import {SettingsPanel,operationError} from './SettingsPanel';
import {useModal} from './useModal';
import './styles.css';

type Task={task_id:string;title:string;state:string;updated_at:string;current_stage?:string;latest_result?:string;analysis_versions?:string[];batch_ids?:string[];input:{duration_seconds?:number};stages:Record<string,{state:string;attempts:number}>};
const states:Record<string,string>={imported:'已导入',queued:'排队中',running:'处理中',cancelling:'正在取消',completed:'已完成',preview_only:'待生成总结',failed:'处理失败',cancelled:'已取消',reused:'已复用',needs_review:'待确认场景'};
const stages:Record<string,string>={asr:'语音识别',diarization:'区分说话人',annotation:'说话人归属',refinement:'精细对齐',analysis:'内容整理',pipeline:'处理流程'};
const duration=(n=0)=>`${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`;
const filename=(path:string)=>path.split('/').pop()||path;
const errorText=(e:unknown)=>typeof e==='string'?e:e instanceof Error?e.message:'操作失败，请重试';
function Logo(){return <div className="brand"><svg viewBox="0 0 30 34" aria-hidden="true"><path d="M8 3h15v25H8M5 8v11M1 11v5M12 7v14M16 10v8M20 12v4M8 28l7 3 8-3"/></svg><div><div className="brand-name">声笺</div><div className="brand-sub">Sonafolio</div></div></div>}

function App(){
 useModal();
 const [summarizing,setSummarizing]=useState(false);
 const [renameValue,setRenameValue]=useState<string|null>(null);
 const [view,setView]=useState('library'),[workspace,setWorkspace]=useState(''),[connected,setConnected]=useState(false),[connecting,setConnecting]=useState(false);
 const [tasks,setTasks]=useState<Task[]>([]),[selected,setSelected]=useState(''),[search,setSearch]=useState(''),[filter,setFilter]=useState('all');
 const [tab,setTab]=useState('summary'),[documentText,setDocumentText]=useState(''),[sourceText,setSourceText]=useState(''),[sourceId,setSourceId]=useState(''),[loading,setLoading]=useState(false);
 const [settings,setSettings]=useState<any>(null);
 const [scenes,setScenes]=useState<any[]>([]),[importing,setImporting]=useState(false),[files,setFiles]=useState<string[]>([]),[mode,setMode]=useState('preview'),[consent,setConsent]=useState(false),[skill,setSkill]=useState('');
 const [batchId,setBatchId]=useState(''),[revision,setRevision]=useState(0),[segments,setSegments]=useState<any[]>([]),[routingReasons,setRoutingReasons]=useState<string[]>([]),[seek,setSeek]=useState<{time:number;nonce:number}|null>(null);
 const [operation,setOperation]=useState(''),[progress,setProgress]=useState(''),[notice,setNotice]=useState(''),[theme,setTheme]=useState('dark'),[mobileDetail,setMobileDetail]=useState(false);
 const connectionId=useRef<number|null>(null),active=useRef(''),kind=useRef(''),finished=useRef<((data:any)=>void)|undefined>(undefined),frames=useRef(new Map<string,Frame[]>()),loaded=useRef(0),listener=useRef<Promise<()=>void>|null>(null),eventHandler=useRef<(frame:Frame)=>void>(()=>{}),refreshRef=useRef<()=>Promise<void>>(async()=>{});
 const task=tasks.find(t=>t.task_id===selected);
 const canSummarize=task?.stages?.annotation?.state==='completed'||task?.state==='preview_only';
 const openImport=()=>{setSummarizing(false);setFiles([]);setConsent(false);setImporting(true);};
 const openSummary=()=>{setSummarizing(true);setMode('cloud');setConsent(false);setImporting(true);};
 async function refresh(){const result=await rpc('tasks.list',{limit:200});setTasks(result.items);setSelected(old=>result.items.some((t:Task)=>t.task_id===old)?old:result.items[0]?.task_id||'');}
 refreshRef.current=refresh;
 useEffect(()=>{listener.current=subscribe(frame=>eventHandler.current(frame));return()=>{listener.current?.then(unlisten=>unlisten());};},[]);
 useEffect(()=>{document.documentElement.dataset.theme=theme==='system'?'':theme;},[theme]);
 function complete(frame:Frame){
  active.current='';setOperation('');setProgress('');setRevision(v=>v+1);
  const callback=finished.current;finished.current=undefined;callback?.(frame.data);
  void refreshRef.current().catch(e=>setNotice(errorText(e)));
  if(frame.data?.batch_id)setBatchId(frame.data.batch_id);
  if(frame.data?.state==='failed')setNotice(operationError(frame.data?.error?.code));
  if(['models','storage'].includes(kind.current))void rpc('settings.get').then(settingsChanged).catch(e=>setNotice(errorText(e)));
 }
 eventHandler.current=(frame:Frame)=>{
  if(frame.connection_id!==connectionId.current)return;
  if(frame.event==='backend.disconnected'){setConnected(false);setNotice('处理进程已退出，请重新连接工作区。');active.current='';setOperation('');return;}
  if(!frame.operation_id)return;
  if(active.current!==frame.operation_id){const list=frames.current.get(frame.operation_id)||[];list.push(frame);frames.current.set(frame.operation_id,list);return;}
  if(frame.event==='task.progress'){setProgress(`${stages[frame.data?.stage]||({model_download:'模型下载',model_verify:'模型校验',storage:'复制迁移'} as any)[frame.data?.stage]||'处理中'} · ${frame.data?.total_bytes?Math.min(100,Math.round((frame.data.bytes||0)/frame.data.total_bytes*100))+'%':states[frame.data?.state]||({downloading:'下载中',connecting:'连接中',copying:'复制中'} as any)[frame.data?.state]||frame.data?.state||''}${frame.data?.device?' · '+(({mps:'Apple GPU',cpu:'CPU',cuda:'CUDA',auto:'自动'} as any)[frame.data.device]||frame.data.device):''}`);if(frame.data?.batch_id)setBatchId(frame.data.batch_id);if(frame.data?.task_id)void refreshRef.current().catch(e=>setNotice(errorText(e)));setRevision(v=>v+1);}
  if(frame.event==='operation.finished')complete(frame);
 };
 async function launch(method:string,params:Record<string,unknown>,done?:(data:any)=>void){
  if(active.current)throw Error('请等待当前操作结束。');
  kind.current=method.split('.')[0];finished.current=done;setProgress(method==='scenes.draft'?'正在起草场景':'正在准备处理');
  active.current='starting';setOperation('starting');let response;try{response=await rpc(method,params);}catch(e){active.current='';setOperation('');throw e;}active.current=response.operation_id;setOperation(response.operation_id);
  const buffered=frames.current.get(response.operation_id)||[];frames.current.delete(response.operation_id);for(const frame of buffered)eventHandler.current(frame);
 }
 async function chooseWorkspace(){try{
  const path=await open({directory:true,multiple:false,title:'选择声笺工作区'});if(typeof path!=='string')return;
  setConnecting(true);setConnected(false);connectionId.current=null;frames.current.clear();setBatchId('');setRevision(v=>v+1);await listener.current;
  const result:any=await connect(path);connectionId.current=result.connection_id;await rpc('hello');
  const config=await rpc('settings.get');const catalog=await rpc('scenes.list');setWorkspace(path);setSettings(config);setTheme(config.appearance||'dark');setScenes(catalog.items);
  await refresh();setConnected(true);setNotice('');
 }catch(e){setNotice(errorText(e));}finally{setConnecting(false);}}
 async function chooseFiles(){try{const paths=await open({multiple:true,filters:[{name:'录音文件',extensions:['m4a','wav','mp3','mp4','flac','ogg']}],title:'导入录音'});if(paths)setFiles(Array.isArray(paths)?paths:[paths]);}catch(e){setNotice(errorText(e));}}
 function startImport(e:React.FormEvent){e.preventDefault();if(summarizing){if(consent){setImporting(false);void resume('cloud');}return;}if(!files.length||active.current||(mode==='cloud'&&!consent))return;setImporting(false);setView('batch');void launch('batches.create',{files,options:{analysis:mode,skill:skill||null,speaker_device:settings.processing?.speaker_device||'auto'}}).catch(e=>setNotice(errorText(e)));setFiles([]);}
 async function resume(analysis?:string){if(!task)return;try{
  const id=task.batch_ids?.at(-1);
  if(id){setBatchId(id);if(task.state==='needs_review'){setView('batch');return;}
   const batch=await rpc('batches.get',{batch_id:id});const item=batch.items.find((i:any)=>i.task_id===task.task_id&&i.state!=='duplicate');
   if(item){await launch('batches.run',{batch_id:id,item_id:item.item_id,speaker_device:settings.processing?.speaker_device||'auto',...(analysis?{analysis}:{})});return;}}
  await launch('tasks.resume',{task_id:task.task_id,speaker_device:settings.processing?.speaker_device||'auto',...(analysis?{analysis}:{})});
 }catch(e){setNotice(errorText(e));}}
 async function refreshScenes(){setScenes((await rpc('scenes.list')).items);}
 const pause=()=>{if(operation&&operation!=='starting')void rpc('operations.cancel',{operation_id:operation}).catch(e=>setNotice(errorText(e)));};
 async function saveRename(e:React.FormEvent){e.preventDefault();if(!task||!renameValue?.trim())return;try{await rpc('tasks.rename',{task_id:task.task_id,title:renameValue});setRenameValue(null);await refresh();}catch(e){setNotice(errorText(e));}}

 function settingsChanged(value:any){if(settings?.storage?.data_dir!==value.storage.data_dir){setBatchId('');void refreshRef.current();}setSettings(value);setTheme(value.appearance||'dark');setRevision(v=>v+1);}
 useEffect(()=>{let current=true;setSegments([]);setSeek(null);setRoutingReasons([]);if(task?.state==='needs_review'&&task.latest_result)void readArtifact(selected,task.latest_result+'/checkpoints/classification.json').then(text=>{if(current)setRoutingReasons((JSON.parse(text).chunks||[]).map((c:any)=>c.reason));}).catch(()=>{});if(!connected||!selected)return;
  void (async()=>{const manifest=JSON.parse(await readArtifact(selected,'manifest.json'));for(const name of ['refinement','annotation','asr']){const attempt=[...(manifest.stages[name]||[])].reverse().find((a:any)=>a.state==='completed');if(attempt){const value=JSON.parse(await readArtifact(selected,attempt.directory+'/transcript.json'));if(current)setSegments(value.segments||[]);return;}}})().catch(()=>{});return()=>{current=false;};
 },[selected,connected,tasks]);
 useEffect(()=>{
  const token=++loaded.current;setDocumentText('');setSourceText('');setSourceId('');if(!connected||!task)return;
  setLoading(true);
  void (async()=>{const detail=await rpc<Task>('tasks.get',{task_id:task.task_id});let path='';
   if(tab==='summary'&&detail.analysis_versions?.length)path=detail.analysis_versions.at(-1)+'/summary.md';
   if(tab==='preview'&&detail.latest_result)path=detail.latest_result+'/checkpoints/outbound-text.md';
   if(tab==='transcript'){
    // Stage attempts remain authoritative even when latest result is a summary.
    const manifest=JSON.parse(await readArtifact(task.task_id,'manifest.json'));
    for(const name of ['refinement','annotation','asr']){const attempts=manifest.stages[name]||[];const attempt=[...attempts].reverse().find((a:any)=>a.state==='completed');if(attempt){path=attempt.directory+'/transcript.md';try{return await readArtifact(task.task_id,path);}catch{return JSON.stringify(JSON.parse(await readArtifact(task.task_id,attempt.directory+'/transcript.json')),null,2);}}}
   }
   if(!path)return '';
   return await readArtifact(task.task_id,path);
  })().then(text=>{if(token===loaded.current)setDocumentText(text||'');}).catch(e=>{if(token===loaded.current)setNotice(errorText(e));}).finally(()=>{if(token===loaded.current)setLoading(false);});
 },[selected,tab,connected,tasks]);
 async function showSource(href:string){const id=safeSourcePath(href);if(!task||id===null)return;try{const version=task.analysis_versions?.at(-1);if(!version)return;setSourceText(await readArtifact(task.task_id,version+'/sources.md'));setSourceId(id);const segment=segments.find(s=>s.id===id||s.source_segment_id===id);if(segment)setSeek({time:segment.start,nonce:Date.now()});}catch(e){setNotice(errorText(e));}}
 const filtered=tasks.filter(t=>t.title.toLowerCase().includes(search.toLowerCase())&&(filter==='all'||t.state===filter));
 const nav=(id:string,label:string,short:string)=> <button className={'nav-item '+(view===id?'active':'')} onClick={()=>{setView(id);setMobileDetail(false);}} aria-current={view===id?'page':undefined}><span className="nav-full">{label}</span><span className="nav-short">{short}</span>{id==='library'&&<span className="count">{tasks.length}</span>}</button>;
 if(!connected)return <div className="welcome"><Logo/><h1>让声音，落在纸上。</h1><p>选择一个文件夹，保存录音、模型、缓存与整理结果。</p><button className="btn primary" onClick={chooseWorkspace} disabled={connecting}>{connecting?'正在连接…':'选择工作区'}</button>{notice&&<p role="alert">{notice}</p>}</div>;
 return <div className={'app live-app '+(view!=='library'?'full-layout ':'')+(mobileDetail?'show-detail':'')}>
  <aside className="sidebar" aria-label="主导航"><Logo/><button className="btn primary import" onClick={openImport} disabled={!!operation}>＋ <span className="import-full">导入录音</span><span className="import-short">导入</span></button><nav className="nav-group">{nav('library','全部录音','录音')}{nav('batch','导入批次','批次')}</nav><div className="sidebar-bottom">{nav('scenes','场景管理','场景')}{nav('settings','设置','设置')}<div className="workspace-note"><div>让 AI 整理，由你掌握</div><button title={workspace} onClick={chooseWorkspace} disabled={!!operation}>{filename(workspace)} ▾</button></div></div></aside>
  {view==='library'&&<><section className="library"><header className="library-head"><div className="row spread"><h2>我的声笺</h2><span className="small muted">{tasks.length} 条</span></div><input className="input" aria-label="搜索录音" placeholder="搜索录音、文件名…" value={search} onChange={e=>setSearch(e.target.value)}/><div className="row spread small muted"><span>按最近更新排列</span><select className="compact-filter" aria-label="筛选录音状态" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部录音</option>{['completed','preview_only','needs_review','failed','cancelled'].map(state=><option key={state} value={state}>{states[state]}</option>)}</select></div></header><div className="records">{filtered.map(t=><button className={'record '+(t.task_id===selected?'selected':'')} key={t.task_id} onClick={()=>{setSelected(t.task_id);setTab('summary');setMobileDetail(true);}}><div className="record-title">{t.title}</div><div className="record-meta"><span>{new Date(t.updated_at).toLocaleDateString('zh-CN')}</span><span>{duration(t.input.duration_seconds)}</span></div><div className="record-bottom"><span className="record-status">{states[t.state]||t.state}</span></div></button>)}{!filtered.length&&<p className="muted empty-list">暂无录音</p>}</div></section><main className="main"><header className="main-header"><button className="mobile-list-button" onClick={()=>setMobileDetail(false)}>← 录音库</button><div className="title-row"><h1>{task?.title||'我的声笺'}</h1><div className="tools">{task&&['failed','cancelled','needs_review'].includes(task.state)&&<button className="btn" disabled={!!operation} onClick={()=>void resume()}>继续处理</button>}{task?.latest_result&&<button className="btn" onClick={()=>void invoke('reveal_artifact',{path:`${settings.storage.data_dir}/runs/${task.task_id}/${task.latest_result}`}).catch(e=>setNotice(errorText(e)))}>查看文件</button>}{task&&<button className="btn" onClick={()=>setRenameValue(task.title)} disabled={!!operation}>重命名</button>}</div></div>{task&&<p className="small muted">{duration(task.input.duration_seconds)} · {states[task.state]||task.state}</p>}<div className="tabs-line"><div className="tabs" role="tablist" aria-label="录音内容">{[['summary','复盘总结'],['transcript','逐字稿'],['preview','发送预览']].map(([id,name])=><button role="tab" aria-selected={tab===id} className={'tab '+(tab===id?'active':'')} onClick={()=>setTab(id)} key={id}>{name}</button>)}</div></div></header><div className="live-reading">{task?.state==='needs_review'&&<div className="notice review-needed"><div><strong>需要确认整理场景</strong>{routingReasons.slice(0,4).map((reason,i)=><p key={i}>{reason}</p>)}<button className="btn" disabled={!!operation} onClick={()=>{setBatchId(task.batch_ids?.at(-1)||'');setView('batch');}}>选择场景并继续</button></div></div>}{tab==='transcript'&&segments.length?<div className="transcript-segments">{segments.map((s:any,i:number)=><div className="transcript-segment" key={s.id||i}><button className="cite" onClick={()=>setSeek({time:s.start,nonce:Date.now()})}>{duration(s.start)}</button><span className="small muted">{s.speaker_id||'说话人未确定'}</span><p>{s.text}</p></div>)}</div>:loading?<p className="muted">正在读取…</p>:documentText?<div className="markdown"><Markdown components={{a:({href,children})=><button className="cite" onClick={()=>void showSource(href||'')}>{children}</button>}}>{documentText}</Markdown></div>:<div className="empty"><h2>{task?'转写完成后，可在这里查看内容':'让声音，落在纸上。'}</h2>{task?<><p>{task.state==='needs_review'?'已保留转写和分类依据，请确认场景后继续。':canSummarize?'文字已在本地生成。确认后发送至 DeepSeek 生成总结。':'可从已有阶段继续处理。'}</p><button className="btn primary" disabled={!!operation} onClick={()=>task.state==='needs_review'?void resume():canSummarize?openSummary():void resume()}>{task.state==='needs_review'?'确认场景':canSummarize?'生成总结':'继续处理'}</button></>:<button className="btn primary" onClick={openImport}>＋ 导入录音</button>}</div>}</div>{task&&<AudioPlayer key={settings.storage.data_dir+'/'+task.task_id} taskId={task.task_id} seek={seek}/ >}{sourceText&&<div className="source-overlay" role="dialog" aria-modal="true" aria-label="原文核对"><header className="row spread"><h2>原文核对{sourceId?' · '+sourceId:''}</h2><button className="btn" data-modal-close onClick={()=>setSourceText('')}>关闭</button></header><div className="markdown"><Markdown>{sourceId?sourceText.slice(Math.max(0,sourceText.indexOf(`<a id="${sourceId}"`)-100),sourceText.indexOf(`<a id="${sourceId}"`)+2500):sourceText}</Markdown></div></div>}</main></>}
  {view==='batch'&&<BatchManager selected={batchId} onSelect={setBatchId} revision={revision} busy={!!operation} progress={progress} speakerDevice={settings.processing?.speaker_device||'auto'} launch={launch} pause={pause} openTask={id=>{setSelected(id);setView('library');setMobileDetail(true);setTab('transcript');}} importFiles={openImport} notice={setNotice}/>}
  {view==='scenes'&&<SceneManager busy={!!operation} launch={launch} changed={refreshScenes} notice={setNotice}/>}
  {view==='settings'&&<SettingsPanel settings={settings} busy={!!operation} revision={revision} progress={progress} launch={launch} changed={settingsChanged} notice={setNotice}/>}
  {operation&&view!=='batch'&&<div className="processing-bar" role="status"><button onClick={()=>setView(kind.current==='scenes'?'scenes':['models','storage'].includes(kind.current)?'settings':'batch')}>{progress||'正在处理录音'} ↗</button><button disabled={operation==='starting'} onClick={()=>void rpc('operations.cancel',{operation_id:operation}).catch(e=>setNotice(errorText(e)))}>取消当前</button></div>}
  {notice&&<div className="live-toast" role="status"><span>{notice}</span><button aria-label="关闭提示" onClick={()=>setNotice('')}>×</button></div>}
  {renameValue!==null&&<div className="modal-backdrop"><form className="live-dialog" role="dialog" aria-modal="true" aria-label="重命名录音" onSubmit={saveRename}><header><h2>重命名录音</h2></header><div className="dialog-body"><label className="field">录音名称<input autoFocus className="input" value={renameValue} onChange={e=>setRenameValue(e.target.value)} maxLength={200}/></label></div><footer className="row"><button type="button" className="btn" data-modal-close onClick={()=>setRenameValue(null)}>取消</button><button type="submit" className="btn primary" disabled={!renameValue.trim()}>保存</button></footer></form></div>}
  {importing&&<div className="modal-backdrop" onClick={e=>{if(e.target===e.currentTarget)setImporting(false);}}><form className="live-dialog" onSubmit={startImport} role="dialog" aria-modal="true" aria-labelledby="import-title"><header className="row spread"><h2 id="import-title">{summarizing?'生成总结':'导入录音'}</h2><button className="icon-btn" type="button" aria-label="关闭导入" data-modal-close onClick={()=>setImporting(false)}>×</button></header><div className="dialog-body">{!summarizing&&<>{settings.model_files&&!Object.values(settings.model_files).every(Boolean)&&<p className="small muted">本地模型尚未准备完整，请先按运行手册下载模型。</p>}<button className="live-dropzone" type="button" onClick={chooseFiles}>＋ 选择录音文件</button>{files.map(path=><div className="small muted file-label" key={path}>{filename(path)}</div>)}<div className="form-grid"><label className="field">场景匹配<select className="input" value={skill} onChange={e=>setSkill(e.target.value)}><option value="">AI 自动匹配</option>{scenes.filter(s=>s.enabled).map(s=><option key={s.id} value={s.id}>{sceneLabel(s)}</option>)}</select></label><label className="field">处理方式<select className="input" value={mode} onChange={e=>{setMode(e.target.value);setConsent(false);}}><option value="preview">本地转写 + 发送预览</option><option value="cloud">本地转写 + AI 整理</option><option value="none">仅本地转写</option></select></label></div></>}{mode==='cloud'&&<label className="consent"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>允许将转写文字发送至 DeepSeek，音频保留本地</label>}</div><footer className="row"><button className="btn" type="button" data-modal-close onClick={()=>setImporting(false)}>取消</button><button className="btn primary" type="submit" disabled={!!operation||(mode==='cloud'&&!consent)||(!summarizing&&!files.length)}>开始整理</button></footer></form></div>}
 </div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
