"""Extend frozen Q4 adaptive to public 500: inherit 300, freshly source and answer 200."""
import argparse,collections,copy,fcntl,hashlib,importlib,json,os,pathlib,shutil,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
REPO=pathlib.Path(__file__).resolve().parents[1]
def read(p):return json.loads(pathlib.Path(p).read_text())
def write(p,v):
 p=pathlib.Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+f'.{os.getpid()}.tmp');tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n');tmp.replace(p)
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def now():return __import__('datetime').datetime.now().astimezone().isoformat()
def load_runtime(out):
 sys.path.insert(0,str(out/'runtime'));return importlib.import_module('script.run_q_v4_experiment')
def pid_identity(pid):
 try:
  stat=pathlib.Path('/proc',str(pid),'stat').read_text().split();return None if stat[2]=='Z' else stat[21]
 except OSError:return None
@contextmanager
def locked(out):
 with (out/'claims.lock').open('a') as f:fcntl.flock(f,fcntl.LOCK_EX);yield

def verify(out):
 def check(item):
  rel,expected=item
  if sha(out/rel)!=expected:raise ValueError('Frozen extension artifact changed: '+rel)
 with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(check,read(out/'frozen_hashes.json').items()))

def prepare(out,old,workers=4):
 assert not out.exists() and 1<=workers<=4
 prior=read(old/'report.json');assert prior['complete'] and prior['completed']==300 and not prior['errors']
 meta=read(REPO/'docs/experiments/Early_Schemes_Records_2026-11-01.json')['records']['full-temporal-videotree-500-20261027-r1']['metadata'];ids=meta['cohorts.json']['500'];oldids=read(old/'cohort.json');newids=meta['cohorts.json']['additional200'];assert len(set(ids))==500 and ids[:300]==oldids and ids[300:]==newids and len(newids)==200
 data=REPO/'data/EgoSchema/egoschema';anno=read(data/'subset_anno.json');caps=read(data/'lavila_subset.json');answers=read(data/'subset_answers.json');assert set(ids)==set(anno)==set(answers)
 for uid in oldids:
  a=anno[uid];question=dict(question_id=uid,video_id=uid,question=a['question'],options=[a[f'option {i}'] for i in range(5)]);assert question==read(old/'cases'/uid/'question.json');assert str(answers[uid])==str(read(old/'evaluation_only.json')[uid])
 out.mkdir(parents=True);(out/'runs').mkdir();(out/'cases').mkdir()
 # The identical old 300 answers and their costs are inherited, never regenerated.
 inherited={}
 for uid in oldids:
  result=old/'runs'/uid/'result.json';assert read(result)['complete'] and not read(result)['error'];inherited[uid]=dict(result=str(result),sha256=sha(result))
  (out/'runs'/uid).symlink_to((old/'runs'/uid).resolve(),target_is_directory=True);(out/'cases'/uid).symlink_to((old/'cases'/uid).resolve(),target_is_directory=True)
 write(out/'inherited_results.json',inherited);write(out/'cohort.json',ids);write(out/'new_case_ids.json',newids);write(out/'evaluation_only.json',{u:answers[u] for u in ids});write(out/'qa_config.json',read(old/'qa_config.json'))
 last_source=REPO/'workdirs/scene-planned-N-L-300-20261104-r4/runs'/oldids[-1]/'result.json';ncfg=read(last_source)['run_config'];assert ncfg['query']['field_extraction_batch_size']==4 and ncfg['models']['max_input_tokens']==32768
 ncfg['models']['cuda_allocator_limit_mib']=30720;write(out/'source_config.json',ncfg);shutil.copy2(old/'qa_expected_artifacts.json',out/'qa_expected_artifacts.json')
 shutil.copytree(old/'runtime',out/'runtime',ignore=shutil.ignore_patterns('__pycache__'));shutil.copy2(REPO/'script/extend_q4_500.py',out/'runtime/script/extend_q4_500.py')
 # Exact model-facing Q4 runtime files are compared before any extension calls.
 original_runtime={str(p.relative_to(old/'runtime')):sha(p) for p in (old/'runtime').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
 assert all(sha(out/'runtime'/n)==h for n,h in original_runtime.items());write(out/'validation/parent_runtime_hashes.json',original_runtime)
 write(out/'historical_videotree.json',dict(source='docs/experiments/Early_Schemes_Records_2026-11-01.json',report_at=meta['report.json']['updated_at'],reports={k:meta['report.json']['reports'][k]['videotree'] for k in ['300','additional200','500']},timing=meta['report.json']['timing'],token_metrics_available=False,per_question_predictions_available=False))
 write(out/'experiment.json',dict(version=prior['version'],plus_version=prior['plus_version'],mode='adaptive',early_stop=True,planned=500,inherited=300,new=200,workers=workers,parent_Q4=str(old),parent_report_sha256=sha(old/'report.json'),source_snapshot='Unmodified parent Q4 runtime; final effective N batch4 source provider configuration recovered from its last saved run_config, allocator explicitly bounded at 30GiB',source_config_origin=str(last_source),source_policy='New questions rebuild the full N source pipeline from original captions/features/frames, then use only original source observations; N answers/facts are not passed as Q evidence',source_stage_budget='Original 600-second directory plus original 1500-second online allowance; 45-second N answer reserve; no retries',q4_stage_budget='Original 3600 including full N budget once; 150-second call cap and 90-second dual-reader reserve; no retries',q4_allocator_mib=22528,source_allocator_mib=30720,source_admission_mib=32768,q4_admission_mib=24576,allowed_gpu_indices=['0','1'],max_new_caption_scenes=4,source_batch=4,q4_filter_batch=4,model_methods_changed=False,training=False,adapter=None,old_answers_reused_only_for_identical_old_300=True,new_answers_reused=False,evaluation_scope='Public labeled 500, not full unlabeled 5031; old 300 previously analyzed and new 200 previously evaluated by older SCRIPT/VideoTree, not untouched holdout. Freeze method before new Q4 outcomes. Report old300/new200/all500 separately.',at=now()))
 started=time.monotonic();sys.path.insert(0,str(out/'runtime'))
 def packet(uid):
  import torch
  from PIL import Image
  from script_core.temporal_entity import partition
  from script_core.config import digest
  a=anno[uid];question=dict(question_id=uid,video_id=uid,question=a['question'],options=[a[f'option {i}'] for i in range(5)]);captions=[dict(id=f'C{i}',interval=[i,i+1],text=t) for i,t in enumerate(caps[uid])]
  feature=(REPO/'data/EgoSchema/egoschema_features'/f'{uid}.pt').resolve();meta=read(feature.with_suffix('.json'));assert meta['fps']==1 and meta['shape']==[len(captions),1280] and meta['frame_indices']==list(range(len(captions))) and pathlib.Path(meta['model']).resolve()==pathlib.Path(ncfg['models']['visual_encoder']).resolve()
  features=torch.load(feature,map_location='cpu',weights_only=True);scenes=partition(uid,features.tolist(),captions)
  frames=[]
  for i in range(len(captions)):
   path=(REPO/'data/EgoSchema/egoschema_frames'/uid/f'{i}.jpg').resolve()
   with Image.open(path) as im:width,height=im.size
   frames.append(dict(frame_id=f'F{i}',pts=float(i),path=str(path),width=width,height=height,sha256=sha(path)))
  p=dict(question=question,captions=captions,frames=frames,caption_records_hash=digest(captions),feature_uri=str(feature),feature_hash=sha(feature),feature_metadata=meta)
  write(out/'source_inputs'/f'{uid}.json',p);write(out/'cases'/uid/'question.json',question)
  return dict(question_id=uid,captions=len(captions),frames=len(frames),scenes=len(scenes),packet_sha256=sha(out/'source_inputs'/f'{uid}.json'))
 with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(packet,newids))
 write(out/'validation/source_preflight.json',dict(passed=True,new_cases=200,all_frames_and_features_hashed=True,question_options_match_dataset=True,labels_in_model_packets=False,model_loaded=False,seconds=time.monotonic()-started,rows=rows))
 write(out/'claims.json',{u:dict(state='complete',inherited=True,index=i+1) for i,u in enumerate(oldids)})
 # Static inputs/runtime are frozen. Derived per-case Q sources get their own hash binding.
 files=[p for folder in ['runtime','source_inputs','validation'] for p in (out/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
 files += [out/n for n in ['experiment.json','cohort.json','new_case_ids.json','evaluation_only.json','qa_config.json','source_config.json','qa_expected_artifacts.json','inherited_results.json','historical_videotree.json']]
 write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in files});verify(out);write(out/'queue_status.json',dict(state='prepared',updated_at=now()));build_report(out);print('PREPARED_Q4_500',out,flush=True)

