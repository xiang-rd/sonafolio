import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
export type Frame={connection_id?:number;event:string;operation_id?:string;data?:any};
export const rpc=<T=any>(method:string,params:Record<string,unknown>={})=>invoke<T>('backend_request',{method,params});
export const connect=(workspace:string)=>invoke<{workspace:string}>('connect_workspace',{workspace});
export const disconnect=()=>invoke('disconnect_workspace');
export const subscribe=(handler:(frame:Frame)=>void)=>listen<Frame>('backend-event',event=>handler(event.payload));
export async function readArtifact(taskId:string,path:string){
  const chunks:Uint8Array[]=[];let offset=0,total=0;
  while(true){
    const result=await rpc('artifacts.read',{task_id:taskId,path,offset,limit:131072});
    const bytes=Uint8Array.from(atob(result.data),c=>c.charCodeAt(0));
    total+=bytes.length;if(total>32*1024*1024)throw new Error('文件过大，请从工作区打开');
    chunks.push(bytes);offset=result.next_offset;if(result.eof)break;
  }
  const data=new Uint8Array(total);let at=0;for(const chunk of chunks){data.set(chunk,at);at+=chunk.length;}
  return new TextDecoder('utf-8',{fatal:true}).decode(data);
}
export function safeSourcePath(path:string){
  if(path==='sources.md')return '';
  const match=path.match(/^sources\.md#([A-Za-z0-9._-]+)$/);
  return match?.[1]??null;
}
