"""Read-only presentation/isolated-preview tests; no model, real home or cloud."""
from pathlib import Path
import ast
import io
import hashlib
import json
import os
import re
import subprocess
from urllib.parse import parse_qs, urlparse, quote

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for name, sub in (("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
                      ("LEGION_SESSIONS_DIR", "legion/sessions"), ("HOME", "home"),
                      ("CODEX_HOME", "home/.codex")):
        folder = tmp_path / sub
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(folder))
    monkeypatch.syspath_prepend(str(SERVICE))


def test_template_is_source_not_a_web_runtime():
    import artifact_delivery as artifacts
    assert artifacts.is_html_template("project/src/index.template.html")
    assert artifacts._kind("project/src/index.template.html") == "code"
    assert not artifacts.is_html_template("project/dist/index.html")


def test_preview_is_additive_and_original_download_bytes_are_not_modified():
    import artifact_delivery as artifacts
    raw = b"<!doctype html><html><head><title>Example</title></head><body>Original</body></html>"
    prepared = artifacts.prepare_html_preview(raw)
    assert prepared.startswith(b"<!doctype html>")
    assert b"data-meyo-preview-storage" in prepared
    assert b"<body>Original</body>" in prepared
    assert raw == b"<!doctype html><html><head><title>Example</title></head><body>Original</body></html>"
    assert artifacts.prepare_html_preview(prepared) == prepared
    utf16 = "<!doctype html><html><head></head><body>原文件</body></html>".encode("utf-16")
    assert artifacts.prepare_html_preview(utf16) == utf16


def test_isolated_storage_fallback_is_per_document_and_never_reads_parent_state():
    import artifact_delivery as artifacts
    prepared = artifacts.prepare_html_preview(b"<!doctype html><html><head></head><body></body></html>")
    source = re.search(rb"<script data-meyo-preview-storage>(.*?)</script>", prepared, re.S)[1].decode()
    harness = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(process.argv[1]);
function isolated() {
  // Keep Window separate from vm's contextified global; vm globals can mask getter exceptions.
  const sandbox={window:{}};
  for(const name of ['localStorage','sessionStorage'])
    Object.defineProperty(sandbox.window,name,{get(){const e=new Error('opaque');e.name='SecurityError';throw e;},configurable:true});
  Object.defineProperty(sandbox.window,'parent',{get(){throw Error('must never use parent storage')}});
  vm.runInNewContext(source,sandbox);
  return sandbox.window;
}
const a=isolated(),b=isolated();
a.localStorage.setItem('x','1');
assert.equal(a.localStorage.getItem('x'),'1');
assert.equal(b.localStorage.getItem('x'),null);
assert.equal(a.sessionStorage.getItem('x'),null);
assert.equal(a.localStorage.length,1);
a.localStorage.removeItem('x');assert.equal(a.localStorage.length,0);
const allowed={window:{localStorage:{marker:'retained'},sessionStorage:{marker:'retained'}}};
vm.runInNewContext(source,allowed);
assert.equal(allowed.window.localStorage.marker,'retained');
console.log('isolated storage passed');
"""
    result = subprocess.run(["node", "-e", harness, json.dumps(source)],
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def _run_preview_observer(checks, *, module="export const loaded = true;", parent_origin=""):
    """Execute the actual injected scripts; only Window's event/network boundary is fake."""
    import artifact_delivery as artifacts
    raw = ('<!doctype html><html><head><script type="module">'
           + module + '</script></head><body>Original</body></html>').encode()
    prepared = artifacts.prepare_html_preview(
        raw, preview_nonce="__ut_preview_nonce", preview_parent_origin=parent_origin)
    harness = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const html=JSON.parse(process.argv[1]),checks=JSON.parse(process.argv[2]);
const {getEventListeners}=require('node:events');
const root='/artifact-preview/__ut_cap/home-outputs/exports/'+
  'a'.repeat(64)+'/'+'b'.repeat(64)+'/';