def prepare_case(out,uid):
 q=load_runtime(out);from script_core import o_sentence_filter as o
 src=out/'source_runs'/uid;nr=read(src/'result.json');assert nr.get('answer') and not nr.get('error') and not list(src.rglob('*.pending.json'));packet=read(out/'source_inputs'/f'{uid}.json');case=out/'cases'/uid;question=read(case/'question.json');assert packet['question']==question;bundle=read(src/'partial_search/evidence_bundle.json')
 semantic=src/'partial_temporal/semantic_sources.json'
 if semantic.exists():sentences=o.sentence_sources(read(semantic)['units'])
 else:
  marker=read(src/'partial_temporal/N/empty_evidence.json');assert marker['observed_sources']==[] and not bundle.get('opened_scenes');sentences=[]
 directory=read(src/'directory.json');query=read(src/'query.json');assert query['question']==question['question'] and query['options']==question['options']
 archive=dict(question_id=uid,scope=query['scope'],captions=directory['captions'],scenes=[{k:s[k] for k in ['id','interval','caption_ids','description']} for s in directory['scenes']],inherited_N_budgeted_seconds=nr['cost_summary']['budgeted_total'],parent_files={str(src/n):sha(src/n) for n in ['directory.json','query.json','result.json']})
 assert 0<=archive['inherited_N_budgeted_seconds']<3000
 write(case/'sentences.json',sentences);write(case/'original_qa.json',bundle['qa_input']);write(case/'archive.json',archive)
 files={str(p.relative_to(src)):sha(p) for p in src.rglob('*') if p.is_file()};write(case/'source_binding.json',dict(source=str(src),files={n:sha(case/n) for n in ['question.json','sentences.json','original_qa.json','archive.json']},original_source_audit=dict(parent=str(src),source_files=files,opened_scenes=bundle.get('opened_scenes',[]),selected_seconds=bundle.get('selected_seconds',[]),explicit_empty_evidence=not semantic.exists(),N_answers_and_extracted_roles_excluded=True),source_provider='Same frozen N source pipeline, source text conversion identical to original O-to-Q adapter',new_navigation=True))
 return case

