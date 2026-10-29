// Share management tools between the desktop dashboard and the admin Mini App.
(async()=>{
 const {extendManagement}=await import('/static/management-ui.js');
 const controller={views,actions:{}};
 const ui={e,n:num,fullDate,a:api,rpc:(path,d,method='POST')=>api(path.replace(/^\/api/,''),{method,body:JSON.stringify(d)}),
  action:actionButton,title:heading,field,state:{},toast,render,
  api:async(path,options={})=>{if(options.blob){const r=await fetch(path,{credentials:'include'});if(!r.ok)throw new Error('فایل در دسترس نیست.');return r.blob();}return api(path.replace(/^\/api/,''),options);},
  sheet:(name,html,save)=>save?form(name,html,save):modal(name,html),close:()=>$('#modal').close(),confirm:confirmation};
 extendManagement(controller,ui);
 pages.push(['branding','نام، متن و لوگو','tag'],['license','لایسنس نصب','shield']);
 try{const context=await fetch('/mini-api/context').then(r=>r.json());if(context.license_issuer){const {extendOwner}=await import('/owner-assets/licenses.js');extendOwner(controller,ui);pages.push(['licenses','مدیریت لایسنس‌ها','shield']);}}catch{}
 $('#nav').innerHTML=pages.map(([key,t,ic])=>`<a href="#${key}" data-page="${key}">${icon(ic)}<span>${t}</span>${key==='tickets'?'<span id="ticket-count" class="nav-count" hidden>۰</span>':''}</a>`).join('');
 window.farstarTools=controller.actions;
 if(['branding','license','licenses'].includes(currentPage()))await render();
})();
