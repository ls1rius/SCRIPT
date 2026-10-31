"""The filtering experiment changes quotations only and counts regressions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from script.run_memory_quotes_comparison import final_inputs,without_quotes,strict_prediction,run_arm,summarize
from script_core.config import load_config
from script_core.state import write_json
from tests.test_partial_temporal import fixture
from script_core import partial_temporal


class Backend:
    def __init__(self,response):self.response=response;self.calls=[]
    def call(self,operation,inputs,timeout):
        self.calls.append(operation)
        if operation=='temporal_qa_check':return dict(input_tokens=100)
        if operation=='videotree_qa':return dict(data=dict(text=self.response,selected_option='2'))
        raise AssertionError(operation)


class MemoryQuotesComparisonTests(unittest.TestCase):
    def pair(self):
        packet,bundle=fixture();memory=partial_temporal.project(bundle['memory'])
        view=partial_temporal.context(memory,bundle['memory'],dict(question='',options=[]))
        context=dict(context=view,selected_seconds=bundle['selected_seconds'])
        return packet,context

    def test_only_quotation_json_differs_and_nonmemory_caption_is_removed(self):
        packet,context=self.pair();before=deepcopy(context)
        visual=[dict(description='Exact retained visual detail.',source_frame_ids=['F0'])]
        pair,audit=final_inputs(packet,context,visual)
        self.assertEqual(without_quotes(pair['matched_full']),without_quotes(pair['memory_quotes']))
        self.assertEqual(pair['matched_full']['head'],pair['memory_quotes']['head'])
        self.assertIn('A wall is white.',pair['matched_full']['prompts'][0])
        self.assertNotIn('A wall is white.',pair['memory_quotes']['prompts'][0])
        for inp in pair.values():self.assertIn('Exact retained visual detail.',inp['prompts'][0])
        self.assertEqual(context,before)

    def test_parser_rejects_character_C_sentence_and_conflicting_declarations(self):
        self.assertEqual(strict_prediction('prediction: B\nexplanation: C holds it.'),'1')
        self.assertEqual(strict_prediction('prediction: b\nprediction: B'),'1')
        for text in ['prediction: C holds a box.','prediction: A\nprediction: B','No prediction']:
            with self.assertRaises(ValueError):strict_prediction(text)

    def test_each_arm_generates_once_and_keeps_legacy_prediction_for_audit(self):
        packet,context=self.pair();pair,_=final_inputs(packet,context,[])
        backend=Backend('prediction: B\nexplanation: Explicit detail.\nconfidence: 90')
        with tempfile.TemporaryDirectory() as folder:
            result=run_arm(Path(folder),'q','memory_quotes',pair['memory_quotes'],backend,load_config())
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(result['selected_option'],'1')
        self.assertEqual(result['legacy_selected_option'],'2')
        self.assertTrue(result['parser_disagrees_with_legacy'])
        self.assertEqual(backend.calls,['temporal_qa_check','videotree_qa'])

    def test_report_counts_both_corrections_and_regressions_and_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);ids=['a','b','c']
            write_json(out/'cohort.json',ids);write_json(out/'evaluation_only.json',dict(a=0,b=0,c=0))
            write_json(out/'controls.json',{u:dict(selected_option='0',error=None) for u in ids})
            write_json(out/'structure.json',{u:dict(empty_memory=False,visual_observations=0,quotes=1,
                original_captions=10) for u in ids})
            for uid,full,filtered in [('a','1','0'),('b','0','1')]:
                for arm,pred in [('matched_full',full),('memory_quotes',filtered)]:
                    write_json(out/'runs'/arm/uid/'result.json',dict(selected_option=pred,error=None,seconds=1))
            report=summarize(out)
        self.assertFalse(report['complete'])
        self.assertEqual(report['paired_completed'],2)
        self.assertEqual(report['paired']['corrected_count'],1)
        self.assertEqual(report['paired']['regressed_count'],1)
        self.assertEqual(report['paired']['net_correct'],0)
        self.assertIsNone(report['arms']['memory_quotes']['accuracy'])
        self.assertEqual(report['arms']['memory_quotes']['pending'],1)


if __name__=='__main__':unittest.main()
