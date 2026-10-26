'use strict';
// Real saved-record interactions. GET only; never submit a research/scan request.
// Owner mode requires an explicit flag and never captures private screenshots/text.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [base,output,moduleFile,...flags]=process.argv.slice(2),target=new URL(base);
if(target.hostname!=='127.0.0.1'||(target.port==='8000'&&!flags.includes('--owner-readonly')))throw Error('Explicit local read-only target required');
const {createRequire}=require('node:module'),{chromium}=createRequire(path.resolve(moduleFile))('playwright');
const checks=[],errors=[],mutations=[],failures=[];
const check=(value,label)=>{if(!value)throw Error(label);checks.push(label);};
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const get=async p=>{const r=await fetch(base+p);check(r.ok,'saved_api_read');return r.json();};
 const list=await get('/api/research/tasks');
 const chosen=list.filter(t=>t.status==='COMPLETE').slice(0,3);
 check(chosen.length===3,'three_saved_complete_records_exist');
 const tasks=await Promise.all(chosen.map(t=>get('/api/research/tasks/'+encodeURIComponent(t.task_id))));
 const before=tasks.map(t=>hash(JSON.stringify(t)));
 const browser=await chromium.launch({headless:true,...(process.env.MIA_BROWSER_CHANNEL==='chromium'?{}:{channel:process.env.MIA_BROWSER_CHANNEL||'chrome'})});
 try{
  const page=await browser.newPage({reducedMotion:'reduce',timezoneId:'UTC'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());
   if(r.method()!=='GET'){mutations.push(r.method()+' '+u.pathname);return route.abort();}
   if(u.origin!==base)return route.abort();
   // This GET may fetch fresh prices on the server. It is outside saved-report QA.
   if(u.pathname==='/api/console/portfolio-overview')return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"saved-record QA: live prices intentionally excluded"}'});
   return route.continue();
  });
  for(const width of [1440,390,375])for(const theme of ['light','dark']){
   await page.setViewportSize({width,height:900});await page.goto(base);
   await page.locator('#mia-theme').selectOption(theme);
   if(flags.includes('--public-replay')){
    await page.locator('#attention-queue .attention-item').first().waitFor();
    check(await page.locator('#public-demo-notice').isVisible(),`synthetic_identity_${width}_${theme}`);
    for(const mode of ['Fast','Deep']){
     await page.locator('#attention-queue button').filter({hasText:'· '+mode}).first().click();
     await page.locator('.desk-core').waitFor();
     check(await page.locator('#desk-observation').isHidden(),`today_to_${mode}_complete_${width}_${theme}`);
     if(mode==='Deep'){
      check(await page.locator('.desk-information-gain').isVisible(),`deep_actual_gain_explained_${width}_${theme}`);
      check(await page.locator('.desk-financial-scroll table').count()===1,`deep_quarters_rendered_${width}_${theme}`);
     }
     await page.locator('#desk-home').click();await page.locator('.rail nav a[href="#attention"]:visible').click();
    }
   }
   await page.locator('.rail nav a[href="#research-history"]:visible').click();
   await page.locator('#desk-recent .desk-recent-task').first().waitFor();
   for(const [i,task]of tasks.entries()){
    const prefix=`${width}_${theme}_record${i+1}`;
    const index=list.findIndex(t=>t.task_id===task.task_id);
    await page.locator('#desk-recent .desk-recent-task').nth(index).click();
    await page.locator('.desk-core').waitFor();
    check(await page.locator('#desk-observation').isHidden(),prefix+'_no_false_read_failure');
    // D-083: with a saved Action the conclusion card shows the direction; the
    // original core judgment is kept once, in the full report (one click).
    if(!task.action_decision)check((await page.locator('.desk-core').innerText()).includes(task.assessment.core_judgment.text),prefix+'_complete_core_judgment');
    if(task.action_decision){
     const a=task.action_decision,card=await page.locator('.desk-core').innerText();
     const head=['ADD','HOLD','REDUCE','EXIT'].includes(a.decision_action)?a.decision_action+' · ':a.decision_status==='NOT_APPLICABLE'?'该操作不适用':'方向未形成 · 证据不足';
     const note=a.applicability_note||({SUPPORTED:'证据支持的研究方向',PROVISIONAL:'暂定 · PROVISIONAL'}[a.decision_status]||'尚无适用且有依据的操作方向');
     check(await page.locator('.desk-core').isVisible()&&card.includes(head)&&card.includes(note),prefix+'_conclusion_shows_action_direction_and_status');
    }
    const brief=page.locator('#desk-result .desk-decision-communication');
    check(await brief.count()===1&&await brief.isVisible(),prefix+'_decision_brief_rendered');
    const briefText=await brief.innerText();
    for(const field of ['key_reasons','counterevidence','action_conditions']){
     const statement=task.assessment[field][0];
     if(statement)check(briefText.includes(statement.text),prefix+'_saved_'+field+'_visible');
    }
    const full=page.locator('.desk-report-details');await full.locator('summary').click();
    check(await full.evaluate(e=>e.open),prefix+'_full_report_expanded');
    const text=await full.innerText();
    check(text.includes(task.assessment.core_judgment.text),prefix+'_complete_core_judgment_in_report');
    for(const field of ['key_reasons','support','counterevidence','action_conditions','next_checks']){
     check(task.assessment[field].every(s=>text.includes(s.text)),prefix+'_all_'+field+'_visible');
    }
    // Wide (>760px, quiet.css): full text in the side panel. Narrow: the first
    // sentence sits under the conclusion. innerText of a display:none node still
    // returns its text, so the branch for this width must also be visible.
    const nextText=task.assessment.next_checks[0].text,nextFirst=(nextText.match(/^.*?[。！？](?:[”」])?/s)||[nextText])[0];
    const nextShown=width>760?page.locator('#desk-next-action .desk-next-check'):page.locator('#desk-result .desk-core-next');
    check(await nextShown.count()===1&&await nextShown.isVisible()&&(await nextShown.innerText()).includes(width>760?nextText:nextFirst),prefix+'_next_check_visible');
    const cite=full.locator('.desk-citation').first();await cite.click();
    check(await page.locator('#desk-evidence-dialog').evaluate(e=>e.open),prefix+'_citation_opens_evidence');
    check(await page.locator('#desk-source-list > details[open]').count()>0,prefix+'_cited_receipt_expanded');
    await page.locator('#desk-back-to-reading').click();
    check(await cite.evaluate(e=>e===document.activeElement),prefix+'_citation_focus_restored');
    await page.locator('#desk-evidence-open').click();
    check(await page.locator('#desk-source-list > details').count()===task.sources.length,prefix+'_all_saved_sources_available');
    await page.locator('#desk-back-to-reading').click();
    if(flags.includes('--quiet')){
     check(await page.locator('#desk-result .desk-depth').count()===0,prefix+'_diagnostics_absent_from_memo');
     check(!(await page.locator('#desk-execution').evaluate(e=>e.open)),prefix+'_diagnostics_collapsed');
     await page.locator('#desk-execution > summary').click();
     check(await page.locator('#desk-trace .desk-depth').isVisible(),prefix+'_diagnostics_available_on_demand');
     await page.locator('#desk-execution > summary').click();
    }
    check(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)),prefix+'_no_horizontal_overflow');
    if(flags.includes('--screenshots')&&target.port!=='8000')await page.screenshot({path:path.join(output,prefix+'.png'),fullPage:true});
    await page.locator('#desk-home').click();
    await page.locator('.rail nav a[href="#research-history"]:visible').click();
    await page.locator('#desk-recent .desk-recent-task').first().waitFor();
   }
  }
  const after=await Promise.all(chosen.map(t=>get('/api/research/tasks/'+encodeURIComponent(t.task_id))));
  check(after.every((t,i)=>hash(JSON.stringify(t))===before[i]),'saved_records_unchanged');
  check(mutations.length===0,'zero_mutating_requests');check(errors.length===0,'zero_page_errors');
 }finally{await browser.close();}
 const result={status:'PASS',checks,errors,mutations,failures,task_hashes:before,utc:new Date().toISOString(),
  source_js_sha256:hash(fs.readFileSync('src/market_agent/static/research-desk.js')),live_price_requests:'EXCLUDED',screenshots:target.port==='8000'?'NONE_PRIVATE_DATA':flags.includes('--screenshots')};
 fs.writeFileSync(path.join(output,'saved-research-browser.json'),JSON.stringify(result,null,2)+'\n');
 console.log(JSON.stringify({status:'PASS',checks:checks.length,records:3,widths:[1440,390,375],themes:['light','dark'],mutations:0}));
})().catch(e=>{fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'saved-research-browser.json'),JSON.stringify({status:'FAIL',failure:e.message,checks,errors,mutations},null,2));console.error(e.message);process.exitCode=1;});
