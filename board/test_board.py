#!/usr/bin/env python3
"""Behavioral regression tests, including encrypted Git transport and posting races."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import board
import board_transport as bt
from board_workflow import reduce_workflow, COARSE
from board_schema import errors, check_schema, history_errors

NOW=dt.datetime.now(dt.timezone.utc)
SCHEMA=json.loads(board.SCHEMA_PATH.read_text())

def event(kind, seq, *, agent=None, reply=None, lease=12, thread='2026-09-06-example'):
    agent=agent or ('claude' if kind in {'request','accept','revise','abandoned'} else 'codex')
    t=NOW+dt.timedelta(seconds=seq)
    e,_=board.view.build_template(kind='update',project='example',thread=thread,task='Example',agent=agent,repo='o/example',restricted=False,lease_hours=12,summary='Example '+kind,now=t)
    e.update(schema_version=2,kind=kind,event_id=f'{board.view.compact(t)}-{agent}-{seq:06x}',request_id=f'{board.view.compact(NOW)}-claude-000000')
    e['actor']['instance']=agent+'@fixture'
    e['actor']['session_id']=agent+'-session-001'
    if kind=='request': e.update(assignee={'agent':'codex','session_id':'codex-session-001'},acceptance='Check the result independently.')
    if kind=='claim': e['lease_until']=board.view.iso_z(t+dt.timedelta(hours=lease))
    if reply: e['reply_to']=reply['event_id']
    if kind=='submit': e['deliverables']=['results/example.md']
    if kind=='accept': e['references']=['results/independent-review.md']
    return e

def chain():
    r=event('request',0); c=event('claim',1); sub=event('submit',2,reply=c)
    return r,c,sub

class TargetModel(unittest.TestCase):
    """request/handover print the addressed session's actual model (transcript) and --expect-model can refuse."""
    def _fixture(self):
        td=tempfile.TemporaryDirectory(); root=Path(td.name)
        (root/'sessions').mkdir(); (root/'projects'/'-w-p').mkdir(parents=True)
        sid='0123abcd-0000-4000-8000-000000000001'
        (root/'sessions'/'1.json').write_text(json.dumps({'pid':__import__('os').getpid(),'sessionId':sid,'cwd':'/w/p','name':'fixture session','hostSessionId':'local_fixture','status':'idle'}))
        (root/'projects'/'-w-p'/f'{sid}.jsonl').write_text(json.dumps({'type':'assistant','message':{'role':'assistant','model':'claude-opus-5-5'},'timestamp':'2026-01-01T00:00:00Z'})+'\n')
        return td, sid
    def test_note_and_expect(self):
        import os
        td,sid=self._fixture(); saved={k:os.environ.get(k) for k in ('CLAUDE_SESSIONS_DIR','CLAUDE_PROJECTS_DIR')}
        os.environ['CLAUDE_SESSIONS_DIR']=str(Path(td.name)/'sessions'); os.environ['CLAUDE_PROJECTS_DIR']=str(Path(td.name)/'projects')
        try:
            if not (board.ENGINE.parent.parent/'claude-config'/'scripts'/'lib'/'session_model.py').is_file():
                self.skipTest('layer-1 session_model.py not installed')
            note=board.target_model_note(sid)
            self.assertIn('claude-opus-5-5',note); self.assertIn('tier opus',note)
            self.assertIn('claude-opus-5-5',board.target_model_note(sid,'opus'))          # tier matches
            self.assertIn('claude-opus-5-5',board.target_model_note(sid,'opus-5-5'))      # substring matches
            with self.assertRaises(ValueError): board.target_model_note(sid,'fable')       # mismatch refuses
            self.assertIn('役割 id',board.target_model_note('role-x-y','fable'))           # a role has no model yet
            self.assertIn('記録に無い',board.target_model_note('ffffffff-0000-4000-8000-000000000009','fable'))  # unknown: reported, not refused
            self.assertIsNone(board.target_model_note(None))
        finally:
            for k,v in saved.items():
                if v is None: os.environ.pop(k,None)
                else: os.environ[k]=v
            td.cleanup()


class CounterpartAddress(unittest.TestCase):
    """After a post, the session that must act next gets a SendMessage address when it is alive on this machine,
    whatever config dir it registered in (a session started through an account-pinned server is not reachable by name)."""
    def test_reviewer_and_role_resolve_to_socket_address(self):
        import os
        if not (board.ENGINE.parent.parent/'claude-config'/'scripts'/'lib'/'session_model.py').is_file():
            self.skipTest('layer-1 session_model.py not installed')
        td=tempfile.TemporaryDirectory(); root=Path(td.name); (root/'sessions').mkdir()
        pid=os.getpid()
        (root/'sessions'/'1.json').write_text(json.dumps({'pid':pid,'sessionId':'claude-session-001','cwd':'/w/p','messagingSocketPath':'/tmp/cc-socks/41.sock'}))
        (root/'sessions'/'2.json').write_text(json.dumps({'pid':pid,'sessionId':'0123abcd-0000-4000-8000-000000000002','cwd':'/w/p','messagingSocketPath':'/tmp/cc-socks/42.sock'}))
        saved=os.environ.get('CLAUDE_SESSIONS_DIR'); os.environ['CLAUDE_SESSIONS_DIR']=str(root/'sessions')
        try:
            r,c,sub=chain()
            note=board.counterpart_note(('codex','codex-session-001'),sub,[r,c])
            self.assertIn('uds:/tmp/cc-socks/41.sock',note)                         # submit → the reviewer
            self.assertIsNone(board.counterpart_note(('claude','claude-session-001'),sub,[r,c]))   # never yourself
            self.assertIsNone(board.counterpart_note(('claude','claude-session-001'),c,[r]))      # claim waits on the codex claimant
            # a role id resolves through the native id prefix the acting session named itself with when it claimed
            r2=event('request',10,thread='2026-09-06-role'); r2['assignee']={'agent':'claude','session_id':'role-example-x'}
            c2=event('claim',11,agent='claude',thread='2026-09-06-role'); c2['actor']['session_id']='role-example-x'
            c2['actor']['task']='0123abcd (opus-5-5)'; c2['request_id']=r2['request_id']
            self.assertEqual(board.live_address('claude','role-example-x',[r2,c2]),'uds:/tmp/cc-socks/42.sock')
            self.assertEqual(board.live_address('claude','role-example-y',[r2,c2]),'')  # no named claim yet → unknown
            self.assertEqual(board.live_address('codex','codex-session-001',[]),'')     # another vendor → unknown
            (root/'sessions'/'1.json').write_text(json.dumps({'pid':2**30,'sessionId':'claude-session-001','messagingSocketPath':'/tmp/cc-socks/41.sock'}))
            self.assertIsNone(board.counterpart_note(('codex','codex-session-001'),sub,[r,c]))  # not alive → nothing
        finally:
            if saved is None: os.environ.pop('CLAUDE_SESSIONS_DIR',None)
            else: os.environ['CLAUDE_SESSIONS_DIR']=saved
            td.cleanup()


class CodexCounterpart(unittest.TestCase):
    """A Codex thread has no SendMessage: after a post the poster gets the `codex queue` command for the Codex thread
    that must act next (alive = its writer lock is held open), and a Codex poster is not told to SendMessage."""
    TID='01a00000-0000-7000-8000-0000000000aa'
    def setUp(self):
        import os, sqlite3
        if not (board.ENGINE.parent.parent/'claude-config'/'scripts'/'lib'/'codex_threads.py').is_file():
            self.skipTest('layer-1 codex_threads.py not installed')
        self.td=tempfile.TemporaryDirectory(); home=Path(self.td.name)
        (home/'thread-writer-locks').mkdir(); self.lock=home/'thread-writer-locks'/f'{self.TID}.lock'; self.lock.write_text('')
        db=sqlite3.connect(home/'state_5.sqlite')
        db.execute('CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, model TEXT, reasoning_effort TEXT, source TEXT, updated_at INTEGER, archived INTEGER)')
        db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)',(self.TID,'/w/p','fixture','codex-model-x','high','vscode',1,0)); db.commit(); db.close()
        exe=home/'codex'; exe.write_text('#!/bin/sh\n'); exe.chmod(0o755)
        self.saved={k:os.environ.get(k) for k in ('CODEX_HOME','CODEX_CLI_PATH','CLAUDE_SESSIONS_DIR')}
        os.environ['CODEX_HOME']=str(home); os.environ['CODEX_CLI_PATH']=str(exe)
        os.environ['CLAUDE_SESSIONS_DIR']=str(home/'no-claude-sessions')
    def tearDown(self):
        import os
        for k,v in self.saved.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
        self.td.cleanup()
    def request_to(self, session):
        r=event('request',0); r['assignee']={'agent':'codex','session_id':session}; return r
    def test_queue_command_for_the_codex_thread(self):
        r=self.request_to(self.TID)
        note=board.counterpart_note(('claude','claude-session-001'),r,[])
        self.assertIn(f'queue --thread {self.TID}',note); self.assertIn('今は開いていない',note)   # lock not held
        self.assertIn('--sync',note)                                                               # the line says how to read
        with open(self.lock):                                                                      # a client holds the lock
            if board._codex_threads().held_lock_ids() is None: self.skipTest('lsof unavailable')
            self.assertIn('生きている',board.counterpart_note(('claude','claude-session-001'),r,[]))
    def test_role_resolves_through_the_claimant_name(self):
        r=self.request_to('role-example-codex'); c=event('claim',1); c['actor']['session_id']='role-example-codex'
        c['actor']['task']=self.TID[:8]+' (codex-model-x)'; c['request_id']=r['request_id']
        blk=event('blocker',2,reply=c); blk['actor']['session_id']='role-example-codex'
        ans=event('update',3,agent='claude',reply=blk)
        note=board.counterpart_note(('claude','claude-session-001'),ans,[r,c,blk])
        self.assertIn(f'queue --thread {self.TID}',note); self.assertIn('--session role-example-codex',note)
    def test_note_inside_a_request_reaches_the_other_participant(self):
        # measured: the requester's note answering a blocker printed no line, and the worker was not told
        r=self.request_to(self.TID); c=event('claim',1); c['actor']['session_id']=self.TID
        blk=event('blocker',2,reply=c); blk['actor']['session_id']=self.TID
        n=event('note',3,agent='claude',reply=blk)
        note=board.counterpart_note(('claude','claude-session-001'),n,[r,c,blk])
        self.assertIn(f'queue --thread {self.TID}',note); self.assertIn('相手の',note)
    def test_unknown_thread_is_silent(self):
        r=self.request_to('01a00000-0000-7000-8000-0000000000bb')   # not in this machine's state, no lock
        self.assertIsNone(board.counterpart_note(('claude','claude-session-001'),r,[]))
        self.assertIsNone(board.counterpart_note(('claude','claude-session-001'),self.request_to('codex-session-001'),[]))
    def test_codex_poster_is_not_told_to_sendmessage(self):
        import os
        sess=Path(self.td.name)/'claude-sessions'; sess.mkdir()
        (sess/'1.json').write_text(json.dumps({'pid':os.getpid(),'sessionId':'claude-session-001','cwd':'/w/p','messagingSocketPath':'/tmp/cc-socks/41.sock'}))
        os.environ['CLAUDE_SESSIONS_DIR']=str(sess)
        if not (board.ENGINE.parent.parent/'claude-config'/'scripts'/'lib'/'session_model.py').is_file():
            self.skipTest('layer-1 session_model.py not installed')
        r,c,sub=chain()
        note=board.counterpart_note(('codex','codex-session-001'),sub,[r,c])
        self.assertIn('uds:/tmp/cc-socks/41.sock',note); self.assertIn('SendMessage が無い',note)
        self.assertNotIn('SendMessage の to',note)
    def test_target_model_of_a_codex_thread(self):
        self.assertIn('codex-model-x effort high',board.target_model_note(self.TID,None,'codex'))
        self.assertIn('codex-model-x',board.target_model_note(self.TID,'model-x','codex'))
        with self.assertRaises(ValueError): board.target_model_note(self.TID,'fable','codex')
        self.assertIn('記録に無い',board.target_model_note('01a00000-0000-7000-8000-0000000000bb','fable','codex'))


