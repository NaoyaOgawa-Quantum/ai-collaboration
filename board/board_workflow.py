"""Version 2 request/receipt protocol. Pure reducer shared by readers and writer."""
from __future__ import annotations
import datetime as dt

KINDS = {'request', 'claim', 'submit', 'accept', 'revise', 'release', 'blocker', 'update', 'finding', 'abandoned', 'handover', 'note'}
LABELS = {'requested':'引受待ち', 'working':'作業中', 'submitted':'確認待ち', 'revision':'修正待ち', 'blocked':'相談待ち', 'stale':'担当期限切れ', 'accepted':'確認済み', 'abandoned':'取り下げ', 'invalid':'記録の要確認'}
COARSE = {'request':'Work requested.', 'submit':'Work submitted.', 'accept':'Work accepted.', 'revise':'Revision requested.', 'release':'Work released.', 'handover':'Work handed over.', 'note':'Status noted.'}

def timestamp(s):
    t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('timezone required')
    return t

def identity(e):
    a = e.get('actor', {})
    return a.get('agent'), a.get('session_id')

def address(actor):
    return {'agent': actor['agent'], 'session_id': actor['session_id']}

def participant_identity(actor):
    return actor.get('agent'), actor.get('session_id')

def reduce_workflow(events, now):
    state = {'status':'invalid', 'request':None, 'claim':None, 'submission':None,
             'blocker':None, 'question':None, 'answer':None, 'review':None,
             'waiting_on':None, 'next_action':'', 'claim_expired':False,
             'errors':[], 'accepted_ids':[], 'assigned_to':None, 'reviewer':None}
    seen = set()
    # Legacy (v1) events cannot move a request, so they are errors only once a v2 request exists in the
    # thread. Before that (v1 status thread later joined by v2 notes) they stay live and inert.
    has_request = any(e.get('kind') == 'request' and e.get('schema_version') == 2 for e in events)
    for e in events:
        if e.get('schema_version') != 2 and not has_request:
            seen.add(e.get('event_id')); state['accepted_ids'].append(e.get('event_id')); continue
        try:
            apply(state, e, seen)
            seen.add(e['event_id'])
            state['accepted_ids'].append(e['event_id'])
        except (ValueError, KeyError, TypeError) as ex:
            state['errors'].append(f"{e.get('event_id', '?')}: {ex}")
    req = state['request']
    if not req:
        return state
    st = state['status']
    state['claim_expired'] = bool(state['claim'] and st not in {'accepted', 'abandoned'}
        and timestamp(state['claim']['lease_until']) <= now)
    # Lease expiry concerns the worker; it cannot answer a question or receipt
    # owed by the reviewer. Preserve that responsibility until an explicit reply.
    if st in {'working', 'revision'} and state['claim_expired']:
        state['status'] = st = 'stale'
    receiver = state['reviewer']
    state['waiting_on'] = None if st in {'accepted','abandoned'} else receiver if st in {'submitted','blocked'} else state['assigned_to']
    state['next_action'] = {'requested':'依頼と完了条件を読み、引き受ける', 'working':'成果物を作り、提出する',
        'submitted':'成果物と完了条件を照合し、確認済みか差し戻しを返す', 'revision':'指摘に対応し、再提出する',
        'blocked':'相談内容を確認して返答する', 'stale':'担当を取り直す。取り下げが必要なら依頼側に相談する',
        'accepted':'', 'abandoned':''}.get(st, '')
    return state

