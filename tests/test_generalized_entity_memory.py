from copy import deepcopy
import unittest

from script_core import generalized_entity_memory as core
from script_core import joint_entity_memory as parent
from script_core.schemas import decoder_schema


def report(**values):
    result = {category: [] for category in core.COLLECTIONS.values()}
    result.update(uncertain=False)
    result.update(values)
    return result


def source(text, uid='U0'):
    return dict(id=uid, text=text)


def accepted(text, uid='U0', **values):
    unit = source(text, uid)
    value = core.accept(unit, report(**values))
    return dict(accepted=value, complete_simple=core.complete_simple_source(unit, value))


def observations(samples):
    rows, values = [], {}
    for i, (time, entity, description) in enumerate(samples):
        uid = f'U{i}'
        text = f'The {entity} is {description}.'
        values[uid] = accepted(text, uid, states=[dict(entity=entity, description=description)])
        rows.append(dict(caption_id=f'C{i}', frame_index=time, source_id=uid,
            sample_interval=[time, time+1], original_text=text))
    return rows, values


class GeneralizedMemoryTests(unittest.TestCase):
    def test_real_constrained_decoder_accepts_null_actor_and_open_state(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        for text, values in [
                ('The server is unreachable.', dict(states=[dict(entity='server', description='unreachable')])),
                ('The box was moved with a crane.', dict(actions=[dict(actor=None, verb='moved', roles=[dict(role='patient', value='box'),dict(role='instrument',value='crane')])])),
                ('C moves the box.', dict(actions=[dict(actor='C',verb='moves',roles=[dict(role='patient',value='box')])]))]:
            schema=decoder_schema(core.output_schema(dict(unit=source(text))))
            data={name:False if name=='uncertain' else [] for name in schema['properties']}
            data.update(values)
            parser=JsonSchemaParser(schema)
            for character in json.dumps(data,separators=(',',':')):
                self.assertIn(character,parser.get_allowed_characters())
                parser=parser.add_character(character)
            self.assertTrue(parser.can_end())

    def test_state_schema_is_source_bound_description_without_fixed_dimensions(self):
        schema = core.output_schema(dict(unit=source('The server is unreachable.')))
        fields = schema['properties']['states']['items']
        self.assertEqual(set(fields['properties']), {'entity', 'description'})
        self.assertEqual(set(fields['required']), {'entity', 'description'})
        self.assertIn('unreachable', fields['properties']['description']['enum'])
        self.assertNotIn('wet', fields['properties']['description']['enum'])
        self.assertEqual(schema['properties']['attributes']['items']['properties']['property']['enum'], list(parent.PROPERTIES))

    def test_states_outside_old_dimensions_are_preserved(self):
        for entity, description in [('server', 'unreachable'), ('device', 'ready for pairing'),
                ('liquid', 'viscous'), ('application', 'awaiting review')]:
            with self.subTest(description=description):
                value = accepted(f'The {entity} is {description}.', states=[dict(entity=entity, description=description)])
                self.assertEqual(value['accepted']['facts'], [dict(kind='state', source_id='U0', entity=entity, description=description)])
                self.assertTrue(value['complete_simple'])

    def test_foreign_state_text_and_legacy_dimension_records_are_rejected(self):
        with self.assertRaises(ValueError):
            accepted('The server is unreachable.', states=[dict(entity='server', description='connected')])
        with self.assertRaises(ValueError):
            accepted('The cloth is wet.', states=[dict(entity='cloth', dimension='wetness', value='wet')])

    def test_cross_clause_entity_misbinding_is_rejected(self):
        value = accepted('The cloth is wet and the book is dry.', states=[dict(entity='cloth', description='dry')])
        self.assertFalse(value['accepted']['facts'])
        self.assertTrue(value['accepted']['rejected'])
        self.assertFalse(value['complete_simple'])

    def test_negative_and_uncertain_sources_remain_literal(self):
        for text in ['The server is not reachable.', 'The server seems unreachable.']:
            value = accepted(text, states=[dict(entity='server', description='reachable' if 'not' in text else 'unreachable')])
            self.assertTrue(value['accepted']['uncertain'])
            self.assertFalse(value['accepted']['facts'])
            self.assertFalse(value['complete_simple'])

    def test_complex_state_clause_keeps_unrepresented_qualifiers(self):
        value = accepted('The cloth is wet near the table.', states=[dict(entity='cloth', description='wet')])
        self.assertFalse(value['complete_simple'])
        rows=[dict(caption_id='C0', frame_index=0, source_id='U0', sample_interval=[0,1], original_text='The cloth is wet near the table.')]
        memory, _ = core.materialize(rows, {'U0': value})
        text, _ = core.render(memory)
        self.assertIn(rows[0]['original_text'], text)

    def test_known_action_roles_and_property_acceptance_match_parent(self):
        unit = source('C wipes the red book with a cloth.')
        data = report(actions=[dict(actor='C', verb='wipes', roles=[dict(role='patient',value='red book'),dict(role='instrument',value='cloth')])],
            attributes=[dict(entity='book', property='color', value='red')])
        self.assertEqual(core.accept(unit, data), parent.accept(unit, data))
        value = core.accept(unit, data)
        self.assertEqual(core.complete_simple_source(unit, value), parent.complete_simple_source(unit, value))

    def test_unknown_actor_stays_unknown_without_dropping_source(self):
        value = accepted('The box was moved with a crane.', actions=[dict(actor=None, verb='moved',
            roles=[dict(role='patient', value='box'), dict(role='instrument', value='crane')])])
        self.assertNotIn('actor', value['accepted']['facts'][0])
        self.assertFalse(value['complete_simple'])
        rows=[dict(caption_id='C0',frame_index=0,source_id='U0',sample_interval=[0,1],original_text='The box was moved with a crane.')]
        memory, audit = core.materialize(rows, {'U0': value})
        text, _ = core.render(memory)
        self.assertEqual(audit['actorless_action_facts'], 1)
        self.assertIn('actor=unknown', text)
        self.assertIn(rows[0]['original_text'], text)

    def test_actorless_body_part_cannot_become_tool(self):
        value = accepted('The box was moved with a hand.', actions=[dict(actor=None, verb='moved',
            roles=[dict(role='patient', value='box'), dict(role='instrument', value='hand')])])
        self.assertFalse(value['accepted']['facts'])
        self.assertEqual(value['accepted']['rejected'][0]['reason'], 'body_part_is_not_instrument')

    def test_explicit_active_actor_cannot_be_silently_omitted(self):
        for text,verb,roles in [
                ('C draws on paper with a pen.','draws',[dict(role='surface',value='paper'),dict(role='instrument',value='pen')]),
                ('The machine produces a basket.','produces',[dict(role='patient',value='basket')])]:
            value=accepted(text,actions=[dict(actor=None,verb=verb,roles=roles)])
            self.assertFalse(value['accepted']['facts'])
            self.assertEqual(value['accepted']['rejected'][0]['reason'],'explicit_subject_of_active_action_must_not_be_omitted')

    def test_repeated_state_retains_samples_and_marks_continuity_as_inferred(self):
        rows, data = observations([(0, 'server', 'unreachable'), (2, 'server', 'unreachable')])
        memory, audit = core.materialize(rows, data)
        self.assertEqual(audit['merged_state_segments'], 1)
        self.assertEqual(memory['state_segments'][0]['observation_indices'], [0,1])
        self.assertEqual(memory['state_segments'][0]['continuity'], 'inferred')
        self.assertEqual([r['sample_interval'] for r in memory['observations']], [[0,1],[2,3]])
        self.assertEqual(set(memory['facts'][0]['fields']), {'entity','description'})
        text, view = core.render(memory)
        self.assertIn('State: unreachable', text)
        self.assertTrue(view['all_fact_observations_rendered'])

    def test_changed_state_and_return_are_separate_segments(self):
        rows, data = observations([(0, 'server', 'unreachable'), (1, 'server', 'reachable'), (2, 'server', 'unreachable')])
        memory, _ = core.materialize(rows, data)
        self.assertEqual([s['interval'] for s in memory['state_segments']], [[0,1],[1,2],[2,3]])

    def test_long_unobserved_gap_is_not_merged(self):
        rows, data = observations([(0, 'server', 'unreachable'), (5, 'server', 'unreachable')])
        memory, audit = core.materialize(rows, data)
        self.assertEqual(audit['merged_state_segments'], 0)
        self.assertEqual(len(memory['state_segments']), 2)

    def test_unsupported_source_and_provenance_survive(self):
        rows, data = observations([(0, 'server', 'unreachable')])
        data['U0']['accepted']['facts'] = []
        data['U0']['complete_simple'] = False
        memory, _ = core.materialize(rows, data)
        text, _ = core.render(memory)
        self.assertIn(rows[0]['original_text'], text)
        changed = deepcopy(rows)
        changed[0]['sample_interval'] = [1,2]
        with self.assertRaisesRegex(ValueError, 'provenance'):
            core.validate_memory(memory, changed)


if __name__ == '__main__':
    unittest.main()
