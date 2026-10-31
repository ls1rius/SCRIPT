from copy import deepcopy
import json
import unittest

from script.prepare_controlled_memory_design import (
    description_parts, fixed_contract, lossless_input, lossless_records, replace_description,
)


class ControlledMemoryInputTests(unittest.TestCase):
    def row(self):
        return dict(packet=dict(captions=[
            dict(id='C0', interval=[0,1], text='#C C lifts the red book.'),
            dict(id='C1', interval=[1,2], text='#C C never wipes the book with a "cloth". 中文'),
            dict(id='C2', interval=[2,3], text='#C C lifts the red book.')]),
            selected_seconds=[2,0], original=dict(query_id='q', head='Original instructions',
                prompts=['Examples\n  Description: Frame 0: old\n\n###\n\n Questions: Why?\n'
                         'Options: A then B\nOriginal answer instructions\n'
                         'Temporal entity memory: {"entities":["keep"]}\n'
                         'Supplemental visual observations: ["keep"]']))

    def test_description_is_the_only_changed_region(self):
        row=self.row();before=deepcopy(row);result=lossless_input(row)
        self.assertEqual(fixed_contract(result),fixed_contract(row['original']))
        self.assertEqual(row,before)
        self.assertNotEqual(description_parts(result)[1],description_parts(row['original'])[1])

    def test_duplicate_text_keeps_separate_ids_and_times(self):
        records=lossless_records(self.row())
        self.assertEqual([r['caption_id'] for r in records],['C0','C2'])
        self.assertEqual([r['sample_interval'] for r in records],[[0,1],[2,3]])
        self.assertEqual(records[0]['evidence'],records[1]['evidence'])

    def test_negation_quotes_unicode_and_markers_survive(self):
        row=self.row();row['selected_seconds']=[1]
        result=lossless_input(row)
        payload=json.loads(description_parts(result)[1])
        self.assertEqual(payload['observations'][0]['evidence']['original_text'],row['packet']['captions'][1]['text'])

    def test_unread_evidence_is_never_added(self):
        row=self.row();row['selected_seconds']=[0]
        self.assertEqual([r['caption_id'] for r in lossless_records(row)],['C0'])

    def test_invalid_or_duplicate_positions_are_rejected(self):
        for selected in [[0,0],[-1],[3],[True]]:
            row=self.row();row['selected_seconds']=selected
            with self.assertRaises(ValueError):lossless_records(row)

    def test_ambiguous_template_boundaries_are_rejected(self):
        original=self.row()['original'];original['prompts'][0]+='  Description: extra'
        with self.assertRaises(ValueError):replace_description(original,'replacement')


if __name__ == '__main__':
    unittest.main()
