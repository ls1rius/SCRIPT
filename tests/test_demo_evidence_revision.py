import json
import unittest

from script_core import demo_evidence_revision as core
from script_core.g_rl import parse_answer as legacy_strict


class DemoRevisionTests(unittest.TestCase):
    def test_local_paraphrases_merge_without_bridging_gaps_or_changes(self):
        rows=[dict(caption_id=f'C{i}',interval=[t,t+1],body=s) for i,(t,s) in enumerate([
            (0,'#C C walks with the dog'), (1,'C is walking with a dog.'),
            (2,'C stops with the dog'), (3,'C walks with the dog'),(8,'C walks with the dog')])]
        text,audit=core.compact_rows(rows)
        self.assertIn('[E001] Frames [0,2):',text)
        self.assertEqual(audit['rendered_rows'],4)
        self.assertEqual(audit['rows'][0]['sources'],rows[:2])
        self.assertNotIn('[3,9)',text)

    def test_negation_roles_tools_sides_and_possessives_remain_distinct(self):
        values=['C cuts the wood','C nails the wood','C never cuts the wood',
            'X cuts the wood','C cuts the wood with a saw','C cuts the wood with a knife',
            'C holds the cup with her left hand','C holds the cup with her right hand',
            'C walks with her dog','C walks with his dog']
        self.assertEqual(len(set(map(core.canonical_body,values))),len(values))

    def test_person_c_is_not_an_option_letter(self):
        options=['one','two','three','four','C repeatedly touches her hair and walks her dog.']
        parsed=legacy_strict('prediction: '+options[-1],options)
        self.assertEqual(parsed['option'],4)

    def test_strict_json_and_citation_validation(self):
        answer=dict(selected_option='A',option_checks=[dict(option_id=o,status='unknown',evidence_ids=[]) for o in 'ABCDE'],
            decisive_evidence_ids=['E001'],explanation='observed')
        self.assertEqual(core.parse_answer(json.dumps(answer),{'E001'})['option'],'0')
        answer['selected_option']='C touches her hair'
        with self.assertRaises(ValueError):core.parse_answer(json.dumps(answer),{'E001'})
        answer['selected_option']='A';answer['decisive_evidence_ids']=['E999']
        with self.assertRaises(ValueError):core.parse_answer(json.dumps(answer),{'E001'})

    def test_bounded_schema_requires_all_five_options_and_existing_ids(self):
        from script_core.schemas import validate_output
        schema=core.output_schema({'E001'})
        answer=dict(option_checks={o:dict(basis='No observed distinction',evidence_ids=[],status='unknown') for o in 'ABCDE'},
            decisive_evidence_ids=['E001'],selected_option='A',explanation='Observed',confidence=50)
        validate_output(answer,schema)
        self.assertEqual(core.parse_answer(json.dumps(answer),{'E001'})['option'],'0')
        answer['option_checks']['A']['evidence_ids']=['E999']
        with self.assertRaises(ValueError):validate_output(answer,schema)


if __name__=='__main__':unittest.main()