def capacity(out):
 q=load_runtime(out);from transformers import AutoTokenizer
 from script_core.o_batch_generation import messages_for
 cfg=read(out/'qa_config.json');tok=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True);model=read(pathlib.Path(cfg['models']['vlm'])/'config.json');native=model.get('max_position_embeddings') or model.get('text_config',{}).get('max_position_embeddings');maxima={}
 def check(kind,messages,uid):
  tokens=tok.apply_chat_template(messages,tokenize=True,add_generation_prompt=True);n=len(tokens)
  if n>maxima.get(kind,{}).get('tokens',0):maxima[kind]=dict(tokens=n,question_id=uid)
  if n>cfg['models']['max_input_tokens'] or n+1536>native:raise ValueError('Capacity overflow without truncation: '+uid+' '+kind)
 for uid in read(out/'new_case_ids.json'):
  p=read(out/'source_inputs'/f'{uid}.json');question=p['question'];sources=[dict(id='S'+str(i),text=c['text'],source_intervals=[c['interval']],source_caption_ids=[c['id']],source_type='caption',source_status='caption_asserted') for i,c in enumerate(p['captions'])]
  for req in q.r.windows(question['question'],sources):check('filter',messages_for('q3_filter',req)[0],uid)
  request=dict(question=question['question'],options=question['options'],observations=q.r.views(sources),regions=[dict(id='first_half',interval=[0,90]),dict(id='second_half',interval=[90,180])]);check('proposal',messages_for('q4_propose',request)[0],uid)
  for i in range(5):
   for j in range(5):
    if i!=j:check('pair',messages_for('q4_pair',dict(request,candidate_index=i,alternative_index=j))[0],uid)
  obs=q.r.views(sources);req=dict(question=question['question'],options=question['options'],observations=obs[:8],context=obs[8:],witness_ids=[o['source_id'] for o in obs]);check('reconsider',messages_for('q3_reconsider',req)[0],uid)
  # Original head/question/option reader bytes are checked dynamically after N; capacity uses same head and complete source closure here.
  original=read(pathlib.Path(read(out/'experiment.json')['parent_Q4'])/'cases'/read(out/'cohort.json')[0]/'original_qa.json');original=copy.deepcopy(original)
  original=dict(query_id=uid,head=original['head'],prompts=['  Description: placeholder\n\n###\n\n Questions: '+question['question']+' \n Options: \n '+''.join(chr(65+i)+': '+s+' \n' for i,s in enumerate(question['options']))+'\n###\n\n The prediction is:'])
  for compact in [False,True]:
   final=q.q.final_input(original,[q.q.observation(s) for s in sources],compact);check('reader_compact' if compact else 'reader',[dict(role='system',content=final['head']),dict(role='user',content=final['prompts'][0])],uid)
 write(out/'validation/capacity_preflight.json',dict(passed=True,new_cases=200,maxima=maxima,configured=cfg['models']['max_input_tokens'],native=native,scope='Full original caption closure and all ordered option pairs; no truncation or model generation. Actual derived N sources checked again at runtime.'))
 print('CAPACITY_PASS',maxima,flush=True)

