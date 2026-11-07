from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from script_core import generalized_lazy_memory as core
from script_core import lazy_script_memory as legacy
from script_core.qa_evidence_view import compact_description
from script.run_generalized_lazy_experiment import extract_source


def unit(name='U0',text='#C C moves the red book with her left hand.',start=9):
    return dict(id=name,text=text,source_caption_ids=[name+'C'],source_intervals=[[start,start+1]])


def row(u):
    return dict(caption_id=u['source_caption_ids'][0],source_id=u['id'],sample_interval=u['source_intervals'][0],frame_index=u['source_intervals'][0][0],original_text=u['text'])


def accepted(facts=(),uncertain=False,rejected=(),error=None):
    return dict(accepted=dict(facts=list(facts),uncertain=uncertain,rejected=list(rejected)),error=error,model_called=True)


class GeneralizedLazyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'script.sqlite3'
        self.db=core.ScriptDB(self.path);self.u=unit();self.r=row(self.u)
        self.base=dict(kind='action',source_id='U0',actor='C',verb='moves',patient='red book')
    def tearDown(self):self.db.close();self.temp.cleanup()

    def test_incremental_fields_persist_across_connections_and_do_not_pollute_projection(self):
        calls=[]
        def extract(u,fields):
            calls.append(fields);return accepted([dict(self.base,body_part='left hand')] if 'actions.body_part' in fields else [self.base])
        first,_=self.db.ensure('v',[self.u],[self.r],['actions.base'],extract)
        later,stats=self.db.ensure('v',[self.u],[self.r],['actions.base','actions.body_part'],extract)
        self.assertEqual(calls,[['actions.base'],['actions.body_part']]);self.assertEqual(stats['cache_hits'],1)
        self.assertNotIn('body_part',first['U0']['accepted']['facts'][0]);self.assertIn('body_part',later['U0']['accepted']['facts'][0])
        self.db.close();self.db=core.ScriptDB(self.path)
        warm,stats=self.db.ensure('v',[self.u],[self.r],['actions.base'],lambda *_:self.fail('re-extracted cached field'))
        text,_,_=self.db.view('v',dict(question='What moves?'),[self.r],warm,compact_description)
        self.assertIn(self.u['text'],text)
        self.assertNotIn('body_part',warm['U0']['accepted']['facts'][0])
        self.assertEqual(stats['missing_fields'],0)

    def test_missing_absent_uncertain_failed_are_distinct_and_not_silently_retried(self):
        key=self.db.register('v',[self.u],[self.r])['U0'];self.assertIsNone(self.db.get('v',key,'results'))
        for field,value,expected in [('results',accepted(),'checked_absent'),('changes',accepted(uncertain=True),'uncertain'),
                ('states',accepted(error={'type':'bad'}),'failed')]:
            self.db.ensure('v',[self.u],[self.r],[field],lambda *_,v=value:v)
            self.assertEqual(self.db.get('v',key,field)['status'],expected)
            self.db.ensure('v',[self.u],[self.r],[field],lambda *_:self.fail('automatic retry'))
        self.assertIsNone(self.db.get('v',key,'attributes.material'))

    def test_extractor_version_and_video_namespace_invalidate_cache(self):
        self.db.ensure('v',[self.u],[self.r],['actions.base'],lambda *_:accepted([self.base]))
        other=core.ScriptDB(self.path,core.VERSION+':new-extractor')
        self.assertIsNone(other.get('v',other.register('v',[self.u],[self.r])['U0'],'actions.base'));other.close()
        self.assertIsNone(self.db.get('other',self.db.register('other',[self.u],[self.r])['U0'],'actions.base'))

    def test_source_mutation_is_rejected_and_only_requested_sources_render(self):
        other=unit('U1',text='#C C holds a spoon.',start=20)
        self.db.ensure('v',[other],[row(other)],['actions.base'],lambda *_:accepted([dict(self.base,source_id='U1',verb='holds',patient='spoon')]))
        result,_=self.db.ensure('v',[self.u],[self.r],['actions.base'],lambda *_:accepted([self.base]))
        text,_,_=self.db.view('v',{},[self.r],result,compact_description)
        self.assertNotIn('spoon',text);self.assertNotIn('[20,21)',text)
        changed=dict(self.u,text='different')
        with self.assertRaisesRegex(ValueError,'Source occurrence changed'):self.db.register('v',[changed],[dict(self.r,original_text='different')])

    def test_schema_disables_unrequested_categories_and_roles(self):
        schema=core.output_schema(core.EXTRACT,dict(unit=dict(id='U0',text=self.u['text']),source_type='caption',requested_fields=['actions.body_part']))
        props=schema['properties'];self.assertEqual(props['states']['maxItems'],0)
        self.assertEqual(props['attributes']['maxItems'],0)
        self.assertEqual(props['actions']['items']['properties']['roles']['items']['properties']['role']['enum'],['patient','body_part'])
        schema=core.output_schema(core.EXTRACT,dict(unit=dict(id='U0',text='The book is wet'),source_type='caption',requested_fields=['states']))
        self.assertEqual(schema['properties']['actions']['maxItems'],0)
        self.assertEqual(set(schema['properties']['states']['items']['properties']),{'entity','description'})

    def test_checked_absent_and_uncertain_both_retain_literal_source(self):
        result,_=self.db.ensure('v',[self.u],[self.r],['results'],lambda *_:accepted())
        text,payload,_=self.db.view('v',{},[self.r],result,compact_description)
        self.assertIn(self.u['text'],text);self.assertEqual(len(payload['observations']),1)
        result,_=self.db.ensure('v',[self.u],[self.r],['changes'],lambda *_:accepted(uncertain=True))
        text,_,_=self.db.view('v',{},[self.r],result,compact_description)
        self.assertIn(self.u['text'],text)

    def test_confident_action_core_cannot_remove_unrequested_tool_or_body_part(self):
        for text in ['#C C moves the red book with her left hand.',
                '#C C cuts the paper with scissors.', '#C C puts the book on the table.']:
            with self.subTest(text=text):
                u=unit(text=text);r=row(u)
                # Project a confident core with no adjunct, as a base-only task does.
                fact=dict(self.base,verb='cuts',patient='paper') if 'cuts' in text else dict(self.base)
                source=dict(accepted=accepted([fact])['accepted'],complete_simple=True)
                body,_,audit=self.db.view('v',{},[r],{'U0':source},compact_description)
                self.assertIn(text,body)
                self.assertEqual(audit['source_coverage'],1.)

    def test_whole_clause_facts_can_replace_original_without_extra_roles(self):
        u=unit(text='#C C moves the red book.');r=row(u)
        result,_=self.db.ensure('simple',[u],[r],['actions.base'],lambda *_:accepted([self.base]))
        body,payload,audit=self.db.view('simple',{},[r],result,compact_description)
        self.assertIn('Action: actor="C"; verb="moves"; patient="red book"',body)
        self.assertNotIn('original_text',payload['observations'][0]['evidence'])
        self.assertEqual(audit['retained_original_rows'],0)

    def test_empty_upstream_rows_fail_instead_of_generating_empty_evidence(self):
        with self.assertRaisesRegex(ValueError,'nonempty upstream'):
            self.db.view('v',{},[],{},compact_description)

    def test_goal_only_plans_get_supporting_actions_without_expanding_source_scope(self):
        for fields in [['changes'],['event_links.purpose'],['states']]:
            class Meter:
                def json(m,op,inputs,required):
                    self.assertIn('actions.instrument',inputs['available_fields'])
                    self.assertNotIn('units',inputs)
                    return dict(fields=fields),None
            task=dict(question='What is the purpose?',options=fields)
            plan,_=self.db.plan('v',task,[self.u],Meter())
            self.assertEqual(plan['model_requested_fields'],fields)
            self.assertEqual(plan['added_support_fields'],['actions.base'])
            self.assertEqual(set(plan['fields']),set(fields)|{'actions.base'})
            self.assertEqual(plan['source_ids'],['U0'])

    def test_schema_skipped_changes_preserve_actions_and_explicit_tools(self):
        protocol=SimpleNamespace(is_infrastructure=lambda _:False)
        for text in ['#C C cuts the leaves with scissors.',
                     '#C C welds the rod with a welding machine.']:
            u=unit(text=text);r=row(u)
            class Meter:
                def json(*args):raise AssertionError('Schema should skip unsupported explicit changes')
            result,stats=self.db.ensure(text,[u],[r],['changes'],
                lambda u,fields:extract_source(u,fields,Meter(),core,protocol))
            self.assertEqual(stats['status_checked_absent'],1)
            body,_,audit=self.db.view(text,{},[r],result,compact_description)
            self.assertIn(text,body)
            self.assertEqual(audit['visible_source_rows'],1)

    def test_legacy_database_is_rejected_without_modification(self):
        import hashlib
        old_path=self.path.with_name('F.sqlite3')
        with legacy.ScriptDB(old_path) as old:
            key=old.register('v',[self.u],[self.r])['U0']
            old.put('v',key,'actions.base','checked_absent',[])
        before=hashlib.sha256(old_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'separate database'):
            core.ScriptDB(old_path)
        self.assertEqual(hashlib.sha256(old_path.read_bytes()).hexdigest(),before)
        with self.assertRaisesRegex(ValueError,'version namespace'):
            core.ScriptDB(self.path,legacy.VERSION)

    def test_role_only_fragment_joins_unique_core_but_not_ambiguous_objects(self):
        slot=dict(kind='action',source_id='U0',actor='C',verb='moves',body_part='left hand')
        merged=core.combine([self.base,slot]);self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['patient'],'red book');self.assertEqual(merged[0]['body_part'],'left hand')
        ambiguous=core.combine([self.base,dict(self.base,patient='blue book'),slot])
        self.assertEqual(len(ambiguous),3);self.assertNotIn('patient',ambiguous[-1])

    def test_plans_cache_by_task_and_authorized_source_scope(self):
        class Meter:
            calls=0
            def json(m,op,inputs,required):
                m.calls+=1;return dict(fields=['actions.base']),None
        meter=Meter();task=dict(question='What?',options=[])
        plan,hit=self.db.plan('v',task,[self.u],meter);self.assertFalse(hit)
        _,hit=self.db.plan('v',task,[self.u],meter);self.assertTrue(hit);self.assertEqual(meter.calls,1)
        _,hit=self.db.plan('v',task,[unit('U1')],meter);self.assertFalse(hit);self.assertEqual(meter.calls,2)

    def test_extractor_inputs_contain_no_task_and_validate_requested_roles(self):
        class Meter:
            inputs=None
            def json(m,op,inputs,required):
                m.inputs=inputs
                data={c:[] for c in core.joint.COLLECTIONS.values()};data['uncertain']=False
                data['actions']=[dict(actor='C',verb='moves',roles=[dict(role='patient',value='red book')])]
                return data,None
        meter=Meter();protocol=SimpleNamespace(is_infrastructure=lambda _:False)
        result=extract_source(self.u,['actions.base'],meter,core,protocol)
        self.assertIsNone(result['error']);self.assertNotIn('question',meter.inputs);self.assertNotIn('options',meter.inputs)
        self.assertEqual(set(meter.inputs),{'unit','requested_fields','source_type'})

    def test_open_conditions_survive_extraction_reopen_and_render_with_provenance(self):
        from tests.test_generalized_entity_memory import report
        for entity,description in [('server','unreachable'),('device','ready for pairing'),
                ('liquid','viscous'),('application','awaiting review')]:
            u=unit(entity,f'The {entity} is {description}.');r=row(u)
            class Meter:
                def json(m,operation,inputs,required):
                    self.assertEqual(set(inputs),{'unit','source_type','requested_fields'})
                    return report(states=[dict(entity=entity,description=description)]),None
            values,_=self.db.ensure('open',[u],[r],['states'],lambda u,f:extract_source(
                u,f,Meter(),core,SimpleNamespace(is_infrastructure=lambda _:False)))
            self.db.close();self.db=core.ScriptDB(self.path)
            warm,stats=self.db.ensure('open',[u],[r],['states'],lambda *_:self.fail('state re-extracted'))
            self.assertEqual(values,warm)
            text,payload,audit=self.db.view('open',{},[r],warm,compact_description)
            fact=payload['observations'][0]['evidence']['states'][0]
            self.assertEqual(fact,dict(entity=entity,description=description))
            self.assertIn(description,text);self.assertIn('Frame [9,10)',text)
            self.assertEqual(audit['open_state_facts'],1)
            self.assertEqual(payload['observations'][0]['caption_id'],r['caption_id'])

    def test_fixed_state_fields_and_foreign_or_legacy_state_records_are_rejected(self):
        from tests.test_generalized_entity_memory import report
        inputs=dict(unit=dict(id='U0',text='The server is unreachable.'),source_type='caption',requested_fields=['states'])
        with self.assertRaises(ValueError):core.output_schema(core.EXTRACT,dict(inputs,requested_fields=['states.wetness']))
        for state in [dict(entity='server',dimension='wetness',value='wet'),dict(entity='server',description='online')]:
            class Meter:
                def json(m,*args):return report(states=[state]),None
            result=extract_source(dict(id='U0',text=inputs['unit']['text']),['states'],Meter(),core,
                SimpleNamespace(is_infrastructure=lambda _:False))
            self.assertIsNotNone(result['error']);self.assertFalse(result['accepted']['facts'])
        key=self.db.register('v',[self.u],[self.r])['U0']
        with self.assertRaises(ValueError):self.db.put('v',key,'states','value',[
            dict(kind='state',source_id='U0',entity='server',dimension='wetness',value='wet')])

    def test_state_category_keeps_multiple_conditions_and_separate_sample_times(self):
        from tests.test_generalized_entity_memory import report
        u=unit('U0','The server is unreachable and the device is ready for pairing.',0)
        repeated=unit('U1',u['text'],5)
        class Meter:
            calls=0
            def json(m,*args):
                m.calls+=1
                return report(states=[dict(entity='server',description='unreachable'),
                    dict(entity='device',description='ready for pairing')]),None
        meter=Meter();units=[u,repeated];rows=list(map(row,units))
        result,_=self.db.ensure('v',units,rows,['states'],lambda u,f:extract_source(
            u,f,meter,core,SimpleNamespace(is_infrastructure=lambda _:False)))
        self.assertEqual(meter.calls,1)
        self.assertEqual(len(result['U1']['accepted']['facts']),2)
        self.assertTrue(all(f['source_id']=='U1' for f in result['U1']['accepted']['facts']))
        text,payload,audit=self.db.view('v',dict(question='Server condition?'),rows,result,compact_description)
        self.assertIn('Frame [0,1)',text);self.assertIn('Frame [5,6)',text)
        self.assertEqual(len(payload['observations']),2)
        self.assertEqual(audit['open_state_facts'],4)
        self.assertFalse(audit['source_rows_merged'])

    def test_nullable_actor_decodes_and_incremental_roles_never_invent_identity(self):
        from tests.test_generalized_entity_memory import report
        u=unit(text='The box was moved with a crane.');r=row(u)
        calls=[]
        class Meter:
            def json(m,op,inputs,required):
                fields=inputs['requested_fields'];calls.append(fields)
                roles=[dict(role='patient',value='box')]
                if 'actions.instrument' in fields:roles.append(dict(role='instrument',value='crane'))
                return report(actions=[dict(actor=None,verb='moved',roles=roles)]),None
        callback=lambda u,f:extract_source(u,f,Meter(),core,SimpleNamespace(is_infrastructure=lambda _:False))
        first,_=self.db.ensure('passive',[u],[r],['actions.base'],callback)
        later,_=self.db.ensure('passive',[u],[r],['actions.base','actions.instrument'],callback)
        self.assertEqual(calls,[['actions.base'],['actions.base','actions.instrument']])
        facts=later['U0']['accepted']['facts'];self.assertEqual(len(facts),1)
        self.assertNotIn('actor',facts[0]);self.assertEqual(facts[0]['instrument'],'crane')
        text,_,audit=self.db.view('passive',{},[r],later,compact_description)
        self.assertIn(u['text'],text);self.assertEqual(audit['actorless_action_facts'],1)

    def test_role_requests_include_alignment_core_and_reject_preposition_actions(self):
        from tests.test_generalized_entity_memory import report
        inputs=core.extraction_inputs(self.u,['actions.body_part'])
        self.assertEqual(inputs['requested_fields'],['actions.base','actions.body_part'])
        self.assertEqual(set(inputs),{'unit','requested_fields','source_type'})
        passive=core.extraction_inputs(unit(text='The crate was lifted with a hoist.'),['actions.instrument'])
        schema=core.output_schema(core.EXTRACT,passive)
        valid=report(actions=[dict(actor=None,verb='lifted',roles=[
            dict(role='patient',value='crate'),dict(role='instrument',value='hoist')])])
        core.validate_output(valid,schema)
        broken=deepcopy(valid)
        broken['actions'].append(dict(actor=None,verb='with',roles=[dict(role='instrument',value='hoist')]))
        with self.assertRaises(ValueError):core.validate_output(broken,schema)

    def test_predicate_contract_rejects_phrase_bypass_for_both_actor_branches(self):
        from tests.test_generalized_entity_memory import report
        for text,actor in [('The box was moved with a crane.',None),
                           ('C moves the box with a crane.','C')]:
            u=unit(text=text)
            schema=core.output_schema(core.EXTRACT,core.extraction_inputs(u,['actions.instrument']))
            for verb in ['with','with a crane','a crane']:
                data=report(actions=[dict(actor=actor,verb=verb,roles=[dict(role='instrument',value='crane')])])
                with self.subTest(text=text,verb=verb):
                    with self.assertRaises(ValueError):core.validate_output(data,schema)
                    with self.assertRaises(ValueError):core.accept(u,data)

    def test_null_actor_cannot_bypass_explicit_subject_on_role_increment(self):
        from tests.test_generalized_entity_memory import report
        for fields in [['actions.body_part'],['actions.base','actions.body_part']]:
            schema=core.output_schema(core.EXTRACT,core.extraction_inputs(self.u,fields))
            data=report(actions=[dict(actor='C',verb='moves',roles=[
                dict(role='patient',value='red book'),dict(role='body_part',value='left hand')])])
            core.validate_output(data,schema)
            data['actions'][0]['actor']=None
            with self.assertRaises(ValueError):core.validate_output(data,schema)

    def test_passive_and_imperative_roles_keep_unknown_actor_and_merge_same_predicate(self):
        from tests.test_generalized_entity_memory import report
        for text,verb,obj,tool in [
            ('The crate was lifted with a hoist.','lifted','crate','hoist'),
            ('Lift the crate with a hoist.','Lift','crate','hoist')]:
            u=unit(text=text)
            data=report(actions=[dict(actor=None,verb=verb,roles=[dict(role='patient',value=obj)]),
                dict(actor=None,verb=verb,roles=[dict(role='instrument',value=tool)])])
            result=core.accept(u,data)
            self.assertFalse(result['rejected'])
            combined=core.combine(result['facts'])
            self.assertEqual(len(combined),1)
            self.assertNotIn('actor',combined[0])
            self.assertEqual(combined[0]['instrument'],tool)

    def test_phrasal_verbs_multiple_actions_and_negative_sources(self):
        from tests.test_generalized_entity_memory import report
        u=unit(text='C picks up the cup then C puts down the cup.')
        data=report(actions=[dict(actor='C',verb=v,roles=[dict(role='patient',value='cup')])
            for v in ['picks up','puts down']])
        schema=core.output_schema(core.EXTRACT,core.extraction_inputs(u,['actions.base']))
        core.validate_output(data,schema)
        self.assertTrue(all(core.predicate_span(v) for v in ['picks up','puts down','looks at']))
        for text in ['C never moves the box with a crane.','The box might be moved with a crane.']:
            class Meter:
                def json(*args):raise AssertionError('Guarded source must not call model')
            result=extract_source(unit(text=text),['actions.instrument'],Meter(),core,
                                  SimpleNamespace(is_infrastructure=lambda _:False))
            self.assertTrue(result['accepted']['uncertain'])
            self.assertFalse(result['accepted']['facts'])

    def test_explicit_actor_cannot_be_omitted_and_negative_conditions_remain_original(self):
        from tests.test_generalized_entity_memory import report
        class Meter:
            calls=0
            def json(m,*args):
                m.calls+=1
                return report(actions=[dict(actor=None,verb='moves',roles=[dict(role='patient',value='box')])]),None
        meter=Meter();u=unit(text='C moves the box.');r=row(u)
        result,_=self.db.ensure('active',[u],[r],['actions.base'],lambda u,f:extract_source(
            u,f,meter,core,SimpleNamespace(is_infrastructure=lambda _:False)))
        self.assertEqual(meter.calls,1)  # F-style boundary: no extra role review.
        self.assertFalse(result['U0']['accepted']['facts'])
        text,_,_=self.db.view('active',{},[r],result,compact_description);self.assertIn(u['text'],text)
        for text in ['The server is not reachable.','The server seems unreachable.']:
            u=unit(text=text);r=row(u)
            result,_=self.db.ensure(text,[u],[r],['states'],lambda u,f:extract_source(
                u,f,meter,core,SimpleNamespace(is_infrastructure=lambda _:False)))
            rendered,_,_=self.db.view(text,{},[r],result,compact_description)
            self.assertIn(text,rendered)
        self.assertEqual(meter.calls,1)

    def test_request_schema_rejects_question_and_unrequested_state_output(self):
        from tests.test_generalized_entity_memory import report
        inputs=dict(unit=dict(id='U0',text='The server is unreachable.'),source_type='caption',requested_fields=['actions.base'])
        with self.assertRaisesRegex(ValueError,'source-only'):core.output_schema(core.EXTRACT,dict(inputs,question='secret'))
        with self.assertRaises(ValueError):core.validate_output(
            report(states=[dict(entity='server',description='unreachable')]),core.output_schema(core.EXTRACT,inputs))

    def test_real_decoder_supports_open_states_and_both_actor_branches(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        from script_core.schemas import decoder_schema
        from tests.test_generalized_entity_memory import report
        for text,fields,values in [
                ('The server is unreachable.',['states'],dict(states=[dict(entity='server',description='unreachable')])),
                ('The box was moved with a crane.',['actions.instrument'],dict(actions=[dict(actor=None,verb='moved',roles=[dict(role='patient',value='box'),dict(role='instrument',value='crane')])])),
                ('C moves the box.',['actions.base'],dict(actions=[dict(actor='C',verb='moves',roles=[dict(role='patient',value='box')])]))]:
            schema=decoder_schema(core.output_schema(core.EXTRACT,dict(unit=dict(id='U0',text=text),source_type='caption',requested_fields=fields)))
            data={k:False if k=='uncertain' else [] for k in schema['properties']};data.update(values)
            parser=JsonSchemaParser(schema)
            parser.config.force_json_field_order=True
            for character in json.dumps(data,separators=(',',':')):
                self.assertIn(character,parser.get_allowed_characters());parser=parser.add_character(character)
            self.assertTrue(parser.can_end())

    def test_real_decoder_blocks_previous_null_actor_and_phrase_bypasses(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        from script_core.schemas import decoder_schema
        for text,actor,verb,role,value in [
            ('C moves the red book with her left hand.',None,'moves','body_part','left hand'),
            ('The box was moved with a crane.',None,'with a crane','instrument','crane')]:
            schema=decoder_schema(core.output_schema(core.EXTRACT,
                core.extraction_inputs(unit(text=text),['actions.'+role])))
            data=dict(actions=[dict(actor=actor,verb=verb,roles=[dict(role=role,value=value)])],uncertain=False)
            parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
            blocked=False
            for character in json.dumps(data,separators=(',',':')):
                if character not in parser.get_allowed_characters():
                    blocked=True;break
                parser=parser.add_character(character)
            self.assertTrue(blocked,text)


if __name__=='__main__':unittest.main()
