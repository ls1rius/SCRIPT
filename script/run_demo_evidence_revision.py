"""Seven-case diagnostic: conservative paraphrase folding, grounded QA, strict IDs.

Uses historical F-derived demo evidence, not the queued H-to-I experiment.
"""
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


def module(name):
    if (Path(__file__).parent/(name+'.py')).exists():
        return importlib.import_module(name)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    return importlib.import_module('script_core.'+name)


def verify(out):
    for name,sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=sha:raise ValueError('Frozen diagnostic changed: '+name)


def prepare(parent,out):
    if out.exists():raise ValueError('Use a fresh demo diagnostic directory')
    verify(parent)
    ids=io.read(parent/'cohort.json')
    if len(ids)!=7:raise ValueError('This diagnostic runs the seven existing demos only')
    core=module('demo_evidence_revision');renderer=module('qa_evidence_view')
    root=Path(__file__).resolve().parents[1]
    out.mkdir(parents=True)
    for name in ['cohort.json','evaluation_only.json','qa_config.json']:
        shutil.copy2(parent/name,out/name)
    for name in ['model.py','prompts.py']:
        target=out/'runtime'/name;target.parent.mkdir(exist_ok=True)
        shutil.copy2(parent/'runtime/qa'/name,target)
    control=out/'control';control.mkdir()
    for source in [Path(__file__),root/'script/controlled_memory_io.py',
                   root/'script_core/demo_evidence_revision.py',root/'script_core/qa_evidence_view.py',root/'script_core/g_rl.py']:
        shutil.copy2(source,control/source.name)
    for uid in ids:
        merged=io.read(parent/'materialized'/uid/'memory.json')
        records={r['id']:r['evidence'] for r in merged['records']}
        payload=dict(observations=[dict(caption_id=r['caption_id'],frame_index=r['frame_index'],
            sample_interval=r['sample_interval'],evidence=records[r['record_id']]) for r in merged['observations']])
        text,audit=renderer.compact_description(payload)
        lines=text.split('\n');rows=[]
        for index,(row,source) in enumerate(zip(payload['observations'],audit['rows'])):
            a=source['prompt_line']-1
            b=audit['rows'][index+1]['prompt_line']-1 if index+1<len(audit['rows']) else len(lines)
            rendered='\n'.join(lines[a:b]);prefix='Frame ['+','.join(__import__('json').dumps(t) for t in row['sample_interval'])+'): '
            if not rendered.startswith(prefix):raise ValueError('Visible source mapping differs')
            rows.append(dict(caption_id=row['caption_id'],frame_index=row['frame_index'],
                interval=row['sample_interval'],body=rendered[len(prefix):]))
        description,projection=core.compact_rows(rows)
        original=io.read(parent/'final_inputs/I_merged_lazy_script'/f'{uid}.json')
        user=original['prompts'][0]
        marker='Temporal entity memory ('
        auxiliary=user[user.index(marker):] if marker in user else ''
        question=io.read(parent/'packets'/f'{uid}.json')['packet']['question']
        question={k:question[k] for k in ['question_id','video_id','question','options']}
        inputs=core.make_input(question,description,original,auxiliary)
        io.write(out/'questions'/f'{uid}.json',question)
        io.write(out/'source_rows'/f'{uid}.json',rows)
        io.write(out/'render_audits'/f'{uid}.json',projection)
        io.write(out/'final_inputs'/f'{uid}.json',inputs)
        (out/'final_evidence').mkdir(exist_ok=True)
        (out/'final_evidence'/f'{uid}.txt').write_text(description+'\n\n'+auxiliary+'\n')
        baseline=io.read(parent/'runs/I_merged_lazy_script'/uid/'result.json')
        io.write(out/'baseline_results'/f'{uid}.json',baseline)
    io.write(out/'experiment.json',dict(created_at=io.now(),version=core.VERSION,planned=7,parent=str(parent),
        evidence_parent='Historical F-derived I-v1 demos, not H or current I.',
        changes=['Source-only conservative local paraphrase normalization and touching-interval union.',
            'Evidence IDs and five concise option-support checks in a single QA generation.',
            'Constrained JSON decoder with fixed A-E keys, bounded short explanations and valid evidence-ID enums.',
            'Strict JSON selected_option; program supplies exact option text.'],
        same='Seven questions, options, selected source captions, auxiliary memory, accepted visuals, checkpoint and greedy generation.',
        no_new_retrieval=True,new_source_extraction_calls=0,answers_reused=False,retries=0,
        development_only=True,not_an_ablation=True,
        resource_policy=dict(preference=['2','1','0','3'],minimum_free_mib=32768,stable_observations=3)))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))


