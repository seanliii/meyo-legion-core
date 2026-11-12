import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// Offline execution of the actual send/replay/discovery functions. No browser,
// live account, server or model. Geometry/rendering has its separate regression.
const html=readFileSync(process.env.LEGION_UI_FILE||new URL('../web/index_v2.html',import.meta.url),'utf8');
function pageFunction(name){
  const start=html.search(new RegExp(`^(?:async )?function ${name}\\(`,'m'));
  if(start<0)return '';
  for(let end=html.indexOf('}',start);end>=0;end=html.indexOf('}',end+1)){
    const code=html.slice(start,end+1);
    try{new vm.Script(`(${code})`);return code;}catch{}
  }
  throw new Error(name);
}
const flush=()=>new Promise(setImmediate);
function node(){return {style:{},dataset:{},children:[],textContent:'',disabled:false,isConnected:true,
  scrollTop:87,scrollHeight:1200,clientHeight:300,addEventListener(){},
  classList:{contains:()=>false,add(){},remove(){},toggle(){}},
  appendChild(child){this.children.push(child);},prepend(child){this.children.unshift(child);},setAttribute(){},
  querySelector(selector){return this.children.find(child=>'.'+child.className===selector)||null;}};}
function fixture(){
  const cards=[],requests=[],timers=new Map(),storage=new Map(),sendButton=node(),q={value:'continue project'};
  let reader,resolveRead,rejectRead,nextTimer=0,poll=()=>({team_id:'team-A',events:[],last_event_id:1});
  const context=vm.createContext({
    document:{title:'original',createElement:node},TextDecoder,AbortSignal,
    _personalSubject:'owner',_personalEpoch:1,_personalSwitchPending:false,clientUid:'owner',
    _readPersonalStatus:async()=>'owner',_sessionView:0,_origTitle:'original',
    running:false,liveTeamId:'',curSession:'session-A',lastEventId:0,_liveMode:'auto',
    _teamReplay:new Map(),_resumeCtx:null,_resumeTimer:null,_renderedTurns:0,
    _initialSourceUrl:'',querySourceConsumed:false,_squad:[],_SQUAD_AGENTS:[],uploads:[],qEl:q,
    setTimeout(fn,delay){const id=++nextTimer;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    sessionStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k),removeItem:k=>storage.delete(k)},
    $:s=>s==='#send'?sendButton:s==='#modeSelect'?{value:'auto'}:null,
    newLegionMsg(){const parts=new Map(['.progress','.pt','.st','.proc-detail','.legion-answers'].map(s=>[s,node()]));
      const card={...node(),_pkMode:false,_pkLanes:{},querySelector:s=>parts.get(s),
        querySelectorAll:()=>[],previousElementSibling:null};cards.push(card);return card;},
    _loadMemberDeliveries(card){card.memberRead=true;},
    _captureProgressScroll:card=>({top:card.querySelector('.proc-detail').scrollTop}),
    _restoreProgressScroll:(card,s)=>{card.querySelector('.proc-detail').scrollTop=s.top;},
    _syncProcessProgress:(card,steps)=>{card._replaySteps=steps;},
    _renderToolStep:()=>true,_renderProductMilestone:()=>false,_renderRuntimePulse:()=>false,
    nameOf:x=>x,esc:x=>String(x),_stepPurpose:()=>'',
    _finalDeliveryData:x=>x,_turnSignature:x=>JSON.stringify(x),
    renderLegionAnswer:(rows,d,card)=>{card.final=d;card._memberDeliveryFinal=true;},
    notifyDone(){},currentQuerySource:()=>({}),loadSessions:async()=>{},
    appendSessionTurns(){},
    ...Object.fromEntries(['addUserMsg','autosize','_savePersonalDraft','_renderModeFacts',
      '_memberDeliverySignal','consumeQuerySource','applyModeReason','toast'].map(n=>[n,()=>{}])),
    fetch:async(url,options={})=>{
      requests.push({url,options});
      if(url==='/api/legion/dispatch'){
        reader={read:()=>new Promise((resolve,reject)=>{resolveRead=resolve;rejectRead=reject;}),cancel:async()=>{}};
        return {ok:true,body:{getReader:()=>reader}};
      }
      if(url.startsWith('/api/legion/events?')){
        const data=await poll(url);return {ok:true,json:async()=>data};
      }
      if(url.startsWith('/api/legion/session/teams?'))return {json:async()=>({sid:'session-A',teams:[{team_id:'team-A',active:true}]})};
      if(url==='/api/legion/cancel')return {json:async()=>({ok:true})};
      throw new Error('unexpected HTTP path');
    },
  });
  const functions=['_replayEntry','_bindReplayCard','_canAttachTeam','saveLive','_markReplayTerminal',
    'applyLiveEvent','_renderTerminalError','_finishResume','_handoffLiveStream','resumeLiveV2','attachExternalLive',
    '_syncSessionOnce','cancelLegion','send'];
  vm.runInContext(functions.map(pageFunction).join('\n'),context);
  return {context,cards,requests,timers,storage,sendButton,q,
    setPoll(fn){poll=fn;},
    async start(){const promise=context.send();await flush();return {promise};},
    async event(event,data,id){assert.ok(resolveRead);const resolve=resolveRead;resolveRead=null;
      resolve({done:false,value:new TextEncoder().encode(`id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`)});await flush();},
    async close(throwing=false){if(throwing)rejectRead(new Error('stream disconnected'));else resolveRead({done:true});await flush();},
    async tick(){const [id,timer]=timers.entries().next().value||[];assert.ok(timer);timers.delete(id);await timer.fn();await flush();},
  };
}
async function accepted(ui){const run=await ui.start();await ui.event('meta',{team_id:'team-A',sid:'session-A'},1);return run;}
function assertWaiting(ui){assert.equal(ui.context.running,true);assert.equal(ui.sendButton.disabled,true);
  assert.match(ui.context.document.title,/⏳/);assert.equal(ui.cards[0].querySelector('.progress').style.display,'flex');}

