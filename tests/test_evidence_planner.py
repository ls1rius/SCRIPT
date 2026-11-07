from copy import deepcopy
import json
import unittest

from script import controlled_memory_io as io
from script_core import evidence_planner as k
from script_core.qa_evidence_view import _fact_sentence


def fixture():
    rows = [dict(caption_id=f'C{i}', frame_index=i, sample_interval=[i, i+1],
        evidence=dict(original_text=text)) for i, text in enumerate([
            '#C C holds a cloth.', '#C C holds a cloth.', '#C C never uses a brush.'])]
    rows[0]['evidence']['actions'] = [dict(actor='C', verb='holds', patient='cloth')]
    memory = dict(observations=rows)
    temporal = dict(entities=[dict(id='E0', label='C', attributes=[], state_ids=['T0'])],
        states=[dict(id='T0', entity_id='E0', description='#C C holds a cloth.',
            source_caption_ids=['C0'], status='caption_asserted')],
        relations=[], transitions=[], identities=[], lineage=[], unresolved_observations=[])
    visual = [dict(description='C wipes the table using a cloth.', interval=[1, 2], source_frame_ids=['F1'])]
    original = dict(query_id='q', head='Frozen answer instruction', prompts=[
        'prefix  Description: OLD\n\n###\n\n Questions: Which tool?\n Options: A B C D E\nAnswer format\n\n'+
        k.TEMPORAL+'source):\n'+json.dumps(temporal)+'\n\n'+k.VISUAL+'samples):\n'+json.dumps(visual)])
    return memory, original