def report(out):
    core=module('g_rl');ids=io.read(out/'cohort.json');truth=io.read(out/'evaluation_only.json')
    cases=[]
    for uid in ids:
        question=io.read(out/'questions'/f'{uid}.json')
        old=io.read(out/'baseline_results'/f'{uid}.json')
        strict=core.parse_answer(old['raw_response'],question['options'])
        path=out/'runs'/uid/'result.json';new=io.read(path) if path.exists() else None
        cases.append(dict(question_id=uid,question=question,gold=str(truth[uid]),baseline=old,
            baseline_strict=strict,revision=new))
    done=[c for c in cases if c['revision'] is not None]
    correct=lambda c,key: str(c[key].get('selected_option'))==c['gold'] and not c[key].get('error')
    summary=dict(updated_at=io.now(),planned=7,completed=len(done),complete=len(done)==7,
        baseline_main_correct=sum(correct(c,'baseline') for c in done),
        baseline_strict_correct=sum(str(c['baseline_strict']['option'])==c['gold'] for c in done),
        revision_correct=sum(correct(c,'revision') for c in done),
        mean_input_tokens=statistics.mean(c['revision']['input_tokens'] for c in done) if done else None,
        mean_qa_seconds=statistics.mean(c['revision']['seconds'] for c in done) if done else None,
        source_rows=sum(io.read(out/'render_audits'/f'{u}.json')['source_rows'] for u in ids),
        rendered_rows=sum(io.read(out/'render_audits'/f'{u}.json')['rendered_rows'] for u in ids),
        new_extraction_calls=0,scope='Seven previously selected badcases; combined diagnostic, not H-to-I evaluation or causal ablation.',
        timing_policy='Shared GPU observations, not a controlled speed comparison.',cases=cases)
    io.write(out/'report.json',summary)
    return summary


