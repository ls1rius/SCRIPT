"""J: frozen I evidence, conservative merging, audited grounding then selection."""
import argparse
import fcntl
import importlib
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARM='J_grounded_reader'
PARENT_ARM='I_merged_lazy_script'


def module(name):
    if (Path(__file__).parent/(name+'.py')).exists():return importlib.import_module(name)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    return importlib.import_module('script_core.'+name)


def verify(out):
    for name,sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=sha:raise ValueError('Frozen J artifact changed: '+name)


def prepare(parent,out,demo=False,gpu_index='3'):
    if out.exists():raise ValueError('Use a fresh J output directory')
    verify(parent);policy=io.read(parent/'experiment.json');ids=io.read(parent/'cohort.json')
    if policy.get('arm')!=PARENT_ARM:raise ValueError('J requires sealed I inputs')
    if demo:
        if len(ids)!=7:raise ValueError('Demo mode requires the existing seven-case cohort')
    elif policy.get('parent_memory_variant')!='H':
        raise ValueError('Formal J must inherit H-derived I, not historical F demos')
    for uid in ids:
        result=parent/'runs'/PARENT_ARM/uid/'result.json'
        inputs=parent/'final_inputs'/PARENT_ARM/f'{uid}.json'
        if not result.exists() or io.read(result).get('input_sha256')!=io.sha(inputs):
            raise ValueError('I answer/input dependency is incomplete: '+uid)
    core=module('grounded_reader');renderer=module('qa_evidence_view')
    root=Path(__file__).resolve().parents[1]
    out.mkdir(parents=True)
    for name in ['cohort.json','evaluation_only.json','qa_config.json']:shutil.copy2(parent/name,out/name)
    runtime=out/'runtime';runtime.mkdir()
    for name in ['model.py','prompts.py']:shutil.copy2(parent/'runtime/qa'/name,runtime/name)
    control=out/'control';control.mkdir()
    # A queue snapshot has these siblings; workspace preparation uses project files.
    for name,source in [('run_grounded_reader_experiment.py',Path(__file__)),
        ('controlled_memory_io.py',Path(__file__).parent/'controlled_memory_io.py'),
        ('gpu_queue.py',Path(__file__).parent/'gpu_queue.py'),
        *[(n,Path(__file__).parent/n if (Path(__file__).parent/n).exists() else root/'script_core'/n)
          for n in ['grounded_reader.py','demo_evidence_revision.py','qa_evidence_view.py','g_rl.py']]]:
        shutil.copy2(source,control/name)
    for uid in ids:
        memory=io.read(parent/'materialized'/uid/'memory.json')
        records={r['id']:r['evidence'] for r in memory['records']}
        observations=[dict(caption_id=r['caption_id'],frame_index=r['frame_index'],sample_interval=r['sample_interval'],
            evidence=records[r['record_id']]) for r in memory['observations']]
        text,audit=renderer.compact_description(dict(observations=observations));lines=text.split('\n');rows=[]
        for index,(row,source) in enumerate(zip(observations,audit['rows'])):
            a=source['prompt_line']-1;b=audit['rows'][index+1]['prompt_line']-1 if index+1<len(audit['rows']) else len(lines)
            rendered='\n'.join(lines[a:b]);prefix='Frame ['+','.join(__import__('json').dumps(t) for t in row['sample_interval'])+'): '
            if not rendered.startswith(prefix):raise ValueError('Source/visible assertion mapping differs')
            rows.append(dict(caption_id=row['caption_id'],frame_index=row['frame_index'],interval=row['sample_interval'],body=rendered[len(prefix):]))
        description,projection=core.compact_rows(rows)
        original=io.read(parent/'final_inputs'/PARENT_ARM/f'{uid}.json');user=original['prompts'][0]
        marker='Temporal entity memory (';auxiliary=user[user.index(marker):] if marker in user else ''
        question=io.read(parent/'packets'/f'{uid}.json')['packet']['question']
        question={k:question[k] for k in ['question_id','video_id','question','options']}
        inputs=core.make_input(question,description,original,auxiliary,projection)
        for folder,value in [('questions',question),('source_rows',rows),('render_audits',projection),('final_inputs',inputs)]:
            io.write(out/folder/f'{uid}.json',value)
        (out/'final_evidence').mkdir(exist_ok=True)
        (out/'final_evidence'/f'{uid}.txt').write_text(description+'\n\n'+auxiliary+'\n')
        io.write(out/'baseline_results'/f'{uid}.json',io.read(parent/'runs'/PARENT_ARM/uid/'result.json'))
    parent_report=io.read(parent/'report.json')
    io.write(out/'experiment.json',dict(created_at=io.now(),arm=ARM,version=core.VERSION,planned=len(ids),
        parent_I=str(parent),parent_memory_variant='historical_F_demo' if demo else 'H_to_I',demo_only=demo,
        changes=['Source-blind conservative wording normalization and adjacent intervals, with complete source restoration.',
            'Atomic option excerpts and verbatim source expansion; observed/inferred/unknown/contradicted kept separate.',
            'Necessary lexical/actor/negative-assertion guards run before final selection.',
            'Two fixed constrained calls: grounding, then selection using audited matches. Strict exact option ID and program-supplied option text.'],
        inherited_construction_seconds=parent_report.get('inherited_construction_seconds',0),
        no_new_retrieval=True,new_source_extraction_calls=0,answers_reused=False,retries=0,reader_calls_per_question=2,
        limitations='Literal quotation and lexical guards do not prove semantic entailment. Do not select versions by seven-case accuracy.',
        resource_policy=dict(preference=['2','1','0','3'] if demo else str(gpu_index).split(','),minimum_free_mib=32768,maximum_utilization=10,stable_observations=3)))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))


