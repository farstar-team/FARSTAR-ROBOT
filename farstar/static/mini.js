import {createCustomer} from './mini-customer.js';
import {createAdmin} from './mini-admin.js';

const $=selector=>document.querySelector(selector);
const e=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=value=>Number(value||0).toLocaleString('fa-IR');
const date=value=>value?new Date(value*1000).toLocaleDateString('fa-IR',{timeZone:'Asia/Tehran',month:'short',day:'numeric'}):'—';
const fullDate=value=>value?new Date(value*1000).toLocaleString('fa-IR',{timeZone:'Asia/Tehran',dateStyle:'medium',timeStyle:'short'}):'—';
const paths={home:'M3 10l9-7 9 7v11h-6v-7H9v7H3V10z',bag:'M5 7h14l1 14H4L5 7zM9 8V5a3 3 0 0 1 6 0v3',
 globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM3 12h18M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18',
 wallet:'M20 7H5a2 2 0 0 1 0-4h13v4M3 5v14a2 2 0 0 0 2 2h15V7M20 12h-5v5h5',
 help:'M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5M12 17h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
 arrow:'M19 12H5M12 5l-7 7 7 7',plus:'M12 5v14M5 12h14',check:'M5 12l4 4L19 6',
 copy:'M8 8h13v13H8zM16 8V3H3v13h5',clock:'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
 users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.87',
 ticket:'M21 11a8 8 0 0 1-8 8H8l-5 3 1-6a8 8 0 1 1 17-5zM8 10h8M8 14h4',
 receipt:'M6 3h12v19l-3-2-3 2-3-2-3 2V3zM9 7h6M9 11h6M9 15h3',
 more:'M5 12h.01M12 12h.01M19 12h.01',gift:'M3 8h18v4H3zM5 12v9h14v-9M12 8v13M12 8H8a3 3 0 1 1 3-3l1 3zm0 0h4a3 3 0 1 0-3-3l-1 3z',
 server:'M4 3h16v7H4zM4 14h16v7H4zM7 6h.01M7 17h.01M12 6h5M12 17h5',
 shield:'M12 2l9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4zM8 12l3 3 5-6',
 activity:'M2 12h4l3-9 6 18 3-9h4',download:'M12 3v12M7 10l5 5 5-5M4 16v5h16v-5',
 send:'M22 2L9 15M22 2l-7 20-4-9-9-4 20-7z',tag:'M20 13l-7 8L2 10V2h8l10 11zM7 7h.01',
 settings:'M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6',qr:'M3 3h6v6H3zM15 3h6v6h-6zM3 15h6v6H3zM15 15h3v3h3v3h-6v-6z'};
