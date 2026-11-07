import json
import unittest
from script_core import grounded_reader as core


class GroundedReaderTests(unittest.TestCase):
    def fixture(self):
        rows=[dict(caption_id='C0',interval=[0,1],body='#C C wipes the hands'),
              dict(caption_id='C1',interval=[1,2],body='#C C cuts the wood'),
              dict(caption_id='C2',interval=[2,3],body='#C C never nails the wood')]
        _,audit=core.compact_rows(rows)
        options=['C is preparing a meal.','C cuts the wood.','C nails the wood.','C paints wood.','C walks.']
        value=dict(option_checks={o:[dict(claim=options[i],relation='unknown',evidence_ids=[])] for i,o in enumerate('ABCDE')})
        return audit,options,value

    def test_bad_quote_downgraded_before_selection_and_exact_quote_attached(self):
        audit,options,value=self.fixture()
        value['option_checks']['A']=[dict(claim='preparing a meal',relation='observed',evidence_ids=['J001'])]
        value['option_checks']['B']=[dict(claim='cuts the wood',relation='observed',evidence_ids=['J002'])]
        result=core.audit_grounding(json.dumps(value),options,audit)
        self.assertEqual(result['option_checks']['A'][0]['relation'],'unknown')
        self.assertEqual(result['option_checks']['B'][0]['relation'],'observed')
        self.assertEqual(result['option_checks']['B'][0]['evidence'][0]['quote'],'#C C cuts the wood')
        selection=core.selection_input(dict(question='q',options=options),result,'q')
        self.assertIn('direct_claim_contains_unobserved_content_words',selection['prompts'][0])

    def test_absence_is_unknown_and_explicit_negation_can_be_recorded(self):
        audit,options,value=self.fixture()
        value['option_checks']['D']=[dict(claim='paints wood',relation='contradicted',evidence_ids=['J002'])]
        value['option_checks']['C']=[dict(claim='nails the wood',relation='contradicted',evidence_ids=['J003'])]
        result=core.audit_grounding(json.dumps(value),options,audit)
        self.assertEqual(result['option_checks']['D'][0]['relation'],'unknown')
        self.assertEqual(result['option_checks']['C'][0]['relation'],'contradicted')

    def test_inference_kept_separate_and_claim_must_be_literal_option_excerpt(self):
        audit,options,value=self.fixture()
        value['option_checks']['A']=[dict(claim='preparing a meal',relation='inferred',evidence_ids=['J001'])]
        value['option_checks']['B']=[dict(claim='builds a house',relation='inferred',evidence_ids=['J002'])]
        result=core.audit_grounding(json.dumps(value),options,audit)
        self.assertEqual(result['option_checks']['A'][0]['relation'],'inferred')
        self.assertEqual(result['option_checks']['B'][0]['relation'],'unknown')

    def test_missing_reference_and_person_c_not_read_as_option(self):
        audit,options,value=self.fixture()
        value['option_checks']['B']=[dict(claim='cuts the wood',relation='observed',evidence_ids=['J999'])]
        with self.assertRaises(ValueError):core.audit_grounding(json.dumps(value),options,audit)
        value['option_checks']['B'][0]['evidence_ids']=['J002']
        grounding=core.audit_grounding(json.dumps(value),options,audit)
        result=core.finish('{"selected_option":"B"}',options,grounding)
        self.assertEqual(result['option_text'],options[1])
        with self.assertRaises(ValueError):core.finish('{"selected_option":"C walks"}',options,grounding)

    def test_global_equivalent_groups_keep_all_sources_and_gaps(self):
        rows=[dict(caption_id=f'C{i}',interval=[t,t+1],body=b) for i,(t,b) in enumerate([
            (0,'#C C walks with the dog'),(1,'C is walking with a dog.'),
            (2,'C stops'),(10,'#C C walks with the dog')])]
        text,audit=core.compact_rows(rows)
        self.assertIn('[0,2), [10,11)',text)
        self.assertEqual(audit['rendered_rows'],2)
        restored=sorted([(index,source) for r in audit['rows'] for index,source in zip(r['source_indices'],r['sources'])])
        self.assertEqual([r[1] for r in restored],rows)

    def test_atomic_plan_is_literal_bounded_and_decoder_has_no_free_claims(self):
        options=['C applies glue and cuts wood.','C applies lubricant and cuts wood.',
            'C paints wood.','C nails wood.','C walks with the dog.']
        plan=core.option_fragments(options)
        self.assertTrue(5<=len(plan)<=10)
        for c in plan:self.assertIn(c['claim'],options[ord(c['option_id'])-65])
        schema=core.answer_schema({'J001'},plan)
        self.assertEqual(set(schema['properties']['claim_checks']['properties']),{c['claim_id'] for c in plan})
        self.assertNotIn('claim',schema['properties']['claim_checks']['properties']['A0']['properties'])

    def test_per_claim_candidate_ids_exclude_irrelevant_source_and_full_evidence_remains(self):
        audit,options,_=self.fixture()
        plan=core.option_fragments(options);candidates=core.claim_candidates(options,audit)
        candidate=next(c for c in plan if c['option_id']=='B')
        self.assertEqual(candidates[candidate['claim_id']]['exact_candidate_ids'],['J002'])
        schema=core.answer_schema(set(core.quote_map(audit)),plan,candidates)
        refs=schema['properties']['claim_checks']['properties'][candidate['claim_id']]['properties']['evidence_ids']
        self.assertEqual(refs['items']['enum'],['J002'])
        selected=core.selection_input(dict(question='q',options=options),dict(option_checks={}), 'q',dict(prompts=['original complete evidence']))
        self.assertIn('original complete evidence',selected['prompts'][0])


if __name__=='__main__':unittest.main()
