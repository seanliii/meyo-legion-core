#!/usr/bin/env python3
"""Discover harness-produced files and turn them into safe user-downloadable artifacts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import stat
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

import legion_paths as _paths
import artifact_content_safety as _content_safety

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.join(HERE, "outputs")
MEDIA = _paths.media_dir()
UPLOADS = _paths.uploads_dir()
# ★ 根因(2026-08-28 逐家测揪出):codex(及未来家)的持久工作目录写到 ~/.meyo/legion/outputs/
#   (= MEDIA 的兄弟目录),但旧 scan 只扫 repo 里的 OUTPUTS + MEDIA → codex 真落盘的文件收集
#   不到(hello_codex.txt 明明在却 files_collected=[])。补上家目录下的 outputs·和 media 同级。
HOME_OUTPUTS = os.path.join(os.path.dirname(MEDIA), "outputs")   # ~/.meyo/legion/outputs
# auto 自研家有自己的 per-user sandbox 工作目录(~/.meyo/users/<uid>/sandbox/)·产物写那里·
# 它 final 里会写出完整路径。把这个根也放行,让 extract 能从 auto 的 final 收集它的真产物
# (auto 不套统一 workdir·硬改它工作目录会干扰它自己的机制·故走"放行它的目录"而非"逼它改")。
AUTO_SANDBOX_ROOT = os.path.realpath(os.path.expanduser("~/.meyo/users"))
def _allowed_roots() -> list[str]:
    """运行时计算允许根；测试/部署切换 MEYO_LEGION_DIR 后不能沿用 import 时旧路径。"""
    media = _paths.media_dir()
    home_outputs = _paths.under("outputs")
    auto_sandbox = os.path.realpath(os.path.join(os.path.dirname(_paths.legion_dir()), "users"))
    # 云真机容器卷宿主挂载根(users/<uid>/desktop/Meyo=容器 /root/Meyo·2026-09-02 跨域产物收集)
    legion_users = os.path.realpath(_paths.under("users"))
    return [os.path.realpath(p) for p in (OUTPUTS, media, _paths.uploads_dir(), home_outputs, auto_sandbox, legion_users)]


def _kind(path: str) -> str:
    if is_html_template(path):
        return "code"
    try:
        import file_hub
        return file_hub.kind_of(path)
    except Exception:
        return "file"


def is_html_template(path: str) -> bool:
    """A declared template filename is source, not a completed browser entry."""
    return bool(re.search(r"\.(?:template|tmpl)\.html?$",
                          os.path.basename(str(path)).lower()))


def _notice_document(title: str, message: str, actions: str) -> bytes:
    from html import escape
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>'
            'body{font:16px/1.7 system-ui,sans-serif;color:#182536;background:#f5f7fb;'
            'margin:0;padding:8vh 20px}main{max-width:680px;margin:auto;background:white;'
            'border:1px solid #dbe3ef;border-radius:16px;padding:28px}'
            'a{display:inline-block;margin:8px 14px 8px 0;color:#125dbe}'
            'p{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            f'<main><h1>{escape(title)}</h1><p>{escape(message)}</p>{actions}</main></html>').encode()


def artifact_access_notice() -> bytes:
    return _notice_document(
        "登录后打开这份成果",
        "当前页面尚未验证身份，或当前账号没有这份文件的访问权限。"
        "\n请在军团主页使用原个人登录码登录，再回到这里重新打开。"
        "\n文件没有因此被删除，也不会自动切换到其他人的账号。",
        '<a href="/" target="_blank" rel="noopener">打开军团并登录</a>'
        '<a href="">我已登录，重新打开</a>',
    )


def template_notice(name: str, download_url: str, candidates: list[dict]) -> bytes:
    from html import escape
    links = []
    for row in candidates:
        url = str(row.get("url") or "")
        if not url.startswith(("/home-outputs/", "/outputs/", "/shared/", "/auto-outputs/")):
            continue
        links.append(f'<a href="{escape(url, quote=True)}">'
                     f'打开记录中的网页：{escape(str(row.get("name") or "网页候选"))}</a>')
    if download_url.startswith("/") and not download_url.startswith("//"):
        links.append(f'<a href="{escape(download_url, quote=True)}" download>下载原模板源码</a>')
    links.append('<a href="/" target="_blank" rel="noopener">返回军团查看成果</a>')
    return _notice_document(
        "这是模板源码，不是可运行入口",
        f"{name}\n这个文件需要经过项目构建，直接打开会停在加载页。"
        "\n原源码完整保留。下方网页入口仅来自同一会话已记录的文件，"
        "不是重命名模板，也不代表作品已通过质量验收。",
        "".join(links),
    )


_ISOLATED_PREVIEW_STORAGE = r"""<script data-meyo-preview-storage>
(()=>{
  let isolated=false;
  for(const name of ['localStorage','sessionStorage']){
    try{void window[name].length;continue;}catch(error){
      if(error.name!=='SecurityError')continue;
    }
    const values=new Map();
    const store={get length(){return values.size},
      key(index){return Array.from(values.keys())[Number(index)]??null},
      getItem(key){return values.get(String(key))??null},
      setItem(key,value){values.set(String(key),String(value))},
      removeItem(key){values.delete(String(key))},clear(){values.clear()}};
    try{Object.defineProperty(window,name,{value:store,configurable:false});isolated=true;}catch{}
  }
  if(isolated&&typeof document!=='undefined'){
    const show=()=>{
      const note=document.createElement('div');
      note.setAttribute('role','status');note.dataset.meyoStorageNotice='true';
      note.style.cssText='position:fixed;top:6px;left:50%;transform:translateX(-50%);z-index:2147483646;padding:4px 10px;border-radius:6px;background:#172238dd;color:white;font:12px/1.5 system-ui;max-width:80vw;text-align:center';
      note.textContent='隔离预览 · 本页存储为临时内存，刷新后不保留';
      const close=document.createElement('button');close.type='button';close.textContent=' ×';
      close.setAttribute('aria-label','关闭预览提示');
      close.style.cssText='background:none;color:inherit;border:0;cursor:pointer';
      close.onclick=()=>note.remove();note.appendChild(close);document.body.appendChild(note);
    };
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',show,{once:true});
    else show();
  }
})();
</script>"""


_PREVIEW_OBSERVER = r"""<script data-meyo-preview-observer>
(()=>{
  const config=__MEYO_PREVIEW_CONFIG__;
  if(!config.nonce||window.parent===window||window.origin!=='null')return;
  let page,target;
  try{
    page=new URL(document.URL);target=new URL(config.parent_origin||page.origin);
    if(!/^https?:$/.test(target.protocol)||target.username||target.password)return;
  }catch{return;}
  const root=page.pathname.match(/^\/artifact-preview\/[^/]+\/home-outputs\/exports\/[a-f0-9]{64}\/[a-f0-9]{64}\//)?.[0];
  const seen=new Set(),messages={
    script_error:'预览脚本执行失败。',resource_error:'预览资源加载失败。',
    unhandled_rejection:'预览出现未处理的异步失败。',network_error:'预览网络请求失败。'
  };
  const cleanResource=value=>{
    const lower=value.toLowerCase();
    // Same high-confidence shapes as artifact_content_safety._TOKEN; URL lowercases hosts.
    const credential=/\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,})\b/i;
    return /apv1\.[a-z0-9_-]{32}\.[a-f0-9]{64}/i.test(value)||
      credential.test(value)||
      [config.nonce,root?.split('/')[2]].some(part=>part&&lower.includes(part.toLowerCase()))?'':value;
  };
  const resource=(value,allowRoute)=>{
    try{
      if(!value)return '';
      const url=new URL(value,document.baseURI||document.URL);
      if(!/^https?:$/.test(url.protocol))return '';
      if(url.origin!==page.origin)return cleanResource(url.protocol+'//'+url.hostname+'/');
      if(allowRoute){
        const route=url.pathname.match(/^\/api\/([a-z][a-z-]{1,31})(?:\/|$)/);
        if(route&&!/key|token|secret|password|credential|bearer|private|^sk-/i.test(route[1]))return cleanResource('/api/'+route[1]);
      }
      if(!root||!url.pathname.startsWith(root))return '';
      const relative=decodeURIComponent(url.pathname.slice(root.length));
      if(!relative||relative.length>240||/[\\?#\x00-\x1f\x7f<>:"@=%]/.test(relative)||
          relative.split('/').some(part=>!part||part==='.'||part==='..'))return '';
      return cleanResource(relative);
    }catch{return '';}
  };
  const emit=(kind,value='',status)=>{
    try{
      const observation={kind,message:messages[kind]||'预览请求未成功。',
        resource:resource(value,kind==='network_error'||kind==='http_error')};
      if(kind==='http_error'&&Number.isInteger(status)&&(status===0||(status>=100&&status<=599))){
        observation.status=status;
        observation.message=status===0?'预览请求未获得可读取的响应状态。':'预览请求返回 HTTP '+status+'。';
      }
      const key=JSON.stringify(observation);
      if(seen.has(key)||seen.size>=10)return;
      seen.add(key);
      window.parent.postMessage({type:'meyo-artifact-preview',nonce:config.nonce,observation},target.origin);
    }catch{}
  };
  // Opaque previews cannot download directly. Transfer only locally-created Blob bytes;
  // the parent still requires its own user click, never a fetch of the child Blob URL.
  if(typeof Blob==='function'&&typeof URL.createObjectURL==='function'&&
      typeof HTMLAnchorElement!=='undefined'){
    const refs=new Map(),notices=new Set(),maxFile=8*1024*1024,maxTotal=16*1024*1024;
    let retained=0,sentBytes=0,sentCount=0,active=true;
    const create=URL.createObjectURL,revoke=URL.revokeObjectURL,click=HTMLAnchorElement.prototype.click;
    const forget=url=>{const blob=refs.get(url);if(blob){retained-=blob.size;refs.delete(url);}};
    URL.createObjectURL=function(blob){
      const url=create.apply(this,arguments);
      if(active&&blob instanceof Blob&&blob.size<=maxFile){
        while(refs.size>=8||retained+blob.size>maxTotal)forget(refs.keys().next().value);
        refs.set(url,blob);retained+=blob.size;
      }
      return url;
    };
    URL.revokeObjectURL=function(url){forget(url);return revoke.apply(this,arguments);};
    const send=download=>{
      if(download.error){
        if(notices.has(download.error))return;
        notices.add(download.error);
      }
      try{window.parent.postMessage({type:'meyo-artifact-preview',nonce:config.nonce,download},target.origin);}catch{}
    };
    const forward=anchor=>{
      if(!anchor?.hasAttribute('download')||!anchor.href.startsWith('blob:'))return false;
      if(!active)return true;
      // Capture synchronously: detached a.click() is often followed immediately by revoke().
      const blob=refs.get(anchor.href);
      if(!blob)send({error:'unavailable'});
      else if(sentCount>=8||sentBytes+blob.size>maxTotal)send({error:'limit'});
      else{
        ++sentCount;sentBytes+=blob.size;
        send({name:anchor.download.slice(0,240),blob});
      }
      return true;
    };
    const scriptedClicks=new WeakSet();
    HTMLAnchorElement.prototype.click=function(){
      let event;
      const capture=value=>{event=value;scriptedClicks.add(value);};
      this.addEventListener('click',capture,{capture:true,once:true});
      try{
        // Preserve native dispatch, including later document/window cancellation or replacement.
        // The sandbox still blocks native downloads; the parent requires its own user click.
        const result=click.apply(this,arguments);
        if(event&&!event.defaultPrevented)forward(this);
        return result;
      }finally{
        this.removeEventListener('click',capture,{capture:true});
        if(event)scriptedClicks.delete(event);
      }
    };
    document.addEventListener('click',event=>{
      if(scriptedClicks.has(event)||event.defaultPrevented||(event.button!==undefined&&event.button!==0)||
          event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
      if(forward(event.target?.closest?.('a[download]')))event.preventDefault();
    });
    window.addEventListener('pagehide',()=>{active=false;refs.clear();retained=0;});
  }
  window.addEventListener('error',event=>{
    try{
      const node=event.target;
      if(node&&node!==window)emit('resource_error',node.currentSrc||node.src||node.href||'');
      else emit('script_error',event.filename||'');
    }catch{}
  },true);
  window.addEventListener('unhandledrejection',()=>emit('unhandled_rejection'));
  if(typeof window.fetch==='function'){
    const original=window.fetch;
    const requestUrl=input=>{
      try{
        if(typeof input==='string')return input;
        if(input instanceof URL)return input.href;
        if(typeof Request!=='undefined'&&input instanceof Request)return input.url;
      }catch{}
      return '';
    };
    window.fetch=function(input){
      let result;
      try{result=original.apply(this,arguments);}
      catch(error){emit('network_error',requestUrl(input));throw error;}
      return Promise.resolve(result).then(response=>{
        try{if(!response.ok)emit('http_error',requestUrl(input),response.status);}catch{}
        return response;
      },error=>{emit('network_error',requestUrl(input));throw error;});
    };
  }
})();
</script>"""


def prepare_html_preview(body: bytes, *, preview_nonce: str = "",
                         preview_parent_origin: str = "") -> bytes:
    """Add isolated, read-only observations before modules; never mutate download bytes."""
    if b"\x00" in body[:256]:
        return body
    scripts = []
    if preview_nonce and b"data-meyo-preview-observer" not in body:
        import json
        config = json.dumps({"nonce": str(preview_nonce),
                             "parent_origin": str(preview_parent_origin or "")},
                            ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
        scripts.append(_PREVIEW_OBSERVER.replace("__MEYO_PREVIEW_CONFIG__", config).encode())
    if b"data-meyo-preview-storage" not in body:
        scripts.append(_ISOLATED_PREVIEW_STORAGE.encode())
    if not scripts:
        return body
    marker = re.search(br"<head(?:\s[^>]*)?>", body, re.I)
    if marker is None:
        marker = re.search(br"<!doctype[^>]*>", body, re.I)
    at = marker.end() if marker else 0
    first_script = re.search(br"<script(?:\s|>)", body, re.I)
    if first_script and first_script.start() < at:
        at = first_script.start()
    return body[:at] + b"".join(scripts) + body[at:]


def _url_for(path: str) -> str:
    """按文件【真实所在的可serve根】定 URL 前缀(用户2026-08-30实测揪出:workdir 是 home_outputs
    子目录·旧代码只判 root==home_outputs 精确相等·workdir 遍历时落 else→错给 /media/ 前缀→
    文件不在 media 目录→404→'无法从网站提取文件'下载失败+iframe空白)。改按 startswith 判归属。"""
    p = os.path.realpath(path)
    media = os.path.realpath(_paths.media_dir())
    home_out = os.path.realpath(_paths.under("outputs"))
    auto_sb = os.path.realpath(os.path.join(os.path.dirname(_paths.legion_dir()), "users"))
    repo_out = os.path.realpath(OUTPUTS)
    legion_users = os.path.realpath(_paths.under("users"))
    for root, prefix in ((media, "/media/"), (repo_out, "/outputs/"),
                         (home_out, "/home-outputs/"), (auto_sb, "/auto-outputs/"),
                         (legion_users, "/desktop-outputs/")):
        if p == root or p.startswith(root + os.sep):
            if prefix == "/media/":
                return prefix + quote(os.path.basename(p))
            rel = os.path.relpath(p, root)
            return prefix + "/".join(quote(x) for x in rel.split(os.sep))
    return "/media/" + quote(os.path.basename(p))  # 兜底


# 文本类交付物提取内容摘要(loop产品/用户角度揪出:cc把方案写文件·对话只显"我做完了"元描述·
# 用户看不到内容非要下载打开)。文本文件附前若干字·让用户对话里就能瞥见内容(理念20结果要可见)。
_TEXT_EXT = {".md", ".txt", ".sql", ".csv", ".json", ".py", ".js", ".ts", ".html",
             ".yaml", ".yml", ".sh", ".r", ".tsv", ".log", ".ipynb"}
# 终端快照(原始 ANSI 流)不进文本 preview——剥过 ANSI 的干净文本由前端终端卡渲染,
# 这里给原始流做 preview 只会是一坨转义码污染对话流。
_TERMINAL_SNAP_PREFIX = _content_safety._TERMINAL_SNAP_PREFIX


def _preview(path: str, limit: int = 600) -> str:
    """读文本文件开头做摘要(非文本/读失败返回空·绝不抛)。"""
    if os.path.splitext(path)[1].lower() not in _TEXT_EXT:
        return ""
    try:
        head = _content_safety.read_public_file(path, limit=4 * (limit + 200)).decode(
            "utf-8", errors="ignore")
        return head[:limit].strip() + ("…" if len(head) > limit else "")
    except Exception:
        return ""


def _safe(path: str):
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p) or _is_internal_artifact_path(p):
        return None
    for root in _allowed_roots():
        if p.startswith(root + os.sep) or p == root:
            return (p, root) if _content_safety.check_file(p)["allowed"] else None
    return None


def _provenance(path: str, source: str) -> dict:
    checked = _content_safety.check_file(path)
    if not checked["allowed"]:
        raise _content_safety.ArtifactUnsafe(checked["reason"])
    return {"source": source, "sha256": checked["sha256"],
            "created_at": checked["mtime_ns"] / 1_000_000_000,
            "verified": True, "verification": "文件存在、非空且内容哈希已计算"}


def extract(text: str, roots: list | None = None) -> list[dict]:
    """Extract existing allowed artifact paths from final text. Never exposes arbitrary host files.

    roots(2026-09-07真跑实锤修复):本单合法产物根目录列表(workdir+已有产物目录)。
    传入时【只认落在根内】的路径——成员跑find/grep扫全仓时输出的【别的任务/别的用户的
    产物路径】不再被归因为本任务产物(污染交付清单·误导用户)。未传时保持旧行为(兼容旧调用)。
    """
    candidates = []
    # Consume matching backtick delimiters as a whole. A fenced readback must
    # not shift the following inline path spans; existing 1/2/3-tick paths work.
    # A bare path is not limited to a hardcoded media-extension list. Its
    # actual file, task roots, internal-state and content checks below are the
    # same boundary as for a quoted path (including old, read-only deliveries).
    pattern = (r"(?<!`)(?P<ticks>`+)(?!`)(?P<quoted>.*?)(?P=ticks)(?!`)"
               r"|(?P<bare>(?:/[^\s`]+|outputs/[^\s`]+))")
    for match in re.finditer(pattern, text or "", flags=re.DOTALL):
        raw = (match.group("quoted") or match.group("bare") or "").strip().rstrip(".,，。;；)")
        if raw.startswith("outputs/"):
            raw = os.path.join(HERE, raw)
        candidates.append(raw)
    _roots = ([os.path.realpath(os.path.expanduser(str(r))) for r in roots if str(r).strip()]
              if roots else None)
    out, seen = [], set()
    for raw in candidates:
        resolved = os.path.realpath(os.path.expanduser(raw))
        if _roots is not None and not any(pathlib.Path(resolved).is_relative_to(r) for r in _roots):
            continue
        got = _safe(resolved)
        if not got:
            continue
        p, root = got
        if p in seen:
            continue
        seen.add(p)
        url = _url_for(p)
        out.append({"name": os.path.basename(p), "path": p, "url": url,
                    "kind": _kind(p), "bytes": os.path.getsize(p),
                    "provenance": _provenance(p, "final-explicit-path")})
    return out


def _dir_maybe_new(dirpath: str, since_ts: float) -> bool:
    """目录在 since_ts 后有变动吗?目录 mtime 会随其【直接子项增删】更新。早于 since_ts 且非
    根级(可能含本次新建子目录)的目录整个跳过——大幅剪掉历史产物目录的遍历,提速 enrich。
    保守:拿不到 mtime 就进去(宁慢不漏)。"""
    try:
        return os.path.getmtime(dirpath) >= since_ts
    except OSError:
        return True


_SCAN_CAP = 5000   # 总遍历文件数上限·防历史巨目录拖慢 enrich(它在交付主流程同步跑)
_SKIP_DIRS = _content_safety._SKIP_DIRS
_INTERNAL_DIR_NAMES = _content_safety._INTERNAL_DIR_NAMES
_INTERNAL_DIR_PREFIXES = _content_safety._INTERNAL_DIR_PREFIXES
_INTERNAL_FILE_NAMES = _content_safety._INTERNAL_FILE_NAMES
_is_internal_dir_name = _content_safety._is_internal_dir_name


def _is_internal_artifact_path(path: str, *, project_root: str = "") -> bool:
    """Reject internal files relative to their storage or bound project root."""
    try:
        candidate = pathlib.Path(os.path.realpath(unquote(str(path))))
        project_relative = None
        if project_root and os.path.isabs(project_root):
            root = pathlib.Path(os.path.realpath(project_root))
            if root != pathlib.Path(root.anchor) and candidate.is_relative_to(root):
                project_relative = candidate.relative_to(root)
        if project_relative is not None:
            # An authorized project can itself live beneath "state" or "logs".
            # Check the same descendants as the exporter, plus the legacy
            # temporary-message prefix below. This does not grant export access.
            if _content_safety._export_internal_path(project_relative):
                return True
            candidate = project_relative
        else:
            roots = [pathlib.Path(root) for root in _allowed_roots()
                     if candidate.is_relative_to(pathlib.Path(root))]
            if roots:
                candidate = candidate.relative_to(max(roots, key=lambda root: len(root.parts)))
        parts = candidate.parts
    except (OSError, TypeError, ValueError):
        return True
    return (any(_is_internal_dir_name(part) for part in parts[:-1])
            or (bool(parts) and (parts[-1].casefold() in _INTERNAL_FILE_NAMES
                or parts[-1].casefold().startswith(_TERMINAL_SNAP_PREFIX)
                or parts[-1].casefold().startswith("deepagents-messages.json.tmp."))))


def _is_private_artifact(artifact: dict, *, project_root: str = "") -> bool:
    """本机/本站运行路径与显式私有标记；项目根仅用于分类，不授予导出权限。"""
    path = artifact.get("path")
    url = str(artifact.get("url") or "")
    if not path and url.startswith("/") and not url.startswith("//"):
        path = url
    if path and not os.path.isabs(str(path)) and project_root and os.path.isabs(project_root):
        relative = pathlib.Path(str(path))
        if ".." in relative.parts or "\\" in str(path) or "\x00" in str(path):
            return True
        path = os.path.join(project_root, str(path))
    if artifact.get("visibility") == "private" or bool(
            path and _is_internal_artifact_path(str(path), project_root=project_root)):
        return True
    # Explicit references and producer-supplied previews cannot bypass discovery.
    for field in ("url", "preview", "description", "next_action"):
        value = artifact.get(field)
        if value and _content_safety.sensitive_text(str(value)):
            return True
    if path and not os.path.isfile(str(path)):
        # Events may carry a public route rather than the on-disk filename.
        # Resolve only declared roots, never search other users' directories.
        roots = (("/home-outputs/", _paths.under("outputs")),
                 ("/outputs/", OUTPUTS), ("/media/", _paths.media_dir()),
                 ("/desktop-outputs/", _paths.under("users")))
        for prefix, root in roots:
            if str(path).startswith(prefix):
                candidate = os.path.realpath(os.path.join(root, unquote(str(path)[len(prefix):])))
                if candidate.startswith(os.path.realpath(root) + os.sep):
                    path = candidate
                break
    return bool(path and not _content_safety.check_file(str(path))["allowed"])


def _is_private_artifact_event(event: dict) -> bool:
    return (event.get("t") in {"artifact_ready", "artifact_recovered", "artifact_handoff"}
            and _is_private_artifact(event))


def _scan_observation(path: str, checked: dict) -> dict:
    """A non-downloadable explanation, never file content or a public URL."""
    name = os.path.basename(path)
    return {"name": "withheld" if _content_safety.sensitive_text(name) else name,
            "scan_status": checked.get("scan_status") or "not_scanned",
            "reason": checked.get("reason") or "artifact_security_unavailable",
            "file_preserved": True, "downloadable": False}


def _not_exported(reason: str) -> dict:
    """No rejected absolute paths, contents or URLs in public failure facts."""
    return {"name": "project", "reason": reason, "scan_status": "not_exported", "downloadable": False,
            "file_preserved": True,
            "message": ("当前工作目录未绑定正式成果出口，原文件已保留。"
                        if reason == "artifact_workdir_unmapped" else
                        "本次成果未导出；原文件已保留，请核对归属、范围或内容检查。")}


_export_internal_path = _content_safety._export_internal_path


def _check_export_archive(raw: bytes, name: str, depth: int = 0) -> None:
    """No separate ZIP-only path pass: every supported container uses one policy."""
    _content_safety.inspect_archive_bytes(raw, name, depth)


def project_export_conversation(project: dict, *, team_id: str, task_id: str,
                                user_id: str, harness_key: str,
                                native_conversation_id: str, workdir: str) -> str:
    """Resolve the project identity without changing/parsing the native lane.

    Called by the trusted runtime after execution. Re-read canonical facts;
    a packet, a similar session prefix or a task from another harness is not
    authority to export. Directory grants remain independently checked by the
    exporter, which still accepts only the exact project conversation.
    """
    import project_context

    fail = _content_safety.ArtifactUnsafe
    packet = project_context.load(team_id)
    conversation = packet.get("conversation_id")
    task = next((row for row in (packet.get("tasks") or [])
                 if isinstance(row, dict) and row.get("task_id") == task_id), None)
    if (not team_id or not task_id or not user_id or not harness_key
            or not isinstance(conversation, str) or not conversation
            or packet.get("team_id") != team_id or not packet.get("user_id")
            or not isinstance(project, dict) or project.get("team_id") != team_id
            or project.get("conversation_id") != conversation
            or not project.get("user_id")
            or task is None or task.get("assigned_harness") != harness_key
            or native_conversation_id not in (conversation, f"{conversation}:task:{task_id}")):
        raise fail("artifact_project_binding_required")
    try:
        project_context.require_owner(packet, user_id, conversation)
        project_context.require_owner(project, user_id, conversation)
    except PermissionError:
        raise fail("artifact_owner_denied") from None
    roots = [project_context.task_workspace(packet, task_id),
             project.get("repo_root") or project.get("workspace_root"), workdir]
    if (any(not isinstance(root, str) or not os.path.isabs(root) for root in roots)
            or len({os.path.realpath(root) for root in roots}) != 1):
        raise fail("artifact_project_binding_required")
    return conversation


def export_project_artifacts(paths: list[str], *, team_id: str, task_id: str,
                             user_id: str, conversation_id: str,
                             requires_operator_grant: bool = False) -> dict:
    """Export selected project bytes through existing /home-outputs routes.

    Call ONLY in the trusted runtime: identity arguments are authenticated
    caller facts. Canonical ProjectContext binds the task, not directory
    authority: personal_identity independently checks the static operator
    project grant or an owned native default-workdir record and export scope.
    requires_operator_grant preserves a runtime selection's restriction from
    the first binding; it supplies no authority and never replaces rereading
    the current grant and its allowed subtrees.

    paths may contain files or selected site directories. Every emitted file
    retains its project-relative name in the same versioned snapshot, including
    unchanged HTML/CSS/JS assets. Choose the containing site/assets directories
    for multi-file pages; this is not a JS build/dependency resolver or a browser
    verification. Register ALL returned rows in the existing result/session
    artifact channel: the server authorizes files, not whole directories.
    """
    from contextlib import ExitStack
    import artifact_store
    import artifact_provenance
    import project_context
    import personal_identity

    out = {"artifacts": [], "artifact_safety_observations": []}
    fail = _content_safety.ArtifactUnsafe
    notes = out["artifact_safety_observations"]
    def binding():
        nonlocal requires_operator_grant
        if not all(isinstance(v, str) and v.strip()
                   for v in (team_id, task_id, user_id, conversation_id)):
            raise fail("artifact_project_binding_required")
        packet = project_context.load(team_id)
        task_ids = {node.get("task_id") for node in (packet.get("tasks") or [])
                    if isinstance(node, dict)}
        if (not packet or packet.get("team_id") != team_id or not packet.get("user_id")
                or packet.get("conversation_id") != conversation_id
                or task_id not in task_ids):
            raise fail("artifact_project_binding_required")
        try:
            project_context.require_owner(packet, user_id, conversation_id)
        except PermissionError:
            raise fail("artifact_owner_denied") from None
        raw = project_context.task_workspace(packet, task_id)
        if not isinstance(raw, str) or not pathlib.Path(raw).is_absolute():
            raise fail("artifact_workdir_unmapped")
        root = pathlib.Path(raw)
        if root == pathlib.Path(root.anchor) or ".." in root.parts:
            raise fail("artifact_workdir_unmapped")
        if _export_internal_path(pathlib.Path(root.name)):
            raise fail("artifact_internal_path")
        grant = personal_identity.authorized_project_path(
            user_id, str(root), conversation_id=conversation_id)
        if not grant or (requires_operator_grant and grant.get("source") != "operator_project_grant"):
            raise fail("artifact_project_not_authorized")
        # Once this export used an explicit grant, its revocation cannot be
        # replaced mid-publication by a default allocation/native-result grant.
        requires_operator_grant = requires_operator_grant or grant.get("source") == "operator_project_grant"
        # The exact selection is checked again before publication, including
        # operator revocation/narrowing while files were being collected.
        for relative in selected:
            if not personal_identity.project_export_path_allowed(grant, relative.as_posix()):
                raise fail("artifact_export_path_not_authorized")
        return root, packet["user_id"]

    try:
        selected = []
        root, owner = binding()
        if not isinstance(paths, (list, tuple)) or not paths or len(paths) > _SCAN_CAP:
            raise fail("artifact_selection_required")
        for value in paths:
            if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
                raise fail("artifact_path_outside_project")
            path = pathlib.Path(value)
            if ".." in path.parts:
                raise fail("artifact_path_outside_project")
            try:
                relative = path.relative_to(root) if path.is_absolute() else path
            except ValueError:
                raise fail("artifact_path_outside_project") from None
            if _export_internal_path(relative):
                raise fail("artifact_internal_path")
            selected.append(relative)
        binding()  # Authorize all selected subtrees before reading any file.

        if not all(hasattr(os, key) for key in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
            raise fail("artifact_export_unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        files, stamps, pins, listings = {}, [], [], []
        total, visited = 0, 0
        with ExitStack() as opened:
            def directory(parent, name):
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode):
                    raise fail("artifact_symlink_refused")
                fd = os.open(name, flags, dir_fd=parent)
                opened.callback(os.close, fd)
                if (entry.st_dev, entry.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    raise fail("artifact_source_changed")
                pins.append((parent, name, fd))
                return fd

            fd = os.open(root.anchor, flags)
            opened.callback(os.close, fd)
            # No-follow traversal also protects intermediate source links.
            for part in root.parts[1:]:
                fd = directory(fd, part)
            directories = {pathlib.Path("."): fd}

            def parent_fd(relative):
                parent = pathlib.Path(".")
                for part in relative.parts[:-1]:
                    child = parent / part
                    if child not in directories:
                        directories[child] = directory(directories[parent], part)
                    parent = child
                return directories[parent]

            def collect(relative):
                nonlocal total, visited
                visited += 1
                if visited > _SCAN_CAP:
                    raise fail("artifact_export_limit")
                if _export_internal_path(relative):
                    if not any(n["reason"] == "artifact_internal_path" for n in notes):
                        notes.append(_not_exported("artifact_internal_path"))
                    return  # Internal descendants are never copied or served.
                if relative == pathlib.Path("."):
                    current_fd = directories[relative]
                    info = os.fstat(current_fd)
                else:
                    parent = parent_fd(relative)
                    info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise fail("artifact_symlink_refused")
                    if stat.S_ISDIR(info.st_mode):
                        if relative not in directories:
                            directories[relative] = directory(parent, relative.name)
                        current_fd = directories[relative]
                if stat.S_ISDIR(info.st_mode):
                    listings.append((current_fd, _content_safety._stamp(os.fstat(current_fd))))
                    for name in sorted(os.listdir(current_fd)):
                        collect(relative / name)
                    return
                if not stat.S_ISREG(info.st_mode):
                    raise fail("artifact_not_regular_file")
                name = relative.as_posix()
                if name in files:
                    return
                if total + info.st_size > _content_safety.MAX_SCAN_BYTES:
                    raise fail("artifact_export_limit")
                read_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=parent)
                with os.fdopen(read_fd, "rb") as stream:
                    # Reuse the same scanner and descriptor as normal downloads.
                    stamp, digest = _content_safety._inspect(stream, name)
                    stream.seek(0)
                    raw = stream.read(stamp[2] + 1)
                    if (_content_safety._stamp(os.fstat(stream.fileno())) != stamp
                            or len(raw) != stamp[2] or hashlib.sha256(raw).hexdigest() != digest
                            or stamp != _content_safety._stamp(info)):
                        raise fail("artifact_source_changed")
                _check_export_archive(raw, name)
                files[name] = raw
                stamps.append((parent, relative.name, stamp))
                total += len(raw)

            for relative in selected:
                collect(relative)
            if not files:
                raise fail("artifact_no_public_files")
            # Detect replacements while collecting a multi-file version.
            for parent, leaf, stamp in stamps:
                if _content_safety._stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != stamp:
                    raise fail("artifact_source_changed")
            for directory_fd, stamp in listings:
                if _content_safety._stamp(os.fstat(directory_fd)) != stamp:
                    raise fail("artifact_source_changed")
            for parent, name, fd in pins:
                entry, pinned = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(fd)
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise fail("artifact_source_changed")
            if binding() != (root, owner):
                raise fail("artifact_project_binding_changed")
            manifest = {
                "schema_version": 1, "user_id": owner, "conversation_id": conversation_id,
                "team_id": team_id, "task_id": task_id, "source_root": str(root),
                "files": [{"relative_path": name, "source_path": str(root / name),
                           "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                          for name, raw in sorted(files.items())],
                "verification": "selected exact bytes; not browser or product acceptance",
            }
            stored = artifact_store.put_export_bundle(files, manifest)
        rows = []
        for file in manifest["files"]:
            destination = str(pathlib.Path(stored["root"]) / file["relative_path"])
            rows.append({
                "name": pathlib.Path(destination).name, "path": destination,
                "url": _url_for(destination), "kind": _kind(destination),
                "bytes": file["bytes"], "sha256": file["sha256"],
                "user_id": owner, "team_id": team_id, "task_id": task_id,
                "export_snapshot_id": stored["snapshot_id"],
                "provenance": {"source": "authorized-project-export",
                               "source_path": file["source_path"], "sha256": file["sha256"],
                               "verified": True, "verification": "安全检查及导出字节哈希一致；非产品验收"},
                "lineage": {"sources": [{"kind": "project_file", "path": file["source_path"],
                                         "sha256": file["sha256"]}],
                            "export": {"user_id": owner, "team_id": team_id,
                                       "conversation_id": conversation_id,
                                       "manifest": stored["manifest"]}},
            })
        artifact_provenance.attach_lineage(rows, task_id=task_id)
        out.update(artifacts=rows, export_manifest=stored["manifest"])
    except _content_safety.ArtifactUnsafe as exc:
        notes.append(_not_exported(str(exc)))
    except (OSError, TypeError, ValueError, RuntimeError, NotImplementedError):
        notes.append(_not_exported("artifact_export_unavailable"))
    return out


def collect_run_artifacts(*, since_ts: float, workdir: str, project: dict,
                          team_id: str, task_id: str, user_id: str,
                          harness_key: str, native_conversation_id: str,
                          request_id: str, final: str = "",
                          extra_roots: list | None = None,
                          previous_artifacts: list | None = None,
                          require_project_authorization: bool = False) -> dict:
    """Collect success and failure through the same authenticated export boundary.

    A refused project export never falls back to mutable sources or old memory.
    Legacy unbound calls retain their existing task-root collection behavior.
    """
    import artifact_provenance

    arts, notes = [], []
    project_root = str(project.get("repo_root") or project.get("workspace_root") or "")
    export_conversation = ""
    collection_source = "legacy-task-collection"
    phase = "artifact_project_binding"
    try:
        exported = None
        if project_root:
            import personal_identity
            import legion_sandbox

            collection_source = "authorized-project-export"
            export_conversation = project_export_conversation(
                project, team_id=team_id, task_id=task_id, user_id=user_id,
                harness_key=harness_key, native_conversation_id=native_conversation_id,
                workdir=workdir)
            phase = "artifact_project_authorization"
            grant = personal_identity.authorized_project_path(
                user_id, project_root, conversation_id=export_conversation)
            allocated = os.path.realpath(os.path.join(
                _paths.under("outputs"), f"{harness_key}_{request_id}"))
            owned_allocation = any(
                row["task_id"] == task_id and row["harness_key"] == harness_key
                and row["root"] == project_root
                for row in legion_sandbox.owned_workdir_allocations(
                    team_id, user_id, export_conversation))
            if (grant is not None or owned_allocation or require_project_authorization
                    or os.path.realpath(workdir) != allocated):
                phase = "artifact_project_export"
                exported = export_project_artifacts(
                    list((grant or {}).get("export_paths") or []),
                    team_id=team_id, task_id=task_id, user_id=user_id,
                    conversation_id=export_conversation,
                    requires_operator_grant=(require_project_authorization
                                             or (grant or {}).get("source") == "operator_project_grant"))
            else:
                collection_source = "legacy-task-collection"
        if exported is not None:
            arts = list(exported.get("artifacts") or [])
            notes.extend(exported.get("artifact_safety_observations") or [])
        else:
            phase = "artifact_scan"
            scanned = scan_new_files(since_ts, workdir=workdir, extra_roots=extra_roots,
                                     safety_observations=notes)
            if not any(note.get("reason") == "artifact_workdir_unmapped" for note in notes):
                phase = "artifact_explicit_paths"
                roots = [workdir, *(extra_roots or [])] if workdir else None
                explicit = extract(final, roots=roots)
                arts = artifact_provenance.merge_artifacts(previous_artifacts, scanned, explicit)
    except _content_safety.ArtifactUnsafe as exc:
        reason = str(exc)
        if not re.fullmatch(r"artifact_[a-z0-9_]{1,80}", reason):
            reason = "artifact_export_unavailable"
        notes.append({**_not_exported(reason), "phase": phase,
                      "error_type": type(exc).__name__})
    except Exception as exc:
        # Public diagnostics retain the stage/type, not credentials or paths.
        notes.append({**_not_exported("artifact_export_unavailable"), "phase": phase,
                      "error_type": type(exc).__name__})
    for note in notes:
        note.setdefault("phase", phase)
    return {
        "artifacts": arts,
        "artifact_safety_observations": notes,
        "artifact_export": {"source": collection_source, "exported_files": len(arts),
                            "observations": notes, "workdir": workdir,
                            "conversation_id": export_conversation},
    }


def scan_new_files(since_ts: float, limit: int = 60, workdir: str = "",
                   scan_shared_roots: bool = False,
                   extra_roots: list | None = None,
                   safety_observations: list | None = None) -> list[dict]:
    """扫本次 workdir 及兼容产物根里【since_ts 之后新产生/修改】的文件。

    根因(2026-08-28 真跑发现):codex 写了完整代码/文件到 outputs,但 final 只回 171 字摘要、
    没写出路径 → 旧 enrich 只从 final 文本正则提路径 → 收不到,用户以为它没干活。修:主动扫本次
    任务窗口(since_ts 后)新增的真文件,不靠 harness 在 final 里写路径。按 mtime 过滤只收本次新增,
    不误收历史文件(outputs 是共享目录·有旧产物)。递归进子目录(代码文件夹如 ai-study-coach-api/)。

    产物隔离(2026-09-02 S3 五根因②):默认只扫【本单 workdir】——共享根(outputs/media/auto
    sandbox)在多单并发时会把别的单/别的用户在窗口内新写的文件混进本单交付(12 家并发 1325s 复盘
    揪出的产物污染)。明确但未映射的 workdir 不回退共享根；可信项目调用方应走
    export_project_artifacts。未指定 workdir 保留旧共享根兼容行为。
    """
    out, seen = [], set()
    _scanned = 0
    # 含家目录 outputs(codex 等家持久工作目录) + auto per-user sandbox(auto 产物写那里)。
    # 用文件级 mtime 过滤(下面 getmtime<since_ts 跳过)只收本次窗口新写的·不靠父目录 mtime 剪枝
    # (那不可靠:在【已存在的目录】里加新文件·父目录 mtime 不变·会漏掉——auto/codex 在已建目录
    # 追加文件正是这情况)。改:①跳过明确的非产物目录(缓存/日志/git等·auto sessions也跳)
    # ②总文件数 5000 上限防历史巨目录拖慢。文件级 mtime 是准的·父目录级不准。
    roots = []
    if workdir:
        candidate = os.path.realpath(os.path.expanduser(workdir))
        if any(candidate == allowed or candidate.startswith(allowed + os.sep) for allowed in _allowed_roots()):
            roots.append(candidate)
        else:
            if safety_observations is not None:
                safety_observations.append(_not_exported("artifact_workdir_unmapped"))
            return []
    # 跨域产物根(extra_roots·2026-09-02 云真机产物收集缺口根修):云真机任务 harness 把
    # 文件写进容器 /root/Meyo(宿主=users/<uid>/desktop/Meyo)·不在本单 workdir·旧逻辑
    # 有 workdir 就只扫它→容器产物军团收集瞎(用户网站看不到云真机交付物·验收只能
    # docker exec 查容器)。修:调用方(run_one)对云真机任务传 meyo 根·同样过放行根
    # 校验(安全)后与 workdir 并扫。隔离不破:只在任务明说云真机时才加·mtime 窗口过滤
    # 已有·users 根本就在允许清单·非云真机零变化。
    for extra in (extra_roots or []):
        try:
            cand = os.path.realpath(os.path.expanduser(str(extra)))
        except Exception:
            continue
        if cand not in roots and any(cand == allowed or cand.startswith(allowed + os.sep)
                                     for allowed in _allowed_roots()):
            roots.append(cand)
    if roots:
        # 产物隔离:有放行根(workdir/extra)就只扫它们·不遍历共享根(防并发串单)。
        # 未指定 workdir 时，保留显式 extra_roots 的兼容行为。
        return _scan_roots(roots, since_ts, limit, out, seen, _scanned, safety_observations)
    media = _paths.media_dir()
    home_outputs = _paths.under("outputs")
    auto_sandbox_root = os.path.realpath(os.path.join(os.path.dirname(_paths.legion_dir()), "users"))
    for root in (OUTPUTS, media, home_outputs, auto_sandbox_root):
        root = os.path.realpath(root)
        if root not in roots:
            roots.append(root)
    return _scan_roots(roots, since_ts, limit, out, seen, _scanned, safety_observations)


def _git_changed_files(root: str) -> set[str] | None:
    """For a cloned repository, return only changed/untracked files; None means non-git root."""
    try:
        probe = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        if probe.returncode != 0:
            return None
        repo = os.path.realpath(probe.stdout.strip())
        if repo != os.path.realpath(root):
            return None
        status = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--no-renames"],
            capture_output=True, timeout=10)
        if status.returncode != 0:
            return None
        changed = set()
        for record in status.stdout.split(b"\0"):
            if len(record) < 4:
                continue
            rel = record[3:].decode("utf-8", errors="surrogateescape")
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            changed.add(os.path.realpath(os.path.join(repo, rel)))
        return changed
    except (OSError, subprocess.SubprocessError):
        return None


def _git_control_bytes(path: str) -> bytes | None:
    """Bounded local metadata read; never follow a control-file symlink or block on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            value = stream.read(4097)
            return value.strip() if len(value) <= 4096 else None
    except (OSError, ValueError):
        return None


def _is_git_admin_dir(directory: str) -> bool:
    """Recognize Git's HEAD + common objects/refs layout, independently of directory names.

    Only automatic discovery uses this classification. It is not an access
    policy: explicit, content-safe evidence still follows extract/_safe.
    """
    try:
        head_path = os.path.join(directory, "HEAD")
        if os.path.islink(head_path):
            # Legacy symbolic HEAD: inspect the link itself, not its target.
            head = b"ref: " + os.fsencode(os.readlink(head_path))
        else:
            head = _git_control_bytes(head_path)
        if not head or not (
            re.fullmatch(rb"ref: refs/[^\x00-\x20]+", head)
            or re.fullmatch(rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head)
        ):
            return False
        common = directory
        common_file = os.path.join(directory, "commondir")
        if os.path.lexists(common_file):
            value = _git_control_bytes(common_file)
            if not value or b"\0" in value:
                return False
            common = os.path.realpath(os.path.join(directory, os.fsdecode(value)))
        return (os.path.isdir(os.path.join(common, "objects"))
                and os.path.isdir(os.path.join(common, "refs")))
    except (OSError, ValueError):
        return False


def _scan_roots(roots: list, since_ts: float, limit: int, out: list, seen: set,
                _scanned: int = 0, safety_observations: list | None = None) -> list:
    """实际遍历 roots；Git 根只收真实改动，避免把刚克隆的整仓源码误报成交付物。"""
    git_admin_cache: dict[str, bool] = {}
    boundaries = tuple(_allowed_roots())

    def inside_git_admin(directory: str) -> bool:
        directory = os.path.realpath(directory)
        # Include ancestors within existing allowed storage roots, so a scan
        # starting at objects/ or an alias to index cannot evade classification.
        while any(directory == root or directory.startswith(root + os.sep) for root in boundaries):
            if directory not in git_admin_cache:
                git_admin_cache[directory] = _is_git_admin_dir(directory)
            if git_admin_cache[directory]:
                return True
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
        return False

    for root in roots:
        if not os.path.isdir(root) or inside_git_admin(root):
            continue
        repo_changes: dict[str, set[str]] = {}
        root_changes = _git_changed_files(root)
        if root_changes is not None:
            repo_changes[os.path.realpath(root)] = root_changes
        for dirpath, dirnames, filenames in os.walk(root):
            real_dir = os.path.realpath(dirpath)
            if (os.path.isdir(os.path.join(dirpath, ".git"))
                    or os.path.isfile(os.path.join(dirpath, ".git"))):
                changes = _git_changed_files(real_dir)
                if changes is not None:
                    repo_changes[real_dir] = changes
            dirnames[:] = [d for d in dirnames if not _is_internal_dir_name(d)
                           and not inside_git_admin(os.path.join(dirpath, d))]
            active_repo = max((repo for repo in repo_changes
                               if real_dir == repo or real_dir.startswith(repo + os.sep)),
                              key=len, default="")
            active_changes = repo_changes.get(active_repo) if active_repo else None
            if active_changes is not None:
                kept = []
                if active_changes:
                    for name in dirnames:
                        # Resolve each child once, not once per changed file.
                        # Keep this local to the scan; symlinks may change later.
                        child = os.path.realpath(os.path.join(dirpath, name))
                        prefix = child + os.sep
                        if any(changed == child or changed.startswith(prefix)
                               for changed in active_changes):
                            kept.append(name)
                dirnames[:] = kept
            if _scanned >= _SCAN_CAP:
                return out
            for fn in filenames:
                _scanned += 1
                if _scanned >= _SCAN_CAP:
                    return out
                if fn.startswith(".") or fn.casefold() in _INTERNAL_FILE_NAMES:
                    continue
                # 跳过内部元数据文件(军团极限测试揪出:.progress.json/sidecar 等内部文件被当交付物
                # 混进用户结果·用户看到一坨json元数据)。这些是harness/父层的过程记录·不是给用户的交付。
                if (fn.endswith(".progress.json") or fn.endswith(".sidecar.json")
                        or fn.endswith(".meta.json") or fn == "state.json"):
                    continue
                p = os.path.realpath(os.path.join(dirpath, fn))
                if _is_internal_artifact_path(p) or inside_git_admin(os.path.dirname(p)):
                    continue
                if active_changes is not None and p not in active_changes:
                    continue
                try:
                    if os.path.getmtime(p) < since_ts or not os.path.isfile(p):
                        continue
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size == 0 or p in seen:   # 空文件不算真交付(codex 空目录壳的遗留坑)
                    continue
                checked = _content_safety.check_file(p)
                if not checked["allowed"]:
                    if safety_observations is not None and len(safety_observations) < limit:
                        safety_observations.append(_scan_observation(p, checked))
                    continue
                seen.add(p)
                url = _url_for(p)   # 按真实归属定前缀(修workdir子目录被错给/media/致下载404)
                out.append({"name": os.path.basename(p), "path": p, "url": url,
                            "kind": _kind(p), "bytes": size, "discovered": True,
                            "preview": _preview(p),
                            "provenance": _provenance(p, "workdir-discovery")})   # 文本类附内容摘要·让用户对话里就看得到内容
                if len(out) >= limit:
                    return out
    return out


def _has_project_export(result: dict, workdir: str, team_id: str) -> bool:
    """Recognize an existing checked snapshot by its saved manifest, not a flag."""
    import artifact_store
    import json

    for artifact in result.get("artifacts") or []:
        try:
            snapshot = artifact.get("export_snapshot_id")
            if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot):
                continue
            export = artifact.get("lineage", {}).get("export", {})
            ref = export.get("manifest") or {}
            if ref.get("sha256") != snapshot:
                continue
            manifest = json.loads(artifact_store.read_text(ref))
            if (manifest.get("team_id") != team_id
                    or manifest.get("task_id") != (result.get("task_id") or result.get("key"))
                    or manifest.get("user_id") != artifact.get("user_id")
                    or manifest.get("conversation_id") != export.get("conversation_id")
                    or os.path.realpath(manifest.get("source_root") or "") != workdir):
                continue
            path = os.path.realpath(artifact.get("path") or "")
            relative = pathlib.Path(path).relative_to(
                pathlib.Path(_paths.under("outputs")).resolve() / "exports")
            if len(relative.parts) < 3 or relative.parts[1] != snapshot:
                continue
            checked = _content_safety.check_file(path)
            if any(row.get("relative_path") == pathlib.Path(*relative.parts[2:]).as_posix()
                   and row.get("sha256") == checked.get("sha256")
                   for row in manifest.get("files") or []) and checked["allowed"]:
                return True
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return False


