from copy import deepcopy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from script_core import o_sentence_filter as core


class SentenceFilterTests(unittest.TestCase):
    def setUp(self):
        self.source=dict(id='S0',text='C puts a brick on a wall with the left hand.',source_intervals=[[1,2],[8,9]])
        self.question=dict(question='Where is the brick placed?',options=['wall','floor','box','chair','bag'])
        self.report=dict(extraction_status='extracted',actions=[dict(actor='C',verb='puts',roles=dict(patient='brick',location='wall',body_part='left hand'))],
            attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])

    def test_schema_has_no_lexical_source_guards_and_extract_has_no_question(self):
        report=deepcopy(self.report);report['actions'][0]['roles']['location']='a place not literally in the sentence'
        units=core.candidates(self.source,report)
        self.assertTrue(any(u.get('value')=='a place not literally in the sentence' for u in units))
        self.assertEqual(set(core.extraction_input(self.source)),{'sentence'})

    def test_all_categories_available_and_unstated_roles_optional(self):
        schema=core.extraction_schema()
        self.assertTrue(all(schema['properties'][k]['maxItems']>0 for k in core.CATEGORIES))
        roles=schema['properties']['actions']['items']['properties']['roles']
        self.assertEqual(roles['required'],[])
        self.assertIn('destination',roles['properties'])
        report=deepcopy(self.report)
        report['actions'][0]['roles']={'patient':'brick','destination':'bag'}
        self.assertTrue(any(u.get('field')=='destination' for u in core.candidates(self.source,report)))
        with self.assertRaisesRegex(ValueError,'only the source sentence'):
            core.schema(core.EXTRACT,dict(sentence='C moves.',requested_fields=['actions.base']))

    def test_extract_then_filter_receives_every_extracted_unit(self):
        from script.run_o_sentence_experiment import process_source
        calls=[];report=deepcopy(self.report)
        report['actions'][0]['roles']={'patient':'brick','destination':'bag'}
        class Meter:
            def call(self,operation,request):
                calls.append((operation,request))
                if operation==core.EXTRACT:data=report
                elif operation==core.FILTER_PLAN:
                    data=dict(option_tests={f'O{i}':dict(single_observation=o,aggregate_requirement='',interpretation='') for i,o in enumerate(request['options'])})
                else:data=dict(observation='puts brick into bag',relevance='location evidence',labels={u['id']:1 for u in request['candidates']})
                return dict(data=data),{}
        with TemporaryDirectory() as tmp:
            result=process_source(self.question,self.source,Meter(),Path(tmp))
        self.assertEqual([c[0] for c in calls],[core.EXTRACT,core.FILTER_PLAN,core.FILTER])
        self.assertEqual(calls[0][1],{'sentence':self.source['text']})
        self.assertEqual(calls[2][1]['options'],self.question['options'])
        self.assertEqual(calls[2][1]['candidates'],result['candidates'])
        self.assertEqual(set(calls[1][1]),{'question','options'})
        self.assertIn('evidence_task',calls[2][1])
        self.assertTrue(any(u.get('field')=='destination' for u in result['selected']))

    def test_binary_selection_can_drop_everything_and_never_restores_rejected_slots(self):
        units=core.candidates(self.source,self.report)
        labels={u['id']:0 for u in units}
        self.assertEqual(core.select(units,labels),[])
        for u in units:
            if u.get('field') in ['verb','patient','location']:labels[u['id']]=1
        selected=core.select(units,labels)
        rows=core.present([self.source],{'S0':dict(selected=selected)})
        record=rows[0]['information'][0]
        self.assertNotIn('actor',record);self.assertNotIn('body_part',record)
        self.assertEqual(record['location'],'wall')
        self.assertEqual(rows[0]['intervals'],[[1,2],[8,9]])
        self.assertNotIn('left hand',str(rows))

    def test_filter_includes_all_options_sentence_and_complete_candidates(self):
        units=core.candidates(self.source,self.report)
        request=core.filter_input(self.question,self.source,units)
        self.assertEqual(request['options'],self.question['options'])
        self.assertEqual(request['sentence'],self.source['text'])
        self.assertEqual(request['candidates'],units)

    def test_retained_action_core_is_local_and_preserves_raw_model_labels(self):
        report=deepcopy(self.report)
        report['actions'].append(dict(actor='D',verb='moves',roles=dict(patient='box')))
        units=core.candidates(self.source,report)
        raw={u['id']:int(u['group']=='actions_0' and u.get('field')=='verb') for u in units}
        before=deepcopy(raw)
        labels,adjustments=core.preserve_action_core(units,raw)
        self.assertEqual(raw,before)
        kept=core.select(units,labels)
        self.assertEqual({u['field'] for u in kept},{'actor','verb','patient'})
        self.assertTrue(all(u['group']=='actions_0' for u in kept))
        self.assertEqual(len(adjustments),2)
        zero={u['id']:0 for u in units}
        self.assertEqual(core.preserve_action_core(units,zero),(zero,[]))
        actor_only={u['id']:int(u.get('field')=='actor') for u in units}
        self.assertEqual(core.preserve_action_core(units,actor_only),(actor_only,[]))
        verb_only=[u for u in units if u.get('field')=='verb']
        self.assertEqual(core.preserve_action_core(verb_only,{u['id']:1 for u in verb_only}),
            ({u['id']:1 for u in verb_only},[]))

    def test_failed_or_empty_extraction_is_raw_candidate_and_is_still_filterable(self):
        report={k:[] for k in core.CATEGORIES};report['extraction_status']='unavailable'
        units=core.candidates(self.source,report)
        self.assertEqual(units[0]['kind'],'raw_sentence')
        self.assertEqual(units[0]['text'],self.source['text'])
        self.assertEqual(core.select(units,{'U0':0}),[])
        self.assertEqual(core.select(units,{'U0':1}),units)

    def test_question_only_condition_never_supplies_options_to_filter_or_compose(self):
        from script.run_o_sentence_experiment import planned_filter_question
        requests=[]
        class Meter:
            def call(self,operation,request):
                requests.append(request)
                return dict(data=dict(option_tests=dict(Q=dict(single_observation='placing a brick',aggregate_requirement='',interpretation='')))),{}
        question=planned_filter_question(Meter(),dict(question=self.question['question']))
        units=core.candidates(self.source,self.report)
        request=core.filter_input(question,self.source,units)
        self.assertNotIn('options',requests[0]);self.assertNotIn('options',request)
        self.assertEqual(request['question'],self.question['question'])
        self.assertEqual(set(request['evidence_task']['option_tests']),{'Q'})
        self.assertNotIn('options',core.compose_input(question,units))

    def test_missing_duplicate_extra_and_nonbinary_labels_rejected(self):
        units=[dict(id='U0'),dict(id='U1')]
        for labels in [{'U0':1},{'U0':1,'U1':0,'U2':0},{'U0':2,'U1':0},{'U0':True,'U1':0}]:
            with self.assertRaises(ValueError):core.select(units,labels)
        with self.assertRaises(ValueError):core.filter_schema([dict(id='U0'),dict(id='U0')])

    def test_composition_sees_only_selected_fields_and_may_remove_subject_only(self):
        units=core.candidates(self.source,self.report)
        selected=[u for u in units if u.get('field')=='actor']
        request=core.compose_input(self.question,selected)
        self.assertEqual(set(request),{'question','groups'})
        self.assertEqual(request['question'],self.question['question'])
        with_options=core.compose_input(self.question,selected,include_options=True)
        self.assertEqual(with_options['options'],self.question['options'])
        with self.assertRaisesRegex(ValueError,'requires the question'):
            core.compose_schema(dict(groups=request['groups']))
        allowed=core.compose_schema(request)['properties']['groups']['properties']['actions_0']['properties']['text']['enum']
        self.assertEqual(allowed,['','Actor: C.'])
        self.assertNotIn('sentence',request)
        self.assertNotIn('left hand',str(request));self.assertEqual(request['groups']['actions_0'],selected)
        rows=core.present([self.source],{'S0':dict(selected=selected)},composed={'S0':dict(groups={'actions_0':dict(text='')},text='')})
        self.assertEqual(rows,[])
        self.assertTrue(core.present([self.source],{'S0':dict(selected=selected)}))

    def test_separate_actions_are_not_rebound(self):
        self.report['actions'].append(dict(actor='D',verb='moves',roles=dict(patient='box',location=None,body_part=None)))
        units=core.candidates(self.source,self.report)
        rows=core.present([self.source],{'S0':dict(selected=units)})
        a,b=rows[0]['information'];self.assertEqual(a['actor'],'C');self.assertEqual(b['actor'],'D')
        self.assertNotIn('location',b)

    def test_source_split_preserves_provenance_and_final_input_strips_old_memory(self):
        source=dict(self.source,source_caption_ids=['C0'],source_frame_ids=[],source_type='caption')
        source['text']='C lifts a box. C places it down.'
        units=core.sentence_sources([source]);self.assertEqual(len(units),2)
        self.assertEqual(units[0]['source_caption_ids'],['C0'])
        original=dict(head='system',prompts=['  Description: OLD\n\n###\n\n Questions: Where?\nTemporal entity memory (caption):\n{"secret":"SECRET"}'])
        final=core.final_input(original,[])
        self.assertNotIn('SECRET',final['prompts'][0]);self.assertNotIn('OLD',final['prompts'][0])
        self.assertIn('No information was retained',final['prompts'][0])


if __name__=='__main__':unittest.main()
