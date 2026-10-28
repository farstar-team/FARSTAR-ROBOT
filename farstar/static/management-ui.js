export function extendManagement(controller,u){
 const {e,n,fullDate,a,rpc,action,title,field,sheet,close,toast,render,state}=u;
 const card=body=>`<section class="card panel">${body}</section>`;
 const statusText={active:'فعال',pending:'منتظر اولین نصب',revoked:'غیرفعال',expired:'منقضی',issuer:'مرجع اصلی',development:'محیط توسعه',missing:'فعال نشده',invalid:'نامعتبر'};
 controller.views.branding=async()=>{
  const b=await a('/branding');state.branding=b;
  return title('نام، متن‌ها و لوگو','فروشگاه را با هویت خودت معرفی کن.')+card(
   `<h2>${e(b.app_name)}</h2>${b.logo_url?'<img src="/brand/logo" alt="لوگو" width="72" height="72">':''}<div class="split-actions actions">${action('brand-name','تغییر نام','tag')}${action('brand-logo','بارگذاری لوگو','plus')}</div><p class="form-note">نام نمایشی پروفایل تلگرام از مدیریت داخل ربات نیز قابل اعمال است. نام کاربری @ را از BotFather تغییر بده.</p>`)+
   card(Object.entries(b.texts).map(([key,x])=>`<div class="list-item"><div><h3>${e(x.label)}</h3><p class="safe-text">${e(x.value.slice(0,160)||'متن پیش‌فرض')}</p></div>${action('brand-text','ویرایش','tag','button small secondary',`data-key="${key}"`)}</div>`).join(''));
 };
 controller.views.license=async()=>{const l=await a('/license');return title('لایسنس نصب','مرجع رسمی دریافت و تمدید: @FARSTAR_VPN_BOT')+card(`<h2>${l.can_sell?'فروش جدید فعال است':'نیازمند بررسی لایسنس'}</h2><p>وضعیت: ${e(statusText[l.status]||l.status)}</p><p>پایان اعتبار: ${fullDate(l.expires_at)}</p>${l.offline_grace?'<p class="form-note">ارتباط مرجع موقتاً برقرار نیست؛ مهلت محدود آفلاین فعال است.</p>':''}${action('license-refresh','بررسی دوباره','activity')}<p class="form-note">انقضای لایسنس اشتراک‌های قبلی را قطع نمی‌کند.</p><a class="button secondary" href="https://t.me/FARSTAR_VPN_BOT" target="_blank" rel="noopener">دریافت / تمدید لایسنس</a>`);};
 Object.assign(controller.actions,{
  'brand-name':async()=>sheet('نام فروشگاه',field('نام نمایشی','app_name',state.branding.app_name,'text','required maxlength="64"'),async d=>{const b=await rpc('/api/branding',{app_name:d.app_name},'PUT');document.title=b.app_name;for(const id of ['brand-name','shop-name']){const el=document.getElementById(id);if(el)el.textContent=b.app_name;}close();toast('نام ذخیره شد.');await render();}),
  'brand-text':async d=>{const b=state.branding.texts[d.key];sheet(b.label,`<label>متن دلخواه (متن ساده)<textarea name="text" maxlength="${d.key.endsWith('_button')?48:2000}">${e(b.value)}</textarea></label>`,async f=>{await rpc('/api/branding',{texts:{[d.key]:f.text}},'PUT');close();toast('متن ذخیره شد.');await render();});},
  'brand-logo':async()=>sheet('لوگوی مینی‌اپ','<label>یک عکس PNG، JPEG یا WebP کوچک‌تر از ۳۵۰ کیلوبایت<input name="logo" type="file" accept="image/png,image/jpeg,image/webp" required></label><p class="form-note">یک نسخه بهینه حداکثر ۲۵۶ پیکسل نگه داشته می‌شود؛ لوگوی قبلی جایگزین می‌شود.</p>',async d=>{
   const file=d.logo;if(!(file instanceof File)||!file.size||file.size>350000)throw new Error('عکس کوچک‌تر از ۳۵۰ کیلوبایت انتخاب کن.');
   const raw=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<raw.length;i++)binary+=String.fromCharCode(raw[i]);
   await rpc('/api/branding/logo',{image:btoa(binary)});const el=document.getElementById('brand-logo');if(el)el.innerHTML='<img src="/brand/logo?v='+Date.now()+'" width="34" height="34" alt="لوگوی فروشگاه">';close();toast('لوگو ذخیره شد.');await render();
  }),
  'license-refresh':async()=>{await rpc('/api/license/refresh',{});await render();}
 });
}