const pageURL='https://meyo.test'+root+'nested/index.html';
async function load(options={}){
  const messages=[],listeners=new Map(),documentListeners=new Map(),revoked=[];
  let nativeClicks=0,serial=0;
  class PreviewURL extends URL {
    static createObjectURL(value){
      if(!(value instanceof Blob))throw new TypeError('not a Blob');
      return 'blob:null/__ut_'+(++serial);
    }
    static revokeObjectURL(value){revoked.push(value);}
  }
  const dispatchDocument=(type,event)=>{for(const listener of documentListeners.get(type)||[])listener(event);};
  // Real cancellable Event/EventTarget; only DOM propagation/default navigation is modeled.
  class HTMLAnchorElement extends EventTarget {
    constructor(){super();this.href='';this._download=null;this.connected=false;}
    get download(){return this._download??'';}
    set download(value){this._download=String(value);}
    get onclick(){return this._onclick??null;}
    set onclick(handler){
      this._onclick=handler;
      if(!this._onclickListener){
        this._onclickListener=event=>{
          if(typeof this._onclick==='function'&&this._onclick.call(this,event)===false)event.preventDefault();
        };
        this.addEventListener('click',this._onclickListener);
      }
    }
    hasAttribute(name){return name==='download'&&this._download!==null;}
    removeAttribute(name){if(name==='download')this._download=null;}
    closest(selector){return selector==='a[download]'?this:null;}
    click(){
      if(this._clickInProgress)return;
      this._clickInProgress=true;
      try{
        nativeClicks++;
        const event=new Event('click',{bubbles:true,cancelable:true});
        this.dispatchEvent(event);
        if(this.connected&&!event.cancelBubble){
          dispatchDocument('click',event);
          if(!event.cancelBubble)dispatch('click',event);
        }
      }finally{this._clickInProgress=false;}
    }
  }
  const window={origin:'null',localStorage:{length:0},sessionStorage:{length:0},
    fetch:options.fetch||(()=>Promise.resolve(new Response('ok'))),
    addEventListener(type,listener,flags){
      if(!listeners.has(type))listeners.set(type,[]);
      listeners.get(type).push({listener,flags});
    }};
  window.parent=options.topLevel?window:{postMessage(message,target){
    if(options.postThrows)throw new Error('parent unavailable');
    messages.push({message:structuredClone(message),target});
  }};
  const document={URL:pageURL,baseURI:pageURL,readyState:'loading',
    createElement(tag){assert.equal(tag,'a');return new HTMLAnchorElement();},
    addEventListener(type,listener){
      if(!documentListeners.has(type))documentListeners.set(type,[]);
      documentListeners.get(type).push(listener);
    }};
  Object.assign(window,{URL:PreviewURL,HTMLAnchorElement,Blob});
  const context=vm.createContext({window,document,URL:PreviewURL,HTMLAnchorElement,Blob,Request,Response,Promise});
  const dispatch=(type,event)=>{for(const row of listeners.get(type)||[])row.listener(event);};
  for(const [,attrs,source] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)){
    if(/type="module"/.test(attrs)){
      if(!options.topLevel){
        assert.ok(listeners.has('error'),'error capture must exist before the first module');
        assert.ok(listeners.has('unhandledrejection'),'rejection capture must exist before module evaluation');
        assert.equal(listeners.get('error')[0].flags,true,'resource errors require capture');
      }
      try{
        const module=new vm.SourceTextModule(source,{context});
        await module.link(()=>{throw new Error('missing import');});
        await module.evaluate();
      }catch(error){
        dispatch('error',{target:window,message:error.message,filename:pageURL,error});
      }
    }else vm.runInContext(source,context);
  }
  return {window,document,messages,listeners,dispatch,dispatchDocument,revoked,
    clickListenerCount:anchor=>getEventListeners(anchor,'click').length,get nativeClicks(){return nativeClicks;}};
}
(async()=>{await new Function('load','assert','Response','Request','pageURL','root',
  'return (async()=>{'+checks+'})()')(load,assert,Response,Request,pageURL,root);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", harness, json.dumps(prepared.decode()),
         json.dumps(checks)], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_detached_blob_download_captures_bytes_before_immediate_revoke():
    _run_preview_observer(r"""
let network=0;
const page=await load({fetch(){network++;throw Error('must not fetch a Blob URL');}});
assert.equal(page.messages.length,1);
const {message,target}=page.messages[0];
assert.equal(target,'https://meyo.test');
assert.equal(message.type,'meyo-artifact-preview');
assert.equal(message.nonce,'__ut_preview_nonce');
assert.equal(message.download.name,'state(1).json');
assert.ok(message.download.blob instanceof Blob);
assert.equal(await message.download.blob.text(),'{"progress":7}');
assert.equal(page.revoked.length,1);
assert.equal(page.window.exportClick.type,'click','the application click handler must run');
assert.equal(page.window.exportClick.isTrusted,false,'do not forge user activation');
assert.equal(network,0);
assert.deepEqual(Object.keys(message).sort(),['download','nonce','type']);
assert.deepEqual(Object.keys(message.download).sort(),['blob','name']);
""", module=r"""
const blob=new Blob(['{"progress":7}'],{type:'application/json'});
const url=URL.createObjectURL(blob),a=document.createElement('a');
a.onclick=event=>{window.exportClick=event;};
a.href=url;a.download='state(1).json';a.click();URL.revokeObjectURL(url);
""")


def test_blob_document_click_and_connected_programmatic_click_are_forwarded_once():
    _run_preview_observer(r"""
const page=await load();
const a=page.document.createElement('a'),clicks=[];
a.addEventListener('click',event=>clicks.push({event,href:a.href}));
a.href=page.window.URL.createObjectURL(new Blob(['one']));a.download='one.txt';a.connected=true;
a.click();
assert.equal(clicks.length,1,'the original event path runs exactly once');
assert.equal(page.messages.length,1);
const event={target:{closest:()=>a},button:0,defaultPrevented:false,
  preventDefault(){this.defaultPrevented=true;}};
page.dispatchDocument('click',event);
assert.equal(page.messages.length,2);assert.equal(event.defaultPrevented,true);
for(const extra of [{defaultPrevented:true},{ctrlKey:true},{button:1}]){
  page.dispatchDocument('click',{...event,defaultPrevented:false,...extra});
}
assert.equal(page.messages.length,2);
a.href='https://other.test/file';a.click();
assert.equal(clicks.length,2);
assert.equal(clicks.at(-1).href,'https://other.test/file');
assert.equal(clicks.at(-1).event.defaultPrevented,false,'ordinary URL click is unchanged');
a.href=page.window.URL.createObjectURL(new Blob(['not a download']));a.removeAttribute('download');a.click();
assert.equal(clicks.length,3);
assert.equal(clicks.at(-1).event.defaultPrevented,false,'non-download Blob click is unchanged');
assert.equal(page.messages.length,2);
""")


@pytest.mark.parametrize("connected,handler_kind", [
    (False, "onclick"), (False, "listener"),
    (True, "onclick"), (True, "listener"), (True, "document"), (True, "window"),
], ids=["detached-onclick", "detached-listener", "connected-onclick",
        "connected-listener", "connected-document", "connected-window"])
@pytest.mark.parametrize("action", ["cancel", "replace"])
def test_blob_click_respects_application_handlers(connected, handler_kind, action):
    _run_preview_observer(
        "const connected=" + json.dumps(connected) + ",handlerKind=" + json.dumps(handler_kind)
        + ",action=" + json.dumps(action) + ";" + r"""
let network=0;
const page=await load({fetch(){network++;throw Error('no URL fetch');}});
const a=page.document.createElement('a'),url=page.window.URL;
const original=url.createObjectURL(new Blob(['old-bytes']));
const replacement=url.createObjectURL(new Blob(['updated-bytes']));
a.connected=connected;a.href=original;a.download='old.bin';
let calls=0,received;
const handler=event=>{
  calls++;received=event;
  assert.equal(page.messages.length,0,'do not forward before late application handlers finish');
  if(action==='cancel'){
    if(handlerKind==='onclick')return false;
    event.preventDefault();
  }else{a.href=replacement;a.download='updated(1).bin';}
};
if(handlerKind==='onclick')a.onclick=handler;
else if(handlerKind==='listener')a.addEventListener('click',handler);
else page[handlerKind].addEventListener('click',handler);
const listenersBefore=page.clickListenerCount(a);
a.click();
url.revokeObjectURL(original);url.revokeObjectURL(replacement);
assert.equal(calls,1,'the original application handler must run exactly once');
assert.equal(received.target,a);assert.equal(received.isTrusted,false);
assert.equal(received.defaultPrevented,action==='cancel','respect the application cancellation result');
assert.equal(page.clickListenerCount(a),listenersBefore,'remove the temporary event observer');
assert.equal(page.messages.length,action==='cancel'?0:1,'cancel or forward exactly once, synchronously');
if(action==='replace'){
  assert.equal(page.messages[0].message.download.name,'updated(1).bin');
  assert.equal(await page.messages[0].message.download.blob.text(),'updated-bytes');
}
assert.equal(network,0);
""")


@pytest.mark.parametrize("connected", [False, True], ids=["detached", "connected"])
def test_blob_click_keeps_native_reentrancy_and_cleans_temporary_listener(connected):
    _run_preview_observer("const connected=" + json.dumps(connected) + ";" + r"""
const page=await load(),a=page.document.createElement('a');let calls=0;
a.connected=connected;a.download='one.txt';
a.href=page.window.URL.createObjectURL(new Blob(['one']));
a.onclick=()=>{calls++;a.click();};
const listenersBefore=page.clickListenerCount(a);
for(let i=1;i<=2;i++){
  a.click();
  assert.equal(calls,i,'reentrant native click does not redispatch the event');
  assert.equal(page.messages.length,i,'one transfer per actual click');
  assert.equal(await page.messages.at(-1).message.download.blob.text(),'one');
  assert.equal(page.clickListenerCount(a),listenersBefore,'even a no-event native call is cleaned up');
}
""")


def test_blob_bridge_bounds_retained_references_transfers_and_total_bytes():
    _run_preview_observer(r"""
const page=await load(),urls=[];
for(let i=0;i<9;i++)urls.push(page.window.URL.createObjectURL(new Blob([String(i)])));
const a=page.document.createElement('a');a.download='state.json';
a.href=urls[0];a.click();
assert.equal(page.messages.length,1,'an evicted Blob produces a bounded local notice');
assert.equal(page.messages.at(-1).message.download.error,'unavailable','oldest reference is evicted');
a.href=urls[8];a.click();assert.equal(await page.messages.at(-1).message.download.blob.text(),'8');
page.window.URL.revokeObjectURL(urls[8]);
a.click();
assert.equal(page.messages.filter(x=>x.message.download.blob).length,1,'revoke clears the bridge reference');
for(let i=0;i<12;i++){a.href=page.window.URL.createObjectURL(new Blob(['tiny']));a.click();}
assert.equal(page.messages.filter(x=>x.message.download.blob).length,8,'at most eight transfers per view');
assert.ok(page.messages.length<=10,'error notices are bounded too');
const bytesPage=await load(),large=new Blob([new Uint8Array(8*1024*1024)]);
const link=bytesPage.document.createElement('a');link.download='large.bin';
for(let i=0;i<3;i++){link.href=bytesPage.window.URL.createObjectURL(large);link.click();}
assert.equal(bytesPage.messages.filter(x=>x.message.download.blob).length,2,'at most 16 MiB transferred');
const oversize=await load(),big=oversize.document.createElement('a');big.download='big.bin';
big.href=oversize.window.URL.createObjectURL(new Blob([new Uint8Array(8*1024*1024+1)]));big.click();
assert.equal(oversize.messages.filter(x=>x.message.download.blob).length,0);
assert.equal(oversize.messages[0].message.download.error,'unavailable');
""")


def test_blob_bridge_cleans_up_on_pagehide_and_is_absent_without_parent():
    _run_preview_observer(r"""
const page=await load(),a=page.document.createElement('a');
a.href=page.window.URL.createObjectURL(new Blob(['private state']));a.download='state.json';
a.click();assert.equal(page.messages.length,1,'bridge must be active before cleanup');
page.dispatch('pagehide',{});a.click();
a.href=page.window.URL.createObjectURL(new Blob(['after close']));a.click();
assert.equal(page.messages.length,1);
page.dispatch('error',{target:page.window,filename:pageURL});
assert.equal(page.messages[1].message.observation.kind,'script_error','existing error flow stays installed');
const top=await load({topLevel:true}),normal=top.document.createElement('a');
normal.href=top.window.URL.createObjectURL(new Blob(['x']));normal.click();
assert.equal(top.nativeClicks,1);assert.equal(top.messages.length,0);
const missing=await load({postThrows:true}),detached=missing.document.createElement('a');
detached.href=missing.window.URL.createObjectURL(new Blob(['x']));detached.click();
assert.equal(missing.messages.length,0,'postMessage failure never triggers a URL fetch');
""")


def test_module_boot_failure_is_observed_early_with_exact_parent_origin_not_raw_error():
    _run_preview_observer(r"""
const page=await load();
assert.equal(page.messages.length,1);
const {message,target}=page.messages[0];
assert.equal(target,'https://meyo.test');
assert.equal(message.type,'meyo-artifact-preview');
assert.equal(message.nonce,'__ut_preview_nonce');
assert.equal(message.observation.kind,'script_error');
assert.equal(message.observation.resource,'nested/index.html');
assert.doesNotMatch(JSON.stringify(message),/private|credential|secret|token=|stack/);
""", module="throw new Error('private credential secret https://internal.test/?token=hidden');")


def test_observer_targets_explicit_parent_and_reduces_resources_without_reading_rejection_reason():
    _run_preview_observer(r"""
const page=await load();
page.dispatch('error',{target:{src:'https://meyo.test'+root+'assets/app.js?key=secret#private'}});
page.dispatch('error',{target:{href:'https://user:secret@cdn.test:8443/private.css?key=hidden#fragment'}});
page.dispatch('error',{target:{src:'https://meyo.test/api/private?key=secret'}});
page.dispatch('unhandledrejection',{get reason(){throw Error('must not inspect raw rejection');}});
assert.deepEqual(page.messages.map(x=>x.target),Array(4).fill('https://parent.test'));
assert.deepEqual(page.messages.map(x=>x.message.observation.resource),
  ['assets/app.js','https://cdn.test/','','']);
assert.equal(page.messages[3].message.observation.kind,'unhandled_rejection');
assert.doesNotMatch(JSON.stringify(page.messages),/secret|private|hidden|fragment|8443|user:/);
""", parent_origin="https://parent.test")


def test_caught_fetch_rejection_still_emits_one_fact_and_rethrows_identical_error():
    _run_preview_observer(r"""
const failure=new TypeError('private https://user:secret@hidden.test/path?key=hidden');
const request='/api/generate?key=secret';
const options={method:'POST',get body(){throw Error('must not read request body');},
  get headers(){throw Error('must not read headers');}};
let calls=0;
const page=await load({fetch(input,init){
  calls++;assert.equal(input,request);assert.equal(init,options);return Promise.reject(failure);
}});
let caught;
try{await page.window.fetch(request,options);}catch(error){caught=error;}
assert.equal(caught,failure);
assert.equal(calls,1,'observer must not retry');
assert.equal(page.messages.length,1,'application catch must not hide the failure');
assert.equal(page.messages[0].message.observation.kind,'network_error');
assert.equal(page.messages[0].message.observation.resource,'/api/generate');
assert.doesNotMatch(JSON.stringify(page.messages),/private|secret|hidden|key=/);
""")


def test_fetch_route_observation_keeps_only_short_action_prefix_not_private_parameters():
    _run_preview_observer(r"""
const page=await load({fetch(){return Promise.reject(new Error('not recorded'));}});
for(const input of ['/api/generate/private-id?key=hidden#private',
  '/api/sk-secret-value/path','/api/123456789/other','/api/private-files/other',
  '/api/generate%2Fsecret','https://other.test/api/generate/private?key=hidden']){
  try{await page.window.fetch(input);}catch{}
}
assert.deepEqual(page.messages.map(x=>x.message.observation.resource),
  ['/api/generate','','https://other.test/']);
assert.doesNotMatch(JSON.stringify(page.messages),/private|hidden|secret|123456789/);
""")


def test_observer_drops_preview_credentials_copied_into_resource_names_or_hosts():
    _run_preview_observer(r"""
for(const value of ['assets/__ut_cap.js','assets/__ut_preview_nonce.js',
  'https://__ut_cap.leak.test/file.js','https://__ut_preview_nonce.leak.test/file.js',
  'assets/apv1.'+'a'.repeat(32)+'.'+'b'.repeat(64)+'.js']){
  const page=await load();
  page.dispatch('error',{target:{src:value}});
  assert.equal(page.messages.length,1);
  assert.equal(page.messages[0].message.observation.resource,'',
    'resource must not carry copied preview credentials');
}
""")


@pytest.mark.parametrize("shape", ["sk", "sk-proj", "sk-ant", "github", "github-pat", "aws", "jwt"])
def test_observer_scrubs_credential_shaped_names_even_when_fetch_failure_is_caught(shape):
    _run_preview_observer("const shape=" + json.dumps(shape) + ";" + r"""
const credential={
  sk:'sk-'+'z'.repeat(24),'sk-proj':'sk-proj-'+'z'.repeat(24),
  'sk-ant':'sk-ant-'+'z'.repeat(24),github:'ghp_'+'z'.repeat(24),
  'github-pat':'github_pat_'+'z'.repeat(24),aws:'AKIA'+'Z'.repeat(16),
  jwt:'eyJ'+'z'.repeat(16)+'.'+'z'.repeat(16)+'.'+'z'.repeat(16)
}[shape];
for(const resource of [root+'assets/'+credential+'.js',
  'https://'+credential+'.invalid/private?key=discarded#discarded']){
  const failure=new TypeError('synthetic caught failure');
  const page=await load({fetch(){return Promise.reject(failure);}});
  let caught;try{await page.window.fetch(resource);}catch(error){caught=error;}
  assert.equal(caught,failure,'observation must not replace the application error');
  assert.equal(page.messages.length,1,'application catch must not hide the fact');
  assert.equal(page.messages[0].message.observation.kind,'network_error');
  assert.equal(page.messages[0].message.observation.resource==='',true,
    'credential-shaped filenames and hosts must be removed before postMessage');
}
""")


def test_observer_keeps_ordinary_resource_names_and_safe_api_prefix():
    _run_preview_observer(r"""
const page=await load({fetch(){return Promise.reject(new Error('synthetic failure'));}});
for(const name of ['assets/token-icon.v2.js','assets/sk-short.js','assets/中文-v2.js']){
  page.dispatch('error',{target:{src:'https://meyo.test'+root+name}});
  assert.equal(page.messages.at(-1).message.observation.resource,name);
}
try{await page.window.fetch('/api/generate/private-id?key=discarded#discarded');}catch{}
assert.equal(page.messages.at(-1).message.observation.resource,'/api/generate');
""")


def test_caught_non_2xx_fetch_is_observed_without_consuming_or_replacing_response():
    _run_preview_observer(r"""
const response=new Response('private response body',{status:503});
const request=new Request('https://user-service.test/private?key=hidden');
const page=await load({fetch(input){assert.equal(input,request);return Promise.resolve(response);}});
let returned;
try{returned=await page.window.fetch(request);if(!returned.ok)throw Error('application handles it');}catch{}
assert.equal(returned,response);
assert.equal(response.status,503);
assert.equal(response.bodyUsed,false);
assert.equal(await response.text(),'private response body');
assert.equal(page.messages.length,1);
assert.equal(page.messages[0].message.observation.kind,'http_error');
assert.equal(page.messages[0].message.observation.status,503);
assert.match(page.messages[0].message.observation.message,/503/);
assert.equal(page.messages[0].message.observation.resource,'https://user-service.test/');
assert.doesNotMatch(JSON.stringify(page.messages),/private|hidden|key=/);
""")


def test_successful_fetch_and_reporting_failure_preserve_application_results():
    _run_preview_observer(r"""
const response=new Response('unchanged success',{status:201});
const page=await load({fetch(){return Promise.resolve(response);}});
const returned=await page.window.fetch('./data.json');
assert.equal(returned,response);assert.equal(returned.status,201);
assert.equal(returned.bodyUsed,false);assert.equal(await returned.text(),'unchanged success');
assert.equal(page.messages.length,0);
const failure=new Error('unchanged original error');
const unavailable=await load({postThrows:true,fetch(){throw failure;}});
let caught;try{await unavailable.window.fetch('./missing.json');}catch(error){caught=error;}
assert.equal(caught,failure,'a failed observer must not replace the application error');
const original=()=>Promise.resolve(response);
const top=await load({topLevel:true,fetch:original});
assert.equal(top.window.fetch,original,'no observation hook outside the embedded preview');
""")


def test_preview_observer_merges_repeated_errors_and_bounds_distinct_facts():
    _run_preview_observer(r"""
const page=await load();
for(let i=0;i<40;i++)page.dispatch('error',{target:{src:pageURL+'?token='+i}});
assert.equal(page.messages.length,1);
for(let i=0;i<30;i++)page.dispatch('error',{target:{src:'https://meyo.test'+root+'asset-'+i+'.js'}});
assert.equal(page.messages.length,10);
""")


def test_observer_injection_is_optional_idempotent_and_cannot_break_out_of_script():
    import artifact_delivery as artifacts
    raw = b'<!doctype html><script type="module">export const x=1;</script><head></head>'
    legacy = artifacts.prepare_html_preview(raw)
    assert b"data-meyo-preview-observer" not in legacy
    nonce = '</script><img src=x onerror=alert(1)>'
    prepared = artifacts.prepare_html_preview(
        legacy, preview_nonce=nonce, preview_parent_origin='https://parent.test')
    assert b"data-meyo-preview-observer" in prepared
    assert b"<img src=x" not in prepared
    assert prepared.index(b"data-meyo-preview-observer") < prepared.index(b'type="module"')
    assert artifacts.prepare_html_preview(
        prepared, preview_nonce=nonce, preview_parent_origin='https://parent.test') == prepared
    utf16 = '<html><head></head><body>原字节</body></html>'.encode("utf-16")
    assert artifacts.prepare_html_preview(utf16, preview_nonce="test") == utf16


def test_template_notice_only_links_given_recorded_candidates_and_escapes_names():
    import artifact_delivery as artifacts
    body = artifacts.template_notice(
        'unsafe"><img src=x>.template.html', "/home-outputs/x/src/index.template.html?download=1",
        [{"name": "built.html", "url": "/home-outputs/x/dist/index.html"},
         {"name": "external", "url": "javascript:alert(1)"}],
    ).decode()
    assert "模板源码" in body and "/home-outputs/x/dist/index.html" in body
    assert 'javascript:' not in body and '<img src=x>' not in body
    assert "?download=1" in body


def test_related_candidates_require_the_same_owned_session(tmp_path, monkeypatch):
    import personal_identity as identity
    import sessions
    source = str((tmp_path / "index.template.html").resolve())
    built = str((tmp_path / "dist/index.html").resolve())
    rows = [{"name": "index.template.html", "path": source},
            {"name": "index.html", "path": built}]
    session = {"sid": "__ut_one", "user_id": "__ut_owner",
               "turns": [{"result": {"results": [{"artifacts": rows}]}}]}
    monkeypatch.setattr(sessions, "list_sessions", lambda **kw: [{"sid": "__ut_one"}])
    monkeypatch.setattr(sessions, "load_session",
                        lambda sid, user_id: session if user_id == "__ut_owner" else None)
    assert identity.related_artifacts(source, "__ut_owner") == rows
    assert identity.related_artifacts(source, "__ut_other") == []
    assert identity.related_artifacts(str(tmp_path / "unrelated.html"), "__ut_owner") == []


def _http_method(name):
    import artifact_delivery as artifacts
    import legion_paths
    tree = ast.parse((SERVICE / "server.py").read_text())
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
    method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"os": os, "HERE": str(SERVICE), "ARTIFACTS": artifacts,
                 "PATHS": legion_paths, "hashlib": hashlib,
                 "parse_qs": parse_qs, "urlparse": urlparse, "quote": quote}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SERVICE / "server.py"), "exec"), namespace)
    return namespace[name]


