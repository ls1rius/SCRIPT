from copy import deepcopy
import json
import unittest

from script import controlled_memory_io as io
from script.preview_compact_evidence import project
from script_core.qa_evidence_view import compact_description, narrative_description


class EvidenceViewTests(unittest.TestCase):
    def row(self, evidence, cid='C0', time=0):
        return dict(caption_id=cid, frame_index=time, sample_interval=[time, time+1],
                    evidence=evidence)

    def test_negation_and_original_whitespace_survive_without_duplicate_assertions(self):
        text = '#C C never puts the cloth on the wall. '
        row = self.row(dict(actions=[dict(actor='C', verb='puts', patient='cloth')],
            categorized_original=dict(original_text=text, information_types=['action'])))
        before = deepcopy(row)
        output, audit = compact_description(dict(observations=[row]))
        self.assertIn(text, output)
        self.assertNotIn('Action:', output)
        self.assertEqual(row, before)
        self.assertEqual(audit['rows'][0]['archived_fact_count'], 1)

    def test_repeated_samples_keep_separate_times_and_source_mapping(self):
        rows = [self.row(dict(original_text='#C C holds a cloth'), f'C{i}', i) for i in [0, 9]]
        output, audit = compact_description(dict(observations=rows))
        self.assertEqual(output, 'Frame [0,1): #C C holds a cloth\n'
                         'Frame [9,10): #C C holds a cloth')
        self.assertNotIn('C9', output)
        self.assertEqual([r['caption_id'] for r in audit['rows']], ['C0', 'C9'])
        self.assertEqual([r['prompt_line'] for r in audit['rows']], [1, 2])

    def test_all_semantic_roles_and_event_connectors_are_retained(self):
        row = self.row(dict(actions=[dict(actor='C', verb='moves', patient='book',
            source_location='shelf', destination='bag', body_part='left hand')],
            event_links=[dict(left_event='wipes', connector='then', right_event='moves',
                             link_type='before')]))
        output, _ = compact_description(dict(observations=[row]))
        for value in ['shelf', 'bag', 'left hand', 'wipes', 'then', 'before']:
            self.assertIn('"'+value+'"', output)

    def test_fractional_sample_times_are_not_rounded(self):
        row = self.row(dict(original_text='C holds cloth'))
        row['sample_interval'] = [0.123456789, 1.123456789]
        output, _ = compact_description(dict(observations=[row]))
        self.assertIn('[0.123456789,1.123456789)', output)

    def test_unknown_schema_and_uncovered_sources_fail_instead_of_dropping_content(self):
        for evidence in [dict(extra_detail='important'), {}, dict(original_text='')]:
            with self.assertRaises(ValueError):
                compact_description(dict(observations=[self.row(evidence)]))

    def test_projection_keeps_existing_memory_visuals_and_answer_protocol_exactly(self):
        payload = dict(observations=[self.row(dict(original_text='#C C holds cloth'))])
        original = dict(query_id='q', head='Original system instruction', prompts=[
            'prefix  Description: '+json.dumps(payload)+'\n\n###\n\n Questions: Q\n'
            'Options A B C D E\nOriginal answer format\nTemporal entity memory: keep\n'
            'Supplemental visual observations: keep'])
        before = deepcopy(original)
        result, audit = project(original)
        self.assertEqual(original, before)
        self.assertEqual(io.fixed(result), io.fixed(original))
        self.assertEqual(audit['rendered_rows'], 1)

    def test_equal_states_share_one_record_until_a_different_observation(self):
        def state(value, time):
            return self.row(dict(states=[dict(entity='door',dimension='open_closed',value=value)]), f'C{time}', time)
        payload = dict(observations=[state('closed',9),state('closed',11),state('open',13),state('closed',15)])
        before = deepcopy(payload)
        output, audit = compact_description(payload, merge_repeated=True)
        self.assertEqual(output.splitlines(), [
            'Frames [9,10), [11,12): State: entity="door"; dimension="open_closed"; value="closed"',
            'Frame [13,14): State: entity="door"; dimension="open_closed"; value="open"',
            'Frame [15,16): State: entity="door"; dimension="open_closed"; value="closed"'])
        self.assertEqual(payload, before)
        self.assertEqual(audit['source_rows'], 4)
        self.assertEqual(audit['rendered_rows'], 3)
        self.assertEqual([r['prompt_line'] for r in audit['rows']], [1,1,2,3])
        self.assertEqual(audit['groups'][0]['caption_ids'], ['C9','C11'])
        self.assertEqual(audit['groups'][0]['sample_intervals'], [[9,10],[11,12]])

    def test_grouped_action_samples_retain_separate_intervals_and_no_event_count(self):
        rows=[self.row(dict(actions=[dict(actor='C',verb='hits',patient='nail')]),f'C{i}',i) for i in [0,9]]
        output,audit=compact_description(dict(observations=rows),merge_repeated=True)
        self.assertEqual(output,'Frames [0,1), [9,10): Action: actor="C"; verb="hits"; patient="nail"')
        self.assertFalse(audit['continuous_duration_inferred'])
        self.assertFalse(audit['event_counts_inferred'])
        self.assertEqual(len(audit['rows']),2)

    def test_merged_multiline_original_preserves_negation_and_whitespace(self):
        text='#C C does not move the cup.\n '
        rows=[self.row(dict(original_text=text),f'C{i}',i) for i in [1,2,3]]
        rows.append(self.row(dict(original_text='C opens the door'),'C4',4))
        output,audit=compact_description(dict(observations=rows),merge_repeated=True)
        self.assertEqual(output,'Frames [1,2), [2,3), [3,4): '+text+'\nFrame [4,5): C opens the door')
        self.assertEqual(audit['rows'][-1]['prompt_line'],3)

    def test_merged_projection_keeps_fixed_contract_and_all_source_rows(self):
        payload=dict(observations=[self.row(dict(original_text='C holds cup'),f'C{i}',i) for i in [0,1]])
        original=dict(head='system',prompts=['  Description: '+json.dumps(payload)+'\n\n###\n\n Questions: Q'])
        output,audit=project(original,merge_repeated=True)
        self.assertEqual(io.fixed(original),io.fixed(output))
        self.assertEqual(audit['source_rows'],2)
        self.assertEqual(audit['rendered_rows'],1)

    def test_narrative_keeps_every_action_role_without_raw_field_labels(self):
        action = dict(actor='C', verb='moves', patient='red book', instrument='tongs',
            material='glue', body_part='left hand', surface='table',
            source_location='shelf', destination='bag', location='room',
            manner='carefully', recipient='woman')
        payload = dict(observations=[self.row(dict(actions=[action]))])
        before = deepcopy(payload)
        text, audit = narrative_description(payload)
        self.assertIn('C carefully moves red book using tongs with left hand from shelf to bag to woman.', text)
        for value in action.values():
            self.assertIn(value, text)
        self.assertIn('The material used in this action is glue.', text)
        self.assertIn('The working surface for this action is table.', text)
        self.assertIn('The location of this action is room.', text)
        self.assertNotIn('actor=', text)
        self.assertNotIn('Action:', text)
        self.assertEqual(payload, before)
        self.assertEqual(audit['rows'][0]['archived_fact_count'], 1)
        self.assertEqual(audit['rows'][0]['representation'], 'facts')

    def test_narrative_preserves_all_categories_and_source_connectors(self):
        evidence = dict(attributes=[dict(entity='book', property='color', value='red')],
            states=[dict(entity='book', dimension='wetness', value='wet')],
            relations=[dict(left='book', predicate='on', right='table', relation_type='spatial'),
                dict(left='C', predicate='holds', right='book', relation_type='possession')],
            changes=[dict(entity='book', before='wet', after='dry')],
            results=[dict(entity='machine', predicate='produces', value='basket')],
            event_links=[dict(left_event='C folds paper', connector='then',
                right_event='C moves paper', link_type='before')])
        text, audit = narrative_description(dict(observations=[self.row(evidence)]))
        for sentence in ['The color of book is red.', 'The observed state of book is wet.',
                'book is on table.', 'C holds book.', 'book changes from wet to dry.',
                'machine produces basket.', 'C folds paper then C moves paper.']:
            self.assertIn(sentence, text)
        self.assertEqual(audit['rows'][0]['archived_fact_count'], 7)
        self.assertNotIn('because', text)

    def test_narrative_keeps_negation_whitespace_and_multiline_source_map(self):
        original = '#C C never picks the paper.\n '
        rows = [self.row(dict(original_text=original,
                    actions=[dict(actor='C', verb='picks', patient='paper')]), 'C0', 0),
                self.row(dict(actions=[dict(actor='C', verb='picks', patient='carton paper')]), 'C1', 1)]
        text, audit = narrative_description(dict(observations=rows))
        self.assertEqual(text, 'Frame [0,1): ' + original + '\nFrame [1,2): C picks carton paper.')
        self.assertEqual([r['prompt_line'] for r in audit['rows']], [1, 3])
        self.assertEqual([r['caption_id'] for r in audit['rows']], ['C0', 'C1'])

    def test_narrative_does_not_merge_repeated_samples_or_drop_unknown_fields(self):
        evidence = dict(actions=[dict(actor='C', verb='picks', patient='carton paper')])
        rows = [self.row(deepcopy(evidence), f'C{i}', i) for i in (1, 9)]
        text, audit = narrative_description(dict(observations=rows))
        self.assertEqual(text, 'Frame [1,2): C picks carton paper.\nFrame [9,10): C picks carton paper.')
        self.assertFalse(audit['source_rows_merged'])
        rows[0]['evidence']['actions'][0]['unexpected_role'] = 'important'
        with self.assertRaisesRegex(ValueError, 'Unsupported narrative fact fields'):
            narrative_description(dict(observations=rows))


if __name__ == '__main__':
    unittest.main()