class EvidencePlannerTests(unittest.TestCase):
    def pool(self):
        memory, original = fixture()
        return k.build_pool(memory, original, io.parts, _fact_sentence)

    def test_all_regions_and_raw_text_survive_and_exact_duplicates_share_sources(self):
        pool = self.pool()
        self.assertEqual(set(pool['source_counts']), {'description', 'F fact', 'temporal state', 'visual'})
        repeated = next(r for r in pool['records'] if r['text'] == 'C holds a cloth.')
        self.assertEqual(len(repeated['sources']), 2)
        self.assertEqual(k.record_times(repeated), [[0,1], [1,2]])
        self.assertTrue(any('never' in r['text'] for r in pool['records']))
        self.assertEqual(len(pool['source_memory']['observations']), 3)

    def test_planner_sees_question_options_and_all_records_without_labels(self):
        pool = self.pool()
        question = dict(question='Which tool?', options=['cloth','brush','knife','spoon','paper'], truth='A')
        inputs = k.planner_input(question, pool, 'q')
        self.assertIn('never uses a brush', inputs['prompts'][0])
        self.assertIn('wipes the table', inputs['prompts'][0])
        self.assertIn('knife', inputs['prompts'][0])
        self.assertNotIn('truth', inputs['prompts'][0])
        for record in pool['records']:
            self.assertIn('"'+record['id']+'"', inputs['prompts'][0])

    def test_skipped_auxiliary_cannot_reenter_original_answer_prompt(self):
        pool = self.pool()
        keep = [r['id'] for r in pool['records'] if r['text'] == 'C holds a cloth.']
        plan = dict(keep=keep, skip=[r['id'] for r in pool['records'] if r['id'] not in keep],
            uncertain=[], merge_groups=[])
        text, audit = k.render(pool, plan)
        _, original = fixture()
        result = k.final_input(original, text, io.parts)
        self.assertNotIn('never uses a brush', result['prompts'][0])
        self.assertNotIn('wipes the table', result['prompts'][0])
        self.assertNotIn(k.TEMPORAL, result['prompts'][0])
        self.assertNotIn(k.VISUAL, result['prompts'][0])
        self.assertIn('Questions: Which tool?', result['prompts'][0])
        self.assertEqual(result['head'], original['head'])
        self.assertTrue(audit['original_sources_retained'])

    def test_merge_preserves_complementary_and_negative_statements_and_all_times(self):
        pool = self.pool()
        ids = [r['id'] for r in pool['records']]
        text, audit = k.render(pool, dict(keep=[], skip=[], uncertain=[], merge_groups=[ids]))
        for record in pool['records']:
            self.assertIn(record['text'], text)
        self.assertIn('[0,1), [1,2)', text)
        self.assertFalse(audit['inferred_continuity'])
        self.assertFalse(audit['inferred_event_counts'])
        self.assertEqual(audit['retained_records'], len(ids))

    def test_partition_requires_every_id_once_and_retains_uncertain(self):
        pool = self.pool()
        valid = k.keep_all(pool)
        first = valid['keep'][0]
        for bad in [dict(valid, keep=valid['keep'][1:]), dict(valid, skip=[first]),
                dict(valid, keep=['invented']), dict(valid, merge_groups=[[first]]),
                dict(keep=[], skip=valid['keep'], uncertain=[], merge_groups=[])]:
            with self.assertRaises(ValueError):
                k.audit_plan(bad, pool)
        valid['keep'].remove(first)
        valid['uncertain'] = [first]
        text, audit = k.render(pool, valid)
        self.assertIn(pool['records'][0]['text'], text)
        self.assertEqual(audit['uncertain_records'], 1)
        changed = deepcopy(pool)
        changed['records'][0]['text'] = 'invented fact'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            k.audit_plan(valid, changed)

    def test_unparsed_auxiliary_and_new_schema_fail_instead_of_silent_loss(self):
        memory, original = fixture()
        original['prompts'][0] += '\nUnparsed important observation'
        with self.assertRaisesRegex(ValueError, 'Unparsed'):
            k.build_pool(memory, original, io.parts, _fact_sentence)
        self.assertRaises(ValueError, k.unpack_auxiliary, k.TEMPORAL+'x):\n[]')

    def test_hypotheses_preserve_direction_and_uncertainty(self):
        memory, original = fixture()
        suffix, temporal, visual = k.unpack_auxiliary(io.parts(original)[2])
        temporal['entities'].append(dict(id='E1', label='book', attributes=[]))
        temporal['relations']=[dict(subject='E0', object='E1', predicate='holds', source_caption_ids=['C0'])]
        temporal['identities']=[dict(left_entity='E0',right_entity='E1',relation='unknown',reason='ambiguous',caption_ids=['C0'])]
        prefix, desc, _ = io.parts(original)
        original['prompts'][0]=prefix+desc+suffix+'\n\n'+k.TEMPORAL+'source):\n'+json.dumps(temporal)
        pool=k.build_pool(memory,original,io.parts,_fact_sentence)
        text,_=k.render(pool,k.keep_all(pool))
        self.assertIn('"C (text reference E0)" as the subject of "holds", with "book (text reference E1)" as its object',text)
        self.assertIn('text-inferred hypothesis',text)
        self.assertIn('ambiguous',text)

    def test_equal_entity_names_keep_distinct_text_references(self):
        memory,original=fixture()
        suffix,temporal,_=k.unpack_auxiliary(io.parts(original)[2])
        temporal['entities']=[dict(id='E0',label='book',attributes=[]),dict(id='E1',label='book',attributes=[])]
        temporal['states']=[dict(id=f'T{i}',entity_id=f'E{i}',description='The book is wet.',
            source_caption_ids=[f'C{i}'],status='caption_asserted') for i in (0,1)]
        prefix,desc,_=io.parts(original)
        original['prompts'][0]=prefix+desc+suffix+'\n\n'+k.TEMPORAL+'source):\n'+json.dumps(temporal)
        pool=k.build_pool(memory,original,io.parts,_fact_sentence)
        states=[r for r in pool['records'] if 'temporal state' in r['kinds']]
        self.assertEqual(len(states),2)
        self.assertIn('text reference E0',states[0]['text'])
        self.assertIn('text reference E1',states[1]['text'])

    def test_real_lmfe_parser_accepts_full_partition_schema(self):
        from lmformatenforcer import JsonSchemaParser
        pool = self.pool()
        parser = JsonSchemaParser(k.plan_schema(pool))
        parser.config.force_json_field_order = True
        output = json.dumps({r['id']:'keep' for r in pool['records']}, separators=(',', ':'))
        for char in output:
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())

    def test_per_record_decisions_preserve_groups_singletons_and_uncertainty(self):
        pool = self.pool()
        ids = [r['id'] for r in pool['records']]
        decisions = {i:'keep' for i in ids}
        decisions.update({ids[0]:'group:'+ids[-1], ids[1]:'group:'+ids[-1],
            ids[2]:'uncertain', ids[-1]:'skip'})
        plan = k.decode_plan(decisions, pool)
        self.assertEqual(plan['merge_groups'], [ids[:2]])
        self.assertEqual(plan['skip'], [ids[-1]])
        self.assertEqual(plan['uncertain'], [ids[2]])
        decisions[ids[1]] = 'group:'+ids[1]
        plan = k.decode_plan(decisions, pool)
        self.assertEqual(plan['merge_groups'], [])
        self.assertIn(ids[0], plan['keep'])
        self.assertIn(ids[1], plan['keep'])

    def test_per_record_parser_blocks_omissions_repetition_and_unknown_decisions_at_scale(self):
        from lmformatenforcer import JsonSchemaParser
        pool = dict(records=[dict(id=f'R{i}') for i in range(130)])
        pool['pool_sha256'] = k.digest(pool['records'])
        valid = {r['id']:'keep' for r in pool['records']}
        def accepts(raw):
            parser = JsonSchemaParser(k.plan_schema(pool))
            parser.config.force_json_field_order = True
            for char in raw:
                if char not in parser.get_allowed_characters():
                    return False
                parser = parser.add_character(char)
            return parser.can_end()
        self.assertTrue(accepts(json.dumps(valid)))
        missing = dict(valid); missing.pop('R0')
        unknown = dict(valid, R0='group:invented')
        duplicate = json.dumps(valid)[:-1]+',"R0":"skip"}'
        for bad in (json.dumps(missing), json.dumps(unknown), duplicate,
                '{"keep":["R1"],"skip":["R1"],"uncertain":[],"merge_groups":[]}'):
            self.assertFalse(accepts(bad))
            with self.assertRaises(ValueError):
                k.decode_plan(bad, pool)
        with self.assertRaisesRegex(ValueError, 'discard every'):
            k.decode_plan({i:'skip' for i in valid}, pool)


if __name__ == '__main__':
    unittest.main()
