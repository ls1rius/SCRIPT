"""Saved-output replay and synthetic decoder checks; no inference."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from script_core import n_source_extraction as core, generalized_lazy_memory as h
from script_core import schemas, backend, prompts, context
from script_core.batch_extraction import render_request
from script.generalized_lazy_execution import SourceExtractor, extraction_counts
from script.run_generalized_lazy_experiment import extract_source


def report(actor='C', verb='moves', **roles):
    keys=['patient','body_part','destination','instrument','location','manner','material','recipient','source_location','surface']
    return dict(actions=[dict(actor=actor, verb=verb, roles={k:roles.get(k) for k in keys})],
                attributes=[], states=[], relations=[], changes=[], results=[], event_links=[], uncertain=False)


class NSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved=json.loads((Path(__file__).parent/'fixtures/n_brick_wall_saved_call.json').read_text())

    def test_frozen_output_replay_locates_role_rejections_not_missing_request(self):
        inputs=self.saved['inputs']
        self.assertTrue({'actions.destination','actions.location','actions.surface'} <= set(inputs['requested_fields']))
        data=schemas.restore_empty_arrays(self.saved['output']['data'], h.output_schema(h.EXTRACT,inputs))
        self.assertEqual(h.accept(inputs['unit'],data),self.saved['frozen_accepted'])
        with self.assertRaises(ValueError):
            schemas.validate_output(data,core.output_schema(core.EXTRACT,inputs))

    def test_supported_surface_and_location_survive_acceptance_and_storage(self):
        inputs=self.saved['inputs'];u=inputs['unit']
        for role in ('surface','location'):
            data=report(patient='brick',body_part='left hand',**{role:'brick wall'})
            schemas.validate_output(data,core.output_schema(core.EXTRACT,inputs))
            accepted=core.accept(u,data)
            self.assertEqual(accepted['rejected'],[])
            self.assertEqual(accepted['facts'][0][role],'brick wall')
            with tempfile.TemporaryDirectory() as td, core.ScriptDB(Path(td)/'m.sqlite3',core.VERSION) as db:
                row=dict(caption_id='C107',frame_index=107,sample_interval=[107,108],source_id=u['id'],original_text=u['text'])
                result,_=db.ensure('v',[u],[row],inputs['requested_fields'],
                    lambda *_:dict(accepted=accepted,error=None,model_called=True))
                self.assertEqual(result[u['id']]['accepted']['facts'][0][role],'brick wall')

    def test_real_decoder_disallows_bad_pairs_but_accepts_supported_pair(self):
        from lmformatenforcer import JsonSchemaParser
        inputs=self.saved['inputs'];schema=schemas.decoder_schema(core.output_schema(core.EXTRACT,inputs))
        def decodes(data):
            parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
            # Match schemas.decoder_schema: omit categories constrained to empty.
            data={k:v for k,v in data.items() if k in schema['properties']}
            for char in json.dumps(data):
                if char not in parser.get_allowed_characters():return False
                parser=parser.add_character(char)
            return parser.can_end()
        self.assertTrue(decodes(report(patient='brick',surface='brick wall',body_part='left hand')))
        self.assertFalse(decodes(report(patient='brick',destination='brick wall')))
        self.assertFalse(decodes(report(patient='brick',instrument='left hand')))

    def test_destination_remains_valid_for_to_into_onto_and_unknown_actor_is_not_invented(self):
        for connector in ('to','into','onto'):
            u=dict(id='U',text=f'C moves a brick {connector} the box with his left hand.')
            data=report(patient='brick',destination='box',body_part='left hand')
            self.assertEqual(core.accept(u,data)['rejected'],[])
        u=dict(id='U',text='The box was moved with a crane.')
        data=report(actor=None,verb='moved',patient='box',instrument='crane')
        accepted=core.accept(u,data)
        self.assertEqual(accepted['rejected'],[])
        self.assertNotIn('actor',accepted['facts'][0])

    def test_requested_fields_source_boundary_and_parent_unchanged(self):
        inputs=deepcopy(self.saved['inputs']);before=h.output_schema(h.EXTRACT,inputs)
        core.output_schema(core.EXTRACT,inputs)
        self.assertEqual(before,h.output_schema(h.EXTRACT,inputs))
        inputs['requested_fields']=['actions.base']
        with self.assertRaises(ValueError):
            schemas.validate_output(report(patient='brick',surface='brick wall'),core.output_schema(core.EXTRACT,inputs))
        with self.assertRaises(ValueError):
            schemas.validate_output(report(patient='brick',surface='table'),core.output_schema(core.EXTRACT,self.saved['inputs']))

    def test_requested_roles_cannot_be_silently_omitted_but_null_is_allowed(self):
        schema=core.output_schema(core.EXTRACT,self.saved['inputs'])
        data=report(patient='brick')
        schemas.validate_output(data,schema)
        accepted=core.accept(self.saved['inputs']['unit'],data)
        self.assertNotIn('surface',accepted['facts'][0])
        del data['actions'][0]['roles']['body_part']
        with self.assertRaises(ValueError):schemas.validate_output(data,schema)

    def test_batch_rendering_and_serial_dispatch_use_identical_schema(self):
        class Processor:
            def apply_chat_template(self,messages,**_):return json.dumps(messages)
        runtime=SimpleNamespace(memory=core,schemas=schemas,context=context,prompts=prompts)
        rendered=render_request(self.saved['inputs'],runtime,Processor())
        self.assertEqual(rendered['schema'], schemas.output_schema(core.EXTRACT,self.saved['inputs']))
        self.assertIn('on/in/at',rendered['prompt'])
        self.assertEqual(prompts.CONTRACTS[core.EXTRACT],core.EXTRACT_PROMPT)

    def test_independent_batch_requests_route_to_m_and_costs_are_counted(self):
        data=report(patient='brick',surface='brick wall',body_part='left hand')
        class Meter:
            ledger=SimpleNamespace(calls=[])
            def call(self,op,inputs):
                self.operation=op;self.inputs=inputs
                return dict(items=[dict(data=data) for _ in inputs['requests']]),None
        meter=Meter()
        extractor=SourceExtractor(meter,core,SimpleNamespace(is_infrastructure=lambda _:False),extract_source,2)
        u=self.saved['inputs']['unit'];v=dict(u,id='U36')
        result=extractor.many([(u,self.saved['inputs']['requested_fields']),(v,self.saved['inputs']['requested_fields'])])
        self.assertEqual(meter.operation,'n_extract_batch')
        self.assertTrue(all(r['error'] is None for r in result.values()))
        calls=[dict(operation=meter.operation,parameters=meter.inputs)]
        self.assertEqual(extraction_counts(calls,core.EXTRACT)['field_extraction_source_requests'],2)
        self.assertEqual(extraction_counts(calls,h.EXTRACT)['field_extraction_model_calls'],0)


if __name__=='__main__':unittest.main()
