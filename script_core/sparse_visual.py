"""Small, optional visual checks after summary navigation has left a concrete gap."""
from copy import deepcopy
import hashlib
from pathlib import Path

from .accounting import metered_cpu
from .budget import ResourceExhausted, ToolFailure, exploration_exhausted
from .state import write_json


LIMITS=dict(max_rounds=1,max_frames=3,max_seconds=60.,max_window_seconds=8.)


def output_schema(operation,inputs):
    from .schemas import record,array,enum,short_text,B
    if operation=='sparse_visual_plan':
        refs=array(enum([c['id'] for c in inputs['captions']]),3);refs['minItems']=1
        options=array(enum([str(i) for i in range(len(inputs['options']))]),5);options['minItems']=2
        return record(dict(needed=B,reason=short_text(200),checks=array(record(dict(
            scene_id=enum(inputs['opened_scenes']),caption_ids=refs,target=short_text(180),
            missing_detail=short_text(180),option_ids=options)),1)))
    fids=[f['frame_id'] for f in inputs['frames']]
    refs=array(enum(fids),3)
    if operation=='sparse_visual_observe':
        return record(dict(description=short_text(240),kind=enum(['state','action','uncertain']),
                           source_frame_ids=refs,unresolved=short_text(180)))
    if operation=='sparse_visual_verify':
        return record(dict(result=enum(['supported','refuted','unresolved']),whole_claim_supported=B,
                           source_frame_ids=refs,evidence=short_text(240),unresolved=short_text(180)))
    raise ValueError(operation)


def select_frames(check,d,frames,opened,used=()):
    """Locate a short window from cited summaries; never sweep the video."""
    scenes={s['id']:s for s in d['scenes']}
    sid=check['scene_id']
    if sid not in opened or sid not in scenes:raise ValueError('Visual check targets an unopened scene')
    ids=list(dict.fromkeys(check['caption_ids']))
    captions={c['id']:c for c in d['captions']}
    if not ids or not set(ids)<=set(scenes[sid]['caption_ids']):
        raise ValueError('Visual check must cite summaries from its selected opened scene')
    a=min(captions[c]['interval'][0] for c in ids)
    b=max(captions[c]['interval'][1] for c in ids)
    if b-a>LIMITS['max_window_seconds']:raise ValueError('Visual target is not a bounded local gap')
    lo=max(scenes[sid]['interval'][0],a-1)
    hi=min(scenes[sid]['interval'][1],b+1)
    if hi-lo>LIMITS['max_window_seconds']:lo=a;hi=b
    available=sorted((f for f in frames if lo<=f['pts']<hi and f['frame_id'] not in used),key=lambda f:f['pts'])
    if len(available)>LIMITS['max_frames']:
        indices=[round(i*(len(available)-1)/(LIMITS['max_frames']-1)) for i in range(LIMITS['max_frames'])]
        available=[available[i] for i in dict.fromkeys(indices)]
    return deepcopy(available)


@metered_cpu('sparse_visual_frame_check')
def checked_frames(frames):
    """Read/hash only the selected cached video frames, never the full video."""
    from PIL import Image
    from .video import visual_quality
    valid=[];invalid=[]
    for frame in frames:
        if hashlib.sha256(Path(frame['path']).read_bytes()).hexdigest()!=frame['sha256']:
            raise ValueError('Selected visual source changed: '+frame['frame_id'])
        with Image.open(frame['path']) as image:
            image=image.convert('RGB')
            if not visual_quality(image)['visual_usable']:invalid.append(frame['frame_id']);continue
            if image.size!=(frame['width'],frame['height']):raise ValueError('Selected frame dimensions changed')
        valid.append(frame)
    return valid,invalid


def observe_and_verify(frames,target,meter):
    """The verifier receives images and the claim, without options or summaries."""
    observation,call=meter.json('sparse_visual_observe',dict(frames=frames,target=target),
        ('description','kind','source_frame_ids','unresolved'))
    record=dict(observation=observation,observation_call_id=call['call_id'],accepted=False)
    if observation['kind']=='uncertain' or not observation['description'].strip() or not observation['source_frame_ids']:
        return record,None
    allowed={f['frame_id']:f for f in frames}
    if not set(observation['source_frame_ids'])<=allowed.keys():raise ValueError('Foreign visual observation citation')
    verification,vcall=meter.json('sparse_visual_verify',dict(frames=frames,
        claim=dict(description=observation['description'],kind=observation['kind'])),
        ('result','whole_claim_supported','source_frame_ids','evidence','unresolved'))
    record.update(verification=verification,verification_call_id=vcall['call_id'])
    refs=list(dict.fromkeys(verification['source_frame_ids']))
    if not set(refs)<=allowed.keys():raise ValueError('Foreign visual verification citation')
    accepted=(verification['result']=='supported' and verification['whole_claim_supported'] and bool(refs)
              and bool(verification['evidence'].strip()) and not verification['unresolved'].strip())
    if observation['kind']=='action' and len({allowed[r]['pts'] for r in refs})<3:
        accepted=False;record['rejection']='An action requires process evidence from three distinct sampled times'
    record['accepted']=bool(accepted)
    if not accepted:return record,None
    support=[allowed[r]['pts'] for r in refs]
    return record,dict(description=observation['description'],kind=observation['kind'],
        interval=[min(support),max(support)],source_frame_ids=refs,
        status='visual_model_checked_sampled_observation',
        evidence=verification['evidence'],verification_call_id=vcall['call_id'],
        scope='Only these sampled images; not proof of absence, total counts, continuous duration or unseen actions')