def build_report(out):
 ids=read(out/'cohort.json');oldset=set(read(out/'inherited_results.json'));gold=read(out/'evaluation_only.json');cases=[read(out/'runs'/u/'result.json') for u in ids if (out/'runs'/u/'result.json').exists()];new=[c for c in cases if c['question_id'] not in oldset];old=[c for c in cases if c['question_id'] in oldset];historical=read(out/'historical_videotree.json');groups={}
 for label,rows,planned in [('original300',old,300),('additional200',new,200),('all500',cases,500)]:
  arms={}
  for a in ['Q','Qplus']:
   n=len(rows);correct=sum(str(c['arms'][a]['answer']['selected_option'])==str(gold[c['question_id']]) for c in rows);arms[a]=dict(completed=n,correct=correct,accuracy_among_completed=correct/n if n else None,accuracy=correct/planned if n==planned else None,mean_input_tokens=sum(c['arms'][a]['input_tokens'] for c in rows)/n if n else None,mean_answer_seconds=sum(c['arms'][a]['answer_seconds'] for c in rows)/n if n else None)
  vk={'original300':'300','additional200':'additional200','all500':'500'}[label];groups[label]=dict(planned=planned,completed=len(rows),arms=arms,videotree=historical['reports'][vk],comparison_complete=len(rows)==planned)
 def sums(kind):
  calls=[]
  for f in (out/'workers').glob('*/loads/*.json'):
   d=read(f)
   if d['kind']==kind:calls.append(d['seconds'])
  return sum(calls)
 source=[read(out/'source_runs'/u/'result.json') for u in read(out/'new_case_ids.json') if (out/'source_runs'/u/'result.json').exists()]
 gates={}
 for kind,paths in [('Q4','*/validation/q4/functional.json'),('Q4_reader','*/validation/q4/reader_pair/result.json')]:gates[kind]=sum(read(f)['cost_summary']['total'] for f in (out/'workers').glob(paths))
 gates['source_N']=sum(read(f)['seconds'] for f in (out/'workers').glob('*/validation/source/functional/cost_summary.json'))
 gates['source_batch']=sum(read(f)['total'] for f in (out/'workers').glob('*/validation/source_batch/validation/cost_summary.json'))
 failure=read(out/'failure.json') if (out/'failure.json').exists() else None
 result=dict(version=read(out/'experiment.json')['version'],plus_version=read(out/'experiment.json')['plus_version'],mode='adaptive',planned=500,completed=len(cases),complete=len(cases)==500 and failure is None,errors=int(failure is not None),arms=groups['all500']['arms'],groups=groups,cases=cases,new_Q4_processing_seconds=sum(c['cost_summary']['total'] for c in new),inherited_Q4_processing_seconds=sum(c['cost_summary']['total'] for c in old),new_source_N_processing_seconds=sum(c['cost_summary']['total'] for c in source),source_N_budget_dependency_seconds=sum(c['source_dependency_seconds'] for c in cases),new_source_completed=len(source),failed_stage_costs=[read(f) for f in (out/'workers').glob('*/failure_costs.json')],setup_seconds=dict(Q4=sums('q4'),source_N=sums('source')),validation_seconds=gates,scheduler_failure=failure,token_scope='Final reader input per arm; aggregate new call token totals are computed after completion. Source and Q4 stages separate; historical successful work is not free.',evaluation_scope=read(out/'experiment.json')['evaluation_scope'],videotree_partial_subset_limitation='Archived original300/new200/all500 metrics only; exact historical VideoTree accuracy on an arbitrary partial new subset is unavailable.',updated_at=now())
 write(out/'report.json',result);return result

