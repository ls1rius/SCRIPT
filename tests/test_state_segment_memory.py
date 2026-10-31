from copy import deepcopy
import unittest

from script_core.state_segment_memory import materialize, render, restore, validate


def row(time, value='closed', evidence=None):
    return dict(caption_id=f'C{time}',frame_index=time,sample_interval=[time,time+1],
        evidence=evidence if evidence is not None else dict(states=[dict(entity='door',dimension='open_closed',value=value)]))


class StateSegmentTests(unittest.TestCase):
    def test_repeated_state_is_one_stored_segment_with_all_original_sources(self):
        source=dict(observations=[row(9),row(11),row(13)])
        before=deepcopy(source)
        memory=materialize(source)
        text,audit=render(memory)
        self.assertEqual(len(memory['records']),1)
        self.assertEqual(len(memory['segments']),1)
        self.assertEqual(memory['segments'][0]['interval'],[9,14])
        self.assertEqual(text,'Frames [9,14): State: entity="door"; dimension="open_closed"; value="closed"')
        self.assertEqual(audit['rows'][0]['sample_intervals'],[[9,10],[11,12],[13,14]])
        self.assertEqual(audit['inferred_persistent_segments'],1)
        self.assertEqual(restore(memory),before)
        self.assertEqual(source,before)

    def test_change_and_return_to_previous_value_start_new_segments(self):
        memory=materialize(dict(observations=[row(9),row(10),row(11,'open'),row(12)]))
        self.assertEqual([s['interval'] for s in memory['segments']],[[9,11],[11,12],[12,13]])
        self.assertEqual(len(memory['records']),2)

    def test_changes_in_larger_state_bundle_close_old_track(self):
        changed=dict(states=[dict(entity='door',dimension='open_closed',value='open')],
                     attributes=[dict(entity='door',property='color',value='white')])
        memory=materialize(dict(observations=[row(9),row(10,evidence=changed),row(11)]))
        self.assertEqual(len(memory['segments']),3)

    def test_long_unread_gap_never_becomes_one_persistent_interval(self):
        memory=materialize(dict(observations=[row(9),row(100)]))
        self.assertEqual(len(memory['segments']),2)

    def test_action_and_original_samples_do_not_acquire_continuous_duration(self):
        for evidence in [dict(actions=[dict(actor='C',verb='picks',patient='bar')]),
                         dict(original_text='#C C never opens the door.\n ')]:
            source=dict(observations=[row(9,evidence=evidence),row(11,evidence=evidence)])
            memory=materialize(source)
            text,audit=render(memory)
            self.assertTrue(text.startswith('Frames [9,10), [11,12): '))
            self.assertEqual(audit['inferred_persistent_segments'],0)
            self.assertEqual(restore(memory),source)

    def test_intervening_action_breaks_a_state_track(self):
        action=dict(actions=[dict(actor='C',verb='opens',patient='door')])
        memory=materialize(dict(observations=[row(9),row(10,evidence=action),row(11)]))
        self.assertEqual(len(memory['segments']),3)

    def test_support_coverage_detects_dropped_and_reused_samples(self):
        memory=materialize(dict(observations=[row(9),row(11)]))
        memory['segments'].append(deepcopy(memory['segments'][0]))
        with self.assertRaisesRegex(ValueError,'lost or duplicated'):
            validate(memory)

    def test_state_inside_different_action_clauses_is_stored_once_and_tracked(self):
        source=dict(observations=[row(t,evidence=dict(
            states=[dict(entity='door',dimension='open_closed',value='closed')],
            actions=[dict(actor='C',verb='touches',patient='door')],
            categorized_original=dict(original_text=f'C touches the closed door at sample {t}',
                                      information_types=['action','state']))) for t in [9,11,13]])
        memory=materialize(source)
        self.assertEqual(len(memory['facts']),2)
        self.assertEqual(len(memory['records']),3)
        self.assertEqual(len(memory['state_segments']),1)
        self.assertEqual(memory['state_segments'][0]['interval'],[9,14])
        text,audit=render(memory)
        self.assertIn('Frames [9,14): State:',text)
        for t in [9,11,13]:
            self.assertIn(f'at sample {t}',text)
        self.assertEqual(restore(memory),source)

    def test_relation_changes_break_persistence_before_old_value_returns(self):
        source=dict(observations=[row(t,evidence=dict(relations=[dict(
            left='cup',predicate='on',right=place,relation_type='spatial')]))
            for t,place in [(9,'table'),(10,'floor'),(11,'table')]])
        memory=materialize(source)
        self.assertEqual([s['interval'] for s in memory['state_segments']],[[9,10],[10,11],[11,12]])


if __name__=='__main__':
    unittest.main()