class Workflow(unittest.TestCase):
    def state(self,events,now=None): return reduce_workflow(events,now or NOW+dt.timedelta(seconds=30))
    def test_delivery_receipt_and_revision(self):
        r,c,sub=chain()
        for events,status,who in [([r],'requested','codex'),([r,c],'working','codex'),([r,c,sub],'submitted','claude')]:
            s=self.state(events); self.assertFalse(s['errors']); self.assertEqual((s['status'],s['waiting_on']['agent']),(status,who))
        rev=event('revise',3,reply=sub); sub2=event('submit',4,reply=c); acc=event('accept',5,reply=sub2)
        self.assertEqual(self.state([r,c,sub,rev])['status'],'revision')
        s=self.state([r,c,sub,rev,sub2,acc]); self.assertFalse(s['errors']); self.assertEqual(s['status'],'accepted'); self.assertIsNone(s['waiting_on'])
    def test_self_accept_wrong_reference_and_legacy_done(self):
        r,c,sub=chain()
        for bad in [event('accept',3,agent='codex',reply=sub),event('accept',3,reply=c),event('done',3)]:
            s=self.state([r,c,sub,bad]); self.assertTrue(s['errors']); self.assertEqual(s['status'],'submitted')
        bad=event('done',3); bad['schema_version']=1
        s=self.state([r,c,sub,bad]); self.assertTrue(s['errors']); self.assertEqual(s['waiting_on']['agent'],'claude')
    def test_claim_race_expiry_and_release(self):
        r,c,sub=chain(); c2=event('claim',3); c2['actor']['session_id']='codex-session-002'
        self.assertTrue(self.state([r,c,c2])['errors'])
        self.assertEqual(self.state([r,c],NOW+dt.timedelta(days=1))['status'],'stale')
        rel=event('release',2,reply=c)
        c2['actor']['session_id']='codex-session-001'
        self.assertFalse(self.state([r,c,rel,c2])['errors'])
        late=event('submit',24*3600,reply=c)
        self.assertTrue(self.state([r,c,late],NOW+dt.timedelta(days=2))['errors'])
        self.assertEqual(self.state([r,c,sub],NOW+dt.timedelta(days=2))['status'],'submitted')
    def test_blocker_return(self):
        r,c,_=chain(); b=event('blocker',2); response=event('update',3,agent='claude',reply=b)
        self.assertEqual(self.state([r,c,b])['waiting_on']['agent'],'claude')
        self.assertEqual(self.state([r,c,b,response])['status'],'working')

    def test_answer_to_an_answered_blocker_names_the_open_one(self):
        # measured: the requester answered the first (already answered) blocker while a second one was open
        r,c,_=chain(); b1=event('blocker',2,reply=c); a1=event('update',3,agent='claude',reply=b1)
        b2=event('blocker',4,reply=c); stale=event('update',5,agent='claude',reply=b1)
        errs=self.state([r,c,b1,a1,b2,stale])['errors']
        self.assertEqual(len(errs),1); self.assertIn('the open blocker is '+b2['event_id'],errs[0])
        self.assertIn('update --reply-to '+b2['event_id'],errs[0])

    def test_expired_worker_lease_keeps_unanswered_question_with_reviewer(self):
        r,c,_=chain(); q=event('blocker',2)
        late=NOW+dt.timedelta(days=2)
        w=self.state([r,c,q],late)
        self.assertEqual(w['status'],'blocked')
        self.assertTrue(w['claim_expired'])
        self.assertEqual(w['waiting_on'],{'agent':'claude','session_id':'claude-session-001'})
        t=board.view.derive([r,c,q],late)['example',r['thread_id']]
        self.assertEqual(t['active_claims'],[])
        self.assertEqual(t['stale_claims'],[c])
        row=board.view.workflow_row('example',r['thread_id'],t,reader=w['waiting_on'])
        self.assertTrue(row['actionable'])
        self.assertTrue(row['claim_expired'])
        self.assertEqual(row['reply_to'],q['event_id'])
        self.assertEqual(row['lease_until'],c['lease_until'])

    def test_reclaim_cannot_answer_or_bypass_an_open_question(self):
        r,c,_=chain(); q=event('blocker',2)
        renewed=event('claim',2*86400)
        w=self.state([r,c,q,renewed],NOW+dt.timedelta(days=2,seconds=10))
        self.assertFalse(w['errors'])
        self.assertFalse(w['claim_expired'])
        self.assertEqual(w['status'],'blocked')
        self.assertEqual(w['blocker'],q)
        self.assertIsNone(w['answer'])
        bad=event('submit',2*86400+1,reply=renewed)
        w=self.state([r,c,q,renewed,bad],NOW+dt.timedelta(days=2,seconds=10))
        self.assertTrue(w['errors'])
        self.assertIsNone(w['submission'])
        self.assertEqual(w['status'],'blocked')

    def test_release_and_handover_preserve_question_until_explicit_reply(self):
        r,c,_=chain(); q=event('blocker',2)
        released=event('release',3,reply=c)
        w=self.state([r,c,q,released])
        self.assertFalse(w['errors']); self.assertIsNone(w['claim'])
        self.assertEqual(w['status'],'blocked'); self.assertEqual(w['blocker'],q)
        answer=event('update',4,agent='claude',reply=q)
        w=self.state([r,c,q,released,answer])
        self.assertEqual(w['status'],'requested'); self.assertEqual(w['answer'],answer)
        hand=event('handover',3,agent='claude',reply=r)
        hand.update(handover_role='assignee',target={'agent':'codex','session_id':'codex-replacement'})
        w=self.state([r,c,q,hand])
        self.assertEqual(w['status'],'blocked'); self.assertEqual(w['blocker'],q)
        self.assertEqual(w['waiting_on']['session_id'],'claude-session-001')
        w=self.state([r,c,q,hand,answer])
        self.assertFalse(w['errors']); self.assertEqual(w['status'],'requested')
        self.assertEqual(w['waiting_on']['session_id'],'codex-replacement')

    def test_late_answer_requires_reclaim_but_late_receipt_does_not(self):
        r,c,sub=chain(); q=event('blocker',2)
        hand=event('handover',3,agent='claude',reply=r)
        hand.update(handover_role='reviewer',target={'agent':'claude','session_id':'claude-new-reviewer'})
        late=NOW+dt.timedelta(days=2,seconds=10)
        w=self.state([r,c,q,hand],late)
        self.assertEqual(w['status'],'blocked')
        self.assertEqual(w['waiting_on']['session_id'],'claude-new-reviewer')
        answer=event('update',2*86400,agent='claude',reply=q)
        answer['actor']['session_id']='claude-new-reviewer'
        w=self.state([r,c,q,hand,answer],late)
        self.assertFalse(w['errors']); self.assertEqual(w['status'],'stale')
        self.assertEqual(w['waiting_on']['agent'],'codex'); self.assertEqual(w['answer'],answer)
        renewed=event('claim',2*86400+1)
        submitted=event('submit',2*86400+2,reply=renewed)
        w=self.state([r,c,q,hand,answer,renewed,submitted],late)
        self.assertFalse(w['errors']); self.assertEqual(w['status'],'submitted')
        # A result submitted under a live claim still awaits receipt after expiry.
        w=self.state([r,c,sub],late)
        self.assertEqual(w['status'],'submitted'); self.assertTrue(w['claim_expired'])
        self.assertEqual(w['waiting_on']['agent'],'claude')
        self.assertEqual(self.state([r,c,sub,event('accept',2*86400,reply=sub)],late)['status'],'accepted')

    def test_handoff_packet_retains_question_answer_and_submission_evidence(self):
        r,c,_=chain(); sub=event('submit',4,reply=c)
        r['references']=['spec.md']; sub['references']=['commit/worker-result']
        sub['summary']='Correction: draft is not author approval.'
        b=event('blocker',2); b['summary']='Is conditional probability assumed?'
        answer=event('update',3,agent='claude',reply=b); answer['summary']='Explain it in the coin example.'
        def row(events):
            state=board.view.derive(events,NOW+dt.timedelta(seconds=30))['example',r['thread_id']]
            return board.view.workflow_row('example',r['thread_id'],state,
                reader={'agent':'claude','session_id':'claude-session-001'})
        blocked=row([r,c,b])
        self.assertEqual(blocked['question']['summary'],b['summary'])
        self.assertIsNone(blocked['answer'])
        answered=row([r,c,b,answer])
        self.assertEqual(answered['answer']['summary'],answer['summary'])
        self.assertEqual(answered['answer']['reply_to'],b['event_id'])
        submitted=row([r,c,b,answer,sub])
        self.assertEqual(submitted['submission']['references'],['commit/worker-result'])
        self.assertEqual(submitted['references'],['spec.md'])
        self.assertEqual(submitted['submission']['summary'],sub['summary'])
        self.assertTrue(submitted['actionable'])
        # An inert note cannot replace a question's actual answer or a submission.
        n=event('note',6,agent='claude'); n['summary']='An unrelated status note.'
        noted=row([r,c,b,answer,sub,n])
        self.assertEqual(noted['answer'],submitted['answer'])
        self.assertEqual(noted['submission'],submitted['submission'])
        self.assertIn(answer['summary'],'\n'.join(board.view.handoff_lines(noted)))

    def test_resubmission_keeps_review_target_and_ignores_fake_receipt(self):
        r,c,sub=chain(); rev=event('revise',3,reply=sub)
        rev['summary']='Clarify the assumptions.'; rev['references']=['review/first']
        sub2=event('submit',4,reply=c); sub2['references']=['commit/second']; sub2['deliverables']=['results/second.md']
        events=[r,c,sub,rev,sub2]
        def state(): return board.view.derive(events,NOW+dt.timedelta(seconds=30))['example',r['thread_id']]
        w=state()['workflow']
        self.assertEqual(w['status'],'submitted')
        self.assertEqual(w['submission']['event_id'],sub2['event_id'])
        self.assertEqual(w['review']['reply_to'],sub['event_id'])
        acc=event('accept',5,reply=sub2); events.append(acc)
        w=state()['workflow']
        self.assertEqual(w['review']['event_id'],acc['event_id'])
        self.assertEqual(w['review']['reply_to'],sub2['event_id'])
        bad=event('accept',6,agent='codex',reply=sub2); events.append(bad)
        broken=state()
        self.assertEqual(broken['workflow']['status'],'invalid')
        self.assertEqual(broken['workflow']['review']['event_id'],acc['event_id'])
        self.assertFalse(board.view.workflow_row('example',r['thread_id'],broken,
            reader={'agent':'codex','session_id':'codex-session-001'})['actionable'])
    def test_sessions_not_vendor_and_explicit_handover(self):
        r,c,sub=chain()
        wrong=event('accept',3,reply=sub); wrong['actor']['session_id']='claude-another-session'
        s=self.state([r,c,sub,wrong]); self.assertTrue(s['errors']); self.assertEqual(s['status'],'submitted')
        hand=event('handover',4,agent='claude',reply=r)
        hand.update(handover_role='reviewer',target={'agent':'claude','session_id':'claude-another-session'})
        s=self.state([r,c,sub,hand]); self.assertFalse(s['errors']); self.assertEqual(s['waiting_on']['session_id'],'claude-another-session')
        accepted=event('accept',5,reply=sub); accepted['actor']['session_id']='claude-another-session'
        self.assertEqual(self.state([r,c,sub,hand,accepted])['status'],'accepted')
        # Different sessions of the same vendor are legitimate participants.
        same_vendor=copy.deepcopy(r); same_vendor['assignee']={'agent':'claude','session_id':'claude-worker-session'}
        worker=event('claim',1,agent='claude'); worker['actor']['session_id']='claude-worker-session'
        self.assertFalse(self.state([same_vendor,worker])['errors'])
        takeover=event('claim',2,agent='claude'); takeover['actor']['session_id']='claude-third-session'
        self.assertTrue(self.state([same_vendor,worker,takeover])['errors'])
        # Owner can recover an unavailable reviewer, explicitly and visibly.
        owner=copy.deepcopy(hand); owner['actor'].update(agent='human',session_id='owner-session')
        self.assertFalse(self.state([r,c,sub,owner])['errors'])
        # Same session can continue on a different host/surface.
        moved=copy.deepcopy(sub); moved['actor']['instance']='codex@another-host'
        self.assertFalse(self.state([r,c,moved])['errors'])
        # Handing the work to another session revokes the former claim.
        transfer=event('handover',2,agent='claude',reply=r)
        transfer.update(handover_role='assignee',target={'agent':'codex','session_id':'codex-replacement'})
        self.assertEqual(self.state([r,c,transfer])['waiting_on']['session_id'],'codex-replacement')
        self.assertTrue(self.state([r,c,transfer,sub])['errors'])

    def test_schema_privacy_and_required_fields(self):
        check_schema(SCHEMA)
        for e in chain(): self.assertEqual(errors(e,SCHEMA),[])
        e=event('request',0); del e['acceptance']; self.assertTrue(errors(e,SCHEMA))
        e=event('request',0); e.update(source_policy='encrypted-metadata-only',summary=COARSE['request'],acceptance='Confirm completion in source.')
        e['project']={'key':'restricted','repo':None}; e['actor']['task']='restricted-task'; e.pop('details')
        self.assertEqual(errors(e,SCHEMA),[])
        e['deliverables']=['secret/path']; self.assertTrue(errors(e,SCHEMA))
        _,_,sub=chain(); accept=event('accept',3,reply=sub); accept['references']=[]
        self.assertTrue(errors(accept,SCHEMA))
        accept.update(source_policy='encrypted-metadata-only',summary=COARSE['accept'])
        accept['project']={'key':'restricted','repo':None}; accept['actor']['task']='restricted-task'
        accept.pop('details')
        self.assertEqual(errors(accept,SCHEMA),[])
    def test_thread_isolation(self):
        r,c,sub=chain(); other=event('request',0,thread='2026-09-06-other'); other['event_id']='20260906T010000Z-claude-aaaaaa'; other['request_id']=other['event_id']
        t=board.view.derive([r,c,sub,other],NOW+dt.timedelta(seconds=30))
        self.assertFalse(t['example','2026-09-06-example']['closed'])
        self.assertEqual(t['example','2026-09-06-other']['workflow']['status'],'requested')