def claim(out,wid):
 with locked(out):
  if (out/'failure.json').exists():return None
  claims=read(out/'claims.json')
  for index,uid in enumerate(read(out/'cohort.json'),1):
   if uid in claims:continue
   if (out/'runs'/uid).exists() or (out/'source_attempts'/uid).exists():raise ValueError('Preexisting unclaimed attempt; refusing implicit retry')
   claims[uid]=dict(state='running',index=index,worker_id=wid,pid=os.getpid(),started_at=now());write(out/'claims.json',claims);return index,uid
 return None

def fail(out,wid,e):
 with locked(out):
  if not (out/'failure.json').exists():write(out/'failure.json',dict(worker_id=wid,pid=os.getpid(),type=type(e).__name__,message=str(e),at=now()))

def worker(out,wid):
 q=load_runtime(out);verify(out);from script_core.budget import ModelWorker
 from script.gpu_queue import acquire
 from script.run_evidence_planner_experiment import verify_model
 verify_model(out);folder=out/'workers'/str(wid);folder.mkdir(parents=True,exist_ok=False);source_validated=False;load_number=0;current={}
 def status(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**current,**kw))
 @contextmanager
 def stage_model(kind):
  nonlocal load_number
  cfg=read(out/('qa_config.json' if kind=='q4' else 'source_config.json'));policy=copy.deepcopy(read(out/'experiment.json'));resource=dict(preference=['0','1'],minimum_free_mib=24576 if kind=='q4' else 32768,maximum_utilization=100,stable_observations=3,admission_mode='memory_shared',allocator_limit_mib=22528 if kind=='q4' else 30720)
  def waiting(d):
   if (out/'failure.json').exists():raise RuntimeError('Pool halted after failure')
   status('waiting_for_gpu',kind=kind,**d)
  backend=ModelWorker(cfg)
  try:
   with acquire(resource,waiting) as (gpu,_):
    os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id'];status('loading',kind=kind,gpu=gpu['index']);started=time.monotonic();details=backend.call('prepare_vlm',{},600);load_number+=1;write(folder/'loads'/f'{load_number:04}.json',dict(kind=kind,seconds=time.monotonic()-started,gpu=gpu,details=details));status('query',kind=kind,gpu=gpu['index']);yield backend,cfg,gpu
    backend.close(graceful=True)
  finally:backend.close(graceful=True);os.environ.pop('VIDEOAGENT_GPU_LEASE',None)
 try:
  # Original real Q4 gates before formal source acquisition; all probe costs separate.
  with stage_model('q4') as (backend,cfg,gpu):
   status('validation',kind='q4',gpu=gpu['index']);from script.q_v4_validation import validate_live;validate_live(backend,cfg,folder/'validation/q4')
  while True:
   item=None
   with stage_model('source') as (backend,cfg,gpu):
    if not source_validated:
     status('validation',kind='source',gpu=gpu['index']);from script.run_n_scene_experiment import functional
     gate_started=time.monotonic()
     try:functional(folder/'validation/source',backend,cfg)
     finally:write(folder/'validation/source/functional/cost_summary.json',dict(seconds=time.monotonic()-gate_started))
     from script.run_n_parallel_validation import run as validate_source_batch
     validate_source_batch(folder/'validation/source_batch',backend,cfg,out)
     source_validated=True
    item=claim(out,wid)
    if item is None:break
    index,uid=item;current.update(index=index,question_id=uid);status('query',kind='source',gpu=gpu['index'])
    import torch
    from script_core.scheme4 import answer
    packet=read(out/'source_inputs'/f'{uid}.json');assert sha(packet['feature_uri'])==packet['feature_hash'];assert all(sha(f['path'])==f['sha256'] for f in packet['frames']);features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist();target=out/'source_attempts'/uid/'run';result=answer(packet,features,cfg,backend,target);assert result.get('answer') and not result.get('error');assert read(target/'partial_temporal/result.json')['memory_variant']=='N';assert not result['cost_summary']['budget_violation'];assert not list(target.rglob('*.pending.json'))
    search=read(target/'partial_search/result.json');assert search['opened_scenes']==search['retrieval']['candidate_order'][:len(search['opened_scenes'])]
    dest=out/'source_runs'/uid;dest.parent.mkdir(parents=True,exist_ok=True);target.rename(dest);target.symlink_to(dest,target_is_directory=True)
   if item is None:break
   case=prepare_case(out,uid)
   if (out/'failure.json').exists():raise RuntimeError('Pool failure before Q4 reader stage; source work retained')
   with stage_model('q4') as (backend,cfg,gpu):
    for n,h in read(case/'source_binding.json')['files'].items():assert sha(case/n)==h
    def progress(phase,**detail):status('query',kind='q4',gpu=gpu['index'],phase=phase,**detail)
    target=out/'attempts'/uid;result=q.run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target,read(case/'archive.json'),'adaptive',progress)
    assert result['complete'] and not result['error'] and not result['cost_summary']['budget_violation'];assert result['arms']['Q']['retained_evidence_hash']==result['arms']['Qplus']['retained_evidence_hash'];assert not list(target.rglob('*.pending.json'));q.base.publish(out,uid,target)
   print('Q4_500_COMPLETE',wid,index,uid,flush=True)
  status('complete')
 except BaseException as e:
  # Preserve measured committed costs even when a stage cannot publish.
  from script.run_m_scene_experiment import leaf_calls
  uid=current.get('question_id');costs=dict(question_id=uid,source_committed_seconds=0.,q4_committed_seconds=0.,pending_reservations=[])
  if uid:
   src=out/'source_attempts'/uid/'run';qp=out/'attempts'/uid
   if src.exists() and not (out/'source_runs'/uid).exists():costs['source_committed_seconds']=sum(x['actual_cost'] for x in leaf_calls(src))
   if qp.exists() and not (out/'runs'/uid).exists() and (qp/'calls.jsonl').exists():costs['q4_committed_seconds']=sum(json.loads(x)['actual_cost'] for x in (qp/'calls.jsonl').read_text().splitlines())
   for path in [src,qp]:
    if path.exists() and not path.is_symlink():costs['pending_reservations'].extend([dict(read(f),path=str(f)) for f in path.rglob('*.pending.json')])
  write(folder/'failure_costs.json',costs);fail(out,wid,e);status('failed',error=f'{type(e).__name__}: {e}');raise