for(const throwing of [false,true])test(`accepted stream ${throwing?'disconnect':'EOF'} retains stop/send lock and replays same card`,async()=>{
  const ui=fixture();const run=await accepted(ui);
  await ui.event('step',{t:'tool_started',agent:'codex'},2);
  const card=ui.cards[0],proc=card.querySelector('.proc-detail');proc.scrollTop=87;
  ui.setPoll(url=>{assert.match(url,/after=2$/);return {team_id:'team-A',events:[],last_event_id:2};});
  await ui.close(throwing);await run.promise;await flush();assertWaiting(ui);
  assert.equal(ui.context._resumeCtx.msgEl,card);assert.equal(ui.context._resumeCtx.entry.cursor,2);
  assert.equal(ui.cards.length,1);assert.equal(proc.scrollTop,87);
  assert.ok(ui.storage.has('legion_live_v2'));
  ui.q.value='do not dispatch twice';await ui.context.send();
  assert.equal(ui.requests.filter(r=>r.url==='/api/legion/dispatch').length,1);
  await ui.context.cancelLegion(node());
  assert.equal(JSON.parse(ui.requests.find(r=>r.url==='/api/legion/cancel').options.body).team_id,'team-A');
  assertWaiting(ui); // A cancel acknowledgement is not the native terminal.
});

test('poll failure/empty poll cannot finish; real terminal unlocks exactly once',async()=>{
  const ui=fixture();const run=await accepted(ui);
  ui.setPoll(()=>{throw new Error('offline');});await ui.close();await run.promise;await flush();assertWaiting(ui);
  ui.setPoll(()=>({team_id:'team-A',events:[],last_event_id:1}));await ui.tick();assertWaiting(ui);
  ui.setPoll(()=>({team_id:'team-A',events:[{event:'final',event_id:2,data:{results:[],cancelled:true}}],last_event_id:2}));
  await ui.tick();assert.equal(ui.context.running,false);assert.equal(ui.sendButton.disabled,false);
  assert.equal(ui.context._resumeCtx,null);assert.equal(ui.cards[0].querySelector('.progress').style.display,'none');
  assert.equal(ui.storage.has('legion_live_v2'),false);assert.equal(ui.timers.size,0);
});

test('wrong-team terminal does not unlock or consume the original cursor',async()=>{
  const ui=fixture();const run=await accepted(ui);
  ui.setPoll(()=>({team_id:'other-team',events:[{event:'final',event_id:100,data:{}}]}));
  await ui.close();await run.promise;await flush();assertWaiting(ui);
  assert.equal(ui.cards[0]._replay.cursor,1);assert.equal(ui.cards[0]._replay.complete,false);
});

test('focus during a live POST must not reload session or replace its card',async()=>{
  const ui=fixture();const run=await accepted(ui);let opens=0;
  ui.context.openSession=async()=>{opens++;};await ui.context.resumeLiveV2();
  assert.equal(opens,0);assert.equal(ui.cards.length,1);
  await ui.event('final',{results:[]},2);await ui.close();await run.promise;
  assert.equal(ui.context.running,false);
});

test('saved running session discovery attaches without a new dispatch',async()=>{
  const ui=fixture();await ui.context._syncSessionOnce({turns:[{team_id:'team-A',status:'running',results:[]}]});
  await flush();assertWaiting(ui);assert.equal(ui.context._resumeCtx.teamId,'team-A');
  assert.equal(ui.requests.filter(r=>r.options.method==='POST').length,0);
});

test('late poll response cannot finish another session view',async()=>{
  const ui=fixture();const run=await accepted(ui);let complete;
  ui.setPoll(()=>new Promise(resolve=>{complete=resolve;}));await ui.close();await run.promise;await flush();
  const old=ui.context._resumeCtx;assert.ok(old);
  ui.context._sessionView++;ui.context.curSession='session-B';ui.context._finishResume(old);
  ui.context.running=true;ui.sendButton.disabled=true;ui.context.document.title='⏳ new task';
  complete({team_id:'team-A',events:[{event:'final',event_id:2,data:{}}]});await flush();
  assert.equal(ui.context.running,true);assert.equal(ui.sendButton.disabled,true);
  assert.equal(ui.context.document.title,'⏳ new task');assert.equal(ui.cards[0].final,undefined);
});