def enrich(out: dict, since_ts: float = 0.0, workdirs: list | None = None) -> dict:
    """把各家写出的文件变成可下载交付。①从 final 文本提路径(harness 明确写了的)②since_ts>0
    时主动扫本次窗口新增文件(harness 没写路径也能收·治 codex 171字摘要但真建了文件夹的坑)。

    workdirs(2026-09-02 S3 五根因②产物隔离):本单各成员的真实 workdir 列表。传了就只扫
    这些目录——多单并发时别的单/别的用户在时间窗内新写的文件不再混进本单交付。没传时退回
    共享根旧行为(兼容·单用户低并发语义不变)。auto 等写 per-user sandbox 的家由调用方把
    其 sandbox 路径一并传入。
    """
    def merge_notes(*groups):
        rows = []
        for group in groups:
            for note in group if isinstance(group, list) else []:
                if isinstance(note, dict) and note not in rows:
                    rows.append(dict(note))
        return rows

    from_final = set()
    member_notes = []
    processed_roots: dict[str, list[bool]] = {}
    # 合法产物根(2026-09-07):workdirs+各result已有产物的父目录。extract只认根内路径,
    # 防成员扫描全仓时把别的任务产物误归因(真跑实锤:codex修复轮跑find把ui30/iview等
    # 无关路径全报成本任务产物)。
    _legal_roots = [str(wd) for wd in (workdirs or []) if str(wd).strip()]
    for result in out.get("results", []):
        for _a in (result.get("artifacts") or []):
            _p = str((_a or {}).get("path") or "")
            if _p:
                _d = os.path.dirname(_p)
                if _d and _d not in _legal_roots:
                    _legal_roots.append(_d)
    for result in out.get("results", []):
        export = result.get("artifact_export")
        export = export if isinstance(export, dict) else {}
        workdir = str(result.get("workdir") or export.get("workdir") or "")
        workdir = os.path.realpath(workdir) if workdir else ""
        # This is a host runtime result, not native_facts. Once export has
        # accepted/refused a selection, neither final text nor a generic scan
        # may reintroduce mutable sources or bypass that selection.
        processed = (type(export.get("exported_files")) is int
                     and export["exported_files"] >= 0
                     and isinstance(export.get("observations"), list))
        if not processed and workdir:
            processed = _has_project_export(result, workdir, str(out.get("team_id") or ""))
        notes = merge_notes(result.get("artifact_safety_observations"), export.get("observations"))
        for candidate in result.get("artifacts") or []:
            path = candidate.get("path") if isinstance(candidate, dict) else None
            if path and os.path.isfile(str(path)) and not _is_internal_artifact_path(str(path)):
                checked = _content_safety.check_file(str(path))
                if not checked["allowed"] and len(notes) < 60:
                    notes.append(_scan_observation(str(path), checked))
        if notes:
            result["artifact_safety_observations"] = notes
            member_notes.extend({**note,
                                 "team_id": str(out.get("team_id") or ""),
                                 "task_id": str(result.get("task_id") or result.get("key") or "")}
                                for note in notes)
        explicit = [] if processed else extract(str(result.get("final") or ""), roots=_legal_roots or None)
        try:
            import artifact_provenance
            artifacts = artifact_provenance.merge_artifacts(result.get("artifacts"), explicit)
            artifact_provenance.attach_lineage(
                artifacts, objective=str(out.get("question") or ""),
                task=str(result.get("task") or result.get("task_id") or ""),
                task_id=str(result.get("task_id") or result.get("key") or ""),
                agent=str(result.get("assigned_harness") or result.get("harness") or result.get("key") or ""),
                actions=result.get("tool_trace") or [],
                inherited_from=str(result.get("relayed_from") or ""))
        except Exception:
            artifacts = [*(result.get("artifacts") or []), *explicit]
        # 血缘补充失败也不能把私有运行日志重新放回公开交付列表。
        artifacts = [a for a in artifacts if isinstance(a, dict) and not _is_private_artifact(a)]
        if artifacts or "artifacts" in result:
            result["artifacts"] = artifacts
            from_final.update(a["path"] for a in artifacts if a.get("path"))
        if workdir:
            processed_roots.setdefault(workdir, []).append(processed)
    notes = merge_notes(member_notes, out.get("artifact_safety_observations"))
    if notes:
        out["artifact_safety_observations"] = notes
    # 主动扫本次新增产物(不靠 final 写路径)·去掉已从 final 提到的·避免重复。
    # 整体兜底:扫描是【增强】,任何异常(目录不存在/权限/大目录慢)绝不能影响交付主流程(上游断链铁律)。
    if since_ts and since_ts > 0:
        try:
            discovered = []
            safety_observations = []
            if workdirs:
                seen_paths = set(from_final)
                for wd in workdirs:
                    if not wd:
                        continue
                    handled = processed_roots.get(os.path.realpath(str(wd)))
                    if handled and all(handled):
                        continue
                    for a in scan_new_files(since_ts, workdir=str(wd), safety_observations=safety_observations):
                        if a["path"] not in seen_paths:
                            discovered.append(a)
                            seen_paths.add(a["path"])
            else:
                discovered = [a for a in scan_new_files(since_ts, safety_observations=safety_observations)
                              if a["path"] not in from_final]
            if safety_observations:
                out["artifact_safety_observations"] = merge_notes(
                    out.get("artifact_safety_observations"), safety_observations)
            if discovered:
                try:
                    import artifact_provenance
                    artifact_provenance.attach_lineage(
                        discovered, objective=str(out.get("question") or ""),
                        task="本次任务目录自动发现", task_id=str(out.get("team_id") or ""),
                        agent="legion", actions=[])
                except Exception:
                    pass
                out["discovered_artifacts"] = discovered
        except Exception:
            pass
    return out