def supervise(out):
 verify(out);assert read(out/'validation/capacity_preflight.json')['passed']
 with (out/'controller.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if (out/'launch.json').exists():raise ValueError('Already launched; no duplicate scheduler')
  ps=[];rows=[];env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
  for wid in range(read(out/'experiment.json')['workers']):
   command=[sys.executable,'-u',str(out/'runtime/script/extend_q4_500.py'),'worker','--out',str(out),'--worker-id',str(wid)]
   with (out/f'worker-{wid}.log').open('x') as log:p=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
   ps.append(p);rows.append(dict(id=wid,pid=p.pid,identity=pid_identity(p.pid),command=command))
  write(out/'launch.json',dict(controller_pid=os.getpid(),identity=pid_identity(os.getpid()),workers=rows,at=now()))
  while True:
   statuses={str(r['id']):read(out/'workers'/str(r['id'])/'status.json') for r in rows if (out/'workers'/str(r['id'])/'status.json').exists()}
   for p,row in zip(ps,rows):
    if p.poll() is not None and statuses.get(str(row['id']),{}).get('stage') not in ['complete','failed']:fail(out,row['id'],RuntimeError('Worker exited without terminal status'))
   with locked(out):report=build_report(out)
   alive=any(p.poll() is None for p in ps);state='running' if alive else 'complete' if report['complete'] else 'failed';write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),workers=statuses,effective_worker_count=len(rows),allowed_gpu_indices=['0','1'],updated_at=now(),error=report['scheduler_failure']))
   if not alive:return
   time.sleep(10)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','capacity','supervise','worker','report']);parser.add_argument('--out',type=pathlib.Path,required=True);parser.add_argument('--old',type=pathlib.Path);parser.add_argument('--workers',type=int,default=4);parser.add_argument('--worker-id',type=int);a=parser.parse_args();out=a.out.resolve()
 if a.action=='prepare':prepare(out,a.old.resolve(),a.workers)
 elif a.action=='worker':worker(out,a.worker_id)
 elif a.action=='report':print(json.dumps(build_report(out),ensure_ascii=False))
 else:globals()[a.action](out)
