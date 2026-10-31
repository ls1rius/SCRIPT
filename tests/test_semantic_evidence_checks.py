from copy import deepcopy
import unittest
from script.semantic_evidence_runtime import functional_gate,probe_bundle
from script_core.semantic_memory import COLLECTIONS


def valid_memory():
    return dict(
        attributes=[dict(source_id='U0',entity='book',property='color',value='red'),
                    dict(source_id='U8',entity='book',property='material',value='bamboo')],
        states=[dict(source_id='U2',entity='book',dimension='wetness',value='wet')],
        relations=[dict(source_id='U9',left='book',predicate='on',right='table',relation_type='spatial')],
        changes=[dict(source_id='U4',entity='book',before='wet',after='dry')],
        results=[dict(source_id='U5',entity='machine',predicate='produces',value='basket')],
        event_links=[dict(source_id='U6',left_event='C wipes the book',connector='then',
                          right_event='C moves the book',link_type='before')],
        actions=[dict(source_id='U0',actor='C',verb='wipes',patient='red book',instrument='cloth'),
                 dict(source_id='U1',actor='C',verb='draws',surface='drawing book',instrument='pencil'),
                 dict(source_id='U3',actor='C',verb='moves',patient='book',source_location='shelf',
                      destination='bag',body_part='left hand')],
        residuals=[dict(source_id='U7',text='C never wipes the book.')])


def valid_audit():
    return dict(batch_failures=[], category_coverage={
        k:dict(checked_units=10,failed_units=0,unattempted_units=0) for k in COLLECTIONS})


class SemanticEvidenceChecks(unittest.TestCase):
    def test_functional_gate_checks_roles_and_categories_before_accuracy(self):
        memory=valid_memory()
        gate=functional_gate(memory,valid_audit())
        self.assertTrue(gate['passed']);self.assertFalse(gate['benchmark_accuracy_used'])
        bad=deepcopy(memory);bad['actions'][1]['patient']=bad['actions'][1].pop('surface')
        self.assertIn('drawing_surface_instrument',functional_gate(bad,valid_audit())['failures'])
        bad=deepcopy(memory);bad['attributes']=[]
        self.assertIn('missing_category:attributes',functional_gate(bad,valid_audit())['failures'])

    def test_nonempty_but_misclassified_or_misattributed_records_fail(self):
        for category,field,value,check in [
            ('states','dimension','color','temporary_wetness'),
            ('changes','source_id','U0','explicit_change'),
            ('event_links','link_type','cause','explicit_event_order'),
            ('relations','right','shelf','explicit_spatial_relation')]:
            with self.subTest(category=category):
                memory=valid_memory();memory[category][0][field]=value
                self.assertIn(check,functional_gate(memory,valid_audit())['failures'])

    def test_unattempted_category_and_positive_negation_cannot_pass(self):
        audit=valid_audit();audit['category_coverage']['state']['checked_units']=9
        audit['category_coverage']['state']['unattempted_units']=1
        self.assertIn('incomplete_category_check:state',functional_gate(valid_memory(),audit)['failures'])
        memory=valid_memory();memory['actions'].append(dict(source_id='U7',actor='C',verb='wipes',patient='book'))
        self.assertIn('negative_clause_has_positive_fact',functional_gate(memory,valid_audit())['failures'])

    def test_required_correct_records_do_not_hide_an_extra_invalid_relation(self):
        memory=valid_memory()
        memory['relations'].append(dict(source_id='U0',left='C',predicate='wipes',right='red book',relation_type='attachment'))
        failures=functional_gate(memory,valid_audit())['failures']
        self.assertIn('invalid_probe_fact:relations:unsupported_relation_subtype_binding',failures)

    def test_probe_inputs_never_contain_benchmark_questions_answers_or_options(self):
        probe=probe_bundle()
        self.assertEqual(len(probe['units']),10)
        self.assertNotIn('question',probe);self.assertNotIn('answer',probe);self.assertNotIn('options',probe)


if __name__=='__main__':unittest.main()