def supplement(query,d,frames,opened,reviews,scope,meter,target):
    """At most one three-frame check, paid inside the original question budget."""
    diagnostic=dict(enabled=True,limits=deepcopy(LIMITS),triggered=False,rounds=[],observations=[],
                    selected_frame_ids=[],frame_ids=[],invalid_frame_ids=[],seconds=0.,errors=[])
    last=reviews[-1] if reviews else {}
    if last.get('sufficient') or not str(last.get('gap','')).strip():
        diagnostic['skip_reason']='Summaries sufficient or no concrete remaining gap'
        return diagnostic
    if scope and scope['scope']=='exhaustive' and not last.get('coverage_check',{}).get('met',False):
        diagnostic['skip_reason']='Sparse frames cannot replace missing exhaustive temporal coverage'
        return diagnostic
    if not frames or meter.ledger.remaining<20.:
        diagnostic['skip_reason']='No cached frame catalog or insufficient visual allowance'
        return diagnostic
    ledger=meter.ledger;start=ledger.actual;previous=(ledger.task_start,ledger.task_cap)
    ledger.task_start=start;ledger.task_cap=min(LIMITS['max_seconds'],ledger.remaining)
    try:
        caps=[c for c in d['captions'] if any(c['id'] in s['caption_ids'] for s in d['scenes'] if s['id'] in opened)]
        plan,call=meter.json('sparse_visual_plan',dict(question=query['question'],options=query['options'],
            captions=caps,opened_scenes=list(opened),remaining_gap=last['gap'],scope=scope,
            policy='Read images only for a concrete visible option distinction missing from summaries; prefer needed=false.'),
            ('needed','reason','checks'))
        diagnostic.update(plan=plan,plan_call_id=call['call_id'])
        if not plan['needed'] or not plan['checks']:
            diagnostic['skip_reason']='Planner found no necessary local visual check';return diagnostic
        check=plan['checks'][0]
        if (not check['target'].strip() or not check['missing_detail'].strip() or len(set(check['option_ids']))<2):
            raise ValueError('Visual check must identify a concrete gap distinguishing at least two options')
        if ledger.allowance(LIMITS['max_seconds'])<20.:
            diagnostic['skip_reason']='Insufficient remaining allowance for observation and verification';return diagnostic
        selected=select_frames(check,d,frames,opened)
        diagnostic['selected_frame_ids']=[f['frame_id'] for f in selected]
        selected,invalid=checked_frames(selected)
        diagnostic.update(invalid_frame_ids=invalid,triggered=bool(selected),frame_ids=[f['frame_id'] for f in selected])
        if not selected:
            diagnostic['skip_reason']='No valid unread frames in the bounded target interval';return diagnostic
        record,fact=observe_and_verify(selected,check['target'],meter)
        diagnostic['rounds'].append(dict(check=check,**record))
        if fact:diagnostic['observations'].append(fact)
    except (ResourceExhausted,ToolFailure) as exc:
        if not exploration_exhausted(exc):raise
        diagnostic['errors'].append(str(exc));diagnostic['skip_reason']='Visual allowance exhausted; retained summary evidence'
    except ValueError as exc:
        # A nonlocal/ungrounded plan causes no further frame access or retries.
        diagnostic['errors'].append(str(exc));diagnostic['skip_reason']='Rejected visual plan or source validation'
    finally:
        diagnostic['seconds']=ledger.actual-start
        ledger.task_start,ledger.task_cap=previous
        write_json(Path(target)/'sparse_visual.json',diagnostic)
    return diagnostic


def probe(packet,meter,target):
    """GPU branch check, stored outside all formal/smoke accuracy rows."""
    selected,invalid=checked_frames(packet['frames'][:3])
    if len(selected)!=3:raise ValueError('Visual branch probe needs three valid cached source frames')
    record,fact=observe_and_verify(selected,'Describe a directly visible object or hand position, without inferring intent.',meter)
    if 'verification_call_id' not in record:
        # Exercise the same independent verifier even if the observer abstains.
        _,call=meter.json('sparse_visual_verify',dict(frames=selected,claim=dict(
            description='A hand is visible in the sampled images.',kind='state')),
            ('result','whole_claim_supported','source_frame_ids','evidence','unresolved'))
        record['abstention_verifier_probe_call_id']=call['call_id']
    value=dict(passed=True,selected_frame_ids=[f['frame_id'] for f in selected],invalid_frame_ids=invalid,
        record=record,observation=fact,accuracy_used_for_gate=False,
        scope='Targeted GPU branch exercise only; does not force visual checks on evaluation questions')
    write_json(Path(target)/'status.json',value)
    return value
