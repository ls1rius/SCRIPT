from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from script_core import p_sentence_filter as p,o_sentence_filter as o
from script_core.o_batch_generation import messages_for,validate_packet
from script_core.backend import LocalModels
from script_core.config import load_config,digest
from script import controlled_memory_io as io
from script.o_batched_matrix import packet_for
from script.run_p_sentence_experiment import run_case,batch


def judged(decisions):
    return {k:dict(fidelity='unfaithful' if v=='drop_unfaithful' else 'faithful',decision=v) for k,v in decisions.items()}


def empty_review():
    return dict(assessments={key:dict(main_support=[],main_counter=[],main_status='unknown',
        detail_support=[],detail_counter=[],details_status='unknown') for key in 'ABCDE'})


class FakeBackend:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def call(self,operation,req,timeout):
        self.calls.append((operation,deepcopy(req)))
        if operation==p.ANSWER:return dict(data=dict(selected_option='0',raw='prediction: A'),usage=dict(input_tokens=20,output_tokens=4))
        if self.fail and req['operation']==p.RELEVANCE:raise RuntimeError('injected')
        validate_packet(req);rows=[]
        for row in req['items']:
            if req['operation']==p.EXTRACT:
                data=dict(extraction_status='extracted',actions=[dict(actor='R9',verb='moves',roles={k:({'patient':'a crate','destination':'shelf'}.get(k)) for k in p.extraction_schema()['properties']['actions']['items']['properties']['roles']['properties']})],attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
            elif req['operation']==p.GROUND:
                data=dict(faithful=row['request']['candidate'].get('field')!='actor')
            elif req['operation']==p.RELEVANCE:
                p.validate_request(row['request'])
                data=dict(decisions={u['id']:'keep_local' for u in row['request']['candidates']})
            elif req['operation']==p.REVIEW:data=empty_review()
            else:raise AssertionError('Unexpected stage '+req['operation'])
            rows.append(dict(source_id=row['source_id'],scope=row['scope'],request_hash=digest(row['request']),data=data))
        return dict(items=rows,usage=dict(input_tokens=10,output_tokens=10))


class PTests(unittest.TestCase):
    def setup_case(self):
        cfg=load_config('configs/script_N.yaml');cfg['query'].update(tool_transient_retries=0,json_repair_retries=0)
        q=dict(question_id='q',question='What happens to the crate?',options=['moved','opened','painted','broken','unchanged'])
        sources=[dict(id=f'S{i}',text=f'R9 moves a crate onto shelf {i}.',source_intervals=[[i,i+1]]) for i in range(5)]
        original=dict(head='Imagine a scene and give confidence.',prompts=['legacy examples: cleaning'])
        return cfg,q,sources,original

    def test_only_original_question_sentence_candidates_reach_model(self):
        _,q,s,_=self.setup_case();q['evidence_task']={'bad':'invented task'}
        req=p.filter_input(q,s[0],[dict(id='U0',kind='raw_sentence',group='S0',text=s[0]['text'])])
        messages,*_=messages_for(p.FILTER,req)
        self.assertEqual(json.loads(messages[1]['content']),req)
        self.assertEqual(set(req),{'question','sentence','candidates'})
        self.assertNotIn('invented task',str(messages));self.assertNotIn('cleaning',str(messages))
        for extra in ['options','evidence_task','task_brief','other_question']:
            with self.assertRaises(ValueError):p.schema(dict(req,**{extra:'bad'}))

    def test_single_decision_cannot_carry_conflicting_reason_or_label(self):
        req=dict(question='Q?',sentence='x',candidates=[dict(id='U0')])
        for decision in p.DECISIONS:
            judgments,labels,_=p.selection(req,dict(decisions=judged(dict(U0=decision))))
            self.assertEqual(labels['U0'],int(decision.startswith('keep_')))
        for extra in ['labels','relevance','observation']:
            with self.assertRaises(ValueError):p.selection(req,dict(decisions=judged(dict(U0='drop_unrelated')),**{extra:1}))

    def test_dropped_subject_or_object_is_never_restored(self):
        candidates=[dict(id='U0',kind='action_field',group='a',field='actor',value='wrong identity'),dict(id='U1',kind='action_field',group='a',field='verb',value='moves'),dict(id='U2',kind='action_field',group='a',field='patient',value='wrong object')]
        req=dict(question='What moved?',sentence='A parcel moved.',candidates=candidates)
        _,labels,selected=p.selection(req,dict(decisions=judged(dict(U0='drop_unfaithful',U1='keep_local',U2='drop_unfaithful'))))
        self.assertEqual(labels,dict(U0=0,U1=1,U2=0));self.assertEqual(selected,[candidates[1]])

    def test_packet_rejects_cross_question_and_contaminated_extraction(self):
        _,q,s,_=self.setup_case();req=p.filter_input(q,s[0],[dict(id='U0')])
        packet=packet_for('q','original_question_only',p.FILTER,[('S0',req),('S1',req)])
        packet['items'][1]['scope']['question_id']='other'
        with self.assertRaises(ValueError):validate_packet(packet)
        with self.assertRaises(ValueError):packet_for('q','source_only',p.EXTRACT,[('S0',dict(sentence='x',question='leak'))])
        with self.assertRaises(ValueError):packet_for('q','original_question_only',p.EXTRACT,[('S0',dict(sentence='x'))])

    def test_fresh_pipeline_uses_P_extraction_no_restoration_and_neutral_reader(self):
        cfg,q,s,original=self.setup_case();backend=FakeBackend()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case';result=run_case(backend,cfg,q,s,original,folder)
            self.assertTrue(result['complete'])
            self.assertTrue(all(op==p.BATCH for op,_ in backend.calls[:-1]));self.assertEqual(backend.calls[-1][0],p.ANSWER)
            self.assertEqual([req['operation'] for _,req in backend.calls[:2]],[p.EXTRACT]*2)
            self.assertTrue(all(req['operation'] in (p.GROUND,p.RELEVANCE) for _,req in backend.calls[2:-2]))
            self.assertEqual(backend.calls[-2][1]['operation'],p.REVIEW)
            for _,packet in backend.calls[2:-2]:
                for row in packet['items']:
                    if packet['operation']==p.GROUND:self.assertNotIn('question',row['request'])
                    else:self.assertTrue(all(u['field']!='actor' for u in row['request']['candidates']))
            decision=io.read(folder/'sentences/S0/selection.json')
            self.assertEqual(decision['labels']['U0'],0);self.assertEqual(decision['program_overrides'],[])
            final=io.read(folder/'final_input.json');self.assertNotIn('cleaning',str(final));self.assertNotIn('Imagine a scene',str(final))
            payload=json.loads(final['prompts'][0]);self.assertEqual(list(payload['options'].values()),q['options'])
            self.assertEqual(payload['option_review'],empty_review());self.assertTrue(result['reader_review_called'])
            self.assertEqual(result['reader_total_seconds'],result['reader_review_seconds']+result['answer_seconds'])
            self.assertEqual(payload['question'],q['question']);self.assertFalse(list(folder.rglob('composition.json')))
            with self.assertRaisesRegex(ValueError,'already attempted'):run_case(backend,cfg,q,s,original,folder)

    def test_frozen_extraction_diagnostic_cost_once_labels_fresh(self):
        cfg,q,s,original=self.setup_case()
        with tempfile.TemporaryDirectory() as td:
            first=Path(td)/'a';run_case(FakeBackend(),cfg,q,s,original,first)
            frozen={x['id']:io.read(first/'sentences'/x['id']/'extraction.json') for x in s}
            backend=FakeBackend();result=run_case(backend,cfg,q,s,original,Path(td)/'b',frozen=frozen,inherited_extraction_seconds=7.)
            self.assertTrue(all(req['operation'] in (p.GROUND,p.RELEVANCE,p.REVIEW) for _,req in backend.calls[:-1]))
            self.assertEqual(result['cost_summary']['shared_dependency_seconds'],7.)

    def test_failures_preserve_cost_without_retry(self):
        cfg,q,s,original=self.setup_case();backend=FakeBackend(fail=True)
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case'
            with self.assertRaisesRegex(RuntimeError,'injected'):run_case(backend,cfg,q,s,original,folder)
            result=io.read(folder/'result.json');self.assertFalse(result['complete']);self.assertTrue(result['error'])
            self.assertEqual(sum(req.get('operation')==p.RELEVANCE for _,req in backend.calls),1)
            self.assertNotIn(p.ANSWER,[op for op,_ in backend.calls])

    def test_empty_evidence_calls_only_reader(self):
        cfg,q,_,original=self.setup_case();backend=FakeBackend()
        with tempfile.TemporaryDirectory() as td:
            result=run_case(backend,cfg,q,[],original,Path(td)/'case')
            self.assertEqual(result['retained_sentences'],0);self.assertEqual([op for op,_ in backend.calls],[p.ANSWER])

    def test_wrong_row_mapping_rejected(self):
        class Meter:
            def call(self,operation,packet):return dict(items=[dict(source_id='other')]),{}
        with self.assertRaisesRegex(ValueError,'identity'):batch(Meter(),'q',p.FILTER,[('S0',dict(question='Q',sentence='s',candidates=[dict(id='U0')]))])

    def test_backend_routes_P_separately(self):
        backend=LocalModels(load_config('configs/script_N.yaml'))
        with patch('script_core.o_batch_generation.generate_batch',return_value={'ok':True}):
            for op in [p.EXTRACT,p.FILTER,p.GROUND,p.RELEVANCE,p.REVIEW]:
                self.assertEqual(backend.call(p.BATCH,dict(operation=op)),{'ok':True})
                with self.assertRaises(ValueError):backend.call('o_independent_batch',dict(operation=op))
            with self.assertRaises(ValueError):backend.call(p.BATCH,dict(operation=o.COMPOSE))


class PTokenizerTests(unittest.TestCase):
    def test_actual_tokenizer_P_batch_has_independent_single_decisions(self):
        import torch
        from types import SimpleNamespace
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.o_batch_generation import generate_batch
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        requests=[('S0',dict(question='What happens?',sentence='ALPHA',candidates=[dict(id='U0')])),('S1',dict(question='What happens?',sentence='BETA',candidates=[dict(id='U1')]))]
        packet=packet_for('q','original_question_only',p.FILTER,requests)
        expected=[dict(decisions=judged(dict(U0='keep_local'))),dict(decisions=judged(dict(U1='drop_unfaithful')))];test=self
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=32768);generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kwargs):
                rows=[]
                for i,(_,req) in enumerate(requests):
                    own=tokenizer.apply_chat_template(messages_for(p.FILTER,req)[0],tokenize=True,add_generation_prompt=True)
                    test.assertEqual(input_ids[i][attention_mask[i].bool()].tolist(),own);state=input_ids[i].clone()
                    for char in json.dumps(expected[i],separators=(',',':')):
                        for token in tokenizer.encode(char,add_special_tokens=False):
                            test.assertIn(token,prefix_allowed_tokens_fn(i,state));state=torch.cat([state,torch.tensor([token])])
                    test.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(i,state));rows.append(torch.cat([state,torch.tensor([tokenizer.eos_token_id])]))
                width=max(map(len,rows));return torch.stack([torch.cat([row,torch.full((width-len(row),),kwargs['pad_token_id'])]) for row in rows])
        model=Model();backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(model,SimpleNamespace(tokenizer=tokenizer)),cfg=dict(models=dict(max_input_tokens=16384)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),model_tokens={},deadline=None,check_deadline=lambda:None)
        actual=generate_batch(backend,packet);self.assertEqual([r['data'] for r in actual['items']],expected)
        self.assertTrue(all(r['decoder']=='P-single-decision-batch-v2' for r in actual['items']))