# Short-lived, process-local preview capabilities. These are not personal
# credentials, session shares, a runtime manager, or a persistent token store.
_PREVIEW_SIGNING_KEY = secrets.token_bytes(32)
_PREVIEW_GRANTS: dict[str, dict] = {}
_PREVIEW_LOCK = threading.RLock()
_PREVIEW_TTL = 600
_PREVIEW_MAX_GRANTS = 256
_PREVIEW_VIEW_LIMIT = 12
_PREVIEW_REPORT_LIMIT = 24
_PREVIEW_REPORT_ID = "artifact-preview-observer"
_PREVIEW_MESSAGES = {
    "script_error": "预览脚本报告错误。",
    "resource_error": "预览资源加载失败。",
    "unhandled_rejection": "预览出现未处理的异步错误。",
    "network_error": "预览网络请求失败。",
    "http_error": "预览请求返回失败状态。",
}


class PreviewError(Exception):
    """Public codes only: never return file paths, credentials or raw exceptions."""

    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code, self.status = code, status


def _preview_target(url: str) -> dict:
    if (not isinstance(url, str) or not url or len(url) > 4096
            or re.search(r"[\x00-\x20\x7f\\]", url)):
        raise PreviewError("preview_invalid_resource", 400)
    try:
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            raise PreviewError("preview_invalid_resource", 400)
        if not parsed.path.startswith("/home-outputs/exports/"):
            raise PreviewError("preview_unsupported", 422)
        # Decode exactly once, never permit encoded separators or a second
        # decoding pass to change the path's meaning.
        if re.search(r"%(?:2f|5c)", parsed.path, re.I):
            raise PreviewError("preview_invalid_resource", 403)
        decoded = unquote(parsed.path, errors="strict")
        pieces = decoded.removeprefix("/home-outputs/exports/").split("/")
        if (len(pieces) < 3 or any(p in {"", ".", ".."} for p in pieces)
                or re.search(r"[\x00-\x1f\x7f\\]|%[0-9a-f]{2}", decoded, re.I)
                or not all(re.fullmatch(r"[0-9a-f]{64}", p) for p in pieces[:2])):
            raise PreviewError("preview_invalid_resource", 403)
        base = pathlib.Path(_paths.under("outputs")).resolve()
        path = base / "exports" / pathlib.Path(*pieces)
        if os.path.realpath(path) != str(path) or _is_internal_artifact_path(str(path)):
            raise PreviewError("preview_invalid_resource", 403)
        canonical = "/home-outputs/exports/" + "/".join(quote(p, safe="") for p in pieces)
        return {"url": canonical, "path": str(path), "base": str(base),
                "scope": pieces[0], "snapshot_id": pieces[1],
                "relative": "/".join(pieces[2:])}
    except (ValueError, UnicodeError, OSError, RuntimeError):
        raise PreviewError("preview_invalid_resource", 400) from None


