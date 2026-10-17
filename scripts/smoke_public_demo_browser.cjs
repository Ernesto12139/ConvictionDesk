'use strict';
// v0.8.1: actual v2 replay, every saved record and complete reading.
const {spawnSync}=require('node:child_process'),path=require('node:path');
const result=spawnSync(process.execPath,[path.join(__dirname,'check_saved_research_browser.cjs'),
 ...process.argv.slice(2),'--quiet','--public-replay','--screenshots'],{stdio:'inherit'});
if(result.error)throw result.error;
process.exitCode=result.status??1;