def report(out):
    ids=io.read(out/'cohort.json');truth=io.read(out/'evaluation_only.json');legacy=module('g_rl')
    cases=[]
    for uid in ids:
        question=io.read(out/'questions'/f'{uid}.json');old=io.read(out/'baseline_results'/f'{uid}.json')
        strict=legacy.parse_answer(old.get('raw_response',''),question['options'])
        p=out/'runs'/uid/'result.json';new=io.read(p) if p.exists() else None
        cases.append(dict(question_id=uid,question=question,gold=str(truth[uid]),baseline=old,baseline_strict=strict,J=new))
    done=[c for c in cases if c['J'] is not None]
    good=lambda c:c['J']['selected_option']==c['gold'] and not c['J']['error']
    correct=sum(good(c) for c in done)
    summary=dict(updated_at=io.now(),planned=len(ids),completed=len(done),complete=len(done)==len(ids),correct=correct,
        accuracy_among_completed=correct/len(done) if done else None,
        baseline_correct=sum(c['baseline'].get('selected_option')==c['gold'] and not c['baseline'].get('error') for c in done),
        baseline_strict_correct=sum(str(c['baseline_strict']['option'])==c['gold'] for c in done),
        corrected_vs_strict=[c['question_id'] for c in done if good(c) and str(c['baseline_strict']['option'])!=c['gold']],
        regressed_vs_strict=[c['question_id'] for c in done if not good(c) and str(c['baseline_strict']['option'])==c['gold']],
        format_errors=sum(bool(c['J']['error']) for c in done),
        mean_input_tokens=statistics.mean(c['J']['input_tokens'] for c in done) if done else None,
        mean_qa_seconds=statistics.mean(c['J']['seconds'] for c in done) if done else None,
        downgraded_claims=sum(len(c['J'].get('parsed',{}).get('downgrades',[])) for c in done),
        selected_without_usable_support=sum(not c['J'].get('parsed',{}).get('selected_has_usable_support',False) for c in done),
        inherited_construction_seconds=io.read(out/'experiment.json')['inherited_construction_seconds'],
        source_rows=sum(io.read(out/'render_audits'/f'{u}.json')['source_rows'] for u in ids),
        rendered_rows=sum(io.read(out/'render_audits'/f'{u}.json')['rendered_rows'] for u in ids),
        new_source_extraction_calls=0,reader_calls_per_question=2,
        token_scope='Sum of grounding and final selection input tokens per question.',
        timing_policy='Shared GPU observations, not a controlled speed comparison.',cases=cases)
    io.write(out/'report.json',summary);return summary


