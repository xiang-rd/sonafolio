import {useEffect} from 'react';

/** One focus boundary for every application dialog, including async editors. */
export function useModal(){
 useEffect(()=>{
  let dialog:HTMLElement|null=null,restore:HTMLElement|null=null;
  const focusable=()=>dialog?[...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')].filter(e=>e.getClientRects().length>0):[];
  const update=()=>{const dialogs=[...document.querySelectorAll<HTMLElement>('[role="dialog"]')];const current=dialogs.at(-1)||null;if(current===dialog)return;
   if(!current){dialog=null;if(restore?.isConnected)restore.focus();restore=null;return;}
   if(!dialog)restore=document.activeElement as HTMLElement;
   dialog=current;const target=dialog.querySelector<HTMLElement>('input:not([type=checkbox]),textarea,select')||focusable()[0];
   if(target)target.focus();else{dialog.tabIndex=-1;dialog.focus();}
  };
  const keyboard=(event:KeyboardEvent)=>{if(!dialog)return;if(event.key==='Escape'){event.preventDefault();dialog.querySelector<HTMLElement>('[data-modal-close]')?.click();return;}
   if(event.key==='Tab'){const items=focusable();event.preventDefault();if(!items.length)return;const index=items.indexOf(document.activeElement as HTMLElement);items[(index+(event.shiftKey?-1:1)+items.length)%items.length].focus();}
  };
  const observer=new MutationObserver(update);observer.observe(document.body,{childList:true,subtree:true});document.addEventListener('keydown',keyboard);update();
  return()=>{observer.disconnect();document.removeEventListener('keydown',keyboard);};
 },[]);
}