def test_denied_document_gets_login_guidance_but_json_call_stays_json(monkeypatch):
    import personal_identity as identity
    monkeypatch.setattr(identity, "file_allowed", lambda *args: False)

    class Handler:
        path = "/home-outputs/fixture.html"
        headers = {}
        _personal_user = lambda self: ""
        _send_json = lambda self, body, status: setattr(self, "reply", ("json", status, body))
        _send_artifact_notice = lambda self, body, status: setattr(self, "reply", ("html", status, body))
        allowed = _http_method("_file_owner_allowed")

    request = Handler()
    assert not request.allowed("/tmp/fixture.html")
    assert request.reply[0:2] == ("json", 403)
    request.headers = {"Accept": "text/html"}
    assert not request.allowed("/tmp/fixture.html")
    assert request.reply[0:2] == ("html", 403)
    assert "登录后打开".encode() in request.reply[2]


def test_html_display_keeps_sandbox_and_download_keeps_exact_original(tmp_path, monkeypatch):
    import artifact_delivery as artifacts
    import artifact_content_safety as safety
    body = b"<!doctype html><html><head></head><body>original bytes</body></html>"
    path = tmp_path / "index.html"
    path.write_bytes(body)
    monkeypatch.setattr(artifacts, "_is_internal_artifact_path", lambda path: False)
    monkeypatch.setattr(safety, "read_public_file", lambda path: body)

    class Handler:
        serve = _http_method("_serve_output")
        _export_bytes_match = _http_method("_export_bytes_match")
        _file_owner_allowed = lambda self, path: True
        send_response = lambda self, status: setattr(self, "status", status)
        send_header = lambda self, key, value: self.headers.update({key: value})
        end_headers = lambda self: None

        def __init__(self):
            self.headers, self.wfile = {}, io.BytesIO()

    request = Handler()
    request.serve("index.html", root=str(tmp_path), download=False)
    assert request.status == 200
    assert request.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in request.headers["Content-Security-Policy"]
    assert b"data-meyo-preview-storage" in request.wfile.getvalue()
    download = Handler()
    download.serve("index.html", root=str(tmp_path), download=True)
    assert download.wfile.getvalue() == body
    assert download.headers["Content-Disposition"].startswith("attachment")