if __name__=='__main__':unittest.main()

class PArgumentGrammarTests(unittest.TestCase):
    def test_sparse_modifiers_map_without_rewriting_or_inference(self):
        wire=dict(extraction_status='extracted',actions=[dict(subject='The operator',predicate='does not turn',arguments={k:{'how_or_direction':'quickly','place_where':'under the roof'}.get(k) for k in p.ARGUMENT_ROLES})],
            attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
        original=deepcopy(wire)
        output=p.normalize_extraction(wire)
        self.assertEqual(output['actions'],[dict(actor='The operator',verb='does not turn',roles=dict(manner='quickly',location='under the roof'))])
        self.assertEqual(wire,original)
        wire['actions'][0]['arguments']['unknown_role']='elsewhere'
        with self.assertRaises(ValueError):p.normalize_extraction(wire)

    def test_bad_model_roles_are_not_silently_fixed(self):
        wire=dict(extraction_status='extracted',actions=[dict(subject='Person X',predicate='walks',arguments={k:'Person X' if k=='object_acted_on' else None for k in p.ARGUMENT_ROLES})],
            attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
        self.assertEqual(p.normalize_extraction(wire)['actions'][0]['roles']['patient'],'Person X')
        # Semantic validation must catch this: conversion is not a hidden repair.

    def test_source_only_wire_contract_does_not_change_O_schema(self):
        messages,schema,_,_=messages_for(p.EXTRACT,dict(sentence='Someone moved.'))
        self.assertEqual(set(schema['properties']['actions']['items']['properties']),{'subject','predicate','arguments'})
        self.assertEqual(set(o.extraction_schema()['properties']['actions']['items']['properties']),{'actor','verb','roles'})
        self.assertEqual(json.loads(messages[-1]['content']),dict(sentence='Someone moved.'))


class PRepairContractTests(unittest.TestCase):
    setup_case=PTests.setup_case
    def test_caption_metadata_has_a_reversible_offset_and_no_identity_rewrite(self):
        for text,expected in [('#META Agent Z31 moves.','Agent Z31 moves.'),
                              ('  #source-tag  A surveyor maps #sector.','A surveyor maps #sector.'),
                              ('Agent #Z31 moves.','Agent #Z31 moves.'),
                              ('#label_without_content','#label_without_content')]:
            view=p.source_view(text)
            self.assertEqual(view['sentence'],expected)
            self.assertEqual(view['original'][view['content_start']:],expected)
            self.assertEqual(p.extraction_input(dict(text=text)),dict(sentence=expected))
        with self.assertRaises(ValueError):p.source_view('#META   ')

    def test_metadata_is_separated_at_extraction_and_both_filter_stages(self):
        cfg,q,s,original=self.setup_case();s=[dict(s[0],text='#SOURCE '+s[0]['text'])]
        backend=FakeBackend()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case';run_case(backend,cfg,q,s,original,folder)
            for _,packet in backend.calls[:-1]:
                for row in packet['items']:
                    self.assertNotIn('#SOURCE',json.dumps(row['request']))
            self.assertEqual(io.read(folder/'sentences/S0/source_view.json')['original'],s[0]['text'])

    def test_extraction_format_error_stops_without_fallback_answer_or_retry(self):
        class Broken(FakeBackend):
            def call(self,op,req,timeout):
                result=super().call(op,req,timeout)
                if req.get('operation')==p.EXTRACT:
                    result['items'][0].update(parse_error='injected invalid JSON',data=None)
                return result
        cfg,q,s,original=self.setup_case();backend=Broken()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case'
            with self.assertRaisesRegex(ValueError,'extraction failed at S0'):
                run_case(backend,cfg,q,s,original,folder)
            self.assertEqual(len(backend.calls),1)
            self.assertTrue(io.read(folder/'result.json')['error'])
            self.assertEqual(io.read(folder/'sentences/S0/extraction.json')['output']['parse_error'],'injected invalid JSON')
            self.assertFalse((folder/'answer.json').exists())

    def test_valid_unavailable_extraction_uses_same_content_view_without_losing_source(self):
        class Unavailable(FakeBackend):
            def call(self,op,req,timeout):
                result=super().call(op,req,timeout)
                if req.get('operation')==p.EXTRACT:
                    for row in result['items']:
                        row['data']=dict(extraction_status='unavailable',**{k:[] for k in o.CATEGORIES})
                return result
        cfg,q,s,original=self.setup_case();s=[dict(s[0],text='#META '+s[0]['text'])]
        backend=Unavailable()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case';run_case(backend,cfg,q,s,original,folder)
            decision=io.read(folder/'sentences/S0/selection.json')
            self.assertEqual(decision['source'],s[0])
            self.assertEqual(decision['candidates'][0]['text'],p.source_view(s[0]['text'])['sentence'])
            self.assertNotIn('#META',json.dumps(io.read(folder/'final_input.json')))

    def test_review_cannot_add_text_facts_foreign_refs_or_change_options(self):
        _,q,_,_=self.setup_case()
        rows=[dict(source_id='S0',information=[dict(kind='action',actor='R9',verb='moves')])]
        req=p.review_input(rows,q);output=empty_review()
        output['assessments']['A']['main_support']=['S0'];p.validate_review(req,output)
        for field,value in [('main_support',['S999']),('explanation','invented event')]:
            bad=deepcopy(output);bad['assessments']['A'][field]=value
            with self.assertRaises(ValueError):p.validate_review(req,bad)
        bad=deepcopy(output);bad['assessments']['A']['main_support']=['S0','S0']
        with self.assertRaises(ValueError):p.validate_review(req,bad)
        badreq=deepcopy(req);badreq['other_question']='leak'
        with self.assertRaises(ValueError):p.review_schema(badreq)
        final=p.final_input({},rows,q,review=output);payload=json.loads(final['prompts'][0])
        self.assertEqual(payload['observations'],rows);self.assertEqual(list(payload['options'].values()),q['options'])
        with self.assertRaises(ValueError):packet_for('q','source_only',p.REVIEW,[('reader',req)])
        with self.assertRaises(ValueError):packet_for('q','answer_with_options',p.REVIEW,[('reader',req),('other',req)])

    def test_bad_review_retains_failure_and_never_calls_final_answer(self):
        class Broken(FakeBackend):
            def call(self,op,req,timeout):
                result=super().call(op,req,timeout)
                if req.get('operation')==p.REVIEW:
                    result['items'][0]['data']['assessments']['A']['main_support']=['other-question-source']
                return result
        cfg,q,s,original=self.setup_case();backend=Broken()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'case'
            with self.assertRaises(ValueError):run_case(backend,cfg,q,s[:1],original,folder)
            self.assertNotIn(p.ANSWER,[op for op,_ in backend.calls])
            self.assertEqual(sum(req.get('operation')==p.REVIEW for _,req in backend.calls),1)
            self.assertTrue(io.read(folder/'result.json')['error'])

    def test_actual_review_tokenizer_supports_all_options_and_empty_refs(self):
        import torch
        from types import SimpleNamespace
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.o_batch_generation import generate_batch
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        _,q,_,_=self.setup_case()
        request=p.review_input([dict(source_id='S0',information=[dict(kind='action',actor='Z31',verb='waits')])],q)
        expected=empty_review();expected['assessments']['E'].update(main_support=['S0'],main_status='direct')
        test=self
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=32768);generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kwargs):
                state=input_ids[0].clone()
                for char in json.dumps(expected,separators=(',',':')):
                    for token in tokenizer.encode(char,add_special_tokens=False):
                        test.assertIn(token,prefix_allowed_tokens_fn(0,state));state=torch.cat([state,torch.tensor([token])])
                test.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(0,state))
                return torch.stack([torch.cat([state,torch.tensor([tokenizer.eos_token_id])])])
        model=Model();backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(model,SimpleNamespace(tokenizer=tokenizer)),
            cfg=dict(models=dict(max_input_tokens=32768)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),model_tokens={},deadline=None,check_deadline=lambda:None)
        row=generate_batch(backend,packet_for('q','answer_with_options',p.REVIEW,[('reader',request)]))['items'][0]
        self.assertIsNone(row['parse_error']);self.assertEqual(row['data'],expected)
        self.assertEqual(json.loads(row['prompt'][-1]['content']),request)


