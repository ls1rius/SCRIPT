from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from script_core import lazy_script_memory as core
from script_core.qa_evidence_view import compact_description
from script.run_lazy_script_experiment import extract_source


def unit(name='U0',text='#C C moves the red book with her left hand.',start=9):
    return dict(id=name,text=text,source_caption_ids=[name+'C'],source_intervals=[[start,start+1]])


def row(u):
    return dict(caption_id=u['source_caption_ids'][0],source_id=u['id'],sample_interval=u['source_intervals'][0],frame_index=u['source_intervals'][0][0],original_text=u['text'])


def accepted(facts=(),uncertain=False,rejected=(),error=None):
    return dict(accepted=dict(facts=list(facts),uncertain=uncertain,rejected=list(rejected)),error=error,model_called=True)


class LazyMemoryTests(unittest.TestCase):
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
                ('states.wetness',accepted(error={'type':'bad'}),'failed')]:
            self.db.ensure('v',[self.u],[self.r],[field],lambda *_,v=value:v)
            self.assertEqual(self.db.get('v',key,field)['status'],expected)
            self.db.ensure('v',[self.u],[self.r],[field],lambda *_:self.fail('automatic retry'))
        self.assertIsNone(self.db.get('v',key,'states.integrity'))

    def test_extractor_version_and_video_namespace_invalidate_cache(self):
        self.db.ensure('v',[self.u],[self.r],['actions.base'],lambda *_:accepted([self.base]))
        other=core.ScriptDB(self.path,'new-extractor')
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
        schema=core.output_schema(core.EXTRACT,dict(unit=dict(text=self.u['text']),requested_fields=['actions.body_part']))
        props=schema['properties'];self.assertEqual(props['states']['maxItems'],0)
        self.assertEqual(props['attributes']['maxItems'],0)
        self.assertEqual(props['actions']['items']['properties']['roles']['items']['properties']['role']['enum'],['patient','body_part'])
        schema=core.output_schema(core.EXTRACT,dict(unit=dict(text='The book is wet'),requested_fields=['states.wetness']))
        self.assertEqual(schema['properties']['actions']['maxItems'],0)
        self.assertEqual(schema['properties']['states']['items']['properties']['dimension']['enum'],['wetness'])

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
        for fields in [['changes'],['event_links.purpose'],['states.wetness']]:
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

    def test_old_version_cache_cannot_be_reused_for_new_planning_or_projection(self):
        key=self.db.register('v',[self.u],[self.r])['U0']
        with core.ScriptDB(self.path,'lazy-script-memory-v1') as old:
            old.put('v',key,'actions.base','checked_absent',[])
        self.assertIsNone(self.db.get('v',key,'actions.base'))

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


if __name__=='__main__':unittest.main()
