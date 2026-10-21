'use strict';
// Only explicitly submitted local calculation POSTs; every research/scan is blocked.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile]=process.argv.slice(2),origin=new URL(base);
if(origin.hostname!=='127.0.0.1'||origin.port==='8000')throw Error('Isolated fixture only');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],errors=[],posts=[],httpErrors=[];
const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
const get=async route=>{const r=await fetch(base+route);if(!r.ok)throw Error(route);return r.json();};
const hash=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 if(fs.existsSync(path.join(output,'dated-valuation-browser.json')))throw Error('Preserve prior evidence');
 const beforeIdentity=await get('/_v09/identity');
 check(beforeIdentity.scope==='OFFLINE_SYNTHETIC_ONLY'&&beforeIdentity.source_on_disk_matches,'pinned_synthetic_source');
 const tasks=await get('/api/research/tasks'),beforeTasks=hash(tasks);
 const profitable=tasks.find(t=>t.focus_symbol==='APP'&&t.task_id!=='synthetic-class-conflict'&&t.mode!=='FAST');
 const loss=tasks.find(t=>t.focus_symbol==='SMR');
 const conflict=tasks.find(t=>t.task_id==='synthetic-class-conflict');
 check(profitable&&loss&&conflict,'three_company_boundary_records');
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({reducedMotion:'reduce'});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.status()>=400)httpErrors.push(response.url()+':'+response.status());});
  await page.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());
   if(u.origin!==origin.origin){errors.push('external browser request');return route.abort();}
   if(r.method()!=='GET'){
    if(r.method()!=='POST'||!/^\/api\/research\/tasks\/[^/]+\/dated-valuation$/.test(u.pathname)){
     errors.push('Unapproved mutation:'+u.pathname);return route.abort();
    }
    posts.push({path:u.pathname,body:JSON.parse(r.postData())});
   }
   return route.continue();
  });
  for(const profile of [{width:1440,height:960,theme:'light'},{width:390,height:844,theme:'dark'},
    {width:1440,height:960,theme:'dark'},{width:390,height:844,theme:'light'}]){
   await page.setViewportSize({width:profile.width,height:profile.height});
   await page.goto(base);await page.evaluate(theme=>localStorage.setItem('mia.theme.v1',theme),profile.theme);
   for(const [task,name]of [[profitable,'profit'],[loss,'loss'],[conflict,'class-conflict']]){
    const tag=profile.width+'-'+profile.theme+'-'+name;
    const count=posts.length;
    await page.goto(base+'/?research_task='+task.task_id);
    const entry=page.locator('.desk-dated-price');await entry.locator('summary').first().waitFor();
    check(await page.locator('.desk-core').isVisible(),tag+'_original_decision_preserved');
    check(await page.locator('html').getAttribute('data-theme')===profile.theme,tag+'_theme');
    await entry.locator('summary').first().click();
    const form=entry.locator('form'),result=entry.locator('.desk-dated-result');
    check(posts.length===count,tag+'_opening_is_zero_post');
    await form.locator('input[type=text]').nth(0).fill('12.50');
    await form.locator('input[type=date]').fill('2026-10-16');
    await form.locator('input[type=text]').nth(1).fill('Self-defined synthetic scenario, not Provider copy');
    const submit=form.locator('button[type=submit]');
    await submit.click();check(posts.length===count,tag+'_required_rights_and_assumptions_no_post');
    for(let i=0;i<4;i++)await form.locator('input[type=checkbox]').nth(i).check();
    await submit.click();await result.locator('h4').waitFor();
    check(posts.length===count+1,tag+'_one_explicit_local_calculation');
    const text=await result.innerText();
    if(name==='class-conflict'){
      check(text.includes('无法形成参考估值')&&text.includes('多股类'),tag+'_known_class_conflict_rejected');
      check(await result.locator('.desk-valuation-list').count()===0,tag+'_no_false_numeric_result');
    }else{
      check(text.includes('假设情景 · 非实际行情')&&text.includes('未核实条件'),tag+'_assumptions_before_values');
      check(text.includes(name==='profit'?'1,250.00 USD':'2,500.00 USD'),tag+'_exact_decimal_equity');
      if(name==='loss')check(text.includes('NOT_MEANINGFUL'),tag+'_loss_not_meaningful');
      check(text.includes('未发送给 Deep')&&text.includes('Thesis／Action 保持不变'),tag+'_action_and_model_boundary');
      if(name==='profit'){
       await result.screenshot({path:path.join(output,tag+'.png')});
       await form.locator('select').first().selectOption('USER_SUPPLIED_OBSERVATION');
       await form.locator('input[type=text]').nth(1).fill('Own synthetic observation with local derivation rights');
       await submit.click();await page.waitForFunction(()=>document.querySelector('.desk-dated-result h4')?.textContent.includes('条件参考'));
       check((await result.innerText()).includes('用户观察尚未核实'),tag+'_observation_not_verified');
       await result.locator('details summary').click();
       const receipt=JSON.parse(await result.locator('pre').innerText());
       check(receipt.report.revision===task.revision&&receipt.persisted===false&&receipt.external_calls===0,tag+'_revision_receipt_and_no_write');
       check(receipt.report.price_evidence.trust_status==='USER_ASSERTION',tag+'_input_identity_in_receipt');
       check(receipt.report.metrics.reference_pe.value==='23.148148148148148148148148148148148148148148148148',tag+'_whole_company_profit_not_eps');
       await result.locator('details summary').click();
       check((await page.locator('.desk-provider-reference').innerText()).includes('98,765.432100'),tag+'_provider_display_separate');
      }
    }
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),tag+'_no_horizontal_overflow');
    if(profile.width===390)check(!(await page.locator('.desk-mobile-action').isVisible()),tag+'_no_fixed_button_obstructs_form');
   }
  }
  const after=await get('/_v09/identity');
  check(hash(await get('/api/research/tasks'))===beforeTasks,'all_original_tasks_unchanged');
  check(after.fake_model_requests===beforeIdentity.fake_model_requests&&after.news_requests===beforeIdentity.news_requests,'no_model_or_provider_calls');
  check(after.external_attempts===0&&after.paid_model_calls===0,'zero_outbound_and_paid');
  check(errors.length===0,'no_page_errors_or_unapproved_requests');
  check(httpErrors.length===0,'no_http_errors');
  fs.writeFileSync(path.join(output,'dated-valuation-browser.json'),JSON.stringify({status:'PASS',identity:after,
    checks,posts,errors,httpErrors,original_task_hash:beforeTasks},null,2));
  process.stdout.write(JSON.stringify({status:'PASS',checks:checks.length,local_calculation_posts:posts.length,errors:errors.length}));
 }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
