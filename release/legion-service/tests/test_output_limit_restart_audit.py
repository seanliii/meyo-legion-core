"""Read-only audit; only isolated temporary state is written. No model/network IO."""
import ast
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_explicit_responses_provider import isolated, explicit_run_one
from test_output_limit_recovery import native_failure, limited_result


def saved_output_pause(case, *, locked=True, with_dependent=False):
    import team_state as teams
    import error_envelope as errors
    case.monkeypatch.setenv('LEGION_TEAM_DIR',str(case.root/'teams'))
    case.monkeypatch.setenv('LEGION_RESUME_MAX_ATTEMPTS','4')
    tasks={'held':{'task':'original unchanged task','assigned_harness':'codex','assignment_locked':locked}}
    deps={'held':[]}
    if with_dependent:
        tasks.update(dependent={'task':'needs held output','assigned_harness':'codex','assignment_locked':True},
                     independent={'task':'independent work','assigned_harness':'codex','assignment_locked':True})
        deps.update(dependent=['held'],independent=[])
    teams.ensure('audit-team','original unchanged question',tasks,deps,
            orchestration_spec={'auto_resume':True,'pattern':'pk','adaptive':False},
            user_id='owner-a',conversation_id='same-session')
    seq=teams.begin_attempt('audit-team','held',owner='offline-audit')
    envelope=errors.output_limit_from_native(native_failure()).to_dict()
    envelope['retryable']=False
    envelope['details']['recovery_status']='unchanged_limit_after_recovery'
    assert teams.finish_attempt('audit-team','held',seq,{
        'status':'error','error':'output recovery paused','final':'','execution_calls':2,
        'error_envelope':envelope,'native_checkpoint':{'kind':'codex_thread','session_id':'same-native-thread'}})
    if with_dependent:
        teams.mark_task_blocked('audit-team','dependent','dependency not delivered',
                facts={'blocked_by':['held'],'dependency_policy':'all_succeeded'})
    return teams,teams.load('audit-team')


@pytest.mark.parametrize('locked',[True,False])
def test_restart_must_not_redispatch_output_paused_task(explicit_run_one,locked):
    case=explicit_run_one
    teams,state=saved_output_pause(case,locked=locked)
    from legion_contracts import resume_plan_from_state
    recoverable=teams.recoverable_tasks('audit-team')
    candidates=teams.list_auto_resumable()
    plan,mode=resume_plan_from_state(state)
    calls=[]
    native_calls=[]
    def execute(*args,**kwargs):
        native_calls.append(args[0])
        return limited_result(case)
    case.monkeypatch.setattr(case.core,'_run_core_inprocess',execute)
    case.monkeypatch.setattr(case.core,'_pick_relay_harness',lambda *a,**kw:None)
    def dispatch(*args,**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if locked:
            return case.core.dispatch(*args,**kwargs)
        return {'results':[]}
    tree=ast.parse(Path(case.core.__file__).with_name('server.py').read_text())
    fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_resume_incomplete_teams')
    env={'TEAMS':teams,'LEGION':SimpleNamespace(dispatch=dispatch,dispatch_persistent=dispatch),
         'SESSIONS':SimpleNamespace(save_turn=lambda *a,**kw:{}),'os':os}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual-server-boot-resume>','exec'),env)
    env[fn.name]()
    print('RESTART',json.dumps({'locked':locked,'recoverable':[r['key'] for r in recoverable],
        'candidates':[r['team_id'] for r in candidates],'boot_dispatches':len(calls),'native_attempts_after_restart':len(native_calls),
        'held_plan':plan['tasks'].get('held'),'stored_attempt_seq':state['tasks']['held']['attempt_seq']},ensure_ascii=False))
    assert calls==[], 'nonretryable output pause is redispatched on service boot'




def test_real_codex_unknown_usage_attempt_must_survive(explicit_run_one):
    case=explicit_run_one
    home=case.root/'codex-home'
    trace=home/'sessions/2026/10/21/rollout-same-native-thread.jsonl'
    trace.parent.mkdir(parents=True)
    case.monkeypatch.setenv('CODEX_HOME',str(home))
    calls=[]
    def native(command,*args,**kwargs):
        calls.append(True)
        ok=len(calls)==2
        turn='current' if ok else 'previous-unknown'
        records=[{'type':'event_msg','payload':{'type':'task_started','turn_id':turn}}]
        if ok:
            records.extend([
                {'type':'token_usage_record','payload':{
                    'thread_id':'same-native-thread','turn_id':turn,'response_id':'known-current',
                    'usage':{'input_tokens':7,'output_tokens':3,'total_tokens':10},
                    'turn_token_usage':{'input_tokens':7,'output_tokens':3,'total_tokens':10}}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':turn}},
            ])
        with trace.open('a') as stream:
            for record in records: stream.write(json.dumps(record)+'\n')
        if ok:Path(command[command.index('-o')+1]).write_text('fixture finished')
        terminal={'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':3}} if ok else native_failure()
        return SimpleNamespace(returncode=0 if ok else 1,stderr='',stdout='\n'.join([
             json.dumps({'type':'thread.started','thread_id':'same-native-thread'}),json.dumps(terminal)]))
    case.monkeypatch.setattr(case.hr,'_run_task',native)
    r=case.core.run_one('codex','original task',qhash='__ut_real_adapter_usage',
            user_id='owner-a',conversation_id='same-session',execution_depth='quick')
    assert len(calls)==2 and r['status']=='done'
    print('ACTUAL_ADAPTER_USAGE',json.dumps({'execution_calls':r['execution_calls'],'usage':r['usage']}))
    assert r['usage']['complete'] is False and r['usage']['attempts_unreported']==1


def test_unknown_dimensions_survive_team_summary(isolated):
    import token_telemetry as telemetry
    u=telemetry.aggregate([{'output_tokens':3}])
    result=telemetry.summarize([{'key':'codex','usage':u}])
    print('SUMMARY',json.dumps({'member_input':u['input_tokens'],'summary_input':result['input_tokens'],
        'coverage':result['coverage']}))
    assert result['input_tokens'] is None


def test_human_changed_request_reuses_same_conversation_without_global_ban(explicit_run_one):
    case=explicit_run_one
    calls=[]
    def native(command,*args,**kwargs):
        calls.append((list(command),kwargs['cwd']))
        ok=len(calls)==3
        if ok:Path(command[command.index('-o')+1]).write_text('fixture changed request completed')
        terminal={'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':3}} if ok else native_failure()
        return SimpleNamespace(returncode=0 if ok else 1,stderr='',stdout='\n'.join([
            json.dumps({'type':'thread.started','thread_id':'same-native-thread'}),json.dumps(terminal)]))
    case.monkeypatch.setattr(case.hr,'_run_task',native)
    work=case.root/'original-work';work.mkdir()
    facts={'project_context':{'repo_root':str(work),'user_id':'owner-a','conversation_id':'same-session'}}
    first=case.core.run_one('codex','original unchanged task',qhash='__ut_old_request',task_facts=copy.deepcopy(facts),
               user_id='owner-a',conversation_id='same-session',execution_depth='quick')
    assert first['error_envelope']['retryable'] is False and len(calls)==2
    second=case.core.run_one('codex','用户已明确调整条件：分段输出，继续原作品',qhash='__ut_changed_request',task_facts=copy.deepcopy(facts),
                user_id='owner-a',conversation_id='same-session',execution_depth='quick')
    assert second['status']=='done' and len(calls)==3
    cmd=calls[-1][0]
    assert cmd[cmd.index('resume')+1]=='same-native-thread'
    assert calls[0][1]==calls[-1][1]
    print('HUMAN_CHANGED_REQUEST',json.dumps({'first_calls':2,'new_request_calls':1,
        'same_native_thread':True,'same_workdir':True,'new_request_status':second['status']}))