class Note(unittest.TestCase):
    def test_note_is_inert_in_request_thread(self):
        r,c,sub=chain(); n=event('note',5,agent='codex')
        w=reduce_workflow([r,c,n],NOW+dt.timedelta(seconds=30))
        self.assertEqual(w['errors'],[]); self.assertEqual(w['status'],'working'); self.assertIn(n['event_id'],w['accepted_ids'])
        w2=reduce_workflow([r,c,sub,n],NOW+dt.timedelta(seconds=30)); self.assertEqual(w2['status'],'submitted')
    def test_note_only_thread_is_open_not_invalid(self):
        n=event('note',0,agent='codex',thread='2026-09-07-status'); n['request_id']=n['event_id']
        self.assertEqual(errors(n,SCHEMA),[])
        w=reduce_workflow([n],NOW); self.assertEqual(w['errors'],[]); self.assertIsNone(w['request'])
        threads=board.view.derive([n],NOW); t=threads[('example','2026-09-07-status')]
        self.assertNotIn('workflow',t); self.assertFalse(t['closed']); self.assertEqual(t['live'],[n])
        self.assertNotIn('要確認',board.view.render(threads,[],surface=False,project=None,now=NOW,locked=False))
    def test_note_joins_legacy_status_thread_without_invalidating_it(self):
        legacy,_=board.view.build_template(kind='update',project='example',thread='2026-09-07-legacy',task='t',agent='codex',repo='o/example',restricted=False,lease_hours=12,summary='legacy status',now=NOW)
        n=event('note',1,agent='claude',thread='2026-09-07-legacy'); n['request_id']=n['event_id']
        w=reduce_workflow([legacy,n],NOW+dt.timedelta(seconds=5)); self.assertEqual(w['errors'],[]); self.assertIsNone(w['request'])
        t=board.view.derive([legacy,n],NOW+dt.timedelta(seconds=5))[('example','2026-09-07-legacy')]
        self.assertNotIn('workflow',t); self.assertEqual(len(t['live']),2)
        # but a legacy 'done' still cannot close a v2 request
        r,c,sub=chain(); done,_=board.view.build_template(kind='done',project='example',thread='2026-09-06-example',task='t',agent='codex',repo='o/example',restricted=False,lease_hours=12,summary='done',now=NOW+dt.timedelta(seconds=9))
        w2=reduce_workflow([r,c,sub,done],NOW+dt.timedelta(seconds=30)); self.assertEqual(w2['status'],'submitted'); self.assertTrue(w2['errors'])
    def test_empty_backticks_in_summary_are_refused(self):
        import argparse
        a=argparse.Namespace(command='note',policy='ordinary',source=Path('/nonexistent/example'),project='example',thread='2026-09-07-x',repo='o/example',
            request=None,summary='thread `` vanished',acceptance=None,agent='claude',session='s1',session_name=None,instance=None,event_id=None,
            reply_to=None,lease_hours=12,reference=[],deliverable=[],to=None,to_session=None,role=None)
        with self.assertRaises(ValueError) as cm: board.event_from(a,[])
        self.assertIn('backticks',str(cm.exception))
    def test_accept_without_reference_is_refused_before_posting(self):
        # The schema needs >= 1 reference on an ordinary accept; without this check the post died with the bare
        # "$.references: too few items" after the summary had been written (measured).
        import argparse
        r,c,sub=chain()
        a=argparse.Namespace(command='accept',policy='ordinary',source=Path('/nonexistent/example'),project=None,thread=None,repo=None,
            request=r['event_id'],summary='checked',acceptance=None,agent='claude',session='claude-session-001',session_name=None,instance=None,
            event_id=None,reply_to=sub['event_id'],lease_hours=12,reference=[],deliverable=[],to=None,to_session=None,role=None)
        with self.assertRaises(ValueError) as cm: board.event_from(a,[r,c,sub])
        self.assertIn('--reference',str(cm.exception))
        a.reference=['results/independent-review.md']
        self.assertEqual(board.event_from(a,[r,c,sub])['references'],['results/independent-review.md'])
    def test_actor_task_is_session_name_not_summary(self):
        import argparse
        base=dict(command='note',policy='ordinary',source=Path('/nonexistent/example'),project='example',thread='2026-09-07-x',repo='o/example',
            request=None,summary='a long status summary that must not become the session name',acceptance=None,agent='claude',session='s1',
            instance=None,event_id=None,reply_to=None,lease_hours=12,reference=[],deliverable=[],to=None,to_session=None,role=None)
        ev=board.event_from(argparse.Namespace(session_name=None,**base),[]); self.assertEqual(ev['actor']['task'],'workflow')
        ev=board.event_from(argparse.Namespace(session_name='notation audit (codex)',**base),[]); self.assertEqual(ev['actor']['task'],'notation audit (codex)')
    def test_review_request_needs_a_referee_copy_or_not_blind(self):
        # A deny list in the spec did not stop a header comment of the target that recorded the previous round's
        # verdict (measured): a request that reads like a review names a comment-free copy, or says --not-blind.
        import argparse
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/'note.tex'; raw.write_text('% Blind review: verdict incorrect; see plans/x-results.md\n\\section{A}\n')
            clean=Path(td)/'note-referee.tex'; clean.write_text('\\section{A} %\ntext\n')
            def ns(**kw):
                base=dict(command='request',policy='ordinary',source=Path('/nonexistent/example'),project='example',
                    thread='2026-09-06-target',repo='o/example',request=None,summary='査読: note の盲検 (第 1 段は自分の目で)',
                    acceptance='ok',agent='claude',session='s1',session_name=None,instance=None,event_id=None,reply_to=None,
                    lease_hours=12,reference=['spec.md'],deliverable=[],to='codex',to_session='x-1',role=None,
                    review_target=[],not_blind=False)
                base.update(kw); return argparse.Namespace(**base)
            with self.assertRaises(ValueError) as cm: board.event_from(ns(),[])
            self.assertIn('--review-target',str(cm.exception))
            with self.assertRaises(ValueError) as cm: board.event_from(ns(review_target=[str(raw)]),[])
            self.assertIn('referee copy',str(cm.exception)); self.assertNotIn('incorrect',str(cm.exception))
            ev=board.event_from(ns(review_target=[str(clean)]),[])
            self.assertEqual(ev['references'],['spec.md',str(clean)])
            self.assertEqual(board.event_from(ns(not_blind=True),[])['references'],['spec.md'])
            with self.assertRaises(ValueError): board.event_from(ns(summary='Please review the hook'),[])
            board.event_from(ns(summary='CI を直す'),[])                                    # not a review: no gate
            board.event_from(ns(agent='human',session='discord:1'),[])                     # a person's chat request
            board.event_from(ns(policy='encrypted-metadata-only',project=None,thread='r-0123456789ab',repo=None,
                                summary=None,acceptance=None,reference=[]),[])   # restricted: coarse summary, no gate
            # Namespaces built before the gate existed (other callers, older tests) still post non-review requests
            old=argparse.Namespace(**{k:v for k,v in vars(ns(summary='CI を直す')).items() if k not in ('review_target','not_blind')})
            board.event_from(old,[])
    def test_restricted_note_summary_allowed(self):
        n=event('note',0,agent='codex',thread='r-0123456789ab'); n['request_id']=n['event_id']
        n.update(source_policy='encrypted-metadata-only',summary=COARSE['note'],project={'key':'restricted','repo':None},touches=[],references=[])
        n['actor']['task']='restricted-task'; n.pop('details',None)
        self.assertEqual(errors(n,SCHEMA),[])

