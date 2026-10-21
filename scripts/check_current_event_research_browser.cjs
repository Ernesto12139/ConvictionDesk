'use strict';
// Same-cutoff/portfolio fictional Today -> cost-confirmed Fast/Deep -> source UI.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile]=process.argv.slice(2),url=new URL(base);
if(url.hostname!=='127.0.0.1'||url.port==='8000')throw Error('Isolated synthetic target required');
const {chromium}=require('node:module').createRequire(path.resolve(moduleFile))('playwright');
const checks=[],errors=[],posts=[];let permit=false;
const check=(v,k)=>{if(!v)throw Error(k);checks.push(k);};
const get=async route=>{const r=await fetch(base+route);if(!r.ok)throw Error(route);return r.json();};
const hash=v=>crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex');
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const identity=await get('/_v09/identity');check(identity.scope==='OFFLINE_SYNTHETIC_ONLY'&&identity.source_on_disk_matches,'same_source_fictional_process');
 const old=await get('/api/research/tasks'),frozen=new Map(old.map(t=>[t.task_id,hash(t)]));
 const queue=await get('/api/attention/queue'),selected=queue.items.find(r=>r.item.symbol==='APP').item;
 const eventId=selected.packet.evidence[0].evidence_id;
 const browser=await chromium.launch({headless:true,channel:'chrome'}),results=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:960},reducedMotion:'reduce'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const request=route.request(),u=new URL(request.url());
   if(u.origin!==base){errors.push('foreign');return route.abort();}
   if(request.method()!=='GET'){
    posts.push(u.pathname);
    if(!permit||u.pathname!=='/api/research/tasks'){errors.push('unconfirmed mutation');return route.abort();}
    permit=false;
   }
   return route.continue();
  });
  for(const mode of ['DEEP','FAST']){
   await page.goto(base);await page.locator('#attention-queue .attention-item').first().waitFor();
   const index=queue.items.findIndex(r=>r.item.item_id===selected.item_id);
   await page.locator('#attention-queue .attention-item').nth(index).getByRole('button',{name:'提问核查此事',exact:true}).click();
   check(await page.locator('#desk-attention-context').isVisible(),mode+'_event_association_visible');
   const question=await page.locator('#desk-question').inputValue();
   await page.locator('#desk-model-profile').selectOption(mode==='DEEP'?'sol-deep':'sol-fast');
   const before=posts.length;
   await page.locator('#desk-submit').click();await page.locator('#mia-cost-dialog').waitFor();
   await page.keyboard.press('Escape');await page.locator('#mia-cost-dialog').waitFor({state:'hidden'});
   check(posts.length===before,mode+'_cancel_no_task');
   await page.locator('#desk-submit').click();await page.locator('#mia-cost-dialog').waitFor();
   permit=true;
   const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/research/tasks'&&r.request().method()==='POST');
   await page.locator('[data-cost="confirm"]').click();
   const job=await(await response).json();
   let task;
   for(let attempt=0;attempt<200;attempt++){
    task=await get('/api/research/tasks/'+job.task_id);
    if(task.status!=='RUNNING')break;
    await new Promise(resolve=>setTimeout(resolve,25));
   }
   check(task.status==='COMPLETE',mode+'_complete_'+JSON.stringify({status:task.status,error:task.error}));
   check(task.attention_item_id===selected.item_id,mode+'_exact_attention_id');
   check(task.sources.some(s=>s.evidence.evidence_id===eventId),mode+'_event_receipt_retained');
   check(task.context_manifest.selected.some(s=>s.evidence_id===eventId),mode+'_event_enters_actual_model_context');
   const report=await get('/api/research/tasks/'+task.task_id+'/valuation?revision='+task.revision);
   if(mode==='DEEP'){
    check(report.metrics.ttm_revenue.value==='500','deep_python_ttm_not_model_arithmetic');
    check(task.tool_calls.some(c=>c.name==='valuation.project'&&c.status==='success'),'deep_actual_projection_tool');
    check(task.sources.some(s=>s.evidence.structured_data.calculation==='task_valuation_v1'&&task.context_manifest.selected.some(r=>r.evidence_id===s.evidence.evidence_id)),'deep_cited_financial_context');
    await page.locator('.desk-valuation-list').first().waitFor();
    await page.locator('.desk-valuation-meta').first().locator('summary').click();
    await page.locator('.desk-valuation-meta').first().getByRole('button',{name:'查看计算依据'}).click();
    check(await page.locator('#desk-evidence-dialog').isVisible(),'financial_source_drawer');
    await page.keyboard.press('Escape');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,'today-deep-desktop.png'),fullPage:true});
   }
   results.push({mode,task_id:task.task_id,question,cutoff:task.cutoff,snapshot_hash:task.snapshot_hash,
    action:task.action_decision.decision_action,action_status:task.action_decision.decision_status,
    investigation:task.investigation,judgment_change:task.assessment.judgment_change,
    change_conditions:task.assessment.action_conditions});
  }
  check(results[0].question===results[1].question&&results[0].cutoff===results[1].cutoff&&results[0].snapshot_hash===results[1].snapshot_hash,'paired_same_question_cutoff_portfolio');
  check(results.every(r=>r.action==='ADD'&&r.action_status==='PROVISIONAL'),'same_action_allowed_with_different_evidence');
  const after=await get('/api/research/tasks');
  check(old.every(t=>frozen.get(t.task_id)===hash(after.find(a=>a.task_id===t.task_id))),'all_prior_tasks_unchanged');
  const final=await get('/_v09/identity');check(!errors.length&&final.external_attempts===0&&final.paid_model_calls===0,'no_page_external_paid_errors');
  fs.writeFileSync(path.join(output,'current-event-research.json'),JSON.stringify({status:'PASS',identity,final,checks,posts,event_id:eventId,results,scope:'Frozen fictional sources/scripted model, not real-model quality'},null,2));
  process.stdout.write(JSON.stringify({status:'PASS',checks:checks.length,confirmed_posts:posts.length}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