def _preview_token(view_id: str, purpose: str) -> str:
    digest = hmac.new(_PREVIEW_SIGNING_KEY, f"{purpose}:{view_id}".encode(),
                      hashlib.sha256).hexdigest()
    return f"apv1.{view_id}.{digest}"


def _preview_grant(token: str, purpose: str) -> dict:
    if not isinstance(token, str) or not re.fullmatch(r"apv1\.[A-Za-z0-9_-]{32}\.[0-9a-f]{64}", token):
        raise PreviewError("preview_invalid_capability")
    view_id = token.split(".")[1]
    if not hmac.compare_digest(token, _preview_token(view_id, purpose)):
        raise PreviewError("preview_invalid_capability")
    with _PREVIEW_LOCK:
        grant = _PREVIEW_GRANTS.get(view_id)
        if not grant:
            raise PreviewError("preview_invalid_capability")
        if time.monotonic() >= grant["expires"]:
            _PREVIEW_GRANTS.pop(view_id, None)
            raise PreviewError("preview_expired", 410)
        return grant


def _preview_manifest(target: dict, user: str, sid: str, session: dict) -> dict:
    """Use the already-published manifest, not a directory scan or caller scope."""
    import artifact_store
    import personal_identity
    import project_context

    expected = personal_identity.expected_export_hash(target["path"], user, sid)
    if not expected:
        raise PreviewError("preview_permission_denied")
    for row in personal_identity._registered_file_rows(target["path"], user, sid):
        try:
            ref = row.get("lineage", {}).get("export", {}).get("manifest", {})
            if ref.get("sha256") != target["snapshot_id"]:
                continue
            raw = artifact_store.read_text(ref)
            if hashlib.sha256(raw.encode()).hexdigest() != target["snapshot_id"]:
                continue
            manifest = json.loads(raw)
            if (manifest.get("user_id") != user or manifest.get("conversation_id") != sid
                    or not isinstance(manifest.get("source_root"), str)):
                continue
            scope = hashlib.sha256(json.dumps(
                [manifest[k] for k in ("user_id", "conversation_id", "team_id", "task_id", "source_root")],
                ensure_ascii=False).encode()).hexdigest()
            if scope != target["scope"]:
                continue
            team_id = manifest["team_id"]
            packet = project_context.load(team_id)
            if (packet.get("team_id") != team_id or packet.get("user_id") != user
                    or packet.get("conversation_id") != sid):
                continue
            turns = session.get("turns") or []
            exact_turns = [
                i for i, turn in enumerate(turns, 1) if isinstance(turn, dict)
                and any(personal_identity._local_reference(a) == target["path"]
                        for a in personal_identity.artifact_rows({"turns": [turn]}))]
            team_turns = [i for i, turn in enumerate(turns, 1)
                          if isinstance(turn, dict) and turn.get("team_id") == team_id]
            if not (exact_turns or team_turns):
                continue
            files = {}
            for file in manifest.get("files") or []:
                relative, digest = file["relative_path"], file["sha256"]
                candidate = _preview_target(
                    "/home-outputs/exports/" + target["scope"] + "/" + target["snapshot_id"]
                    + "/" + "/".join(quote(p, safe="") for p in relative.split("/")))
                if (candidate["scope"] != scope or candidate["snapshot_id"] != target["snapshot_id"]
                        or not re.fullmatch(r"[0-9a-f]{64}", digest) or relative in files):
                    raise ValueError("invalid manifest member")
                files[relative] = digest
            if not files or len(files) > 2048 or files.get(target["relative"]) != expected:
                continue
            return {"files": files, "source_team_id": team_id,
                    "source_turn": (exact_turns or team_turns)[-1]}
        except (KeyError, TypeError, ValueError, AttributeError, OSError, PreviewError):
            continue
    raise PreviewError("preview_unsupported", 422)