const icon=name=>`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.home}"/></svg>`;
const labels={pending:['منتظر پرداخت','amber'],awaiting_review:['منتظر تأیید فیش','amber'],checkout:['در حال پرداخت','amber'],paid:['در صف تحویل','purple'],processing:['در حال ساخت','purple'],completed:['تحویل شد','green'],failed:['بررسی پشتیبانی','red'],cancelled:['لغو شد',''],expired:['منقضی',''],refunded:['بازپرداخت شد',''],refund_pending:['بازپرداخت در صف','amber'],approved:['تأیید شد','green'],rejected:['رد شد','red'],open:['منتظر پاسخ','amber'],answered:['پاسخ داده شد','green'],closed:['بسته شد','']};
const badge=value=>{const [title,kind]=labels[value]||[value,''];return `<span class="badge ${kind}">${e(title)}</span>`;};
let toastTimer,renderId=0,controller;
const state={role:location.pathname==='/admin-app'?'admin':'customer',csrf:'',token:'',me:null,context:null};
const tg=()=>window.Telegram?.WebApp;
const unit=()=>state.role==='admin'?(state.meta?.currency==='XTR'?'استار':'تومان'):(state.me?.currency==='XTR'?'استار':'تومان');
const money=value=>`${n(value)}<small>${unit()}</small>`;
function toast(text,error=false){clearTimeout(toastTimer);$('#mini-toast').textContent=text;$('#mini-toast').className=error?'error':'';$('#mini-toast').hidden=false;toastTimer=setTimeout(()=>$('#mini-toast').hidden=true,4500);}
async function api(path,options={}){
 const headers={'X-CSRF-Token':state.csrf,'X-Mini-Session':state.token,...options.headers};
 if(options.body&&!(options.body instanceof FormData))headers['Content-Type']='application/json';
 const response=await fetch(path,{credentials:'include',...options,headers});
 if(options.blob){if(!response.ok)throw new Error('تصویر در دسترس نیست.');return response.blob();}
 const data=await response.json().catch(()=>({detail:'پاسخ سرور معتبر نیست.'}));
 if(!response.ok){let detail=data.detail;if(Array.isArray(detail))detail=detail.map(x=>x.msg).join('؛ ');throw new Error(detail||'عملیات انجام نشد.');}
 return data;
}
const rpc=(path,data,method='POST')=>api(path,{method,body:JSON.stringify(data??{})});
const c=(path,options)=>api('/mini-api'+path,options);
const a=(path,options)=>api('/api'+path,options);
const action=(name,text,ic='',cls='button',attrs='')=>`<button type="button" class="${cls}" data-action="${name}" ${attrs}>${ic?icon(ic):''}${text}</button>`;
const title=(name,sub='',right='')=>`<div class="page-title"><div><h1>${name}</h1>${sub?`<p>${sub}</p>`:''}</div>${right}</div>`;
const field=(label,name,value='',type='text',extra='')=>`<label>${label}<input type="${type}" name="${name}" value="${e(value)}" ${extra}></label>`;
const select=(label,name,values,value)=>`<label>${label}<select name="${name}">${values.map(([key,text])=>`<option value="${e(key)}" ${String(key)===String(value)?'selected':''}>${e(text)}</option>`).join('')}</select></label>`;
const check=(label,name,checked=false)=>`<label class="check"><input type="checkbox" name="${name}" ${checked?'checked':''}>${label}</label>`;
const empty=(text,sub='',ic='bag',buttons='')=>`<section class="card empty">${icon(ic)}<h2>${text}</h2><p>${sub}</p>${buttons}</section>`;
const metric=(name,value,small,ic)=>`<article class="metric"><div class="metric-top">${name}${icon(ic)}</div><div class="metric-value">${n(value)}<small>${small}</small></div></article>`;
const nav=key=>{location.hash=key;};
function sheet(name,html,submit=null,label='تأیید'){
 if(state.orderPoll){clearInterval(state.orderPoll);state.orderPoll=null;}
 $('#sheet-title').textContent=name;
 $('#sheet-content').innerHTML=submit?`<form id="sheet-form">${html}<div class="form-error" id="sheet-error" role="alert"></div><div class="form-actions">${action('close','انصراف','','button bare')}<button class="button" type="submit">${label}</button></div></form>`:html;
 state.sheetSubmit=submit;
 if(!$('#sheet').open)$('#sheet').showModal();
}
function close(){if(state.orderPoll){clearInterval(state.orderPoll);state.orderPoll=null;}if($('#sheet').open)$('#sheet').close();state.sheetSubmit=null;if(state.blobURL){URL.revokeObjectURL(state.blobURL);state.blobURL=null;}}
const confirm=(name,text,fn,label='تأیید')=>sheet(name,`<p class="safe-text">${e(text)}</p>`,fn,label);
async function render(){
 const id=++renderId,key=location.hash.slice(1)||'home';
 document.querySelectorAll('#bottom-nav a').forEach(el=>el.classList.toggle('active',el.dataset.page===key));
 $('#screen').innerHTML='<div class="loader"><span></span>در حال دریافت اطلاعات…</div>';
 try{const result=await (controller.views[key]||controller.views.home)();if(id===renderId)$('#screen').innerHTML=result;}
 catch(error){if(id===renderId)$('#screen').innerHTML=empty('اطلاعات دریافت نشد.',e(error.message),'activity',action('refresh','تلاش دوباره'));}
}
const ui={$,e,n,date,fullDate,icon,badge,labels,state,tg,unit,money,toast,api,rpc,c,a,action,title,field,select,check,empty,metric,nav,sheet,close,confirm,render};
async function start(){
 document.body.classList.toggle('admin',state.role==='admin');
 $('#role-label').textContent=state.role==='admin'?'مرکز مدیریت':'همراه اتصال تو';
 try{
  state.context=await c('/context');
  const raw=tg()?.initData;
  let auth;
  if(raw){auth=await rpc('/mini-api/auth',{init_data:raw,role:state.role});}
  else if(state.context.demo){
   $('#gate-copy').textContent='پیش‌نمایش مینی‌اپ با اطلاعات نمونه؛ بدون اتصال خارجی.';
   $('#gate-actions').innerHTML=`<span class="demo-label">محیط توسعه · نمای ${state.role==='admin'?'مدیر':'مشتری'}</span>${action('demo-start','ورود به نسخه نمایشی','arrow')}`;
   return;
  }else{throw new Error('برای ورود امن، این مینی‌اپ را از دکمه داخل ربات Farstar باز کن.');}
  await enter(auth);
 }catch(error){$('#gate-copy').textContent=error.message;const username=state.context?.bot_username;$('#gate-actions').innerHTML=username?`<a class="button" href="https://t.me/${encodeURIComponent(username)}">باز کردن ربات در تلگرام</a>`:'<p class="form-note">از منوی ربات، دکمه مینی‌اپ را بزن.</p>';}
}
async function enter(auth){
 Object.assign(state,{csrf:auth.csrf,token:auth.session_token||''});
 controller=state.role==='admin'?createAdmin(ui):createCustomer(ui);
 if(state.role==='customer'){state.me=await c('/me');$('#profile-button').textContent=state.me.user.name.slice(0,1);}
 else{state.meta=await a('/meta');$('#profile-button').textContent='A';}
 $('#bottom-nav').innerHTML=controller.nav.map(([page,label,ic])=>`<a href="#${page}" data-page="${page}">${icon(ic)}<span>${label}</span></a>`).join('');
 $('#gate').hidden=true;$('#mini-shell').hidden=false;$('#demo-note').hidden=!state.context.demo;
 tg()?.ready();tg()?.expand();
 try{tg()?.setHeaderColor('#0c1819');tg()?.setBackgroundColor('#0c1819');}catch{}
 await render();
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
 const {action:name,...data}=button.dataset;
 try{
  tg()?.HapticFeedback?.impactOccurred('light');
  button.disabled=true;
  if(name==='close')close();
  else if(name==='refresh')await render();
  else if(name==='goto'){close();nav(data.page);if((location.hash.slice(1)||'home')===data.page)await render();}
  else if(name==='demo-start'){await enter(await rpc('/mini-api/demo-login',{role:state.role}));}
  else if(name==='copy'){try{await navigator.clipboard.writeText(data.value);toast('کپی شد.');}catch{sheet('کپی دستی',`<div class="subscription-box">${e(data.value)}</div><p class="form-note">متن را انتخاب و کپی کن.</p>`);}}
  else if(controller?.actions[name])await controller.actions[name](data,button);
 }catch(error){toast(error.message,true);}finally{button.disabled=false;}
});
document.addEventListener('submit',async event=>{
 if(event.target.id==='sheet-form'){
  event.preventDefault();const handler=state.sheetSubmit;if(!handler)return;const form=event.target,button=form.querySelector('[type=submit]');button.disabled=true;$('#sheet-error').textContent='';
  try{await handler(Object.fromEntries(new FormData(form)),form);if(state.sheetSubmit===handler)close();}
  catch(error){const el=$('#sheet-error');if(el)el.textContent=error.message;else toast(error.message,true);}
  finally{button.disabled=false;}
 }else if(controller?.forms[event.target.id]){event.preventDefault();const button=event.target.querySelector('[type=submit]');if(button)button.disabled=true;try{await controller.forms[event.target.id](event.target);}catch(error){toast(error.message,true);}finally{if(button)button.disabled=false;}}
});
$('#sheet').addEventListener('click',event=>{if(event.target===$('#sheet')){const rect=event.target.getBoundingClientRect();if(event.clientY<rect.top||event.clientY>rect.bottom||event.clientX<rect.left||event.clientX>rect.right)close();}});
window.addEventListener('hashchange',()=>{if(controller){close();render();window.scrollTo({top:0,behavior:'instant'});}});
if(document.readyState==='complete')start();else window.addEventListener('load',start,{once:true});