def worker(out):
    verify(out);core=module('demo_evidence_revision')
    sys.path.insert(0,str(out/'runtime'))
    from model import Qwen
    import torch
    from lmformatenforcer import JsonSchemaParser
    from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn, build_token_enforcer_tokenizer_data
    cfg=io.read(out/'qa_config.json');began=time.monotonic()
    model=Qwen(cfg['models']['vlm'],temperature=0,device='cuda',max_new_tokens=cfg['models']['generation']['max_new_tokens'])
    tokenizer_data=build_token_enforcer_tokenizer_data(model.tokenizer)
    io.write(out/'setup.json',dict(seconds=time.monotonic()-began,model=cfg['models']['vlm']))
    for index,uid in enumerate(io.read(out/'cohort.json')):
        folder=out/'runs'/uid
        if folder.exists():raise ValueError('No retry or replay of an attempted case')
        inputs=io.read(out/'final_inputs'/f'{uid}.json')
        size=len(model.tokenizer.apply_chat_template([dict(role='system',content=inputs['head']),
            dict(role='user',content=inputs['prompts'][0])],tokenize=True,add_generation_prompt=True))
        if size>cfg['models']['max_input_tokens']:raise ValueError('Input overflow; no truncation')
        io.write(out/'progress.json',dict(index=index+1,question_id=uid,planned=7,updated_at=io.now()))
        io.write(folder/'started.json',dict(input_sha256=io.sha(out/'final_inputs'/f'{uid}.json'),updated_at=io.now()))
        valid_ids={r['evidence_id'] for r in io.read(out/'render_audits'/f'{uid}.json')['rows']}
        schema=core.output_schema(valid_ids)
        parser=JsonSchemaParser(schema)
        prefix=build_transformers_prefix_allowed_tokens_fn(tokenizer_data,parser)
        parser.config.force_json_field_order=True
        encoded=model.tokenizer.apply_chat_template([dict(role='system',content=inputs['head']),
            dict(role='user',content=inputs['prompts'][0])],tokenize=True,add_generation_prompt=True,
            return_dict=True,return_tensors='pt').to(model.model.device)
        began=time.monotonic()
        with torch.inference_mode():
            generated=model.model.generate(**encoded,max_new_tokens=model.max_new_tokens,
                do_sample=False,prefix_allowed_tokens_fn=prefix)
        tokens=generated[0,encoded.input_ids.shape[1]:]
        raw=model.tokenizer.decode(tokens,skip_special_tokens=True).strip()
        seconds=time.monotonic()-began
        result=dict(question_id=uid,selected_option=None,error=None,raw_response=raw,
            input_tokens=int(encoded.input_ids.shape[1]),output_tokens=len(tokens),seconds=seconds,
            input_sha256=io.sha(out/'final_inputs'/f'{uid}.json'),new_QA_calls=1)
        try:
            parsed=core.parse_answer(raw,valid_ids);result.update(selected_option=parsed['option'],parsed=parsed)
            options=io.read(out/'questions'/f'{uid}.json')['options']
            result['selected_option_text']=options[int(parsed['option'])]
        except (ValueError,TypeError,KeyError) as exc:
            result['error']=dict(type=type(exc).__name__,message=str(exc))
        io.write(folder/'result.json',result)
        print('DEMO',index+1,'option',result['selected_option'],'error',result['error'],flush=True)
    report(out)
    return 0


def queue(out):
    verify(out);policy=io.read(out/'experiment.json')['resource_policy']
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Diagnostic already attempted')
        stable={g:0 for g in policy['preference']}
        while True:
            inventory=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.free','--format=csv,noheader,nounits'],text=True)
            rows={v[0]:(v[1],int(v[2])) for line in inventory.splitlines() if (v:=[s.strip() for s in line.split(',')])}
            lease=None
            for g in policy['preference']:
                uuid,free=rows[g];stable[g]=stable[g]+1 if free>=policy['minimum_free_mib'] else 0
                if stable[g]<policy['stable_observations']:continue
                candidate=(Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{uuid}.lock').open('a')
                try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:candidate.close();continue
                lease=candidate;gpu=g;break
            if lease is not None:break
            io.write(out/'queue_status.json',dict(state='waiting_for_gpu',free_mib={g:v[1] for g,v in rows.items()},updated_at=io.now()))
            time.sleep(10)
        with lease:
            uuid=rows[gpu][0]
            env=dict(os.environ,CUDA_VISIBLE_DEVICES=uuid,CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
            command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out)]
            with (out/'worker.log').open('a') as log:
                proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,
                    stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
            launch=dict(controller_pid=os.getpid(),worker_pid=proc.pid,gpu=gpu,command=command,started_at=io.now())
            io.write(out/'launch.json',launch)
            while proc.poll() is None:
                io.write(out/'queue_status.json',dict(launch,state='running',updated_at=io.now()))
                time.sleep(5)
            summary=report(out)
            state='complete' if proc.returncode==0 and summary['complete'] else 'failed'
            io.write(out/'queue_status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
            return proc.returncode or (0 if summary['complete'] else 2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--parent',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if args.parent is None:parser.error('prepare needs --parent demo7 directory')
        prepare(args.parent.resolve(),out)
    elif args.mode=='worker':return worker(out)
    elif args.mode=='queue':return queue(out)
    else:report(out)
    return 0


if __name__=='__main__':raise SystemExit(main())
