"""Completion comparison: stable denominators, provenance and partial reports."""
from pathlib import Path
import tempfile
import unittest

from script import run_full_temporal_videotree as run


class FullTemporalVideoTreeTests(unittest.TestCase):
    def test_pending_never_becomes_final_accuracy_and_failed_answers_count_wrong(self):
        truth=dict(a=0,b=1,c=2)
        rows=dict(a=dict(answer={'selected_option':'0'},error=None,total_measured_seconds=2,
                         source_incomplete=[{'reason':'rejected'}]),
                  b=dict(answer={'selected_option':'1'},error={'type':'failed'},total_measured_seconds=4))
        summary=run.summary(list(truth),rows,truth,'script')
        self.assertEqual(summary['correct'],1)
        self.assertEqual(summary['errors'],1)
        self.assertEqual(summary['incomplete'],1)
        self.assertEqual(summary['pending'],1)
        self.assertIsNone(summary['accuracy'])
        self.assertEqual(summary['median_seconds'],3)
        rows['c']=dict(answer={'selected_option':'2'},error=None,total_measured_seconds=6)
        self.assertEqual(run.summary(list(truth),rows,truth,'script')['accuracy'],2/3)

    def test_paired_reports_do_not_mix_subset_denominators_and_export_all_pending_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            run.write(out/'cohorts.json',{'100':['a'],'300':['a','b'],'500':['a','b','c'],'additional200':['c']})
            run.write(out/'evaluation_only.json',dict(a=0,b=1,c=2))
            run.write(out/'runs/script/a/result.json',dict(answer={'selected_option':'0'},error=None,total_measured_seconds=5))
            run.write(out/'runs/script/b/result.json',dict(answer={'selected_option':'0'},error=None,total_measured_seconds=7))
            run.write(out/'videotree/runs/a/result.json',dict(prediction=1,error=None,inference_seconds=2))
            run.write(out/'videotree/runs/b/result.json',dict(prediction=1,error=None,inference_seconds=4))
            report=run.report(out)
            self.assertEqual(report['reports']['300']['paired']['script_only_correct'],['a'])
            self.assertEqual(report['reports']['300']['paired']['videotree_only_correct'],['b'])
            self.assertEqual(report['reports']['300']['paired']['accuracy_delta_percentage_points'],0)
            self.assertIsNone(report['reports']['500']['paired']['accuracy_delta_percentage_points'])
            self.assertIsNone(report['reports']['additional200']['script']['accuracy'])
            self.assertEqual(report['reports']['additional200']['script']['pending'],1)
            import csv
            with (out/'reports/per_question.csv').open() as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[-1]['subset'],'additional200')
            self.assertEqual(rows[-1]['script_answer'],'')

    def test_frozen_carried_result_mutation_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);result=out/'runs/script/a/result.json'
            run.write(result,dict(answer={'selected_option':'0'}))
            run.write(out/'frozen_hashes.json',{'runs/script/a/result.json':run.sha(result)})
            run.verify(out)
            run.write(result,dict(answer={'selected_option':'1'}))
            with self.assertRaisesRegex(ValueError,'Frozen source/input/result changed'):
                run.verify(out)

    def test_videotree_parser_fallback_is_visible_and_errors_are_not_correct(self):
        rows=dict(a=dict(prediction=0,error=None,inference_seconds=3,qa_parser_fallback=True),
                  b=dict(prediction=1,error={'type':'failed'},inference_seconds=7))
        summary=run.summary(['a','b'],rows,dict(a=0,b=1),'videotree')
        self.assertEqual(summary['accuracy'],.5)
        self.assertEqual(summary['parser_fallbacks'],1)
        self.assertEqual(summary['mean_seconds'],5)


if __name__=='__main__':unittest.main()