def apply(s, e, seen):
    def need(ok, msg):
        if not ok: raise ValueError(msg)
    eid, kind = e['event_id'], e['kind']
    need(eid not in seen, 'duplicate event_id')
    need(e.get('schema_version') == 2, 'legacy event cannot change a request workflow')
    need(kind in KINDS, 'unsupported workflow event (use submit, not done)')
    need(not e.get('supersedes'), 'workflow corrections use revise/release; supersedes is legacy-only')
    at = timestamp(e['created_at'])
    if kind == 'note':
        # Status sharing that never moves a request. Allowed before or after a request exists,
        # so agents do not fall back to hand-committed legacy v1 events (measured: sessions of both vendors did).
        return
    req = s['request']
    if kind == 'request':
        need(req is None, 'one request per thread')
        need(e['request_id'] == eid, 'request_id must equal request event_id')
        need(participant_identity(e['assignee']) != identity(e), 'requester and assignee sessions must differ')
        need(bool(e['acceptance'].strip()), 'completion conditions required')
        s.update(request=e, status='requested', assigned_to=e['assignee'], reviewer=address(e['actor']))
        return
    need(req is not None, 'request missing')
    need(e['request_id'] == req['event_id'], 'wrong request_id')
    need(e['project'] == req['project'] and e['source_policy'] == req['source_policy'], 'request scope changed')
    need(s['status'] not in {'accepted','abandoned'}, 'request already closed; create a new request')
    actor = e['actor']['agent']
    reviewer = identity(e) == participant_identity(s['reviewer'])
    worker = identity(e) == participant_identity(s['assigned_to'])
    claim = s['claim']
    if kind == 'handover':
        need(reviewer or actor == 'human', 'only current reviewer or owner can hand over a request')
        need(e.get('reply_to') == req['event_id'], 'handover must name the request')
        target=e['target']; role=e['handover_role']
        other=s['reviewer'] if role=='assignee' else s['assigned_to']
        need(participant_identity(target) != participant_identity(other), 'worker and reviewer sessions must differ')
        if role=='assignee':
            need(s['status'] != 'submitted', 'review or return the current submission before handing over work')
            s.update(assigned_to=target, claim=None, status='blocked' if s['blocker'] else 'requested')
        elif role=='reviewer':
            s['reviewer']=target
        else: raise ValueError('unknown handover role')
    elif kind == 'abandoned':
        need(reviewer, 'only requester session can withdraw')
        s.update(status='abandoned', blocker=None)
    elif kind == 'claim':
        need(worker, 'only addressed session can claim')
        need(s['status'] != 'submitted', 'submission awaits receipt')
        need(timestamp(e['lease_until']) > at, 'lease must be in future')
        need(claim is None or timestamp(claim['lease_until']) <= at, 'another live claim exists; release it first')
        s.update(claim=e, status='blocked' if s['blocker'] else 'working')
    elif kind in {'submit','release'}:
        need(claim is not None and identity(e) == identity(claim), 'only current claimant can submit/release')
        need(e.get('reply_to') == claim['event_id'], 'reply_to must identify current claim')
        if kind == 'release':
            need(s['status'] != 'submitted', 'cannot release a submitted result')
            s.update(claim=None, status='blocked' if s['blocker'] else 'requested')
        else:
            need(s['blocker'] is None, 'question awaits reviewer answer; cannot submit while blocked')
            need(timestamp(claim['lease_until']) > at, 'claim expired; reclaim before submission')
            need(s['status'] != 'submitted', 'already submitted')
            need(e['source_policy'] == 'encrypted-metadata-only' or bool(e.get('deliverables')), 'deliverable location required')
            s.update(submission=e, status='submitted', blocker=None)
    elif kind in {'accept','revise'}:
        need(reviewer, f"only designated reviewer session can review: post with --session {s['reviewer']['session_id']}")
        need(s['status'] == 'submitted', 'no submission awaiting receipt')
        need(e.get('reply_to') == s['submission']['event_id'], 'reply_to must identify latest submission')
        s.update(status='accepted' if kind == 'accept' else 'revision', blocker=None, review=e)
    elif kind == 'blocker':
        need(claim is not None and identity(e) == identity(claim), 'only current claimant can raise a blocker')
        need(s['status'] in {'working','revision','blocked'}, 'cannot block at this stage')
        s.update(status='blocked', blocker=e, question=e, answer=None)
    else:
        need(reviewer or (claim is not None and identity(e) == identity(claim)),
             f"not a workflow participant: post with --session {s['reviewer']['session_id']} (the reviewer side)"
             + (f" or --session {claim['actor']['session_id']} (the current claimant)" if claim is not None else ''))
        if s['status'] == 'blocked' and kind == 'update' and reviewer:
            need(e.get('reply_to') == s['blocker']['event_id'],
                 f"reply_to must identify the blocker being answered: the open blocker is {s['blocker']['event_id']}"
                 f" (reply_to={e.get('reply_to') or 'none'}); answer it with update --reply-to {s['blocker']['event_id']}")
            s.update(status='working' if s['claim'] else 'requested', blocker=None, answer=e)
