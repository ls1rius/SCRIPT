from copy import deepcopy
import json
import unittest

from script_core import n_scene_selection as n, m_scene_memory as m, schemas


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.inputs=dict(question='Which activity?',options=['folding','hanging','ironing'],
            directory=[dict(id='S0',interval=[0,1],description='Ironing clothes'),
                       dict(id='S1',interval=[1,2],description='Empty sky')],source_type='caption')
        self.raw=dict(scene_reviews={s['id']:dict(option_reviews={f'O{i}':dict(
            reason='Source activity reviewed',priority=0,anchor=None) for i in range(3)}) for s in self.inputs['directory']})

    def positive(self,sid,oid,score):
        quote=next(s['description'] for s in self.inputs['directory'] if s['id']==sid)
        self.raw['scene_reviews'][sid]['option_reviews'][oid].update(anchor=quote,priority=score)

    def test_later_option_retains_scene_without_changing_other_reviews(self):
        self.positive('S0','O2',90)
        before=deepcopy(self.raw)
        selected=n.normalize_selection(self.raw,self.inputs)
        self.assertEqual(m.validate_candidates(selected,self.inputs),['S0'])
        self.assertEqual(self.raw,before)
        self.assertEqual(selected['scene_decisions']['S0']['priority'],90)
        self.assertEqual(selected['scene_decisions']['S1']['relevance'],'excluded')

    def test_every_option_and_scene_required_no_foreign_id_or_empty_reason(self):
        for change in ('option','scene','foreign','blank'):
            raw=deepcopy(self.raw)
            if change=='option':del raw['scene_reviews']['S0']['option_reviews']['O2']
            elif change=='scene':del raw['scene_reviews']['S1']
            elif change=='foreign':raw['scene_reviews']['S0']['option_reviews']['O9']=raw['scene_reviews']['S0']['option_reviews']['O0']
            else:raw['scene_reviews']['S0']['option_reviews']['O0']['reason']=' '
            with self.assertRaises(ValueError):n.normalize_selection(raw,self.inputs)

    def test_truly_unrelated_can_stay_empty(self):
        self.raw['scene_reviews']['S1']['option_reviews']['O0']['anchor']='Empty sky'
        selected=n.normalize_selection(self.raw,self.inputs)
        self.assertEqual(m.validate_candidates(selected,self.inputs),[])

    def test_max_score_and_directory_order_tie_break(self):
        self.positive('S1','O0',50)
        self.positive('S0','O2',50)
        self.positive('S0','O1',25)
        selected=n.normalize_selection(self.raw,self.inputs)
        self.assertEqual(m.validate_candidates(selected,self.inputs),['S0','S1'])

    def test_real_character_decoder_requires_all_reviews(self):
        from lmformatenforcer import JsonSchemaParser
        schema=schemas.decoder_schema(n.output_schema(n.OPERATION,self.inputs))
        parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
        for char in json.dumps(self.raw):
            self.assertIn(char,parser.get_allowed_characters())
            parser=parser.add_character(char)
        self.assertTrue(parser.can_end())
        self.assertEqual(schemas.output_schema(n.OPERATION,self.inputs),n.output_schema(n.OPERATION,self.inputs))

    def test_positive_review_needs_local_literal_anchor_and_index_cannot_be_forged(self):
        self.positive('S0','O2',90)
        for anchor in (None,'E999'):
            raw=deepcopy(self.raw);raw['scene_reviews']['S0']['option_reviews']['O2']['anchor']=anchor
            with self.assertRaises(ValueError):n.normalize_selection(raw,self.inputs)
        prepared=n.prepare_inputs(self.inputs)
        self.assertEqual(prepared['directory'][0]['description'],self.inputs['directory'][0]['description'])
        self.assertEqual(prepared,n.prepare_inputs(prepared))
        prepared['directory'][0]['summary_clauses']['E0']='Invented action'
        with self.assertRaises(ValueError):n.prepare_inputs(prepared)

    def test_each_model_call_has_one_scene_and_all_options_with_no_prior_outputs(self):
        self.positive('S0','O2',90)
        calls=[];events=[];raw=self.raw
        class Meter:
            def json(_,operation,request,keys):
                calls.append(deepcopy(request));sid=request['directory'][0]['id']
                oid=request['focus_option']['option_id']
                return dict(scene_reviews={sid:dict(option_reviews={oid:raw['scene_reviews'][sid]['option_reviews'][oid]})}),dict(call_id=sid)
        combined=n.review_scenes(self.inputs,Meter(),lambda sid,*args:events.append(sid))
        self.assertEqual(combined,self.raw)
        self.assertEqual(events,['S0']*3+['S1']*3)
        self.assertEqual(len(calls),6)
        for request in calls:
            self.assertEqual(len(request['directory']),1)
            self.assertEqual(request['options'],self.inputs['options'])
            self.assertNotIn('scene_reviews',request)
            oid=request['focus_option']['option_id']
            self.assertEqual(request['focus_option']['text'],self.inputs['options'][int(oid[1:])])


if __name__=='__main__':unittest.main()
