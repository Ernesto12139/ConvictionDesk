'use strict';
// Fictional same-task user journey. Explicit scripted research/local arithmetic only.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile]=process.argv.slice(2),origin=new URL(base);
if(origin.hostname!=='127.0.0.1'||origin.port==='8000')throw Error('Synthetic isolated service only');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],errors=[],posts=[],journeys=[];let permit=null,failNextCalculation=false;
const check=(v,k)=>{if(!v)throw Error(k);checks.push(k);};
const get=async p=>{const r=await fetch(base+p);if(!r.ok)throw Error(p);return r.json();};
const hash=v=>crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex');
const wait=async id=>{for(let i=0;i<200;i++){const t=await get('/api/research/tasks/'+id);if(t.status!=='RUNNING')return t;await new Promise(r=>setTimeout(r,25));}throw Error('Scripted task timeout');};
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const identity=await get('/_v09/identity');
 check(identity.scope==='OFFLINE_SYNTHETIC_ONLY'&&identity.source_on_disk_matches,'fixed_synthetic_source');
 const before=await get('/api/research/tasks'),frozen=new Map(before.map(t=>[t.task_id,hash(t)]));
 const queue=await get('/api/attention/queue'),index=queue.items.findIndex(r=>r.item.symbol==='APP');
 const event=queue.items[index].item,originals=event.packet.evidence.map(e=>e.evidence_id);
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({reducedMotion:'reduce'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());
   if(u.origin!==origin.origin){errors.push('foreign browser request');return route.abort();}
   if(r.method()!=='GET'){
    if(r.method()!=='POST'||u.pathname!==permit){errors.push('unconfirmed mutation '+u.pathname);return route.abort();}
    permit=null;posts.push({path:u.pathname,body:JSON.parse(r.postData())});
    if(failNextCalculation&&u.pathname.endsWith('/dated-valuation')){
     failNextCalculation=false;return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"synthetic calculation read failure"}'});
    }
   }
   return route.continue();
  });
  for(const [width,theme]of [[1440,'light'],[390,'dark']]){
   const tag=width+'-'+theme;
   await page.setViewportSize({width,height:960});await page.goto(base);
   await page.locator('#mia-theme').selectOption(theme);
   await page.locator('#attention-queue .attention-item').nth(index).getByRole('button',{name:'提问核查此事',exact:true}).click();
   await page.locator('#desk-model-profile').selectOption('sol-fast');
   const count=posts.length;
   for(const key of ['Escape','Enter']){
    await page.locator('#desk-submit').click();await page.locator('#mia-cost-dialog').waitFor();
    await page.keyboard.press(key);await page.locator('#mia-cost-dialog').waitFor({state:'hidden'});
    check(posts.length===count,tag+'_new_'+key+'_no_post');
   }
   const started=performance.now();
   await page.locator('#desk-submit').click();await page.locator('#mia-cost-dialog').waitFor();
   permit='/api/research/tasks';
   const created=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/research/tasks'&&r.request().method()==='POST');
   await page.locator('[data-cost="confirm"]').click();
   const first=await(await created).json(),fast=await wait(first.task_id);
   const firstUsefulSeconds=(performance.now()-started)/1000;
   check(fast.status==='COMPLETE'&&fast.mode==='FAST'&&fast.action_decision.decision_action==='HOLD',tag+'_fast_waits_for_cash_condition');
   check(originals.every(id=>fast.context_manifest.selected.some(s=>s.evidence_id===id)),tag+'_fast_all_six_originals_in_context');
   check(fast.context_manifest.selected.some(s=>s.identity==='OWNER_CONFIRMED_THESIS'),tag+'_confirmed_thesis_in_fast_context');
   await page.locator('.desk-core').waitFor();
   await page.locator('#desk-followup-model-profile').selectOption('sol-deep');
   await page.locator('#desk-followup').fill('APP 深入核查客户验收和回款条件。');
   const followCount=posts.length;
   await page.locator('#desk-continue').click();await page.locator('#mia-cost-dialog').waitFor();
   check((await page.locator('#mia-cost-dialog').innerText()).includes('深入研究'),tag+'_deep_choice_disclosed_before_fee_confirmation');
   await page.keyboard.press('Escape');await page.locator('#mia-cost-dialog').waitFor({state:'hidden'});
   check(posts.length===followCount,tag+'_deep_cancel_keeps_revision');
   await page.locator('#desk-continue').click();await page.locator('#mia-cost-dialog').waitFor();
   permit='/api/research/tasks';
   const continued=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/research/tasks'&&r.request().method()==='POST');
   await page.locator('[data-cost="confirm"]').click();await continued;
   const deep=await wait(first.task_id);
   check(deep.status==='COMPLETE'&&(deep.mode || 'DEEP')==='DEEP'&&deep.revision===2,tag+'_same_task_deep_revision');
   check(deep.action_decision.decision_action==='ADD'&&deep.assessment.judgment_change==='CHANGED',tag+'_cash_evidence_changes_scripted_direction');
   check(deep.snapshot_hash===fast.snapshot_hash&&deep.cutoff===fast.cutoff,tag+'_same_frozen_context_pair');
   check(originals.every(id=>deep.context_manifest.selected.some(s=>s.evidence_id===id)),tag+'_deep_all_six_originals');
   const old=await get('/api/research/tasks/'+first.task_id+'/revisions/1');
   check(hash(old)===hash(fast),tag+'_fast_record_immutable');
   await page.goto(base+'/?research_task='+deep.task_id);
   await page.locator('.desk-dated-price summary').first().click();
   const form=page.locator('.desk-dated-form'),result=page.locator('.desk-dated-result');
   await form.locator('input[type=text]').nth(0).fill('10');
   await form.locator('input[type=date]').fill('2026-10-16');
   await form.locator('input[type=text]').nth(1).fill('Self-defined fictional price pair, not Provider quotes');
   await form.locator('input[type=text]').nth(2).fill('12.50');
   for(let i=0;i<4;i++)await form.locator('input[type=checkbox]').nth(i).check();
   const beforeCalls=(await get('/_v09/identity')).fake_model_requests;
   if(width===1440){
    permit='/api/research/tasks/'+deep.task_id+'/dated-valuation';failNextCalculation=true;
    await form.locator('button[type=submit]').click();
    await result.getByText('本次计算未完成。',{exact:false}).waitFor();
    check((await get('/_v09/identity')).fake_model_requests===beforeCalls,tag+'_calculation_failure_has_no_model_retry');
   }
   permit='/api/research/tasks/'+deep.task_id+'/dated-valuation';
   await form.locator('button[type=submit]').click();await result.locator('.desk-price-difference').waitFor();
   const text=await result.innerText();
   check(text.includes('-250.00 USD')&&text.includes('-20.00%'),tag+'_exact_price_sensitivity');
   check(text.includes('不是实际市场变化')&&text.includes('未发送给 Deep'),tag+'_scenario_actual_isolation');
   await result.locator('.desk-price-action-link > summary').click();
   check((await result.locator('.desk-price-action-link').innerText()).includes(deep.assessment.action_conditions[0].text),tag+'_original_change_condition_findable');
   check((await get('/_v09/identity')).fake_model_requests===beforeCalls,tag+'_zero_models_for_scenario');
   check(hash(await get('/api/research/tasks/'+deep.task_id))===hash(deep),tag+'_price_does_not_rewrite_action');
   const calculationReceipt=JSON.parse(await result.locator('pre').last().textContent());
   await result.screenshot({path:path.join(output,tag+'-price-difference.png')});
   await form.locator('input[type=text]').nth(2).fill('15');
   check((await result.innerText()).includes('输入已改变'),tag+'_changed_form_invalidates_old_receipt');
   await page.locator('#desk-history-open').click();
   await page.locator('#desk-history-select').selectOption('1');
   await page.waitForFunction(()=>document.querySelector('.desk-core h3')?.textContent.includes('HOLD'));
   check(await page.locator('#desk-continue').isDisabled(),tag+'_history_readonly');
   await page.locator('#desk-evidence-open').click();
   check(originals.every(id=>fast.sources.some(s=>s.evidence.evidence_id===id)),tag+'_history_sources_retained');
   check(await page.locator('#desk-source-list > details').count()===fast.sources.length,tag+'_every_saved_source_visible');
   await page.locator('#desk-back-to-reading').click();
   check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),tag+'_no_overflow');
   await page.screenshot({path:path.join(output,tag+'-fast-history.png'),fullPage:true});
   journeys.push({task_id:deep.task_id,fast_revision:1,deep_revision:2,
    time_to_first_useful_answer_seconds:firstUsefulSeconds,
    calculation_receipt:calculationReceipt,
    scripted_browser_seconds:(performance.now()-started)/1000,fast_action:'HOLD',deep_action:'ADD',
    source_ids:originals,fast_context_bytes:fast.context_manifest.context_bytes,deep_context_bytes:deep.context_manifest.context_bytes,
    new_evidence_ids:deep.investigation.new_evidence_ids});
  }
  const after=await get('/api/research/tasks');
  check(before.every(t=>frozen.get(t.task_id)===hash(after.find(r=>r.task_id===t.task_id))),'all_preexisting_records_unchanged');
  const final=await get('/_v09/identity');
  check(final.external_attempts===0&&final.paid_model_calls===0,'zero_outbound_and_paid_calls');
  check(errors.length===0,'no_page_or_request_errors');
  fs.writeFileSync(path.join(output,'closure-browser.json'),JSON.stringify({status:'PASS',identity,final,checks,errors,posts,journeys,
   real_model_quality:'REAL_MODEL_QUALITY_NOT_TESTED'},null,2));
  console.log(JSON.stringify({status:'PASS',checks:checks.length,explicit_scripted_posts:posts.length}));
 }finally{await browser.close();}
})().catch(e=>{fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({error:String(e),checks,errors,posts},null,2));console.error(e);process.exitCode=1;});
