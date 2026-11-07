import copy,json,tempfile,unittest
from pathlib import Path
from script_core import q_v4 as v4
from script_core.config import digest
from script.q_v4_validation import probe_requests,synthetic_case
from script.q_repair_validation import source


def proposal(req,choice='A'):
    ids=[o['source_id'] for o in req['observations']]
    temporal={z['id']:next((o['source_id'] for o in req['observations'] if any(a<z['interval'][1] and b>z['interval'][0] for a,b in o['intervals'])),'none') for z in req['regions']}
    return dict(choice=choice if ids else 'undetermined',w0=ids[-1] if ids else 'none',w1='none',temporal=temporal)


def pair(req,ready=True):
    p=proposal(req);ready=ready and bool(req['observations'])
    return dict(choice='A' if ready else 'U',w0=p['w0'],w1='none',basis='direct' if ready else 'unknown',
                gap='none' if ready else 'action',missing='' if ready else 'What happens later?',temporal=p['temporal'])



def assessment(req,ready=True,choice='A'):
    p=proposal(req,choice);labels=v4.letters(req)
    return dict(proposal=p,pairs={k:pair(req,ready) for k in labels if k!=p['choice']} if p['choice'] in labels else {},regions=req['regions'],option_labels=labels)


class QV4Tests(unittest.TestCase):
    def test_positive_comparison_does_not_require_proving_alternative_false(self):
        req=probe_requests()[0][1];a=assessment(req,choice='C')
        self.assertTrue(v4.certificate(a)['sufficient'])
        for i,v in enumerate(a['pairs'].values()):v4.validate_verdict('q4_pair',dict(req,candidate_index=2,alternative_index=i),v)

    def test_unresolved_missing_comparison_unknown_and_time_block(self):
        req=probe_requests()[0][1];base=assessment(req,choice='C')
        for change in ['unresolved','opposite','incomplete','unknown','no_witness']:
            a=copy.deepcopy(base)
            if change=='unresolved':a['pairs']['A']['choice']='U';a['pairs']['A']['gap']='action'
            elif change=='opposite':a['pairs']['A']['choice']='B'
            elif change=='incomplete':a['pairs'].pop('B')
            elif change=='unknown':a['proposal']['choice']='undetermined'
            else:a['proposal']['w0']='none'
            self.assertFalse(v4.certificate(a)['sufficient'],change)
        self.assertFalse(v4.certificate(assessment(probe_requests()[-2][1]))['sufficient'])

    def test_wrong_time_foreign_refs_and_shared_identity_rejected(self):
        req=probe_requests()[-2][1];req=dict(req,candidate_index=0,alternative_index=1);v=pair(req)
        v['temporal']['second_half']='S0'
        with self.assertRaises(ValueError):v4.validate_verdict('q4_pair',req,v)
        v=pair(req);v['w0']='foreign'
        with self.assertRaises(ValueError):v4.validate_verdict('q4_pair',req,v)
        req['alternative_index']=0
        with self.assertRaises(ValueError):v4.validate_request('q4_pair',req)

    def test_supported_requires_witness_and_no_gap(self):
        req=dict(probe_requests()[0][1],candidate_index=0,alternative_index=1);v=pair(req)
        v['w0']='none'
        with self.assertRaises(ValueError):v4.validate_verdict('q4_pair',req,v)
        v=pair(req);v['gap']='action'
        with self.assertRaises(ValueError):v4.validate_verdict('q4_pair',req,v)
        req=probe_requests()[9][1];p=proposal(req);v4.validate_verdict('q4_propose',req,p)
        self.assertFalse(v4.certificate(assessment(req))['sufficient'])

    def test_partial_witness_can_nominate_rescue_without_auto_keep(self):
        req=probe_requests()[0][1];a=assessment(req,False)
        self.assertIn('S0',v4.witness_ids(v4.certificate(a)))

    def test_packet_scope_and_exact_text_binding(self):
        from script_core.o_batch_generation import validate_packet,messages_for
        req=dict(probe_requests()[0][1],candidate_index=2,alternative_index=0)
        scope=dict(question_id='q',condition='evidence_audit_with_options')
        packet=dict(scope=scope,operation='q4_pair',items=[dict(source_id='W0',scope=dict(scope),request=req)])
        validate_packet(packet);wire=json.loads(messages_for('q4_pair',req)[0][1]['content'])
        self.assertEqual(wire['option_A'],req['options'][2]);self.assertEqual(wire['option_B'],req['options'][0])
        self.assertNotIn('candidate_index',wire)
        packet['items'][0]['scope']['question_id']='another'
        with self.assertRaises(ValueError):validate_packet(packet)
        bad=copy.deepcopy(req);bad['gold']='C'
        with self.assertRaises(ValueError):v4.validate_request('q4_pair',bad)

    def test_qualified_facts_are_not_sufficient_for_unqualified_claims(self):
        req=dict(question='What occurred?',options=['M rotates the dial.','M removes the dial.'],
            observations=v4.views([source('S0','M might rotate the dial.',0)]),regions=[],candidate_index=0,alternative_index=1)
        value=pair(req)
        self.assertTrue(v4.uncertainty_block(req,value))
        a=assessment(req);a['qualification_blocks']={'B':True}
        self.assertFalse(v4.certificate(a)['sufficient'])
        req['options'][0]='M might rotate the dial.'
        self.assertFalse(v4.uncertainty_block(req,value))
        self.assertFalse(v4.qualified('M does not rotate the dial.'))
        self.assertTrue(v4.qualified('If called, M rotates the dial.'))

    def test_exact_request_cache_is_per_question_context_and_operation(self):
        from script.run_q_v4_experiment import ask
        class Meter:
            count=0
            def call(self,op,payload):
                self.count+=1
                rows=[dict(source_id=i['source_id'],scope=i['scope'],request_hash=digest(i['request']),parse_error=None,ended_normally=True,data=proposal(i['request'])) for i in payload['items']]
                return dict(items=rows),dict(output_ref='original-ref')
        m=Meter();req=probe_requests()[0][1]
        ask(m,'id','q4_propose',[req]);ask(m,'id','q4_propose',[copy.deepcopy(req)]);self.assertEqual(m.count,1)
        self.assertEqual(m._q4_reuses[0]['output_ref'],'original-ref')
        ask(m,'other','q4_propose',[req]);self.assertEqual(m.count,2)
        changed=copy.deepcopy(req);changed['observations'][0]['text']='An altered observation.'
        ask(m,'id','q4_propose',[changed]);self.assertEqual(m.count,3)

    def test_adaptive_control_pipeline_no_audit_leakage_and_no_free_retry(self):
        from script.run_q_v4_experiment import run_case
        class Fake:
            def call(self,operation,payload,*args,**kwargs):
                if operation=='q4_independent_batch':
                    result=[]
                    for item in payload['items']:
                        req=item['request'];op=payload['operation']
                        if op in ('q3_filter','q3_reconsider'):
                            data={o['source_id']:('uncertain' if o['source_id']=='S9' else 'irrelevant' if 'flag' in o['text'] else 'direct') for o in req['observations']}
                        elif op=='q4_propose':data=proposal(req)
                        elif op=='q4_pair':data=pair(req,ready=any(o['source_id']=='R_C1' for o in req['observations']))
                        elif op=='q3_navigate':data=dict(scene_id=next((s['id'] for s in req['scenes'] if s['id']=='T1'),req['scenes'][0]['id']))
                        else:raise AssertionError(op)
                        result.append(dict(source_id=item['source_id'],scope=item['scope'],request_hash=digest(req),data=data,parse_error=None,ended_normally=True))
                    return dict(items=result,usage=dict(input_tokens=1,output_tokens=1))
                if operation in ('q_answer','qplus_answer'):
                    text=json.dumps(payload['inputs']);assert 'private uncertainty' not in text and 'candidate_supported' not in text
                    return dict(data=dict(selected_option='0'),parse_error=None,usage=dict(input_tokens=1,output_tokens=1))
                raise AssertionError(operation)
        cfg=json.loads(Path('workdirs/Q3-coverage-repair-300-20261105-r2/qa_config.json').read_text())
        question,archive,original,ss=synthetic_case();archive['inherited_N_budgeted_seconds']=12
        ss=ss+[source('S9','private uncertainty',0)]
        with tempfile.TemporaryDirectory() as tmp:
            results={}
            for mode in ['adaptive','fixed_depth_control']:
                folder=Path(tmp)/mode;result=run_case(Fake(),cfg,question,ss,original,folder,archive,mode);results[mode]=result
                self.assertTrue(result['complete']);self.assertFalse(result['cost_summary']['budget_violation'])
                self.assertAlmostEqual(result['cost_summary']['budgeted_total']-result['cost_summary']['total'],12)
                self.assertEqual(result['arms']['Q']['retained_evidence_hash'],result['arms']['Qplus']['retained_evidence_hash'])
                with self.assertRaises(ValueError):run_case(Fake(),cfg,question,ss,original,folder,archive,mode)
            self.assertEqual([x['scene_id'] for x in results['adaptive']['retrievals']],['T1'])
            self.assertEqual(results['adaptive']['stop_reason'],'audited_sufficient')
            self.assertEqual([x['scene_id'] for x in results['fixed_depth_control']['retrievals']],['T1','T2'])
            self.assertTrue(results['fixed_depth_control']['final_certificate']['sufficient'])

if __name__=='__main__':unittest.main()
