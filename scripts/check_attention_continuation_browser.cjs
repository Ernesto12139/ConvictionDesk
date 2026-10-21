'use strict';
// Explicit fictional fixture only. Confirmed POST is allowed only to saved-current
// continuation; all other mutations/external origins are blocked and fail QA.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile]=process.argv.slice(2),url=new URL(base);
if(url.hostname!=='127.0.0.1'||url.port==='8000')throw Error('Isolated fixture required');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],failures=[],posts=[],responses=[];
const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
const hash=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const get=async route=>{const response=await fetch(base+route);if(!response.ok)throw Error('GET '+route);return response.json();};
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 if(fs.existsSync(path.join(output,'continuation-browser.json')))throw Error('Preserve prior report');
 const identity=await get('/_v09/identity');
 check(identity.scope==='OFFLINE_SYNTHETIC_ONLY'&&identity.source_on_disk_matches,'source_pinned_fictional_fixture');
 const savedTasks=hash(await get('/api/research/tasks'));
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 let allowContinue=false;
 try{
  const page=await browser.newPage({reducedMotion:'reduce'});
  page.on('pageerror',e=>failures.push(e.message));
  page.on('response',r=>{if(r.status()>=400)responses.push({path:new URL(r.url()).pathname,status:r.status()});});
  await page.route('**/*',route=>{
   const request=route.request(),target=new URL(request.url());
   if(target.origin!==base){failures.push('foreign origin');return route.abort();}
   if(request.method()!=='GET'){
    posts.push({path:target.pathname,permitted:allowContinue&&target.pathname==='/api/attention/continue'});
    if(!allowContinue||target.pathname!=='/api/attention/continue'){failures.push('unconfirmed mutation');return route.abort();}
    allowContinue=false;
   }
   return route.continue();
  });
  for(const width of [1440,390])for(const theme of ['light','dark']){
   const tag=`${width}-${theme}`;
   await page.setViewportSize({width,height:960});await page.goto(base);
   await page.locator('#mia-theme').selectOption(theme);
   await page.locator('#attention-continue').waitFor({state:'visible'});
   check(await page.locator('#v09-fixture-notice').isVisible(),tag+'_fictional_notice');
   check((await page.locator('#attention-summary').innerText()).includes('过期未筛'),tag+'_expiry_visible_without_diagnostics');
   const before=await get('/api/attention/queue'),sourceBefore=await get('/_v09/identity');
   const count=before.pending_lifecycle.current,postCount=posts.length;
   for(const exit of ['cancel','escape','enter']){
    await page.locator('#attention-continue').click();await page.locator('#mia-cost-dialog').waitFor();
    const text=await page.locator('#mia-cost-dialog').innerText();
    check(text.includes('付费模型')&&text.includes('新闻请求 0 次')&&text.includes(`最多筛选 ${before.screen_batch_limit}`),tag+'_'+exit+'_cost_and_scope');
    check(await page.evaluate(()=>document.activeElement?.dataset.cost==='cancel'),tag+'_'+exit+'_cancel_default');
    if(exit==='cancel')await page.locator('[data-cost="cancel"]').click();
    else await page.keyboard.press(exit==='escape'?'Escape':'Enter');
    await page.locator('#mia-cost-dialog').waitFor({state:'hidden'});
   }
   check(posts.length===postCount,tag+'_cancel_escape_enter_zero_post');
   check((await get('/_v09/identity')).fake_model_requests===sourceBefore.fake_model_requests,tag+'_cancel_zero_model');
   await page.locator('#attention-continue').click();await page.locator('#mia-cost-dialog').waitFor();
   allowContinue=true;await page.locator('[data-cost="confirm"]').click();
   await page.waitForFunction(expected=>document.querySelector('#attention-continue').textContent.includes(`（${expected}）`),count-before.screen_batch_limit);
   const after=await get('/api/attention/queue'),sourceAfter=await get('/_v09/identity');
   check(posts.length===postCount+1&&posts.at(-1).permitted,tag+'_exactly_one_confirmed_post');
   check(after.pending_lifecycle.current===count-before.screen_batch_limit,tag+'_one_bounded_batch_progress');
   check(after.pending_lifecycle.expired_unscreened===before.pending_lifecycle.expired_unscreened,tag+'_expired_preserved');
   check(sourceAfter.news_requests===sourceBefore.news_requests,tag+'_zero_source_search');
   check(after.latest_scan.report.scan_mode==='SAVED_CURRENT_ONLY'&&after.scan_result_status==='PARTIAL',tag+'_saved_only_partial');
   check((await page.locator('#attention-status').innerText()).includes('未获取新来源'),tag+'_no_new_coverage_claim');
   await page.locator('#attention-audit > summary').click();
   const audit=await page.locator('#attention-scan-details').innerText();
   check(audit.includes('没有推进来源抓取游标')&&audit.includes('过期未筛'),tag+'_source_and_per_symbol_disclosure');
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),tag+'_no_page_overflow');
   await page.locator('#attention-audit > summary').click();
   await page.evaluate(()=>window.scrollTo(0,0));
   await page.screenshot({path:path.join(output,tag+'.png'),fullPage:true});
  }
  check(hash(await get('/api/research/tasks'))===savedTasks,'saved_research_revisions_unchanged');
  const final=await get('/_v09/identity');
  check(final.source_on_disk_matches&&final.external_attempts===0&&final.paid_model_calls===0,'final_identity_zero_external_paid');
  check(!failures.length&&!responses.length,'zero_page_errors_http_errors_foreign_unconfirmed_mutations');
  const result={status:'PASS',identity,final,checks,posts,failures,responses,scope:'Fictional/scripted model only; actual manual UI POSTs are bounded saved-current continuations'};
  fs.writeFileSync(path.join(output,'continuation-browser.json'),JSON.stringify(result,null,2));
  process.stdout.write(JSON.stringify({status:'PASS',checks:checks.length,confirmed_posts:posts.length,news_calls_added:final.news_requests-identity.news_requests}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