class Touches(unittest.TestCase):
    """touch / untouch / touching: the first session to declare a file holds it; the others queue behind it."""
    def tn(self, seq, sid, paths, *, thread='2026-10-03-wrap', hours_ago=0):
        e=event('note',seq,agent='claude',thread=thread); e['request_id']=e['event_id']
        e['actor']['session_id']=sid; e['actor']['task']=sid+' name'; e['touches']=paths
        if hours_ago: e['created_at']=board.view.iso_z(board.view.parse_ts(e['created_at'])-dt.timedelta(hours=hours_ago))
        return e
    def test_overlap_rules(self):
        self.assertTrue(board.touch_overlaps('r/a.md','r/a.md'))
        self.assertTrue(board.touch_overlaps('r/docs','r/docs/a.md'))       # a directory covers its files
        self.assertFalse(board.touch_overlaps('r/doc','r/docs/a.md'))       # a prefix of a name is not a parent
        self.assertFalse(board.touch_overlaps('h:abc','h:abd'))             # hashed paths collide only exactly
    def test_first_declarer_holds_and_queue_moves_on_release(self):
        A,B=('claude','sA'),('claude','sB')
        ev=[self.tn(0,'sA',['r/x.md']),self.tn(1,'sB',['r/x.md','r/y.md'])]
        st=board.touch_state(ev,NOW+dt.timedelta(seconds=30),12)
        self.assertEqual(board.touch_owner(st,'r/x.md')['who'],A)
        self.assertEqual(board.touch_owner(st,'r/y.md')['who'],B)
        lines,blocked=board.touch_verdict(st,B,['r/x.md','r/y.md'])
        self.assertTrue(blocked); self.assertTrue(lines[0].startswith('🔴')); self.assertTrue(lines[1].startswith('🟢'))
        ev.append(self.tn(2,'sA',[board.NONE_TOUCH]))                       # A releases everything
        st=board.touch_state(ev,NOW+dt.timedelta(seconds=30),12)
        self.assertEqual(board.touch_owner(st,'r/x.md')['who'],B)
        self.assertNotIn(A,st)
    def test_restated_set_keeps_since_and_plain_note_is_inert(self):
        ev=[self.tn(0,'sA',['r/x.md']),self.tn(1,'sB',['r/x.md']),self.tn(2,'sA',['r/x.md','r/z.md'])]
        plain=self.tn(3,'sA',[]); ev.append(plain)                           # an ordinary note releases nothing
        st=board.touch_state(ev,NOW+dt.timedelta(seconds=30),12)
        self.assertEqual(board.touch_owner(st,'r/x.md')['who'],('claude','sA'))  # re-stating does not lose the place
    def test_stale_declaration_does_not_hold(self):
        ev=[self.tn(0,'sA',['r/x.md'],hours_ago=13),self.tn(1,'sB',['r/x.md'])]
        st=board.touch_state(ev,NOW+dt.timedelta(seconds=30),12)
        self.assertEqual(board.touch_owner(st,'r/x.md')['who'],('claude','sB'))
    def test_thread_filter_narrows_listing_only(self):
        ev=[self.tn(0,'sA',['r/x.md'],thread='2026-10-02-other'),self.tn(1,'sB',['r/y.md'])]
        self.assertEqual(set(board.touch_state(ev,NOW+dt.timedelta(seconds=30),12,thread='2026-10-03-wrap')),{('claude','sB')})
        self.assertEqual(len(board.touch_state(ev,NOW+dt.timedelta(seconds=30),12)),2)
    def test_norm_touch_names_one_file_and_keeps_no_post_off_the_board(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)/'Claude'; repo=base/'r'; (repo/'secret').mkdir(parents=True); (base/'secrets-config').mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            (repo/'.gitattributes').write_text('secret/** filter=git-crypt diff=git-crypt\n')
            (repo/'CLAUDE.md').write_text('x'); (base/'CLAUDE.md').symlink_to(repo/'CLAUDE.md')
            saved=os.environ.get('AGENT_BOARD_PATH_BASE'); os.environ['AGENT_BOARD_PATH_BASE']=str(base)
            try:
                self.assertEqual(board.norm_touch('r/a.md'),'r/a.md')
                self.assertEqual(board.norm_touch(str(repo/'a.md')),'r/a.md')
                self.assertEqual(board.norm_touch('CLAUDE.md'),'r/CLAUDE.md')          # the symlink and its target are one file
                h=board.norm_touch('r/secret/plan.md'); self.assertTrue(h.startswith('h:')); self.assertNotIn('plan',h)
                for bad in (str(Path(td)/'Dropbox'/'x.md'),'secrets-config/k.txt'):
                    with self.assertRaises(ValueError): board.norm_touch(bad)
            finally:
                if saved is None: os.environ.pop('AGENT_BOARD_PATH_BASE',None)
                else: os.environ['AGENT_BOARD_PATH_BASE']=saved
    def test_touches_reach_the_event_and_restricted_refuses_them(self):
        import argparse
        base=dict(command='note',source=Path('/nonexistent/example'),thread='2026-10-03-wrap',request=None,summary='触る: r/x.md',
            acceptance=None,agent='claude',session='claude-session-001',session_name='wrap A',instance=None,event_id=None,reply_to=None,lease_hours=12,
            reference=[],deliverable=[],to=None,to_session=None,role=None,touch=['r/x.md','r/x.md'])
        ev=board.event_from(argparse.Namespace(policy='ordinary',project='example',repo='o/example',**base),[])
        self.assertEqual(ev['touches'],['r/x.md']); self.assertEqual(errors(ev,SCHEMA),[])
        with self.assertRaises(ValueError):
            board.event_from(argparse.Namespace(policy='encrypted-metadata-only',project=None,repo=None,**{**base,'summary':None}),[])