def test_summary_keeps_early_nested_artifacts_after_later_empty_failures():
    import sessions
    artifact = {"name": "index.html", "url": "/home-outputs/__ut/dist/index.html"}
    original = {"sid": "__ut_early", "user_id": "__ut_owner", "turns": [
        {"question": "original", "result": {"status": "error", "results": [
            {"key": "codex", "status": "error", "artifacts": [artifact]}]}}
    ] + [{"question": "continue", "result": {"status": "error", "results": []}}
         for _ in range(7)]}
    before = json.dumps(original)
    row = sessions._summary_from_session(original)
    assert row["artifacts"][0]["url"] == artifact["url"]
    assert row["status"] == "error"
    assert row["turns"] == 8
    assert json.dumps(original) == before


def test_summary_projection_refreshes_unchanged_original_session(tmp_path):
    import sessions
    original = {"sid": "__ut_projection", "user_id": "__ut_owner", "updated": 10,
                "turns": [{"question": "old", "status": "error", "results": [
                    {"status": "error", "artifacts": [
                        {"name": "report.pdf", "url": "/home-outputs/__ut/report.pdf"}]}]}]}
    sessions._ensure_dir()
    file = Path(sessions.sessions_dir()) / "__ut_projection.json"
    raw = json.dumps(original).encode()
    file.write_bytes(raw)
    stamp = [file.stat().st_mtime_ns, file.stat().st_size]
    stale = {"sid": "__ut_projection", "user_id": "__ut_owner", "title": "old",
             "turns": 1, "updated": 10, "artifacts": [], "source_stamp": stamp}
    Path(sessions._summary_index_path()).write_text(json.dumps({"__ut_projection": stale}))
    rows = sessions.list_sessions(user_id="__ut_owner")
    assert rows[0]["artifacts"][0]["name"] == "report.pdf"
    assert file.read_bytes() == raw
    assert [file.stat().st_mtime_ns, file.stat().st_size] == stamp


def test_recent_projection_preserves_explicit_artifact_role():
    import performance_views
    rows, _ = performance_views._session_signals([{
        "sid": "__ut_roles", "title": "request", "artifacts": [{
            "name": "report.pdf", "url": "/home-outputs/__ut/report.pdf",
            "delivery_role": "primary", "is_primary": True, "entrypoint": True,
        }],
    }])
    assert rows[0]["delivery_role"] == "primary"
    assert rows[0]["is_primary"] is True
    assert rows[0]["entrypoint"] is True


@pytest.mark.parametrize("status", ["cancelled", "error"])
def test_nested_partial_result_does_not_override_explicit_turn_terminal_status(status):
    import sessions
    original = {"sid": "__ut_terminal", "user_id": "__ut_owner", "turns": [{
        "question": "original", "status": status,
        "result": {"results": [{"status": "done", "final": "partial work saved",
                                "artifacts": [{"name": "report.md", "url": "/home-outputs/__ut/report.md"}]}]},
    }]}
    summary = sessions._summary_from_session(original)
    assert summary["status"] == status
    assert summary["artifacts"][0]["name"] == "report.md"
