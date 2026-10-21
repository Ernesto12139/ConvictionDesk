'use strict';
// Read-only browser journey, synthetic private acceptance process only.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile]=process.argv.slice(2),url=new URL(base);
if(url.hostname!=='127.0.0.1'||url.port==='8000')throw Error('Isolated fixture required');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],errors=[],posts=[],httpErrors=[];
const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
const hash=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const get=async route=>{const response=await fetch(base+route);if(!response.ok)throw Error(route);return response.json();};
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 if(fs.existsSync(path.join(output,'capital-reference-browser.json')))throw Error('Preserve previous evidence');
 const identity=await get('/_v09/identity');
 check(identity.scope==='OFFLINE_SYNTHETIC_ONLY'&&identity.source_on_disk_matches,'source_pinned_synthetic_process');
 const tasks=await get('/api/research/tasks'),before=hash(tasks);
 const deep=tasks.find(t=>t.focus_symbol==='APP'&&t.mode!=='FAST'&&t.investigation.status==='NEW_EVIDENCE');
 const fast=tasks.find(t=>t.focus_symbol==='APP'&&t.mode==='FAST');
 const loss=tasks.find(t=>t.focus_symbol==='SMR');
 check(deep&&fast&&loss,'three_complete_saved_records');
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({reducedMotion:'reduce'});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.status()>=400)httpErrors.push(response.status());});
  await page.route('**/*',route=>{
   const request=route.request();
   if(new URL(request.url()).origin!==url.origin){errors.push('external request');return route.abort();}
   if(request.method()!=='GET'){posts.push(request.url());return route.abort();}
   return route.continue();
  });
  for(const profile of [{name:'desktop-light',width:1440,height:960,theme:'light'},
      {name:'mobile-dark',width:390,height:844,theme:'dark'},
      {name:'desktop-dark',width:1440,height:960,theme:'dark'},
      {name:'mobile-light',width:390,height:844,theme:'light'}]){
   await page.setViewportSize({width:profile.width,height:profile.height});
   await page.goto(base);await page.evaluate(theme=>localStorage.setItem('mia.theme.v1',theme),profile.theme);
   for(const task of [fast,deep,loss]){
    const tag=profile.name+'-'+task.focus_symbol+'-'+(task.mode||'DEEP');
    await page.goto(base+'/?research_task='+task.task_id);
    const reference=page.locator('.desk-provider-reference');await reference.waitFor();
    check(await page.locator('html').getAttribute('data-theme')===profile.theme,tag+'_actual_theme');
    check(await page.locator('.desk-core').isVisible(),tag+'_decision_preserved');
    check((await reference.innerText()).includes(task.focus_symbol==='APP'?'98,765.432100':'4,444.00'),tag+'_exact_reported_value');
    check((await reference.innerText()).includes('字段生效时点未知'),tag+'_unknown_effective_time_visible');
    check((await reference.innerText()).includes('未发送给模型'),tag+'_display_model_boundary_visible');
    await reference.locator('summary').click();
    check((await reference.innerText()).includes('SOURCE_REPORTED_REFERENCE'),tag+'_qualification_visible');
    check((await reference.innerText()).includes('12.50 USD/股')&&!(await reference.innerText()).includes('999.99'),tag+'_regular_not_afterhours');
    check((await reference.innerText()).includes('local:sha256:'),tag+'_source_hash_visible');
    check(await reference.locator('a[href^="https://api.massive.com/"]').count()===2,tag+'_credential_free_source_uris');
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),tag+'_no_horizontal_overflow');
    const report=await get('/api/research/tasks/'+task.task_id+'/valuation?revision='+task.revision);
    if(report.metrics.market_cap_reference.value!=null)check((await reference.innerText()).includes('两者不可互换'),tag+'_different_capital_basis_not_interchangeable');
    check(report.local_capital_reference.value!=='1250',tag+'_reported_not_recomputed');
    if(task.focus_symbol==='SMR')check(report.metrics.pe_ttm.status==='NOT_MEANINGFUL'&&report.metrics.price_sales_ttm.status==='NOT_MEANINGFUL',tag+'_loss_valuation_not_meaningful');
    if(task===deep)await page.screenshot({path:path.join(output,profile.name+'.png'),fullPage:true});
   }
  }
  // Tamper only a verified fictional capture; never source, Owner or real data.
  const capture=path.join(path.dirname(identity.database),'capital-captures','APP-overview.json');
  const original=fs.readFileSync(capture),fixture=JSON.parse(original);
  check(fixture.results.cik==='synthetic-APP'&&fixture.results.market_cap==='98765.432100','tamper_target_is_own_fictional_capture');
  try{
   fs.writeFileSync(capture,Buffer.concat([original,Buffer.from(' ')]));
   await page.goto(base+'/?research_task='+deep.task_id);
   await page.getByText('本地 Provider 参考不可用或完整性核查失败；未以缓存、零或模型猜测补齐。',{exact:true}).waitFor();
   check(await page.locator('.desk-provider-reference').count()===0,'tamper_hides_value_without_cached_fallback');
   check(await page.locator('.desk-core').isVisible(),'tamper_preserves_saved_conclusion');
  }finally{fs.writeFileSync(capture,original);}
  await page.reload();await page.locator('.desk-provider-reference').waitFor();
  check((await page.locator('.desk-provider-reference').innerText()).includes('98,765.432100'),'explicit_reload_recovers_after_restored_integrity');
  const journalPath=path.join(path.dirname(capture),'journal.json'),journalBytes=fs.readFileSync(journalPath);
  try{
   const journal=JSON.parse(journalBytes);
   for(const record of journal.records)if(record.symbol==='APP')record.retrieved_at='2026-08-01T12:00:00+00:00';
   fs.writeFileSync(journalPath,JSON.stringify(journal));
   await page.reload();
   await page.getByText('本地 Provider 快照已超过七天，未作为本轮近期参考展示；需另行取得合格新来源，打开本页不会自动刷新。',{exact:true}).waitFor();
   check(await page.locator('.desk-provider-reference').count()===0,'stale_snapshot_has_explicit_disclosure_and_no_value');
  }finally{fs.writeFileSync(journalPath,journalBytes);}
  await page.reload();await page.locator('.desk-provider-reference').waitFor();
  check(hash(await get('/api/research/tasks'))===before,'all_saved_task_bytes_unchanged');
  const final=await get('/_v09/identity');
  check(final.fake_model_requests===identity.fake_model_requests,'reading_and_recovery_zero_model_calls');
  check(final.external_attempts===0&&final.paid_model_calls===0&&!posts.length,'zero_external_paid_or_post');
  check(!errors.length&&!httpErrors.length,'zero_page_or_http_errors');
  fs.writeFileSync(path.join(output,'capital-reference-browser.json'),JSON.stringify({status:'PASS',identity,final,
   checks,errors,httpErrors,posts,scope:'Fictional provider fields/scripted saved research only; not actual valuation/model quality'},null,2));
  process.stdout.write(JSON.stringify({status:'PASS',checks:checks.length,posts:posts.length}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