test('no acceptance metadata stays uncertain and blocks an automatic duplicate POST',async()=>{
  const ui=fixture(),run=await ui.start();await ui.close();await run.promise;
  assertWaiting(ui);assert.equal(ui.context._resumeCtx,null);
  assert.match(ui.cards[0].querySelector('.st').textContent,/尚未确认任务是否已接收/);
  ui.q.value='same request';await ui.context.send();
  assert.equal(ui.requests.filter(r=>r.url==='/api/legion/dispatch').length,1);
});

test('explicit HTTP rejection is distinct from an accepted disconnected task',async()=>{
  const ui=fixture();ui.context.fetch=async()=>({ok:false,status:403});
  await ui.context.send();assert.equal(ui.context.running,false);assert.equal(ui.sendButton.disabled,false);
  assert.equal(ui.cards[0].querySelector('.progress').style.display,'none');
  assert.match(ui.cards[0].querySelector('.st').textContent,/HTTP 403/);
  assert.equal(ui.context._resumeCtx,null);
});

for(const status of [408,499,502,504])test(`HTTP ${status} cannot prove the backend rejected dispatch`,async()=>{
  const ui=fixture();ui.context.fetch=async()=>({ok:false,status});
  await ui.context.send();assertWaiting(ui);
  assert.match(ui.cards[0].querySelector('.st').textContent,/尚未确认任务是否已接收/);
});

test('an accepted source-link task retains its own session on stream handoff',async()=>{
  const ui=fixture(),run=await accepted(ui);ui.context._initialSourceUrl='https://example.invalid/reference';
  await ui.close();await run.promise;await flush();assertWaiting(ui);
  assert.equal(ui.context.curSession,'session-A');assert.equal(ui.context.liveTeamId,'team-A');
  assert.ok(ui.requests.some(r=>r.url.startsWith('/api/legion/events?')));
});

test('focus replaces the pending timer; duplicate replay events do not repeat progress',async()=>{
  const ui=fixture(),run=await accepted(ui);
  await ui.event('step',{t:'tool_started',agent:'codex'},2);
  ui.setPoll(()=>({team_id:'team-A',events:[
    {event:'step',event_id:2,data:{t:'tool_started',agent:'codex'}},
    {event:'step',event_id:3,data:{t:'tool_end',agent:'codex'}}]}));
  await ui.close();await run.promise;await flush();assertWaiting(ui);
  assert.equal(ui.cards[0]._replaySteps,2);assert.equal(ui.timers.size,1);
  await ui.context.resumeLiveV2();assert.equal(ui.timers.size,1);
  assert.equal(ui.cards[0]._replaySteps,2);
  assert.equal(ui.cards[0].querySelector('.proc-detail').scrollTop,87);
});

test('real terminal error releases polling controls without manufacturing a final result',async()=>{
  const ui=fixture(),run=await accepted(ui);
  ui.setPoll(()=>({team_id:'team-A',events:[{event:'error',event_id:2,data:{error:'fixture terminal'}}]}));
  await ui.close();await run.promise;await flush();
  assert.equal(ui.context.running,false);assert.equal(ui.context._resumeCtx,null);
  assert.equal(ui.sendButton.disabled,false);assert.equal(ui.cards[0].final,undefined);
  assert.match(ui.cards[0].querySelector('.st').textContent,/fixture terminal/);
});

// Optional private evidence: replay only lifecycle fields, never copy source
// content, credentials, media paths or reasoning into this public regression.
if(process.env.LEGION_REPLAY_EVENTS&&process.env.LEGION_REPLAY_SESSION)test('saved running event lifecycle survives EOF without a terminal',async()=>{
  const saved=JSON.parse(readFileSync(process.env.LEGION_REPLAY_EVENTS,'utf8'));
  const session=JSON.parse(readFileSync(process.env.LEGION_REPLAY_SESSION,'utf8'));
  const turn=session.turns.find(t=>(t.result?.team_id||t.team_id)===saved.team_id);
  assert.equal(turn.status,'running');
  assert.ok(saved.events.length>1);assert.ok(saved.events.every(e=>['meta','step'].includes(e.event)));
  const ui=fixture(),run=await ui.start();
  for(const event of saved.events){
    const data=event.event==='meta'?{team_id:'team-A',sid:'session-A'}:
      {t:event.data.t,agent:'codex',status:event.data.status};
    await ui.event(event.event,data,event.event_id);
  }
  await ui.close();await run.promise;await flush();assertWaiting(ui);
  assert.equal(ui.context._resumeCtx.entry.cursor,saved.last_event_id);
  assert.equal(ui.cards.length,1);
  assert.equal(ui.requests.filter(r=>r.url==='/api/legion/dispatch').length,1);
});
