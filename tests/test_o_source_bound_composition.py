import json
from pathlib import Path
import unittest

from script_core import o_sentence_filter as core
from script_core.o_generation import decoder_contract


def action(group='a', **fields):
    return [dict(id=f'U{i}',kind='action_field',group=group,field=k,value=v)
            for i,(k,v) in enumerate(fields.items())]


class SourceBoundTests(unittest.TestCase):
    def test_question_and_options_cannot_add_facts(self):
        chosen=action(actor='C',verb='walks',location='around')
        req=core.compose_input(dict(question='How does C coordinate arms for balance?',
            options=['C packs books in the garage.']),chosen,include_options=True)
        canonical,wire,inputs,aliases=decoder_contract(core.COMPOSE,req)
        self.assertEqual(set(aliases['a'].values()),{
            'actor: C; verb: walks; location: around','C walks [location: around]'})
        for bad in ['C walks around to balance.', 'C packs books in the garage.',
                    'C walks using the right hand and right leg.']:
            with self.assertRaises(ValueError):
                core.validate_output(dict(groups=dict(a=dict(text=bad))),canonical)
            with self.assertRaises(ValueError):
                core.validate_output(dict(groups=dict(a=dict(text=bad))),wire)

    def test_roles_negation_and_uncertainty_are_never_rewritten(self):
        units=action(actor='D',verb='may not give',patient='cup',recipient='E',location='room')
        for text in core.compose_renderings(units)[1:]:
            for value in ['D','may not give','cup','E','room']:
                self.assertIn(value,text)
            self.assertIn('recipient: E',text)
            self.assertIn('location: room',text)
        for kind,record in [('changes',dict(entity='door',before='closed',after='possibly open')),
                            ('event_links',dict(left_event='does not open',connector='before',right_event='reads',link_type='before'))]:
            unit=dict(id='U0',group='g',kind=kind,record=record)
            self.assertEqual(json.loads(core.compose_renderings([unit])[1]),record)

    def test_group_cannot_borrow_participants_or_roles(self):
        units=action('a',actor='D',verb='gives',patient='cup')+action('b',actor='E',verb='reads',patient='book')
        req=core.compose_input(dict(question='What happens?'),units)
        with self.assertRaises(ValueError):
            core.validate_output(dict(groups=dict(a=dict(text='D gives book'),b=dict(text='E reads cup'))),core.compose_schema(req))
        with self.assertRaises(ValueError):core.compose_renderings(units)

    def test_final_reader_boundary_rejects_tampered_joined_text(self):
        units=action(actor='C',verb='puts',patient='wood',destination='machine')
        source=dict(id='S0',source_intervals=[[1,2]])
        group=dict(a=dict(text=core.compose_renderings(units)[-1]))
        item=dict(groups=group,text=group['a']['text'])
        self.assertEqual(core.present([source],{'S0':dict(selected=units)},composed={'S0':item})[0]['text'],item['text'])
        item['text']+=' to improve efficiency in the garage'
        with self.assertRaisesRegex(ValueError,'differs'):
            core.present([source],{'S0':dict(selected=units)},composed={'S0':item})

    def test_raw_fallback_is_exact_and_unicode_choices_use_ascii(self):
        raw='张三说“不放进去”，而不是搬进车库。'
        unit=dict(id='U0',group='S0',kind='raw_sentence',text=raw)
        req=core.compose_input(dict(question='Why?'),[unit])
        canonical,wire,inputs,aliases=decoder_contract(core.COMPOSE,req)
        self.assertEqual(list(aliases['S0'].values()),[raw])
        self.assertTrue(all(code.isascii() for code in aliases['S0']))

    def test_saved_demonstrated_hallucinations_are_rejected(self):
        root=Path('workdirs/O-Oplus-N-question-only-300-20261103-r1/runs')
        cases=[('7dcf85fb-1217-4eb7-b9c8-cc2ab06507fe','S18'),
               ('5d15c716-f179-462c-84a6-fa94e9d14e94','S80')]
        for uid,sid in cases:
            folder=root/uid/'question_only/sentences'/sid
            req=json.loads((folder/'composition_input.json').read_text())
            old=json.loads((folder/'composition.json').read_text())
            with self.assertRaises(ValueError):
                core.validate_output(dict(groups=old['groups']),core.compose_schema(req))


if __name__=='__main__':unittest.main()