def worker(out):
    verify(out);core=module('grounded_reader');sys.path.insert(0,str(out/'runtime'))
    from model import Qwen
    import torch
    from lmformatenforcer import JsonSchemaParser
    from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn,build_token_enforcer_tokenizer_data
    cfg=io.read(out/'qa_config.json');began=time.monotonic()
    model=Qwen(cfg['models']['vlm'],temperature=0,device='cuda',max_new_tokens=cfg['models']['generation']['max_new_tokens'])
    tokenizer_data=build_token_enforcer_tokenizer_data(model.tokenizer)
    io.write(out/'setup.json',dict(seconds=time.monotonic()-began,model=cfg['models']['vlm']))

    def generate(inputs,schema,folder,name):
        io.write(folder/f'{name}_input.json',inputs)
        encoded=model.tokenizer.apply_chat_template([dict(role='system',content=inputs['head']),dict(role='user',content=inputs['prompts'][0])],
            tokenize=True,add_generation_prompt=True,return_dict=True,return_tensors='pt').to(model.model.device)
        if encoded.input_ids.shape[1]>cfg['models']['max_input_tokens']:raise ValueError('J context overflow; no truncation')
        parser=JsonSchemaParser(schema);prefix=build_transformers_prefix_allowed_tokens_fn(tokenizer_data,parser)
        parser.config.force_json_field_order=True
        began=time.monotonic()
        with torch.inference_mode():
            generated=model.model.generate(**encoded,max_new_tokens=model.max_new_tokens,do_sample=False,prefix_allowed_tokens_fn=prefix)
        tokens=generated[0,encoded.input_ids.shape[1]:];raw=model.tokenizer.decode(tokens,skip_special_tokens=True).strip()
        usage=dict(raw=raw,input_tokens=int(encoded.input_ids.shape[1]),output_tokens=len(tokens),seconds=time.monotonic()-began)
        io.write(folder/f'{name}_output.json',usage)
        return usage

    # Positive/negative synthetic source probe, independent of benchmark labels.
    probe_question=dict(question='Which action is explicitly observed?',options=[
        'C cuts wood.','C paints wood.','C walks.','C nails wood.','C types.'])
    probe_rows=[dict(caption_id='P0',interval=[0,1],body='#C C wipes hands.'),
        dict(caption_id='P1',interval=[1,2],body='#C C cuts wood.'),
        dict(caption_id='P2',interval=[2,3],body='#C C never paints wood.')]
    description,probe_audit=core.compact_rows(probe_rows)
    probe_input=core.make_input(probe_question,description,dict(query_id='J-functional'),auxiliary='',audit=probe_audit)
    probe=generate(probe_input,core.answer_schema(set(core.quote_map(probe_audit)),core.option_fragments(probe_question['options']),core.claim_candidates(probe_question['options'],probe_audit)),out/'functional','grounding')
    checked_probe=core.audit_grounding(probe['raw'],probe_question['options'],probe_audit)
    passed=any(c['relation']=='observed' for c in checked_probe['option_checks']['A']) and all(
        c['relation'] not in {'observed','inferred'} for c in checked_probe['option_checks']['D'])
    io.write(out/'validation/functional.json',dict(passed=passed,benchmark_labels_used=False,seconds=probe['seconds'],
        input_tokens=probe['input_tokens'],output_tokens=probe['output_tokens'],checked=checked_probe,updated_at=io.now()))
    if not passed:raise ValueError('J positive/negative source grounding functional gate failed')

    for index,uid in enumerate(io.read(out/'cohort.json')):
        folder=out/'runs'/uid
        if folder.exists():raise ValueError('J case already attempted; no retry')
        io.write(folder/'started.json',dict(input_sha256=io.sha(out/'final_inputs'/f'{uid}.json'),updated_at=io.now()))
        io.write(out/'progress.json',dict(index=index+1,question_id=uid,stage='grounding',updated_at=io.now()))
        question=io.read(out/'questions'/f'{uid}.json');audit=io.read(out/'render_audits'/f'{uid}.json')
        inputs=io.read(out/'final_inputs'/f'{uid}.json');calls=[]
        result=dict(question_id=uid,selected_option=None,error=None,input_sha256=io.sha(out/'final_inputs'/f'{uid}.json'))
        try:
            ground=generate(inputs,core.answer_schema(set(core.quote_map(audit)),core.option_fragments(question['options']),core.claim_candidates(question['options'],audit)),folder,'grounding');calls.append(ground)
            checked=core.audit_grounding(ground['raw'],question['options'],audit)
            io.write(folder/'audited_grounding.json',checked)
            selection=core.selection_input(question,checked,inputs['query_id'],inputs)
            io.write(out/'progress.json',dict(index=index+1,question_id=uid,stage='selection',updated_at=io.now()))
            answer=generate(selection,core.object_schema(dict(selected_option=dict(type='string',enum=list('ABCDE')))),folder,'selection');calls.append(answer)
            parsed=core.finish(answer['raw'],question['options'],checked)
            result.update(selected_option=parsed['option'],selected_option_text=parsed['option_text'],parsed=parsed,raw_response=answer['raw'])
        except (ValueError,KeyError,TypeError) as exc:
            result['error']=dict(type=type(exc).__name__,message=str(exc))
        result.update(input_tokens=sum(c['input_tokens'] for c in calls),output_tokens=sum(c['output_tokens'] for c in calls),
            seconds=sum(c['seconds'] for c in calls),reader_calls=len(calls))
        io.write(folder/'result.json',result)
        print('J',index+1,'option',result['selected_option'],'error',result['error'],flush=True)
        report(out)
    return 0


