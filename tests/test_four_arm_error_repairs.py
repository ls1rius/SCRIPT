"""Regressions for the local 300-video run's actual error categories."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script_core import caption_temporal as text
from script_core.backend import decode_structured_text
from script_core.budget import BudgetLedger,Meter,ToolDeadlineExceeded,ToolFailure
from script_core.config import load_config
from script_core.schemas import validate_output
from script_core.temporal_entity import new_network
from script_core.videotree_tools import explicit_qa_answer
from tests.test_caption_temporal import captions,row,report
from script import run_four_arm_repair as repair


class FourArmErrorRepairs(unittest.TestCase):
    def test_recovery_binds_label_and_state_to_the_same_original_caption(self):
        caps=[dict(id='C0',interval=[0,1],text='C holds a box.'),
              dict(id='C1',interval=[1,2],text='C holds a cup.')]
        schema=text.output_schema(dict(captions=caps,entities=[],already_reported=[],
            page_size=1,copy_caption_states=True,grounded_labels=['cup']))
        good=row(cid='C1',state=caps[1]['text']);good['label']='cup'
        validate_output(report(good),schema)
        bad=deepcopy(good);bad['observations'][0].update(caption_id='C0',state=caps[0]['text'])
        with self.assertRaises(ValueError):validate_output(report(bad),schema)

    def test_empty_causal_citations_become_unknown_without_inventing_sources(self):
        r=row();r['observations'][0]['cause'].update(action='holds',basis='inferred_action')
        original=report(r);filtered,rejected=text.grounded_causes(original,captions())
        self.assertEqual(len(rejected),1)
        self.assertEqual(filtered['entities'][0]['observations'][0]['cause']['basis'],'unknown')
        self.assertEqual(original['entities'][0]['observations'][0]['cause']['action'],'holds')

    def test_unsupported_relation_is_logged_but_foreign_citation_stays_invalid(self):
        rel=dict(subject='B0',object='B1',predicate='contains',caption_id='C0',
                 update='present',source_caption_ids=['C0'])
        value=report(row());value['relations']=[rel]
        filtered,rejected=text.grounded_relations(value,captions())
        self.assertEqual(filtered['relations'],[]);self.assertEqual(rejected,[rel])
        value['relations'][0]['source_caption_ids']=['C999']
        filtered,rejected=text.grounded_relations(value,captions())
        self.assertEqual(rejected,[])
        with self.assertRaisesRegex(ValueError,'Foreign caption relation'):
            text.commit_page(new_network(),filtered,captions(),'S0','b',[])

    def test_reusable_full_call_timeout_retries_a_smaller_page(self):
        requests=[]
        class ScriptedMeter:
            def json(self,op,inputs,required):
                requests.append(deepcopy(inputs))
                if len(requests)==1:
                    raise ToolDeadlineExceeded('timeout',worker_reusable=True)
                return report(row()),{'call_id':'retry'}
        net=new_network()
        with tempfile.TemporaryDirectory() as tmp:
            text.open_scene(net,dict(id='S0',caption_ids=['C0','C1'],interval=[0,2]),
                            captions(),ScriptedMeter(),tmp,{})
        self.assertEqual(requests[0]['page_size'],3)
        self.assertEqual(requests[1]['page_size'],1)
        self.assertEqual(len(net['entities']),1)

    def test_budget_limited_timeout_is_not_restarted(self):
        class ScriptedMeter:
            def json(self,*args,**kwargs):
                raise ToolDeadlineExceeded('budget exhausted',budget_limited=True,worker_reusable=True)
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ToolDeadlineExceeded):
            text.open_scene(new_network(),dict(id='S0',caption_ids=['C0'],interval=[0,1]),
                            captions(),ScriptedMeter(),tmp,{})

    def test_literal_source_newline_survives_and_truncated_json_is_rejected(self):
        self.assertEqual(decode_structured_text('{"state":"original\ncaption"}'),
                         {'state':'original\ncaption'})
        with self.assertRaises(ValueError):decode_structured_text('{"scope":"global","reason":"cut off')

    def test_invalid_scope_regenerates_from_canonical_inputs_and_both_calls_are_charged(self):
        calls=[]
        class Backend:
            def call(self,op,inputs,timeout):
                calls.append((op,deepcopy(inputs)))
                return {'data':'{"scope":"global","reason":"cut off'} if len(calls)==1 else {
                    'data':dict(scope='global',reason='whole video')}
        with tempfile.TemporaryDirectory() as tmp:
            ledger=BudgetLedger(600);meter=Meter(Backend(),ledger,load_config(),Path(tmp))
            try:value,_=meter.json('scene_scope',dict(question='Overall?',options=['a']*5),('scope','reason'))
            finally:meter.close()
        self.assertEqual(value['scope'],'global')
        self.assertEqual([op for op,_ in calls],['scene_scope','scene_scope'])
        self.assertNotIn('original_output',calls[1][1])
        self.assertEqual(sum(c['operation']=='scene_scope' for c in ledger.calls),2)

    def test_qa_parser_reads_only_one_explicit_declaration(self):
        self.assertEqual(explicit_qa_answer('**Prediction:** B'),1)
        self.assertEqual(explicit_qa_answer('The correct answer is option C.'),2)
        self.assertEqual(explicit_qa_answer('A is plausible; B is also plausible'),-1)
        self.assertEqual(explicit_qa_answer('prediction: A\nprediction: B'),-1)

    def test_successful_source_is_reused_only_with_complete_paired_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            repair.write(root/'result.json',dict(answer=dict(selected_option='2'),error=None))
            self.assertTrue(repair.successful(root/'result.json'))
            self.assertFalse(repair.source_usable(root))
            bundle=dict(incomplete=[dict(reason='budget')],network=dict(diagnostics=[]))
            repair.write(root/'evidence_bundle.json',bundle)
            self.assertFalse(repair.source_usable(root))
            bundle['incomplete']=[];repair.write(root/'evidence_bundle.json',bundle)
            self.assertTrue(repair.source_usable(root))

    def test_retry_archives_spent_trace_and_cannot_mutate_reference_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'arms/temporal_local/runs/v'
            repair.write(target/'result.json',dict(error=dict(message='timeout')))
            (target/'calls.jsonl').write_text('spent budget\n')
            repair.archive_attempt(root,'temporal_local','v')
            self.assertFalse(target.exists())
            self.assertEqual((root/'attempts/temporal_local/v/attempt-01/calls.jsonl').read_text(),'spent budget\n')
            target.symlink_to(root/'attempts/temporal_local/v/attempt-01',target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'Never mutate'):
                repair.archive_attempt(root,'temporal_local','v')


if __name__=='__main__':unittest.main()