class PSourceWitnessTests(unittest.TestCase):
    def request(self,field,value,sentence='The curator T47 lifts a vessel.',verb='lifts'):
        units=[dict(id='U0',kind='action_field',group='actions_0',field=field,value=value)]
        if field!='verb':units.append(dict(id='U1',kind='action_field',group='actions_0',field='verb',value=verb))
        return p.ground_input(dict(question='unused',sentence=sentence,candidates=units),units[0])

    def test_wrong_candidate_value_is_hidden_from_source_locator(self):
        req=self.request('actor','INVENTED PERSON')
        messages,schema,_,_=messages_for(p.GROUND,req)
        payload=json.loads(messages[-1]['content'])
        self.assertEqual(set(payload),{'sentence','field','event_predicate','role_definition'})
        self.assertNotIn('INVENTED PERSON',json.dumps(messages));self.assertNotIn('unused',json.dumps(messages))
        self.assertEqual(set(schema['properties']),{'source_span'})

    def test_identity_and_object_match_full_witness_without_restoration(self):
        for candidate,expected in [('The curator T47',True),('The curator',False),('T47 T47',False),('The curator t47',False)]:
            req=self.request('actor',candidate);before=deepcopy(req)
            self.assertEqual(p.normalize_ground(req,dict(source_span='The curator T47')),dict(faithful=expected))
            self.assertEqual(req,before)
        self.assertEqual(p.normalize_ground(self.request('patient','a vessel'),dict(source_span='a vessel')),dict(faithful=True))
        self.assertFalse(p.normalize_ground(self.request('patient','vessel'),dict(source_span='a vessel'))['faithful'])

    def test_negation_absent_roles_and_nonliteral_witnesses(self):
        source='An analyst does not rotate a dial.'
        req=self.request('verb','rotate',source,'rotate')
        self.assertFalse(p.normalize_ground(req,dict(source_span='does not rotate'))['faithful'])
        self.assertFalse(p.normalize_ground(self.request('body_part','sideways','A rover moves sideways.','moves'),dict(source_span=None))['faithful'])
        req=self.request('patient','a vessel')
        self.assertEqual(p.ground_assessment(req,dict(source_span='a bowl')),dict(faithful=False,outcome='unsupported_witness'))
        with self.assertRaises(ValueError):p.normalize_ground(req,dict(source_span=123))

    def test_modifier_source_preposition_and_unicode_whitespace(self):
        req=self.request('destination','an arch','A rover moves toward an arch.','moves')
        self.assertTrue(p.normalize_ground(req,dict(source_span='toward an arch'))['faithful'])
        req=self.request('actor','研究员 Ω7','研究员  Ω7 moves.','moves')
        self.assertTrue(p.normalize_ground(req,dict(source_span='研究员 Ω7'))['faithful'])

    def test_model_case_variation_recovers_source_but_never_changes_candidate_identity(self):
        req=self.request('actor','The curator T47')
        self.assertTrue(p.normalize_ground(req,dict(source_span='the curator t47'))['faithful'])
        req=self.request('actor','The curator t47')
        self.assertFalse(p.normalize_ground(req,dict(source_span='the curator t47'))['faithful'])
        req=self.request('actor','Agent X','Agent X meets agent x.','meets')
        self.assertEqual(p.ground_assessment(req,dict(source_span='agent x')),dict(faithful=False,outcome='ambiguous_witness'))

    def test_nominal_head_matches_only_its_source_bound_qualifiers(self):
        for candidate,expected in [('The curator T47',True),('The curator',False),('T47 T47',False),('The curator t47',False)]:
            self.assertEqual(p.normalize_ground(self.request('actor',candidate),dict(source_span='T47'))['faithful'],expected)
        self.assertTrue(p.normalize_ground(self.request('patient','a vessel'),dict(source_span='vessel'))['faithful'])
        self.assertFalse(p.normalize_ground(self.request('actor','The curator T47 lifts'),dict(source_span='T47'))['faithful'])
        self.assertFalse(p.normalize_ground(self.request('actor','Another curator T47'),dict(source_span='T47'))['faithful'])

    def test_actual_tokenizer_independent_witnesses_and_record_boolean(self):
        import torch
        from types import SimpleNamespace
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.o_batch_generation import generate_batch
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        requests=[self.request('actor','The curator'),self.request('patient','a vessel'),
                  self.request('body_part','sideways','A rover moves sideways.','moves'),
                  p.ground_input(dict(sentence='The lamp is blue.',candidates=[]),dict(id='U0',kind='raw_sentence',text='The lamp is blue.'))]
        wire=[dict(source_span='The curator T47'),dict(source_span='a vessel'),dict(source_span='invented anatomical part'),dict(faithful=True)]
        test=self
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=32768);generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kwargs):
                rows=[]
                for i,req in enumerate(requests):
                    own=tokenizer.apply_chat_template(messages_for(p.GROUND,req)[0],tokenize=True,add_generation_prompt=True)
                    test.assertEqual(input_ids[i][attention_mask[i].bool()].tolist(),own)
                    state=input_ids[i].clone()
                    for char in json.dumps(wire[i],separators=(',',':')):
                        for token in tokenizer.encode(char,add_special_tokens=False):
                            test.assertIn(token,prefix_allowed_tokens_fn(i,state));state=torch.cat([state,torch.tensor([token])])
                    test.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(i,state))
                    rows.append(torch.cat([state,torch.tensor([tokenizer.eos_token_id])]))
                width=max(map(len,rows))
                return torch.stack([torch.cat([r,torch.full((width-len(r),),kwargs['pad_token_id'])]) for r in rows])
        model=Model();backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(model,SimpleNamespace(tokenizer=tokenizer)),
            cfg=dict(models=dict(max_input_tokens=32768)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),model_tokens={},deadline=None,check_deadline=lambda:None)
        output=generate_batch(backend,packet_for('witness-test','source_only',p.GROUND,[(f'S{i}',r) for i,r in enumerate(requests)]))
        self.assertEqual([r['data'] for r in output['items']],[dict(faithful=v) for v in [False,True,False,True]])
        self.assertEqual([r['wire_data'] for r in output['items']],wire)
        self.assertTrue(all(not r['parse_error'] for r in output['items']))
        self.assertEqual(output['items'][2]['grounding_audit'],dict(faithful=False,outcome='unsupported_witness'))