def queue(out):
    verify(out);policy=io.read(out/'experiment.json')['resource_policy']
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('J already attempted')
        from gpu_queue import acquire
        def waiting(details):
            io.write(out/'queue_status.json',dict(state='waiting_for_gpu',
                controller_pid=os.getpid(),updated_at=io.now(),**details))
        with acquire(policy, waiting) as (device, lease_fds):
            gpu=device['index'];uuid=device['uuid']
            env=dict(os.environ,CUDA_VISIBLE_DEVICES=uuid,CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
            command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out)]
            with (out/'worker.log').open('a') as log:
                proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,pass_fds=lease_fds)
            launch=dict(controller_pid=os.getpid(),worker_pid=proc.pid,gpu=gpu,gpu_uuid=uuid,command=command,started_at=io.now());io.write(out/'launch.json',launch)
            while proc.poll() is None:
                io.write(out/'queue_status.json',dict(launch,state='running',updated_at=io.now()));time.sleep(5)
            summary=report(out);state='complete' if proc.returncode==0 and summary['complete'] and not summary['format_errors'] else 'failed'
            io.write(out/'queue_status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
            return proc.returncode or (0 if state=='complete' else 2)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--parent',type=Path);parser.add_argument('--demo',action='store_true')
    parser.add_argument('--gpu-index',default='3');args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if args.parent is None:parser.error('prepare requires --parent I directory')
        prepare(args.parent.resolve(),out,args.demo,args.gpu_index)
    elif args.mode=='worker':return worker(out)
    elif args.mode=='queue':return queue(out)
    else:report(out)
    return 0


if __name__=='__main__':raise SystemExit(main())
