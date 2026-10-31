from copy import deepcopy
import json
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace

from script import controlled_memory_io as io
from script.prepare_controlled_memory_design import lossless_input


class ControlledRuntimeTests(unittest.TestCase):
    def fixture(self,text='#C C wipes the book with a cloth.',fact=None):
        source=dict(id='U0',text=text.removeprefix('#C '),raw_text=text,source_marker='#C',
            source_type='caption',status='caption_asserted',source_caption_ids=['C0','C1'],
            source_frame_ids=[],source_intervals=[[0,1],[1,2]])
        memory=dict(sources=[source],residuals=[],**{c:[] for c in io.COLLECTIONS.values()})
        fact=fact or dict(actor='C',verb='wipes',patient='book',instrument='cloth')
        memory['actions']=[dict(id='F0',kind='action',source_id='U0',**fact)]
        bundle=dict(units=[{k:v for k,v in source.items() if k not in ('raw_text','source_marker')}],read_caption_ids=['C0','C1'])
        bundle['units'][0]['text']=text
        row=dict(packet=dict(captions=[dict(id='C0',text=text,interval=[0,1]),dict(id='C1',text=text,interval=[1,2])]),
            selected_seconds=[0,1],original=dict(query_id='q',head='original system',
                prompts=['prefix  Description: old descriptions\n\n###\n\n Questions: Q\nOptions A B\n'
                         'Original answer instructions\nTemporal entity memory: keep\nVisual: keep']))
        return row,lossless_input(row),memory,bundle

    def test_semantic_projection_preserves_original_protocol_and_all_sample_rows(self):
        row,b,memory,bundle=self.fixture();before=deepcopy(memory)
        c,audit=io.semantic_input(row['original'],b,memory,bundle)
        self.assertEqual(io.fixed(c),io.fixed(row['original']))
        records=json.loads(io.parts(c)[1])['observations']
        self.assertEqual([r['sample_interval'] for r in records],[[0,1],[1,2]])
        self.assertEqual([r['caption_id'] for r in records],['C0','C1'])
        self.assertEqual(records[0]['evidence']['actions'][0]['instrument'],'cloth')
        self.assertEqual(audit['covered_captions'],2);self.assertEqual(memory,before)

    def test_cached_validation_does_not_require_an_unexported_runner_function(self):
        from script.run_controlled_memory_experiment import validate_memory
        _,_,memory,bundle=self.fixture()
        validate_memory(memory,bundle,SimpleNamespace(binding_supported=lambda fact,text:(True,None)))

    def test_particle_patient_is_quarantined_and_entire_original_restored(self):
        row,b,m,bundle=self.fixture('#C C looks around.',dict(actor='C',verb='looks',patient='around'))
        c,audit=io.semantic_input(row['original'],b,m,bundle)
        evidence=json.loads(io.parts(c)[1])['observations'][0]['evidence']
        self.assertNotIn('actions',evidence)
        self.assertEqual(evidence['categorized_original']['original_text'],'#C C looks around.')
        self.assertEqual(audit['quarantined_records'][0]['reason'],'adverb_or_particle_is_not_patient')

    def test_body_part_is_not_relabelled_as_tool(self):
        reason=io.quarantine_reason('action',dict(actor='C',verb='plays',patient='guitar',instrument='left hand'),
                                     'C plays the guitar with his left hand')
        self.assertEqual(reason,'body_part_is_not_instrument')

    def test_tool_name_and_source_location_do_not_become_material(self):
        self.assertEqual(io.quarantine_reason('action',dict(verb='paints',instrument='paint brush',material='paint'),
            'C paints the wall with a paint brush'),'material_is_only_part_of_tool_name')
        self.assertEqual(io.quarantine_reason('action',dict(verb='lifts',material='wooden plank',source_location='wooden plank'),
            'C lifts a plank from the wooden plank'),'material_conflicts_with_another_role')

    def test_explicit_material_separate_from_tool_is_preserved(self):
        self.assertIsNone(io.quarantine_reason('action',dict(verb='paints',instrument='brush',material='paint'),
            'C paints with paint using a brush'))

    def test_video_device_surface_is_quarantined(self):
        self.assertEqual(io.quarantine_reason('action',dict(verb='watches',patient='video',surface='phone'),
            'C watches a video on phone'),'display_medium_is_not_verified_physical_surface')

    def test_empty_extraction_keeps_raw_text_and_does_not_claim_all_categories(self):
        row,b,m,bundle=self.fixture();m['actions']=[]
        c,_=io.semantic_input(row['original'],b,m,bundle)
        e=json.loads(io.parts(c)[1])['observations'][0]['evidence']['categorized_original']
        self.assertEqual(e['information_types'],['unclassified'])
        self.assertEqual(e['original_text'],row['packet']['captions'][0]['text'])

    def test_foreign_source_changed_text_and_changed_time_are_rejected(self):
        for mutation in ('foreign','text','time'):
            row,b,m,bundle=self.fixture()
            if mutation=='foreign':m['actions'][0]['source_id']='foreign'
            if mutation=='text':m['sources'][0]['raw_text']='another sentence'
            if mutation=='time':m['sources'][0]['source_intervals']=[[9,10]]
            with self.assertRaises(ValueError):io.semantic_input(row['original'],b,m,bundle)

    def test_strict_audit_does_not_read_person_C_as_option_C(self):
        options=['first','second','third','fourth','C is trying to fix a lawn mower.']
        self.assertEqual(io.strict_audit('prediction: C is trying to fix a lawn mower.',options)['option'],'4')
        self.assertEqual(io.strict_audit('prediction: C\nexplanation: person C',options)['option'],'2')

    def test_report_pairs_only_same_completed_questions_and_counts_errors_wrong(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);io.write(p/'cohort.json',['q0','q1']);io.write(p/'evaluation_only.json',{'q0':'0','q1':'1'})
            io.write(p/'historical_only.json',{'q0':{'selected_option':'0'},'q1':{'selected_option':'1'}})
            io.write(p/'experiment.json',{})
            for arm,selected,error in [('A_original','0',None),('B_lossless_layout',None,{'message':'format'}),('C_semantic','0',None)]:
                io.write(p/'runs'/arm/'q0/result.json',dict(selected_option=selected,error=error,seconds=1))
            io.write(p/'runs/A_original/q1/result.json',dict(selected_option='1',error=None,seconds=1))
            report=io.report(p)
            self.assertEqual(report['common_completed'],1)
            self.assertEqual(report['arms']['B_lossless_layout']['errors'],1)
            self.assertIsNone(report['arms']['C_semantic']['accuracy'])
            self.assertEqual(report['comparisons']['C_semantic_vs_A_original']['completed'],1)
            self.assertEqual(report['arms']['A_original']['accuracy'],1.)


if __name__=='__main__':unittest.main()
