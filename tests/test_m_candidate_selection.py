from copy import deepcopy
import json
import unittest

from script_core import m_candidate_selection as core, schemas
from script_core.m_scene_memory import validate_candidates


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.inputs = dict(question='Which activity?',options=['washing','cutting','watering'],
            directory=[dict(id='S0',description='Empty road'),dict(id='S1',description='C waters plants')])
        self.raw = dict(scene_reviews=dict(
            S0=dict(anchor=None,evidence_level='unrelated'),
            S1=dict(anchor='C waters plants',evidence_level='task_evidence')))

    def test_later_option_and_empty_candidate_boundary(self):
        self.assertEqual(validate_candidates(core.normalize_selection(self.raw,self.inputs),self.inputs),['S1'])
        self.raw['scene_reviews']['S1'].update(evidence_level='unrelated')
        self.assertEqual(validate_candidates(core.normalize_selection(self.raw,self.inputs),self.inputs),[])

    def test_complete_scene_coverage_and_score_bounds(self):
        for change in ('missing','negative','too_large','blank'):
            raw=deepcopy(self.raw);scene=raw['scene_reviews']['S1']
            if change=='missing':del raw['scene_reviews']['S0']
            elif change=='negative':scene['evidence_level']='invalid'
            elif change=='too_large':scene['evidence_level']=101
            else:scene['anchor']='invented quote'
            with self.subTest(change=change),self.assertRaises(ValueError):
                core.normalize_selection(raw,self.inputs)

    def test_two_calls_with_source_bound_quotes_preserve_original_inputs_and_all_claims(self):
        calls=[];events=[];raw=deepcopy(self.raw)
        brief=dict(option_claims={f'O{i}':dict(observable_claim=o,proposed_explanation='')
            for i,o in enumerate(self.inputs['options'])},retrieval_question='Which activity occurs?')
        class Meter:
            def json(_,op,request,keys):
                calls.append((op,deepcopy(request)))
                return deepcopy(brief if op==core.PLAN_OPERATION else dict(
                    scene_reviews={s['id']:raw['scene_reviews'][s['id']] for s in request['directory']})),dict(call_id=str(len(calls)))

        before=deepcopy(self.inputs)
        self.assertEqual(core.review_scenes(self.inputs,Meter(),lambda stage,*args:events.append(stage)),raw)
        self.assertEqual(self.inputs,before)
        self.assertEqual(events,['query_plan','joint_ranking'])
        self.assertEqual(len(calls),2)
        self.assertNotIn('directory',calls[0][1])
        self.assertEqual(calls[1][1]['directory'],core.prepare_inputs(before)['directory'])
        self.assertEqual(calls[1][1]['options'],before['options'])
        self.assertEqual(calls[1][1]['retrieval_brief'],brief)
        del brief['option_claims']['O2']
        with self.assertRaises(ValueError):core.review_scenes(self.inputs,Meter())

    def test_decoder_accepts_complete_positive_and_empty_branches(self):
        from lmformatenforcer import JsonSchemaParser
        schema=schemas.decoder_schema(schemas.output_schema(core.OPERATION,self.inputs))
        parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
        wire=dict(scene_reviews={sid:{k:r[k] for k in ('anchor','evidence_level')}
            for sid,r in self.raw['scene_reviews'].items()})
        for char in json.dumps(wire):
            self.assertIn(char,parser.get_allowed_characters())
            parser=parser.add_character(char)
        self.assertTrue(parser.can_end())
        self.assertEqual(schemas.restore_empty_arrays(wire,core.output_schema(core.OPERATION,self.inputs)),self.raw)


if __name__=='__main__':unittest.main()
