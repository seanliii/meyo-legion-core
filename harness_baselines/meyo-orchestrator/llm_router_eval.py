# -*- coding: utf-8 -*-
"""5-fold LLM harness router 真实评测。

目标:学习“什么问题适合哪个 harness”,不是聚合答案。每折只给32题训练数据(题面+topic+各家hit),
让模型为8道未见题选 harness;不提供测试题 gold/hit。5次调用完成40题 out-of-fold 预测。
若 OOF > 最强固定单家,才证明路由有真实增益。
"""
from __future__ import annotations
import json, glob, re, urllib.request
from pathlib import Path

_HERE=Path(__file__).resolve().parent
_FB=_HERE.parent/'fair_bench_40'
HS=['auto','cc','codex','deepagents','deepseek','hermes']
GW="https://aigc.sankuai.com/v1/openai/native/chat/completions"
APPID = os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID", "")
MODELS=("gpt-5.6-sol","GLM-5.3")


def _load():
    m=json.loads((_FB/'manifest_40.json').read_text())
    qs={q['qid']:q for q in m['questions']}
    hits={q:{} for q in qs}
    for p in glob.glob(str(_FB/'traces/*/*/latest.json')):
        try:
            d=json.loads(Path(p).read_text());h=d['harness'];q=d['qid']
            if q in hits and h in HS:hits[q][h]=bool(d['evaluation'].get('hit'))
        except:continue
    return list(qs.values()),hits


def _post(prompt):
    if not APPID or APPID.startswith("appid-"):
        raise ValueError("Configure a resolved MEYO_UNIFIED_APPID or AIGC_APPID")
    for model in MODELS:
        try:
            body={'model':model,'messages':[{'role':'system','content':
              '你是harness路由学习器。根据训练样本学习各harness在不同问题结构上的成功模式。'
              '对测试题只选一个最可能成功的harness。严格输出JSON对象 {qid:harness}，不解释。'},
              {'role':'user','content':prompt}],'max_tokens':2000}
            req=urllib.request.Request(GW,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':f'Bearer {APPID}'})
            r=json.loads(urllib.request.urlopen(req,timeout=240).read())
            return r['choices'][0]['message']['content'] or ''
        except Exception:continue
    return '{}'


def _extract(text):
    try:return json.loads(text)
    except:
        m=re.search(r'\{.*\}',text,re.S)
        try:return json.loads(m.group()) if m else {}
        except:return {}


def run(k=5):
    qs,hits=_load();pred={}
    for fold in range(k):
        test=[q for i,q in enumerate(qs) if i%k==fold]
        train=[q for i,q in enumerate(qs) if i%k!=fold]
        tr=[]
        for q in train:
            tr.append({'question':q['problem'],'topic':q.get('topic'),
                       'success':[h for h in HS if hits[q['qid']].get(h)]})
        te=[{'qid':q['qid'],'question':q['problem'],'topic':q.get('topic')} for q in test]
        prompt=json.dumps({'available_harnesses':HS,'training_examples':tr,'test_questions':te},ensure_ascii=False)
        out=_extract(_post(prompt))
        for q in test:
            p=out.get(q['qid'],'cc')
            pred[q['qid']]=p if p in HS else 'cc'
        print('fold',fold,'pred',[(q['qid'],pred[q['qid']]) for q in test],flush=True)
    score=sum(hits[q['qid']].get(pred[q['qid']],False) for q in qs)
    fixed={h:sum(hits[q['qid']].get(h,False) for q in qs) for h in HS}
    best=max(fixed,key=fixed.get)
    union=sum(any(hits[q['qid']].get(h,False) for h in HS) for q in qs)
    res={'n':len(qs),'router_hits':score,'predictions':pred,'fixed_scores':fixed,
         'best_fixed':best,'best_fixed_hits':fixed[best],'union':union,
         'beats_best_fixed':score>fixed[best]}
    (_HERE/'captain_traces/llm_router_5fold_eval.json').write_text(json.dumps(res,ensure_ascii=False,indent=2))
    print('RESULT router',score,'/',len(qs),'best',best,fixed[best],'union',union,'beats=',res['beats_best_fixed'])
    return res

if __name__=='__main__':run()
