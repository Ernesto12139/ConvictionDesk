'use strict';
// Isolated OWNER copy only. All POSTs are intercepted and never reach the server.
const fs=require('node:fs'),path=require('node:path');
const [base,output,moduleFile]=process.argv.slice(2),url=new URL(base);
if(url.hostname!=='127.0.0.1'||url.port==='8000')throw Error('Isolated copy required');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],posts=[],errors=[];
const check=(v,k)=>{if(!v)throw Error(k);checks.push(k);};
(async()=>{
 fs.mkdirSync(output,{recursive:true});const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const original=await(await fetch(base+'/api/attention/queue')).json();
  check(original.scan_result_status==='PARTIAL','actual_copy_is_honestly_partial');
  let scenario=structuredClone(original),polls=0;
  const page=await browser.newPage({reducedMotion:'reduce'});page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());if(u.origin!==base)return route.abort();
   if(r.method()!=='GET'){
    posts.push({method:r.method(),path:u.pathname});
    if(u.pathname==='/api/attention/scan')return route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({scan_id:'browser-only-stub',status:'RUNNING'})});
    return route.abort();
   }
   if(u.pathname==='/api/console/portfolio-overview')return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"live price GET excluded"}'});
   if(u.pathname==='/api/attention/queue')return route.fulfill({json:scenario});
   if(u.pathname==='/api/attention/scans/browser-only-stub'){polls++;return route.fulfill({json:{status:'COMPLETED',report:{}}});}
   return route.continue();
  });
  for(const width of [1440,390,375]){
   await page.setViewportSize({width,height:900});await page.goto(base);
   await page.waitForFunction(()=>document.querySelector('#attention-status').textContent.includes('PARTIAL'));
   await page.locator('#attention-audit > summary').click();
   const audit=await page.locator('#attention-scan-details').innerText();
   check(audit.includes('本次已检索')&&audit.includes('本次未检索')&&audit.includes('尚无完整成功扫描'),`actual_scope_cache_and_absent_success_${width}`);
   for(const exit of ['cancel','escape','enter']){
    const before=posts.length;await page.locator('#attention-scan').click();await page.locator('#mia-cost-dialog').waitFor();
    check((await page.locator('#mia-cost-body').innerText()).includes('付费模型'),`paid_model_disclosed_${exit}_${width}`);
    check((await page.locator('#mia-cost-facts').innerText()).includes('4 条'),`bounded_batch_disclosed_${exit}_${width}`);
    check(await page.evaluate(()=>document.activeElement.dataset.cost)==='cancel',`default_cancel_${exit}_${width}`);
    if(exit==='cancel')await page.locator('[data-cost="cancel"]').click();else await page.keyboard.press(exit==='escape'?'Escape':'Enter');
    await page.waitForFunction(()=>!document.querySelector('#mia-cost-dialog').open);
    check(posts.length===before,`cancel_no_post_${exit}_${width}`);
   }
   await page.locator('#attention-scan').click();await page.locator('[data-cost="confirm"]').click();
   await page.waitForFunction(()=>!document.querySelector('#attention-scan').disabled);
   check(posts.length>0&&posts.every(p=>p.path==='/api/attention/scan'),`explicit_confirmation_only_stub_post_${width}`);
   const before=posts.length;
   await page.evaluate(()=>sessionStorage.setItem('mia-attention-scan','browser-only-stub'));await page.reload();
   await page.locator('#attention-scan').filter({hasText:'继续查看扫描'}).waitFor();await page.locator('#attention-scan').click();
   await page.waitForFunction(()=>!document.querySelector('#attention-scan').disabled);
   check(posts.length===before,`resume_is_get_only_${width}`);
   check(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)),`today_no_overflow_${width}`);
  }
  for(const [state,phrase]of [['COMPLETED_NO_MATERIAL_EVENTS','未发现新的重要事件'],['PROVIDER_UNAVAILABLE','来源暂时不可用']]){
   scenario=structuredClone(original);scenario.scan_result_status=state;scenario.items=[];scenario.historical_items=[];
   scenario.pending_events=0;scenario.latest_scan.status='COMPLETED';Object.assign(scenario.latest_scan.report,{coverage_incomplete:false,pending_events:0,symbols_not_checked:0,errors:[]});
   await page.goto(base);await page.waitForFunction(text=>document.querySelector('#attention-status').textContent.includes(text),phrase);
   check((await page.locator('#attention-status').innerText()).includes(phrase),'distinct_'+state);
  }
  // Evidence question drafts into the same task and remains behind cost consent.
  const records=await(await fetch(base+'/api/research/tasks')).json(),saved=records.find(t=>t.status==='COMPLETE');
  await page.goto(base+'/?research_task='+saved.task_id);await page.locator('.desk-core').waitFor();
  await page.locator('#desk-evidence-open').click();await page.locator('#desk-source-list > details > summary').first().click();
  const source=page.locator('#desk-source-list > details').first(),id=await source.getAttribute('data-source-id');
  await source.locator('.desk-evidence-followup').click();
  check((await page.locator('#desk-followup').inputValue()).includes(id),'evidence_question_carries_exact_saved_id');
  check(new URL(page.url()).searchParams.get('research_task')===saved.task_id,'evidence_question_retains_task');
  const before=posts.length;await page.locator('#desk-continue').click();await page.locator('#mia-cost-dialog').waitFor();await page.locator('[data-cost="cancel"]').click();
  check(posts.length===before,'evidence_followup_cancel_sends_nothing');
  check(errors.length===0,'no_page_errors');check(polls>=6,'confirmed_scan_and_resume_observed');
  fs.writeFileSync(path.join(output,'today-cost-browser.json'),JSON.stringify({status:'PASS',checks,intercepted_posts:posts,server_posts:0,errors,external_requests:0,live_prices:'EXCLUDED',utc:new Date().toISOString()},null,2));
  console.log(JSON.stringify({status:'PASS',checks:checks.length,server_posts:0,intercepted_posts:posts.length}));
 }finally{await browser.close();}
})().catch(e=>{fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'today-cost-browser.json'),JSON.stringify({status:'FAIL',failure:e.message,checks,posts,errors},null,2));console.error(e.message);process.exitCode=1;});