def _read_preview_file(grant: dict, target: dict) -> bytes:
    """No fallback roots; no-follow directory descriptors pin this snapshot read."""
    from contextlib import ExitStack
    import personal_identity

    if any(target[key] != grant[key] for key in ("base", "scope", "snapshot_id")):
        raise PreviewError("preview_permission_denied")
    digest = grant["files"].get(target["relative"])
    if (not digest or personal_identity.expected_export_hash(
            target["path"], grant["owner"], grant["sid"]) != digest):
        raise PreviewError("preview_permission_denied")
    try:
        with ExitStack() as opened:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(grant["base"], flags)
            opened.callback(os.close, parent)
            chain = []
            pieces = ["exports", grant["scope"], grant["snapshot_id"], *target["relative"].split("/")]
            for piece in pieces[:-1]:
                child = os.open(piece, flags, dir_fd=parent)
                opened.callback(os.close, child)
                chain.append((parent, piece, child))
                parent = child
            fd = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stream = opened.enter_context(os.fdopen(fd, "rb"))
            stamp, _ = _content_safety._inspect(stream, target["relative"])
            stream.seek(0)
            body = stream.read(stamp[2] + 1)
            for directory, name, child in chain:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                held = os.fstat(child)
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (held.st_dev, held.st_ino):
                    raise PreviewError("preview_permission_denied")
            current = os.stat(pieces[-1], dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or _content_safety._stamp(current) != stamp
                    or _content_safety._stamp(os.fstat(fd)) != stamp
                    or hashlib.sha256(body).hexdigest() != digest):
                raise PreviewError("artifact_version_mismatch", 409)
            return body
    except FileNotFoundError:
        raise PreviewError("preview_resource_not_found", 404) from None
    except _content_safety.ArtifactUnsafe:
        raise PreviewError("preview_resource_unsafe") from None
    except (OSError, ValueError, RuntimeError):
        raise PreviewError("preview_permission_denied") from None