def test_existing_retained_channel_keeps_graph_and_independent_work(explicit_run_one):
    case=explicit_run_one
    teams,state=saved_output_pause(case,with_dependent=True)
    calls=[]
    def execute(*args,**kwargs):
        tid=(kwargs.get('task_facts') or {}).get('task_id')
        calls.append(tid)
        assert tid=='independent','held task or its dependent was reexecuted'
        return {'status':'ok','final':'fixture independent done','error':'',
                'native_facts':{'process_returncode':0,'turn_status':'completed','turn_result':{'type':'turn.completed'}}}
    case.monkeypatch.setattr(case.core,'_run_core_inprocess',execute)
    case.monkeypatch.setattr(case.core,'_pick_relay_harness',lambda *a,**kw:None)
    plan={'team_id':'audit-team','tasks':{
       key:{'task':task['text'],'assigned_harness':task['assigned_harness'],'assignment_locked':True,
            'depends_on':task['dependencies'],'dependency_policy':task['dependency_policy']}
       for key,task in state['tasks'].items()},
       'orchestration_spec':state['orchestration_spec'],
       'max_failover':0,'team_policy':{'candidate_harnesses':['codex'],'max_parallel':1}}
    held={'held':{**teams.resolve_result('audit-team','held'),'task_id':'held','key':'held',
                  'harness':'codex','assigned_harness':'codex'}}
    out=case.core.dispatch('original unchanged question',plan=plan,qhash='audit-team',mode='pk',
               user_id='owner-a',conversation_id='same-session',_retained_results=held)
    assert calls==['independent']
    assert out['dependencies']['dependent']==['held']
    result={r['task_id']:r for r in out['results']}
    assert result['held']['error_envelope']['retryable'] is False
    assert result['dependent']['status']=='blocked'
    assert result['independent']['status']=='done'
    print('EXISTING_RETAINED_CHANNEL',json.dumps({'native_calls':calls,'dependency_preserved':True,
        'held_preserved':True,'independent_completed':True}))


def test_retryable_process_error_remains_auto_resumable(explicit_run_one):
    teams,state=saved_output_pause(explicit_run_one)
    assert teams.reopen_task('audit-team','held',text='separate retryable fixture',reason='fixture new condition')
    seq=teams.begin_attempt('audit-team','held',owner='offline-fixture')
    assert teams.finish_attempt('audit-team','held',seq,{'status':'error','error':'transient process failure',
          'error_envelope':{'category':'process','scope':'request','retryable':True,'message':'transient'}})
    from legion_contracts import resume_plan_from_state
    state=teams.load('audit-team')
    assert [r['key'] for r in teams.recoverable_tasks('audit-team')]==['held']
    assert len(teams.list_auto_resumable())==1
    assert 'held' in resume_plan_from_state(state)[0]['tasks']
