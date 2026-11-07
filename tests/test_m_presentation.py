import unittest
from copy import deepcopy
import json
from script_core import m_scene_memory as core
from script import controlled_memory_io as io


def pool_fixture():
    records=[]
    for i,(text,status,times) in enumerate([
            ('C walks dog.', 'source assertion', [[4,5],[5,6],[15,16],[16,17]]),
            ('C holds leash.', 'source assertion', [[6,7]]),
            ('C walks dog.', 'text-inferred hypothesis', [[17,18]])]):
        records.append(dict(id=f'R{i}',text=text,qualification=status,kinds=['probe'],
            sources=[dict(region='probe')],occurrences=[dict(interval=t) for t in times]))
    return dict(records=records,pool_sha256=core.parent.digest(records))


class MPresentationTests(unittest.TestCase):
    def test_temporal_attribute_schema_from_failed_case_is_verbalized_and_audited(self):
        payload=dict(observations=[dict(caption_id=f'C{t}',frame_index=t,sample_interval=[t,t+1],
            evidence=dict(original_text='#C C kneads dough.')) for t in (72,73,74)])
        memory=core.merged.materialize(payload)
        attribute=dict(field='material',value='dough',source_caption_ids=['C72','C73','C74'],observed_at=[72,73,74],
            source_status='caption_asserted',scene_id='S1',batch_id='adaptive:E0')
        temporal=dict(entities=[dict(id='E0',label='C',attributes=[attribute])],states=[],relations=[])
        original=dict(head='Source assertions',prompts=['  Description: '+core.merged.render(memory)[0]+
            '\n\n###\n\n Questions: What material?\nTemporal entity memory (caption):\n'+json.dumps(temporal)])
        with self.assertRaisesRegex(ValueError,'explicit verbalization'):
            core.parent.build_pool(memory,original,io.parts)
        pool=core.build_pool(memory,original,io.parts)
        text,audit=core.render(pool,core.parent.keep_all(pool))
        self.assertIn('records attribute "material" with value "dough" for C (text reference E0)',text)
        self.assertIn('Frames [72,75) (text-inferred hypothesis)',text)
        record=next(r for r in pool['records'] if 'temporal attribute' in r['kinds'])
        self.assertEqual({k:v for k,v in record['sources'][0]['record'].items() if k!='entity_id'},attribute)
        self.assertEqual(record['sources'][0]['record']['source_caption_ids'],['C72','C73','C74'])
        self.assertEqual(record['occurrences'],[dict(caption_id=f'C{t}',interval=[t,t+1]) for t in (72,73,74)])
        changed=deepcopy(original)
        changed['prompts'][0]=changed['prompts'][0].replace('"C72", "C73", "C74"','"C999"')
        with self.assertRaisesRegex(ValueError,'unread or missing'):
            core.build_pool(memory,changed,io.parts)

    def test_adjacent_ranges_coalesce_but_gaps_statements_and_qualifications_stay_separate(self):
        pool=pool_fixture()
        text,audit=core.render(pool,core.parent.keep_all(pool))
        self.assertIn('Frames [4,6), [15,17): C walks dog.',text)
        self.assertIn('Frames [6,7): C holds leash.',text)
        self.assertIn('Frames [17,18) (text-inferred hypothesis): C walks dog.',text)
        self.assertNotIn('(source assertion)',text)
        self.assertEqual(audit['display_qualifications']['R0'],'source assertion')
        self.assertNotIn('At sampled intervals',text)
        self.assertNotIn('[4,17)',text)
        display=audit['groups'][0]['interval_display'][0]
        self.assertEqual(display['original_sample_intervals'],[[4,5],[5,6],[15,16],[16,17]])
        self.assertEqual(display['display_intervals'],[[4,6],[15,17]])
        self.assertEqual(audit['presentation_version'],core.PRESENTATION_VERSION)
        self.assertFalse(audit['inferred_continuity'])

    def test_parent_l_keeps_original_prefix_and_identical_source_audit(self):
        pool=pool_fixture();plan=dict(keep=['R2'],skip=[],uncertain=[],merge_groups=[['R0','R1']])
        old,old_audit=core.parent.render(pool,plan)
        new,new_audit=core.render(pool,plan)
        expected=old.replace('At sampled intervals ','Frames ').replace(' (source assertion), ',': ')
        expected=expected.replace(' (text-inferred hypothesis), ',' (text-inferred hypothesis): ')
        self.assertEqual(new,expected)
        self.assertEqual({k:v for k,v in new_audit.items()
                          if k not in ('presentation_version','interval_label','display_qualifications')},old_audit)


if __name__=='__main__': unittest.main()