def create_preview_grant(user: str, sid: str, url: str, *, parent_origin: str) -> dict:
    """Called only by the authenticated, same-origin parent POST."""
    import personal_identity

    session = personal_identity.owned_session(sid, user)
    if not session or session.get("sid") != sid:
        raise PreviewError("preview_permission_denied")
    target = _preview_target(url)
    url_sid = parse_qs(urlsplit(url).query).get("sid")
    if url_sid is not None and url_sid != [sid]:
        raise PreviewError("preview_permission_denied")
    manifest = _preview_manifest(target, user, sid, session)
    grant = {**target, **manifest, "owner": user, "sid": sid, "parent_origin": parent_origin,
             "nonce": secrets.token_urlsafe(18), "accepted": 0}
    # Mint only after the entry itself passes the same read-back checks as GET.
    _read_preview_file(grant, target)
    with _PREVIEW_LOCK:
        now = time.monotonic()
        for key in list(_PREVIEW_GRANTS):
            if _PREVIEW_GRANTS[key]["expires"] <= now:
                del _PREVIEW_GRANTS[key]
        if (len(_PREVIEW_GRANTS) >= _PREVIEW_MAX_GRANTS
                or sum(g["owner"] == user for g in _PREVIEW_GRANTS.values()) >= 32):
            raise PreviewError("preview_capacity", 429)
        view_id = secrets.token_urlsafe(24)
        grant.update(view_id=view_id, expires=now + _PREVIEW_TTL)
        _PREVIEW_GRANTS[view_id] = grant
    return {"ok": True, "preview_url": "/artifact-preview/" + _preview_token(view_id, "read") + target["url"],
            "preview_nonce": grant["nonce"], "observation_token": _preview_token(view_id, "observe"),
            "snapshot_id": target["snapshot_id"]}