class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-history-test-')
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.root)], check=True)
        for k, v in [('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid')]:
            board.run(self.root, 'config', k, v)
        (self.root / 'board.json').write_text(json.dumps({'board_format': 1, 'audience': 'owner', 'encryption': 'none'}))
        board.run(self.root, 'add', 'board.json')
        board.run(self.root, 'commit', '-q', '-m', 'Initialize fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, e, *, commit=True, path=None):
        p = self.root / (path or ('events/' + e['project']['key'] + '/' + e['thread_id'] + '/'
            + board.view.compact(board.view.parse_ts(e['created_at'])) + '--' + e['event_id'] + '.json'))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(e))
        if commit:
            board.run(self.root, 'add', str(p))
            board.run(self.root, 'commit', '-q', '-m', 'Add event')
        return p

    def test_committed_legacy_summary_is_preserved_but_new_posts_stay_strict(self):
        e, _ = board.view.build_template(kind='finding', project='example', thread='legacy-thread',
            task='Fixture', agent='claude', repo=None, restricted=False, lease_hours=12,
            summary='x' * 1224, now=NOW)
        p = self.add(e, commit=False)
        events, invalid = board.view.load_events(self.root)
        self.assertEqual(len(invalid), 1)
        board.run(self.root, 'add', str(p)); board.run(self.root, 'commit', '-q', '-m', 'Add event')
        original = p.read_bytes()
        events, invalid = board.view.load_events(self.root)
        self.assertFalse(invalid)
        self.assertEqual(events[0]['summary'], e['summary'])
        self.assertEqual(len(board.view.history_warnings(events)), 1)
        self.assertEqual(board.view.validate(self.root, events, invalid), [])
        self.assertEqual(p.read_bytes(), original)
        self.assertTrue(errors(e, SCHEMA))
        self.assertRaises(ValueError, board.post, self.root, e)
        for changed in [dict(e, summary='x' * 4001), dict(e, source_policy='encrypted-metadata-only'),
                        dict(event('request', 0), summary='x' * 1224), dict(e, created_at='bad-time')]:
            self.assertTrue(history_errors(changed, SCHEMA, committed=True)[0])
        payload = board.view.to_json_payload(self.root, board.view.derive(events, NOW), invalid,
            now=NOW, locked=False, warnings=board.view.history_warnings(events))
        self.assertEqual(len(payload['warnings']), 1)
        self.assertFalse(payload['degraded'])
        # The compatibility rule cannot be borrowed by an uncommitted edit.
        p.write_text(json.dumps(dict(e, summary='y' * 1224)))
        self.assertEqual(len(board.view.load_events(self.root)[1]), 1)
        p.unlink()
        events, invalid = board.view.load_events(self.root)
        self.assertEqual(len(invalid), 1)
        self.assertRaises(ValueError, board.view.assert_writable, events, 'example', 'legacy-thread')

    def test_corrupt_record_cannot_disappear_into_a_completed_thread(self):
        r, c, sub = chain()
        for e in (r, c, sub, event('accept', 3, reply=sub)):
            self.add(e)
        bad = event('update', 4); bad['summary'] = 'x' * 1001
        self.add(bad)
        other = event('request', 20, thread='healthy-thread'); other['request_id'] = other['event_id']
        self.add(other)
        events, invalid = board.view.load_events(self.root)
        states = board.view.derive(events, NOW)
        damaged = states['example', r['thread_id']]
        self.assertEqual(damaged['workflow']['status'], 'invalid')
        self.assertFalse(damaged['closed'])
        self.assertIsNone(damaged['workflow']['waiting_on'])
        self.assertEqual(states['example', 'healthy-thread']['workflow']['status'], 'requested')
        self.assertRaises(ValueError, board.view.assert_writable, events, 'example', r['thread_id'])
        board.view.assert_writable(events, 'example', 'healthy-thread')
        self.assertRaisesRegex(ValueError, 'event_id already', board.view.assert_writable,
            events, 'example', 'healthy-thread', bad['event_id'])
        self.assertTrue(board.view.validate(self.root, events, invalid))
        payload = board.view.to_json_payload(self.root, states, invalid, now=NOW, locked=False)
        self.assertTrue(payload['degraded'])
        self.assertEqual(len(payload['invalid']), 1)
        import sys
        result = subprocess.run([sys.executable, str(board.ENGINE / 'board-view.py'),
            '--validate', '--root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        result = subprocess.run([sys.executable, str(board.ENGINE / 'board.py'),
            'inbox', '--root', str(self.root), '--agent', 'codex', '--session', 'codex-session-001'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('healthy-thread', result.stdout)
        self.assertIn('invalid event', result.stdout)
        self.assertNotIn('対応待ちはありません', result.stdout)

    def test_bad_paths_duplicates_and_unscoped_corruption(self):
        r = event('request', 0)
        self.add(r)
        # A misplaced but otherwise valid record damages both candidate threads.
        filename = board.view.compact(NOW) + '--' + r['event_id'] + '.json'
        p = self.add(r, path='events/example/other-thread/' + filename)
        events, invalid = board.view.load_events(self.root)
        for tid in [r['thread_id'], 'other-thread']:
            self.assertRaises(ValueError, board.view.assert_writable, events, 'example', tid)
        board.view.assert_writable(events, 'example', 'untouched-thread')
        # Duplicate identifiers cannot silently select one record on replay.
        duplicate = dict(r, thread_id='other-thread')
        p.write_text(json.dumps(duplicate))
        events, invalid = board.view.load_events(self.root)
        self.assertEqual(len(events), 0)
        self.assertEqual(len(invalid), 2)
        orphan = self.root / 'events/orphan.json'; orphan.write_text('{malformed')
        events, invalid = board.view.load_events(self.root)
        self.assertRaisesRegex(ValueError, 'unscoped', board.view.assert_writable,
            events, 'example', 'untouched-thread')

    def test_protocol_error_after_acceptance_does_not_report_completion(self):
        r, c, sub = chain()
        for e in (r, c, sub, event('accept', 3, reply=sub), event('accept', 4, agent='codex', reply=sub)):
            self.add(e)
        events, invalid = board.view.load_events(self.root)
        self.assertFalse(invalid)  # The failure is in the protocol, not the schema.
        state = board.view.derive(events, NOW)['example', r['thread_id']]
        self.assertFalse(state['closed'])
        self.assertEqual(state['workflow']['status'], 'invalid')
        self.assertRaises(ValueError, board.view.assert_writable, events, 'example', r['thread_id'])

    def test_legacy_supersedes_cannot_suppress_another_thread(self):
        e, _ = board.view.build_template(kind='claim', project='example', thread='first-thread',
            task='Fixture', agent='codex', repo=None, restricted=False, lease_hours=12,
            summary='Claim', now=NOW)
        other = dict(e, thread_id='other-thread', kind='update', event_id='20260906T000000Z-codex-aaaaaa',
            supersedes=[e['event_id']])
        self.assertEqual(len(board.view.derive([e, other], NOW)['example', 'first-thread']['active_claims']), 1)

    def test_closed_receipt_is_inspectable_without_reentering_actionable_inbox(self):
        import sys
        r,c,sub=chain(); acc=event('accept',3,reply=sub)
        sub['references']=['commit/result']; acc['summary']='Checked the file and accepted.'
        for e in (r,c,sub,acc): self.add(e)
        def cli(command,*args,session='codex-session-001'):
            return subprocess.run([sys.executable,str(board.ENGINE/'board.py'),command,
                '--root',str(self.root),'--agent','codex','--session',session,'--json',*args],
                capture_output=True,text=True)
        default=cli('inbox'); self.assertEqual(default.returncode,0,default.stderr)
        self.assertEqual(json.loads(default.stdout),[])
        completed=cli('inbox','--include-closed')
        self.assertEqual(completed.returncode,0,completed.stderr)
        rows=json.loads(completed.stdout); self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['review']['summary'],acc['summary'])
        self.assertEqual(rows[0]['submission']['references'],sub['references'])
        self.assertFalse(rows[0]['actionable'])
        self.assertIsNone(rows[0]['reply_to'])
        other=cli('inbox','--include-closed',session='codex-another-session')
        self.assertEqual(json.loads(other.stdout),[])
        shown=cli('show','--request',r['event_id'])
        self.assertEqual(shown.returncode,0,shown.stderr)
        self.assertEqual(json.loads(shown.stdout),rows[0])
        missing=cli('show','--request','absent')
        self.assertNotEqual(missing.returncode,0)
        self.assertFalse(missing.stdout.strip())

    def test_unscoped_corruption_is_not_an_empty_json_inbox(self):
        import sys
        (self.root/'events').mkdir()
        (self.root/'events/orphan.json').write_text('{unreadable')
        for command,args in [('inbox',[]),('show',['--request','unknown'])]:
            result=subprocess.run([sys.executable,str(board.ENGINE/'board.py'),command,
                '--root',str(self.root),'--agent','codex','--session','codex-session-001','--json',*args],
                capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('unscoped',result.stderr)
            self.assertFalse(result.stdout.strip())


class Transport(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp=tempfile.TemporaryDirectory(prefix='board-test-'); self.base=Path(self.tmp.name)
        self.root=self.base/'source'; self.remote=self.base/'remote.git'
        self._cache_saved=os.environ.get('AGENT_BOARD_CACHE_DIR'); os.environ['AGENT_BOARD_CACHE_DIR']=str(self.base/'cache')   # never the user's ~/.cache
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        subprocess.run(['git','init','-q','-b','main',str(self.root)],check=True)
        for k,v in [('user.name','Fixture'),('user.email','fixture@example.invalid')]: board.run(self.root,'config',k,v)
        subprocess.run(['git-crypt','init'],cwd=self.root,capture_output=True,check=True)
        (self.root/'.gitattributes').write_text('* filter=git-crypt diff=git-crypt\n/.gitattributes !filter !diff\n')
        (self.root/'board.json').write_text(json.dumps({'board_format':1,'audience':'owner','encryption':'git-crypt'}))
        board.run(self.root,'add','.gitattributes','board.json'); board.run(self.root,'commit','-q','-m','Initialize fixture')
        board.run(self.root,'remote','add','origin',str(self.remote)); board.run(self.root,'push','-u','origin','main')
    def tearDown(self):
        import os
        self.tmp.cleanup()
        if self._cache_saved is None: os.environ.pop('AGENT_BOARD_CACHE_DIR',None)
        else: os.environ['AGENT_BOARD_CACHE_DIR']=self._cache_saved
    def test_encrypted_roundtrip_and_unchanged_caller_index(self):
        (self.root/'unrelated.txt').write_text('Other agent work'); board.run(self.root,'add','unrelated.txt')
        before=board.run(self.root,'diff','--cached','--raw').stdout
        r,c,sub=chain(); acc=event('accept',3,reply=sub)
        for e in [r,c,sub,acc]: board.post(self.root,e)
        self.assertEqual(before,board.run(self.root,'diff','--cached','--raw').stdout)
        self.assertEqual(board.post(self.root,acc),acc['event_id']) # idempotent replay
        with board.snapshot(self.root) as snap:
            es=board.read(snap); self.assertEqual(len(es),4)
            self.assertEqual(reduce_workflow(es,NOW+dt.timedelta(seconds=30))['status'],'accepted')
            board.run(self.root,'fetch','-q','origin','main')
            for e in es:
                blob=board.run(self.root,'cat-file','blob','origin/main:'+str(e['_path'].relative_to(snap))).stdout
                self.assertTrue(blob.startswith(board.view.GITCRYPT_MAGIC))
    def test_concurrent_claim_loser_revalidates(self):
        r,c,_=chain(); board.post(self.root,r)
        competitor=event('claim',2); competitor['actor']['instance']='codex@fixture/competitor'
        def race(attempt):
            if attempt==0: board.post(self.root,competitor)
        with self.assertRaisesRegex(ValueError,'live claim'):
            board.post(self.root,c,before_push=race)
        with board.snapshot(self.root) as snap:
            self.assertEqual(len(board.read(snap)),2)
    def test_cli_restricted_roundtrip_and_retry(self):
        import sys
        script=board.ENGINE/'board.py'
        def cli(kind, agent, *args):
            cmd=[sys.executable,str(script),kind,'--root',str(self.root),'--agent',agent,'--session',agent+'-cli-session']
            if kind!='inbox': cmd += ['--instance',agent+'@fixture/cli','--source',str(self.root),'--policy','encrypted-metadata-only']
            p=subprocess.run(cmd+list(args),capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            return p.stdout
        def eid(output): return next(l.split('=',1)[1] for l in output.splitlines() if l.startswith('event_id='))
        req=eid(cli('request','claude','--thread','r-a1b2c3d4e5f6','--to','codex','--to-session','codex-cli-session'))
        self.assertIn('codex / codex-cli-session 対応待ち',cli('inbox','codex','--sync'))
        claim=eid(cli('claim','codex','--request',req))
        sub=eid(cli('submit','codex','--request',req,'--reply-to',claim))
        self.assertIn('確認待ち',cli('inbox','claude','--sync'))
        accept=eid(cli('accept','claude','--request',req,'--reply-to',sub))
        cli('retry','claude','--event-id',accept)
        self.assertIn('対応待ちはありません',cli('inbox','claude','--sync'))
        with board.snapshot(self.root) as root:
            self.assertEqual(len(board.read(root)),4)

    def test_source_policy_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError,'forbids'): board.source_gate(self.root,'no-post')
        with self.assertRaisesRegex(ValueError,'encrypted'): board.source_gate(self.root,'ordinary')
        board.source_gate(self.root,'encrypted-metadata-only')

    def test_expired_question_encrypted_roundtrip(self):
        import sys
        r,c,_=chain(); q=event('blocker',2)
        for i,e in enumerate((r,c,q)):
            at=NOW-dt.timedelta(days=2)+dt.timedelta(seconds=i)
            e['created_at']=board.view.iso_z(at)
            e['event_id']=f"{board.view.compact(at)}-{e['actor']['agent']}-{i:06x}"
        for e in (r,c,q): e['request_id']=r['event_id']
        c['lease_until']=board.view.iso_z(NOW-dt.timedelta(days=1))
        for e in (r,c,q): self.import_record(e)
        result=subprocess.run([sys.executable,str(board.ENGINE/'board.py'),'inbox',
            '--root',str(self.root),'--agent','claude','--session','claude-session-001','--sync','--json'],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        rows=json.loads(result.stdout)
        self.assertEqual(rows[0]['status'],'blocked')
        self.assertTrue(rows[0]['claim_expired'])
        self.assertEqual(rows[0]['reply_to'],q['event_id'])
        renewed=event('claim',100); renewed['request_id']=r['event_id']
        board.post(self.root,renewed)
        submit=event('submit',102,reply=renewed); submit['request_id']=r['event_id']
        with self.assertRaisesRegex(ValueError,'question awaits reviewer'):
            board.post(self.root,submit)
        answer=event('update',101,agent='claude',reply=q); answer['request_id']=r['event_id']
        board.post(self.root,answer); board.post(self.root,submit)
        accept=event('accept',103,reply=submit); accept['request_id']=r['event_id']
        board.post(self.root,accept)
        with board.snapshot(self.root) as root:
            es=board.read(root)
            self.assertEqual(len(es),7)
            state=reduce_workflow(es,NOW+dt.timedelta(seconds=120))
            self.assertFalse(state['errors']); self.assertEqual(state['status'],'accepted')
            self.assertEqual(state['answer']['event_id'],answer['event_id'])

    def import_record(self, e, *, corrupt_json=False):
        """Simulate already-published malformed history through encrypted Git (a raw git write = the clone transport)."""
        with board.snapshot_clone(self.root) as root:
            p = root / 'events' / e['project']['key'] / e['thread_id'] / (board.view.compact(
                board.view.parse_ts(e['created_at'])) + '--' + e['event_id'] + '.json')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{malformed' if corrupt_json else json.dumps(e))
            board.run(root, 'config', 'user.name', 'Fixture')
            board.run(root, 'config', 'user.email', 'fixture@example.invalid')
            board.run(root, 'add', str(p)); board.run(root, 'commit', '-q', '-m', 'Add event')
            board.run(root, 'push', 'origin', 'HEAD:main')

    def test_unrelated_corruption_does_not_block_full_receipt_cycle(self):
        bad = event('request', 50, thread='damaged-thread')
        self.import_record(bad, corrupt_json=True)
        legacy, _ = board.view.build_template(kind='done', project='example', thread='legacy-thread',
            task='Fixture', agent='claude', repo=None, restricted=False, lease_hours=12,
            summary='x' * 1224, now=NOW)
        self.import_record(legacy)
        r, c, sub = chain(); acc = event('accept', 3, reply=sub)
        for e in (r, c, sub, acc):
            board.post(self.root, e)
        self.assertEqual(board.post(self.root, acc), acc['event_id'])
        with board.snapshot(self.root) as root:
            events = board.read(root); states = board.view.derive(events, NOW)
            self.assertTrue(states['example', r['thread_id']]['closed'])
            self.assertEqual(states['example', 'damaged-thread']['workflow']['status'], 'invalid')
            self.assertEqual(len(board.view.history_warnings(events)), 1)

    def test_race_introducing_corruption_rejects_retry_in_target_thread(self):
        r, c, _ = chain(); board.post(self.root, r)
        bad = event('update', 50)
        def race(attempt):
            if attempt == 0:
                self.import_record(bad, corrupt_json=True)
        with self.assertRaisesRegex(ValueError, 'target thread has invalid'):
            board.post(self.root, c, before_push=race)
        with board.snapshot(self.root) as root:
            self.assertEqual(len(board.read(root)), 1)


class CollaboratorBoards(unittest.TestCase):
    """A board inside a shared project (`<project>/board/`) read by that project's collaborators, next to an owner board."""
    def setUp(self):
        import os, sys
        self.sys=sys
        self.tmp=tempfile.TemporaryDirectory(prefix='board-collab-'); base=Path(self.tmp.name)
        self.ws=base/'ws'; remotes=base/'remotes'; self.ws.mkdir(); remotes.mkdir()
        def repo(name, files):
            r=self.ws/name; bare=remotes/(name+'.git')
            subprocess.run(['git','init','--bare','-q','-b','main',str(bare)],check=True)
            subprocess.run(['git','init','-q','-b','main',str(r)],check=True)
            for k,v in [('user.name','Fixture'),('user.email','fixture@example.invalid')]: board.run(r,'config',k,v)
            for rel,text in files.items():
                (r/rel).parent.mkdir(parents=True,exist_ok=True); (r/rel).write_text(text)
            board.run(r,'add','-A'); board.run(r,'commit','-q','-m','init')
            board.run(r,'remote','add','origin',str(bare)); board.run(r,'push','-q','-u','origin','main')
            return r
        owner={'board_format':1,'audience':'owner','encryption':'none'}
        collab={'board_format':1,'audience':'collaborators','encryption':'none','sources':['proj']}
        self.owner=repo('owner-board',{'board.json':json.dumps(owner)})
        self.proj=repo('proj',{'CLAUDE.md':'shared project','notes/a.md':'x','board/board.json':json.dumps(collab)})
        self.private=repo('private-notes',{'CLAUDE.md':'owner only','plan.md':'x'})
        self.saved=os.environ.get('AGENT_BOARD_WORKSPACE'); os.environ['AGENT_BOARD_WORKSPACE']=str(self.ws)
        self._cache_saved=os.environ.get('AGENT_BOARD_CACHE_DIR'); os.environ['AGENT_BOARD_CACHE_DIR']=str(base/'cache')
    def tearDown(self):
        import os
        if self.saved is None: os.environ.pop('AGENT_BOARD_WORKSPACE',None)
        else: os.environ['AGENT_BOARD_WORKSPACE']=self.saved
        if self._cache_saved is None: os.environ.pop('AGENT_BOARD_CACHE_DIR',None)
        else: os.environ['AGENT_BOARD_CACHE_DIR']=self._cache_saved
        self.tmp.cleanup()
    def cli(self, *args, check=True):
        p=subprocess.run([self.sys.executable,str(board.ENGINE/'board.py'),*args],capture_output=True,text=True)
        if check: self.assertEqual(p.returncode,0,p.stderr)
        return p
    def eid(self, out): return next(l.split('=',1)[1] for l in out.splitlines() if l.startswith('event_id='))

    def test_discovery_and_resolution(self):
        import board_config as bc
        found={b['name']:b for b in bc.discover(self.ws)}
        self.assertEqual(set(found),{'owner-board','proj'})
        self.assertEqual(found['proj']['root'],self.proj/'board')
        self.assertEqual(bc.resolve(board='proj'),self.proj/'board')
        with self.assertRaises(ValueError): bc.resolve(board='private-notes')
        rows=json.loads(self.cli('boards','--json').stdout)
        self.assertEqual({r['name']:r['audience'] for r in rows},{'owner-board':'owner','proj':'collaborators'})

    def test_full_cycle_inside_a_project_repository(self):
        b=str(self.proj/'board')
        req=self.eid(self.cli('request','--root',b,'--agent','claude','--session','c-1','--thread','2026-10-05-check',
            '--to','codex','--to-session','x-1','--summary','Check notes/a.md','--acceptance','Reproduce it',
            '--reference','notes/a.md').stdout)                       # project, source and policy come from board.json
        self.assertIn('対応待ち',self.cli('inbox','--root',b,'--agent','codex','--session','x-1','--sync').stdout)
        claim=self.eid(self.cli('claim','--root',b,'--agent','codex','--session','x-1','--request',req,'--summary','on it').stdout)
        sub=self.eid(self.cli('submit','--root',b,'--agent','codex','--session','x-1','--request',req,'--reply-to',claim,
            '--summary','done','--deliverable','notes/a.md').stdout)
        self.cli('accept','--root',b,'--agent','claude','--session','c-1','--request',req,'--reply-to',sub,
            '--summary','checked','--reference','proj@'+board.run(self.proj,'rev-parse','HEAD').stdout.decode().strip()[:10])
        head_before=board.run(self.proj,'rev-parse','HEAD').stdout
        with board.snapshot(self.proj/'board') as snap:
            self.assertEqual(snap.name,'board')
            es=board.read(snap); self.assertEqual(len(es),4)
            self.assertEqual(reduce_workflow(es,NOW+dt.timedelta(minutes=5))['status'],'accepted')
        board.run(self.proj,'fetch','-q','origin','main')
        names=board.run(self.proj,'log','--name-only','--format=','origin/main').stdout.decode().split()
        posted=[n for n in names if '/events/' in '/'+n]
        self.assertEqual(len(posted),4); self.assertTrue(all(n.startswith('board/events/proj/2026-10-05-check/') for n in posted))
        # the caller's checkout was not touched (a project checkout is never fast-forwarded); its pull brings the events
        self.assertEqual(board.run(self.proj,'rev-parse','HEAD').stdout,head_before)
        self.assertFalse((self.proj/'board'/'events').exists())
        board.run(self.proj,'pull','-q','origin','main')
        out=self.cli('show','--root',b,'--agent','claude','--session','c-1','--request',req,'--json').stdout
        self.assertEqual(json.loads(out)['status'],'accepted')

    def test_reader_gate_refuses_what_collaborators_must_not_see(self):
        b=str(self.proj/'board')
        base=['request','--root',b,'--agent','claude','--session','c-1','--thread','2026-10-05-gate','--to','codex','--to-session','x-1',
              '--acceptance','ok','--preview']
        ok=self.cli(*base,'--summary','Read notes/a.md','--reference','https://github.com/someone/other-paper/issues/1',check=False)
        self.assertNotEqual(ok.returncode,0); self.assertIn('repository',ok.stderr)     # a GitHub repo outside the sources
        for extra,needle in [(['--summary','see private-notes/plan.md'],'private-notes'),
                             (['--summary','ok','--reference',str(self.private/'plan.md')],'outside'),
                             (['--summary','ok','--reference','private-notes/plan.md'],'private-notes'),
                             (['--summary','ok','--reference','private-notes@abcdef1'],'not a source'),
                             (['--summary','ok','--project','private-notes','--source',str(self.private)],'not a source'),
                             (['--summary','ok','--policy','encrypted-metadata-only'],'ordinary')]:
            p=self.cli(*base,*extra,check=False)
            self.assertNotEqual(p.returncode,0,extra); self.assertIn(needle,p.stderr,extra)
        p=self.cli(*base,'--summary','Read notes/a.md in proj/notes','--reference',str(self.proj/'notes/a.md'))
        self.assertIn('"key": "proj"',p.stdout)
        t=self.cli('touch','--root',b,'--agent','claude','--session','c-1','--thread','2026-10-05-wrap','--path','private-notes/plan.md',check=False)
        self.assertNotEqual(t.returncode,0); self.assertIn('not a source',t.stderr)

    def test_blocker_answer_kinds_reach_the_cli(self):
        # the slips this guards against (measured): a note as the answer, a stop posted as a note, an answer to an answered blocker
        b=str(self.proj/'board'); x=['--agent','codex','--session','x-1']; c=['--agent','claude','--session','c-1']
        req=self.eid(self.cli('request','--root',b,*c,'--thread','2026-10-05-blk','--to','codex','--to-session','x-1',
            '--summary','Check notes/a.md','--acceptance','Reproduce it','--reference','notes/a.md').stdout)
        claim=self.eid(self.cli('claim','--root',b,*x,'--request',req,'--summary','on it').stdout)
        b1=self.eid(self.cli('blocker','--root',b,*x,'--request',req,'--reply-to',claim,'--summary','which input?').stdout)
        p=self.cli('note','--root',b,*c,'--request',req,'--reply-to',b1,'--summary','use the new copy')
        self.assertIn('⚠️ 開いた blocker '+b1,p.stderr); self.assertIn('[相談待ち]',p.stdout)
        p=self.cli('update','--root',b,*c,'--request',req,'--reply-to',b1,'--summary','use the new copy')
        self.assertIn('回答済み',p.stdout); self.assertIn('[作業中]',p.stdout)
        p=self.cli('note','--root',b,*x,'--request',req,'--summary','second run 停止 again; please advise')
        self.assertIn('blocker で出す',p.stderr)
        b2=self.eid(self.cli('blocker','--root',b,*x,'--request',req,'--reply-to',claim,'--summary','second stop').stdout)
        p=self.cli('update','--root',b,*c,'--request',req,'--reply-to',b1,'--summary','answer',check=False)
        self.assertNotEqual(p.returncode,0); self.assertIn('the open blocker is '+b2,p.stderr)
        self.assertNotIn('event_id=',p.stdout)                      # refused before an id is printed for retry
    def test_review_target_flags_reach_the_cli(self):
        b=str(self.proj/'board')
        (self.proj/'notes/raw.tex').write_text('% previous round: verdict incorrect, see plans/r.md\n\\section{A}\n')
        (self.proj/'notes/referee.tex').write_text('\\section{A}\ntext\n')
        board.run(self.proj,'add','notes'); board.run(self.proj,'commit','-q','-m','fixture')
        base=['request','--root',b,'--agent','claude','--session','c-1','--thread','2026-10-05-target','--to','codex',
              '--to-session','x-1','--summary','Blind review of notes/referee.tex','--acceptance','ok','--preview']
        p=self.cli(*base,check=False); self.assertNotEqual(p.returncode,0); self.assertIn('--review-target',p.stderr)
        p=self.cli(*base,'--review-target',str(self.proj/'notes/raw.tex'),check=False)
        self.assertNotEqual(p.returncode,0); self.assertIn('referee copy',p.stderr); self.assertNotIn('incorrect',p.stderr)
        p=self.cli(*base,'--review-target',str(self.proj/'notes/referee.tex'))
        self.assertIn('notes/referee.tex',p.stdout)
        self.cli(*base,'--not-blind')
        (self.proj/'notes/deixis.tex').write_text('\\section{B}\nThe present version retains form A and has dropped form B.\n')
        board.run(self.proj,'add','notes'); board.run(self.proj,'commit','-q','-m','fixture 2')
        p=self.cli(*base,'--review-target',str(self.proj/'notes/deixis.tex'))   # posted (preview), with the warning
        self.assertIn('not refused',p.stderr); self.assertIn('revision-deixis',p.stderr)
    def test_readable_checkouts_may_be_named_but_not_posted_from_or_touched(self):
        import board_config as bc
        cfg=bc.validate({'board_format':1,'audience':'collaborators','encryption':'none','sources':['proj'],'readable':['private-notes']})
        bc.check_post(cfg,self.proj/'board',project='proj',policy='ordinary',
                      texts=[('--summary','see private-notes/plan.md')],refs=[('--reference','private-notes/plan.md')])
        with self.assertRaisesRegex(ValueError,'not a source'):
            bc.check_post(cfg,self.proj/'board',project='proj',policy='ordinary',touches=[('private-notes/plan.md','private-notes/plan.md')])
        with self.assertRaisesRegex(ValueError,'not a source'):
            bc.check_post(cfg,self.proj/'board',project='private-notes',policy='ordinary')
        with self.assertRaises(ValueError): bc.validate({'board_format':1,'audience':'owner','encryption':'none','readable':['x']})

    def test_owner_post_points_to_the_collaborator_board_and_views_merge(self):
        o=str(self.owner)
        p=self.cli('note','--root',o,'--agent','claude','--session','c-1','--policy','ordinary','--project','proj','--source',str(self.proj),
                   '--thread','2026-10-05-owner','--summary','owner-only note about proj')
        self.assertIn('--board proj',p.stderr)
        self.cli('request','--root',str(self.proj/'board'),'--agent','codex','--session','x-1','--thread','2026-10-05-ask',
                 '--to','claude','--to-session','c-1','--summary','Please review','--acceptance','Read it','--not-blind')
        board.run(self.proj,'pull','-q','origin','main'); board.run(self.owner,'pull','-q','origin','main')
        v=subprocess.run([self.sys.executable,str(board.ENGINE/'board-view.py'),'--all-boards','--json'],capture_output=True,text=True)
        self.assertEqual(v.returncode,0,v.stderr); merged=json.loads(v.stdout)
        self.assertEqual({b['board'] for b in merged['boards']},{'owner-board','proj'})
        self.assertEqual({(t['board'],t['thread_id']) for t in merged['threads']},{('owner-board','2026-10-05-owner'),('proj','2026-10-05-ask')})
        rows=json.loads(self.cli('inbox','--all-boards','--agent','claude','--session','c-1','--json').stdout)
        self.assertEqual([(r['board'],r['thread']) for r in rows],[('proj','2026-10-05-ask')])

    def test_init_scaffolds_a_collaborator_board(self):
        import board_config as bc
        new=self.private/'board'
        out=self.cli('init','--root',str(new),'--audience','collaborators','--encryption','none','--sources','private-notes').stdout
        self.assertIn('git -C',out)
        cfg=bc.load(new); self.assertEqual(cfg['sources'],['private-notes']); self.assertTrue((new/'README.md').is_file())
        self.assertNotEqual(self.cli('init','--root',str(new),'--audience','owner','--encryption','none',check=False).returncode,0)
        with self.assertRaises(ValueError): bc.validate({'board_format':1,'audience':'collaborators','encryption':'none'})
        with self.assertRaises(ValueError): bc.validate({'board_format':1,'audience':'owner','encryption':'none','sources':['x']})


class StateAndKindHints(unittest.TestCase):
    """Posts and watch say the request's state as just read, and warn when a post's kind will not do what its text
    asks (a note never answers a blocker; a stop posted as a note never reaches the requester as a question)."""
    REVIEWER=('claude','claude-session-001'); CLAIMANT=('codex','codex-session-001')
    def flow(self):
        r,c,_=chain(); b=event('blocker',2,reply=c); a=event('update',3,agent='claude',reply=b)
        return r,c,b,a
    def w(self,events): return reduce_workflow(events,NOW+dt.timedelta(seconds=30))
    def test_state_line(self):
        r,c,b,a=self.flow()
        blocked=board.request_state_line(self.w([r,c,b]))
        self.assertIn('相談待ち',blocked); self.assertIn('開いた blocker = '+b['event_id'],blocked)
        self.assertIn(f"update --request {r['event_id']} --reply-to {b['event_id']}",blocked)
        working=board.request_state_line(self.w([r,c,b,a]))
        self.assertIn('作業中',working); self.assertIn('開いた blocker なし',working)
        self.assertIn(f"{b['event_id']} は update {a['event_id']} で回答済み",working)
        self.assertIsNone(board.request_state_line(self.w([event('note',0,agent='claude')])))   # no request: nothing
    def test_note_from_the_reviewer_on_an_open_blocker_warns(self):
        r,c,b,a=self.flow(); n=event('note',3,agent='claude',reply=b)
        h=board.kind_hints(n,self.w([r,c,b]),self.REVIEWER)
        self.assertEqual(len(h),1); self.assertIn('⚠️',h[0]); self.assertIn('--reply-to '+b['event_id'],h[0])
        self.assertEqual(board.kind_hints(n,self.w([r,c,b,a]),self.REVIEWER),[])       # nothing open: a note is fine
    def test_stop_posted_as_a_note_by_the_claimant_gets_a_hint(self):
        r,c,b,a=self.flow()
        stop=event('note',4); stop['summary']='Stage 1 停止: input shows history; please advise'
        self.assertIn('blocker で出す',board.kind_hints(stop,self.w([r,c,b,a]),self.CLAIMANT)[0])
        progress=event('note',4); progress['summary']='Stage 1 running, 4 checks written'
        self.assertEqual(board.kind_hints(progress,self.w([r,c,b,a]),self.CLAIMANT),[])
    def test_update_to_an_answered_blocker_with_nothing_open_says_so(self):
        r,c,b,a=self.flow(); again=event('update',4,agent='claude',reply=b)
        self.assertIn('回答済み',board.kind_hints(again,self.w([r,c,b,a]),self.REVIEWER)[0])
    def test_other_party_of_a_note(self):
        r,c,b,a=self.flow(); w=self.w([r,c,b,a])
        self.assertEqual(board.other_party(w,self.REVIEWER),{'agent':'codex','session_id':'codex-session-001'})
        self.assertEqual(board.other_party(w,self.CLAIMANT),{'agent':'claude','session_id':'claude-session-001'})
        self.assertEqual(board.other_party(self.w([r]),self.REVIEWER),{'agent':'codex','session_id':'codex-session-001'})
    def test_watch_report_names_the_answer_and_the_full_resume_command(self):
        r,c,b,a=self.flow(); me={'agent':'claude','session_id':'claude-session-001'}
        out='\n'.join(board.watch_report(('example',r['thread_id']),[b],[r,c,b],me,root='/b',agent='claude',
                                          session='claude-session-001',request=r['event_id'],quiet={'note'}))
        self.assertIn('この blocker に答える = update --request '+r['event_id']+' --reply-to '+b['event_id'],out)
        self.assertIn('📋 この依頼の今: [相談待ち]',out)
        self.assertIn(f"watch --root /b --agent claude --session claude-session-001 --request {r['event_id']} --quiet-kind note --since {b['event_id']}",out)
        self.assertNotIn('...',out)
        other='\n'.join(board.watch_report(('example',r['thread_id']),[a],[r,c,b,a],{'agent':'codex','session_id':'codex-session-001'},
                                            root='/b',agent='codex',session='codex-session-001',request=r['event_id']))
        self.assertNotIn('この blocker に答える',other); self.assertIn('回答済み',other)


class Watch(unittest.TestCase):
    """watch wakes on the other side's events; quiet kinds wait for the next waking event; sync failures retry."""
    def test_quiet_note_waits_for_next_event(self):
        me={'agent':'claude','session_id':'claude-session-001'}
        r,c,sub=chain(); note=event('note',3,agent='codex')
        seen={r['event_id']}; pending=[]
        self.assertIsNone(board.watch_wake([r,c,note],seen,me,{'note','claim'},pending))
        self.assertEqual([e['event_id'] for e in pending],[c['event_id'],note['event_id']])
        seen.update(e['event_id'] for e in (c,note))
        out=board.watch_wake([r,c,note,sub],seen,me,{'note','claim'},pending)
        self.assertEqual([e['kind'] for e in out],['claim','note','submit'])
        self.assertEqual(pending,[])
    def test_without_quiet_any_event_wakes_and_own_events_do_not(self):
        me={'agent':'claude','session_id':'claude-session-001'}
        r,c,_=chain(); note=event('note',3,agent='codex')
        self.assertIsNone(board.watch_wake([r],set(),me,set(),[]))
        self.assertEqual([e['kind'] for e in board.watch_wake([r,note],{r['event_id']},me,set(),[])],['note'])
    def test_only_sync_failures_are_transient(self):
        self.assertTrue(board.transient_sync_error(ValueError('git clone failed: Connection reset by peer')))
        self.assertTrue(board.transient_sync_error(subprocess.TimeoutExpired('git',1)))
        self.assertFalse(board.transient_sync_error(ValueError('request not found or ambiguous: x')))
        self.assertFalse(board.transient_sync_error(KeyError('event_id')))


class FetchTransport(Transport):
    """What the default (fetch) transport adds: the checkout moves after a post, old and new transports read one
    history, a reviewer role outlives the requesting session, and watch wakes on a post. Runs on the Transport fixture."""
    def _env(self, **kv):
        import os
        saved={k:os.environ.get(k) for k in kv}
        for k,v in kv.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
        def restore():
            for k,v in saved.items():
                if v is None: os.environ.pop(k,None)
                else: os.environ[k]=v
        return restore
    def test_clean_owner_checkout_moves_after_a_post_and_a_dirty_one_stays(self):
        if bt.transport()=='clone': self.skipTest('clone transport never moves the checkout')
        r,c,sub=chain()
        board.post(self.root,r)
        board.run(self.root,'fetch','-q','origin','main')
        self.assertEqual(board.run(self.root,'rev-parse','HEAD').stdout,board.run(self.root,'rev-parse','origin/main').stdout)
        f=self.root/'events'/'example'/r['thread_id']
        self.assertEqual(len(list(f.glob('*.json'))),1)
        self.assertEqual(json.loads(next(f.glob('*.json')).read_text())['event_id'],r['event_id'])   # decrypted by the checkout's filter
        (self.root/'board.json').write_text((self.root/'board.json').read_text()+'\n')              # a tracked file edited = not clean
        board.post(self.root,c)
        board.run(self.root,'fetch','-q','origin','main')
        self.assertNotEqual(board.run(self.root,'rev-parse','HEAD').stdout,board.run(self.root,'rev-parse','origin/main').stdout)
        self.assertEqual(len(list(f.glob('*.json'))),1)
        board.run(self.root,'checkout','-q','--','board.json')
        board.post(self.root,sub)                                                                  # clean again: moves
        self.assertEqual(len(list(f.glob('*.json'))),3)
    def test_both_transports_read_one_history_in_one_order(self):
        r,c,sub=chain()
        restore=self._env(AGENT_BOARD_TRANSPORT=None)
        try:
            board.post(self.root,r)
            restore2=self._env(AGENT_BOARD_TRANSPORT='clone'); board.post(self.root,c); restore2()
            board.post(self.root,sub)
            with board.snapshot_clone(self.root) as a, board.snapshot(self.root) as b:
                ea,eb=board.read(a),board.read(b)
                self.assertEqual([e['event_id'] for e in ea],[e['event_id'] for e in eb])
                self.assertEqual([e['_commit']['sha'] for e in ea],[e['_commit']['sha'] for e in eb])
                self.assertEqual(reduce_workflow(eb,NOW+dt.timedelta(seconds=30))['status'],'submitted')
                self.assertEqual(board.view.to_json_payload(b,board.view.derive(eb,NOW),[],now=NOW,locked=False)['head'],
                                 board.run(self.root,'rev-parse','--short','origin/main').stdout.decode().strip()[:7])
        finally: restore()
    def test_reviewer_role_receives_the_submission_and_may_accept(self):
        import sys
        script=board.ENGINE/'board.py'
        def cli(kind, agent, session, *args, ok=True):
            cmd=[sys.executable,str(script),kind,'--root',str(self.root),'--agent',agent,'--session',session]
            if kind!='inbox': cmd += ['--source',str(self.root),'--policy','encrypted-metadata-only']
            p=subprocess.run(cmd+list(args),capture_output=True,text=True)
            if ok: self.assertEqual(p.returncode,0,p.stderr)
            return p
        def eids(output): return [l.split('=',1)[1] for l in output.splitlines() if l.startswith('event_id=')]
        out=cli('request','claude','claude-win-1','--thread','r-0a1b2c3d4e5f','--to','codex','--to-session','codex-cli-1','--reviewer','role-x-owner-review').stdout
        req,=eids(out); self.assertIn('検収役 = claude/role-x-owner-review',out)
        claim,=eids(cli('claim','codex','codex-cli-1','--request',req).stdout)
        sub,=eids(cli('submit','codex','codex-cli-1','--request',req,'--reply-to',claim).stdout)
        self.assertIn('確認待ち',cli('inbox','claude','role-x-owner-review','--sync').stdout)       # the role's inbox, not the window's
        self.assertIn('対応待ちはありません',cli('inbox','claude','claude-win-1','--sync').stdout)
        p=cli('accept','claude','claude-win-1','--request',req,'--reply-to',sub,ok=False)
        self.assertNotEqual(p.returncode,0); self.assertIn('designated reviewer',p.stderr)
        cli('accept','claude','role-x-owner-review','--request',req,'--reply-to',sub,'--session-name','01234567 (fixture)')
        self.assertIn('対応待ちはありません',cli('inbox','claude','role-x-owner-review','--sync').stdout)
        # reviewer = assignee is refused before anything is posted
        p=cli('request','claude','claude-win-2','--thread','r-0a1b2c3d4e60','--to','codex','--to-session','codex-cli-2','--reviewer','codex-cli-2',ok=False)
        self.assertNotEqual(p.returncode,0); self.assertIn('differ',p.stderr)
    def test_watch_wakes_on_the_other_side_s_post(self):
        import sys, time
        r,c,_=chain(); board.post(self.root,r)
        w=subprocess.Popen([sys.executable,str(board.ENGINE/'board.py'),'watch','--root',str(self.root),'--agent','claude',
                            '--session','claude-session-001','--request',r['event_id'],'--interval','1','--max-minutes','1'],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        time.sleep(2); board.post(self.root,c)
        out,err=w.communicate(timeout=60)
        self.assertEqual(w.returncode,0,err); self.assertIn('claim',out); self.assertIn(c['event_id'][:10],out)


class LaunchCommand(unittest.TestCase):
    """A request to a role prints the CLI command that opens the worker with model, effort and config dir pinned, writes
    the hand-off text to the outbox, and --launch runs it (a stub CLI records what it got)."""
    def test_plan_and_launch(self):
        import os
        td=tempfile.TemporaryDirectory(); base=Path(td.name)
        root=base/'board'; root.mkdir(); (root/'.local').mkdir()
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        stub=base/'claude'; stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STUB_OUT"; printf \'cfg=%s\\n\' "${CLAUDE_CONFIG_DIR:-}" >> "$STUB_OUT"; echo backgrounded 1234abcd\n'); stub.chmod(0o755)
        saved={k:os.environ.get(k) for k in ('AGENT_BOARD_LAUNCH_CONFIG_DIR','AGENT_BOARD_LAUNCH_MODEL','AGENT_BOARD_LAUNCH_EFFORT','AGENT_BOARD_LAUNCH_CLI','CLAUDE_EFFORT','STUB_OUT')}
        os.environ.update({'AGENT_BOARD_LAUNCH_CONFIG_DIR':'~/.claude-fixture','AGENT_BOARD_LAUNCH_MODEL':'claude-model-x','AGENT_BOARD_LAUNCH_CLI':str(stub),'CLAUDE_EFFORT':'high','STUB_OUT':str(base/'stub.out')})
        os.environ.pop('AGENT_BOARD_LAUNCH_EFFORT',None)
        try:
            argv,env,line,path=board.launch_plan(root,'role-p-f','20260101T000000Z-claude-000001','p/spec.md',None,'claude-session-001')
            self.assertEqual(argv[:6],[str(stub),'--bg','--model','claude-model-x','--effort','high'])
            self.assertIn('You are the board worker for role `role-p-f`',argv[6]); self.assertIn('spec `p/spec.md`',argv[6])
            self.assertIn('run on claude-model-x at high effort',argv[6])
            self.assertEqual(env['CLAUDE_CONFIG_DIR'],os.path.expanduser('~/.claude-fixture')); self.assertNotIn('CLAUDECODE',env)
            self.assertTrue(line.startswith('CLAUDE_CONFIG_DIR=')); self.assertIn('--bg --model claude-model-x --effort high "$(cat ',line)
            self.assertEqual(path,root/'.local'/'outbox'/'20260101T000000Z-claude-000001.handoff.txt'); self.assertEqual(path.read_text(),argv[6])
            # --expect-model wins over the default; no config dir → no prefix
            os.environ.pop('AGENT_BOARD_LAUNCH_CONFIG_DIR')
            argv2,env2,line2,_=board.launch_plan(root,'role-p-f','20260101T000000Z-claude-000002',None,'fable','claude-session-001')
            self.assertEqual(argv2[2:4],['--model','fable']); self.assertNotIn('CLAUDE_CONFIG_DIR=',line2); self.assertIn('(the request itself)',argv2[-1])
            out=board.launch(argv,env); self.assertIn('backgrounded 1234abcd',out)
            got=(base/'stub.out').read_text().splitlines()
            self.assertEqual(got[:5],['--bg','--model','claude-model-x','--effort','high']); self.assertEqual(got[-1],'cfg='+os.path.expanduser('~/.claude-fixture'))
        finally:
            for k,v in saved.items():
                if v is None: os.environ.pop(k,None)
                else: os.environ[k]=v
            td.cleanup()


class LaunchCommandCLI(Transport):
    """The request command prints the launch line for a role address and nothing for a native one."""
    def test_request_to_role_prints_launch_line(self):
        import sys, os
        script=board.ENGINE/'board.py'
        def cli(*args):
            p=subprocess.run([sys.executable,str(script),*args],capture_output=True,text=True); self.assertEqual(p.returncode,0,p.stderr); return p.stdout
        base=['--root',str(self.root),'--agent','claude','--session','claude-win-9','--source',str(self.root),'--policy','encrypted-metadata-only']
        saved=os.environ.get('AGENT_BOARD_LAUNCH_MODEL'); os.environ['AGENT_BOARD_LAUNCH_MODEL']='claude-model-y'
        try:
            out=cli('request',*base,'--thread','r-00112233aabb','--to','claude','--to-session','role-q-work')
            self.assertIn('受け手を起こす',out); self.assertIn('--bg --model claude-model-y',out); self.assertIn('.handoff.txt',out)
            rid=next(l.split('=',1)[1] for l in out.splitlines() if l.startswith('event_id='))
            hp=board.outbox_dir(self.root)/(rid+'.handoff.txt'); self.assertTrue(hp.is_file()); self.assertIn(rid,hp.read_text())
            out2=cli('request',*base,'--thread','r-00112233aabc','--to','claude','--to-session','claude-win-other')
            self.assertNotIn('受け手を起こす',out2)
        finally:
            if saved is None: os.environ.pop('AGENT_BOARD_LAUNCH_MODEL',None)
            else: os.environ['AGENT_BOARD_LAUNCH_MODEL']=saved


class TransportClone(Transport):
    """The previous transport (AGENT_BOARD_TRANSPORT=clone) still passes the transport tests."""
    def setUp(self):
        import os
        self._saved=os.environ.get('AGENT_BOARD_TRANSPORT'); os.environ['AGENT_BOARD_TRANSPORT']='clone'
        super().setUp()
    def tearDown(self):
        import os
        super().tearDown()
        if self._saved is None: os.environ.pop('AGENT_BOARD_TRANSPORT',None)
        else: os.environ['AGENT_BOARD_TRANSPORT']=self._saved


class CollaboratorBoardsClone(CollaboratorBoards):
    """A board inside a project through the previous transport."""
    def setUp(self):
        import os
        self._saved=os.environ.get('AGENT_BOARD_TRANSPORT'); os.environ['AGENT_BOARD_TRANSPORT']='clone'
        super().setUp()
    def tearDown(self):
        import os
        super().tearDown()
        if self._saved is None: os.environ.pop('AGENT_BOARD_TRANSPORT',None)
        else: os.environ['AGENT_BOARD_TRANSPORT']=self._saved


if __name__=='__main__': unittest.main()
