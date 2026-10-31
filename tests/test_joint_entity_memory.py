from copy import deepcopy
import unittest

from script_core.joint_entity_memory import (accept,complete_simple_source,materialize,render,
    Resolver,COLLECTIONS,ROLES,validate_memory)


def report(**values):
    result={category:[] for category in COLLECTIONS.values()}
    values.pop('mentions',None)
    result.update(uncertain=False);result.update(values)
    for index,action in enumerate(result['actions']):
        result['actions'][index]=dict(actor=action['actor'],verb=action['verb'],roles=[
            dict(role=role,value=action[role]) for role in ROLES if action.get(role) is not None])
    return result


def source(text,uid='U0'):
    return dict(id=uid,text=text)


def accepted(text,uid='U0',**values):
    unit=source(text,uid);a=accept(unit,report(**values))
    return dict(accepted=a,complete_simple=complete_simple_source(unit,a))


class JointEntityTests(unittest.TestCase):
    def test_literal_constraints_block_unstated_tools_and_preposition_attributes(self):
        with self.assertRaises(ValueError):
            accepted('C draws on paper with a pen.',actions=[dict(actor='C',verb='draws',instrument='ink')])
        with self.assertRaises(ValueError):
            accepted('The book is on the table.',attributes=[dict(entity='book',property='material',value='on')])

    def test_multiple_explicit_actors_bind_to_their_own_clauses(self):
        value=accepted('C folds the book then C moves the book.',actions=[
            dict(actor='C',verb='folds',patient='book'),dict(actor='C',verb='moves',patient='book')])
        self.assertEqual(len(value['accepted']['facts']),2)
        self.assertFalse(value['complete_simple'])

    def test_action_subject_is_not_promoted_to_a_surface_relation(self):
        value=accepted('C draws on paper with a pen.',relations=[dict(left='C',predicate='on',right='paper',relation_type='spatial')])
        self.assertFalse(value['accepted']['facts'])

    def test_action_word_is_not_an_object_attribute(self):
        value=accepted('C picks a book.',actions=[dict(actor='C',verb='picks',patient='book')],
            attributes=[dict(entity='book',property='material',value='picks')])
        self.assertEqual([f['kind'] for f in value['accepted']['facts']],['action'])
        self.assertFalse(value['complete_simple'])

    def test_joint_action_attribute_and_complete_clause_preserve_roles(self):
        data=accepted('#C C wipes the red book with a cloth.',
            actions=[dict(actor='C',verb='wipes',patient='red book',instrument='cloth')],
            attributes=[dict(entity='book',property='color',value='red')])
        self.assertEqual([f['kind'] for f in data['accepted']['facts']],['action','attribute'])
        self.assertTrue(data['complete_simple'])
        self.assertEqual(data['accepted']['facts'][0]['patient'],'red book')

    def test_negative_clause_and_omitted_modifier_keep_original(self):
        negative=accepted('C never wipes the book.',actions=[dict(actor='C',verb='wipes',patient='book')])
        self.assertFalse(negative['accepted']['facts']);self.assertFalse(negative['complete_simple'])
        modifier=accepted('C wipes the book slowly.',actions=[dict(actor='C',verb='wipes',patient='book')])
        self.assertFalse(modifier['complete_simple'])

    def test_origin_destination_and_body_part_survive_joint_extraction(self):
        data=accepted('C moves the book from the shelf into the bag with her left hand.',
            actions=[dict(actor='C',verb='moves',patient='book',source_location='shelf',destination='bag',body_part='left hand')])
        self.assertTrue(data['complete_simple'])
        self.assertEqual(data['accepted']['facts'][0]['source_location'],'shelf')

    def test_body_part_cannot_be_accepted_as_instrument(self):
        data=accepted('C moves the book with her left hand.',
            actions=[dict(actor='C',verb='moves',patient='book',instrument='left hand')])
        self.assertFalse(data['accepted']['facts'])
        self.assertEqual(data['accepted']['rejected'][0]['reason'],'body_part_is_not_instrument')

    def test_local_aliases_share_id_but_conflicting_objects_do_not(self):
        r=Resolver()
        red=r.resolve('red book',0,dict(head='book',entity_type='object'))
        generic=r.resolve('book',1,dict(head='book',entity_type='object'))
        blue=r.resolve('blue book',1,dict(head='book',entity_type='object'))
        self.assertEqual(red,generic);self.assertNotEqual(red,blue)
        ambiguous=r.resolve('it',2,dict(head='it',entity_type='object'))
        self.assertNotIn(ambiguous,[red,blue])
        self.assertEqual(r.decisions[-1]['basis'],'ambiguous_new_entity')

    def test_unread_gaps_split_object_identity(self):
        r=Resolver()
        first=r.resolve('book',0,dict(head='book',entity_type='object'))
        later=r.resolve('book',30,dict(head='book',entity_type='object'))
        self.assertNotEqual(first,later)
        c=r.resolve('C',0);self.assertEqual(c,r.resolve('C',30))

    def test_entity_timeline_merges_location_and_keeps_actor_and_action(self):
        rows=[];data={}
        for time,verb,noun in [(9,'places','red book'),(11,'moves','book')]:
            uid=f'U{time}';text=f'C {verb} the {noun} on the table.'
            data[uid]=accepted(text,uid,actions=[dict(actor='C',verb=verb,patient=noun,surface='table')],
                relations=[dict(left=noun,predicate='on',right='table',relation_type='spatial')],
                mentions=[dict(label=noun,head='book',entity_type='object')])
            rows.append(dict(caption_id=f'C{time}',frame_index=time,sample_interval=[time,time+1],source_id=uid,original_text=text))
        memory,audit=materialize(rows,data)
        self.assertEqual(audit['merged_state_segments'],1)
        self.assertEqual(memory['state_segments'][0]['interval'],[9,12])
        self.assertEqual(audit['original_clauses_replaced'],2)
        text,view=render(memory)
        self.assertIn('C)',text);self.assertIn('places',text);self.assertIn('moves',text);self.assertIn('[9,12)',text)
        self.assertTrue(view['all_fact_observations_rendered'])
        self.assertEqual(len(memory['action_observations']),2)

    def test_changed_state_and_return_have_separate_segments(self):
        rows=[];data={}
        for t,value in [(9,'wet'),(10,'dry'),(11,'wet')]:
            uid=f'U{t}';text=f'The cloth is {value}.'
            data[uid]=accepted(text,uid,states=[dict(entity='cloth',dimension='wetness',value=value)])
            rows.append(dict(caption_id=f'C{t}',frame_index=t,sample_interval=[t,t+1],source_id=uid,original_text=text))
        memory,_=materialize(rows,data)
        self.assertEqual([s['interval'] for s in memory['state_segments']],[[9,10],[10,11],[11,12]])

    def test_unknown_sources_remain_visible_and_provenance_is_checked(self):
        data={'U0':accepted('C might be looking for a tool.')}
        rows=[dict(caption_id='C0',frame_index=0,sample_interval=[0,1],source_id='U0',original_text='C might be looking for a tool.')]
        memory,_=materialize(rows,data);text,_=render(memory)
        self.assertIn(rows[0]['original_text'],text)
        changed=deepcopy(rows);changed[0]['sample_interval']=[1,2]
        with self.assertRaisesRegex(ValueError,'provenance'):
            validate_memory(memory,changed)


if __name__=='__main__':unittest.main()