def read_preview_resource(capability: str, url: str) -> dict:
    grant = _preview_grant(capability, "read")
    target = _preview_target(url)
    body = _read_preview_file(grant, target)
    return {"body": body, "name": pathlib.Path(target["relative"]).name,
            "preview_nonce": grant["nonce"], "preview_parent_origin": grant["parent_origin"]}


def _preview_observation_resource(value: object, grant: dict, *, kind: str = "") -> str:
    """Only a snapshot-relative file, a short API prefix, or an external host."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return ""
        resource = ""
        external = False
        if parsed.netloc:
            parent = urlsplit(grant["parent_origin"])
            scheme = parsed.scheme or parent.scheme
            host = parsed.hostname or ""
            if scheme not in {"http", "https"} or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                return ""
            origin = (scheme, host, parsed.port or (443 if scheme == "https" else 80))
            parent_origin = (parent.scheme, parent.hostname,
                             parent.port or (443 if parent.scheme == "https" else 80))
            if origin != parent_origin:
                # Check origin BEFORE inspecting paths: another host can copy
                # an API or snapshot prefix, but that does not make it local.
                resource, external = scheme + "://" + host + "/", True
        elif parsed.scheme:
            return ""
        if not external:
            if parsed.path.startswith("/api/"):
                if kind not in {"network_error", "http_error"}:
                    return ""
                # Match the literal path, before decoding. Keep only the first
                # short action segment, never query/fragment or path parameters.
                route = re.match(r"^/api/([a-z][a-z-]{1,31})(?:/|$)", parsed.path)
                if not route or re.search(
                        r"key|token|secret|password|credential|bearer|private|^sk-", route[1]):
                    return ""
                resource = "/api/" + route[1]
            else:
                path = unquote(parsed.path, errors="strict")
                if path.startswith("/artifact-preview/"):
                    path = "/" + path.split("/", 3)[3]
                prefix = f"/home-outputs/exports/{grant['scope']}/{grant['snapshot_id']}/"
                if path.startswith(prefix):
                    resource = path[len(prefix):]
                elif path.startswith("/"):
                    return ""
                else:
                    resource = path
                if (any(p in {"", ".", ".."} for p in resource.split("/"))
                        or re.search(r'[<>:"@=]', resource)):
                    return ""
        if (len(resource) > 240 or re.search(r"[\x00-\x1f\x7f\\?#%]", resource)
                or re.search(r"apv1\.[A-Za-z0-9_-]+\.[0-9a-f]+", resource, re.I)
                or grant["nonce"].lower() in resource.lower() or _content_safety.sensitive_text(resource)):
            return ""
        return resource
    except (ValueError, UnicodeError, IndexError):
        return ""


def record_preview_observation(user: str, sid: str, url: str, observation_token: str,
                               observation: dict) -> dict:
    """Append neutral, versioned facts to the same project continuation reads.

    One bounded report per project, shared by all views. No task dispatch,
    original-session rewrite, quality decision, raw browser text or credentials.
    """
    import personal_identity
    import project_context
    import sessions

    grant = _preview_grant(observation_token, "observe")
    target = _preview_target(url)
    if (grant["owner"] != user or grant["sid"] != sid or grant["url"] != target["url"]
            or grant["base"] != target["base"]
            or personal_identity.expected_export_hash(target["path"], user, sid)
            != grant["files"].get(target["relative"])):
        raise PreviewError("preview_permission_denied")
    if not isinstance(observation, dict) or observation.get("kind") not in _PREVIEW_MESSAGES:
        raise PreviewError("preview_invalid_observation", 400)
    kind = observation["kind"]
    fact = {"kind": kind, "message": _PREVIEW_MESSAGES[kind],
            "resource": _preview_observation_resource(observation.get("resource"), grant, kind=kind),
            "observed_snapshot_id": grant["snapshot_id"],
            "source_team_id": grant["source_team_id"], "source_turn": grant["source_turn"]}
    for key in ("line", "column", "status"):
        value = observation.get(key)
        if type(value) is int and ((key == "status" and (value == 0 or 100 <= value <= 599))
                                  or (key != "status" and 0 <= value <= 10_000_000)):
            fact[key] = value
    with _PREVIEW_LOCK:
        # Select at observation time: a newer legal turn may exist since mint.
        context = sessions.continuation_project_context(sid, user_id=user)
        team_id = context.get("continued_from_team_id")
        packet = project_context.load(team_id) if team_id else {}
        if (not packet or packet.get("team_id") != team_id
                or packet.get("conversation_id") != sid
                or packet.get("user_id") not in {user, "sess:" + sid}):
            raise PreviewError("preview_project_unavailable", 409)
        reports = [r for r in packet.get("verification_findings") or [] if isinstance(r, dict)]
        previous = next((r for r in reports if r.get("report_id") == _PREVIEW_REPORT_ID), {})
        rows = [r for r in previous.get("observations") or [] if isinstance(r, dict)]
        receipt = {"ok": True, "report_id": _PREVIEW_REPORT_ID, "team_id": team_id}
        if any({k: v for k, v in row.items() if k != "recorded_at"} == fact for row in rows):
            return receipt  # Do not rewrite timestamps or promote duplicates.
        if (grant["accepted"] >= _PREVIEW_VIEW_LIMIT
                or (not previous and len(reports) >= project_context._MAX_ITEMS)):
            raise PreviewError("preview_observation_limit", 429)
        # Continuation inherits this report across versions. A lifetime cap
        # would permanently blind later snapshots: rotate only observer facts,
        # retaining each surviving fact's original snapshot/turn provenance.
        rows = (rows + [{**fact, "recorded_at": time.time()}])[-_PREVIEW_REPORT_LIMIT:]
        report = {"report_id": _PREVIEW_REPORT_ID, "role": "artifact_preview_observer",
                  "trust": "untrusted_browser_observation", "observations": rows}
        saved = project_context.record_verification(team_id, report)
        if not any(r == report for r in saved.get("verification_findings") or []):
            raise PreviewError("preview_observation_unavailable", 409)
        grant["accepted"] += 1
        return receipt
