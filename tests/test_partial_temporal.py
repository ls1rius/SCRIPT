"""Fixed-evidence temporal treatment: sources, atomicity, budgets and one QA."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from script_core import adaptive_transition as partial, partial_temporal as pipeline
from script_core.compatibility import qa_inputs
from script_core.config import digest,load_config


def fixture():
    captions=[dict(id=f'C{i}',interval=[i,i+1],text=t) for i,t in enumerate([
        'Alice holds a closed box.','Alice opens the box.','Alice holds the box.',
        'Alice holds a box.','Alice puts down the box.','A wall is white.'])]
    m=partial.new_memory('v',captions,'source');partial.rebuild_indexes(m)
    for sid,cs in [('S0',captions[:3]),('S1',captions[3:5])]:
        report=dict(entities=[dict(entity_ref='new',label='Alice',source_caption_ids=[c['id'] for c in cs],
            attributes=[],observations=[dict(caption_id=c['id'],kind='state') for c in cs])],relations=[],unresolved='')
        partial.commit_delta(m,report,cs,sid,dict(request_key=sid,fields=['state']),'old-call')
    packet=dict(question=dict(question_id='q',video_id='v',question='What does Alice do?',options=['a','b','c','d','e']),captions=captions)
    query=dict(packet['question'],id='q');qa=qa_inputs(query,captions,list(range(6)))
    import json
    qa['prompts'][-1]+='\n\nTemporal entity memory (original summary/caption assertions; identities are text inferences; not pixel verified):\n'+json.dumps(partial.memory_context(m),ensure_ascii=False,separators=(',',':'))
    bundle=dict(packet_hash=digest(packet),memory=m,opened_scenes=['S0','S1'],selected_seconds=list(range(6)),
        supplemental_visual=dict(observations=[],frame_ids=[]),source_incomplete=[],preparation_seconds=12.,qa_input=qa)
    return packet,bundle


class Backend:
    def __init__(self):self.calls=[]
    def call(self,op,inp,timeout):
        self.calls.append((op,deepcopy(inp)))
        if op=='semantic_memory_extract':
            return dict(data=dict(facts=[],uncertain_source_ids=[u['id'] for u in inp['units']]))
        if op=='partial_temporal_organize':
            return dict(data=dict(decisions=[dict(pair_id=p['id'],continuity='changed',
                source_caption_ids=[p['before']['caption_id'],p['after']['caption_id']],reason='explicit change') for p in inp['pairs']]))
        if op=='scene_correspondence_lineage':
            a=inp['left']['entities'][0];b=inp['right']['entities'][0]
            return dict(data=dict(matches=[dict(left=dict(entity=a['id'],caption_ids=a['caption_ids'][:1]),
                right=dict(entity=b['id'],caption_ids=b['caption_ids'][:1]),relation='same_instance',reason='text inference')]))
        if op=='partial_temporal_identity':
            return dict(data=dict(decisions=[dict(pair_id=p['id'],relation='same_instance',
                left_caption_ids=p['left']['caption_ids'][:1],right_caption_ids=p['right']['caption_ids'][:1],
                reason='text inference') for p in inp['pairs']]))
        if op=='temporal_qa_check':return dict(input_tokens=200)
        if op=='videotree_qa':return dict(data=dict(selected_option='0'))
        raise AssertionError(op)


class PartialTemporalTests(unittest.TestCase):
    def test_projection_retains_all_local_facts_and_does_not_mutate_source(self):
        _,b=fixture();before=deepcopy(b['memory']);m=pipeline.project(before)
        self.assertEqual(before,b['memory'])
        for k in ['entities','relations','source_captions','extraction_requests']:
            self.assertEqual(m[k],before[k])
        self.assertEqual(len(pipeline.observation_pairs(m)),3)
        self.assertEqual(m['cross_matches'],[])

    def test_continuity_decision_is_atomic_and_unobserved_gaps_remain_unknown(self):
        _,b=fixture();m=pipeline.project(b['memory']);pairs=pipeline.observation_pairs(m);p=pairs[0]
        inp=dict(pairs=[p]);report=dict(decisions=[dict(pair_id=p['id'],continuity='changed',
            source_caption_ids=[p['before']['caption_id'],p['after']['caption_id']],reason='change')])
        before=deepcopy(m);bad=deepcopy(report);bad['decisions'][0]['source_caption_ids']=['C0','FOREIGN']
        with self.assertRaises(ValueError):pipeline.commit_decisions(m,inp,bad,'call')
        self.assertEqual(m,before)
        pipeline.commit_decisions(m,inp,report,'call')
        self.assertTrue(m['temporal_network']['transitions'])
        # Construct a valid, later assertion with an unobserved intervening second.
        m=pipeline.project(b['memory']);m['observations'].pop('O1')
        p=pipeline.observation_pairs(m)[0];inp=dict(pairs=[p])
        report['decisions'][0].update(pair_id=p['id'],continuity='same',source_caption_ids=[p['before']['caption_id'],p['after']['caption_id']])
        pipeline.commit_decisions(m,inp,report,'call')
        self.assertEqual(m['temporal_decisions'][0]['applied_continuity'],'uncertain')

    def test_duplicate_decisions_are_rejected_before_mutation(self):
        _,b=fixture();m=pipeline.project(b['memory']);inp=dict(pairs=pipeline.observation_pairs(m)[:2]);p=inp['pairs'][0]
        decision=dict(pair_id=p['id'],continuity='uncertain',source_caption_ids=[p['before']['caption_id'],p['after']['caption_id']],reason='unknown')
        before=deepcopy(m)
        with self.assertRaises(ValueError):pipeline.commit_decisions(m,inp,dict(decisions=[decision,decision]),'call')
        self.assertEqual(m,before)

    def test_run_keeps_qa_evidence_and_generates_only_one_independent_answer(self):
        packet,bundle=fixture();before=deepcopy(bundle);backend=Backend();cfg=load_config('configs/script_temporal_full.yaml')
        with tempfile.TemporaryDirectory() as folder:
            result=pipeline.run(packet,bundle,backend,cfg,Path(folder),dict(online_seconds=1500))
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(bundle,before)
        self.assertEqual(result['cost_summary']['initialization'],12.)
        self.assertGreater(result['identity_comparisons'],0)
        self.assertEqual(result['temporal_pair_count'],3)
        self.assertEqual(result['new_pixel_frame_count'],0)
        self.assertEqual(result['selected_seconds'],bundle['selected_seconds'])
        ops=[op for op,inp in backend.calls]
        self.assertEqual(ops.count('videotree_qa'),1)
        self.assertTrue(set(ops)<={'partial_temporal_organize','partial_temporal_identity','temporal_qa_check','videotree_qa'})
        self.assertTrue(all(not inp.get('frames') and not inp.get('images') for op,inp in backend.calls))
        from script.run_followup_validation import direct_input
        qa=next(inp for op,inp in backend.calls if op=='videotree_qa')
        self.assertEqual(direct_input(qa),direct_input(bundle['qa_input']))

    def test_contaminated_packet_or_unread_source_is_rejected_without_model_calls(self):
        packet,bundle=fixture();backend=Backend();cfg=load_config('configs/script_temporal_full.yaml')
        with tempfile.TemporaryDirectory() as folder:
            for bad in [dict(bundle,answer='0'),dict(bundle,packet_hash='wrong'),dict(bundle,selected_seconds=[5])]:
                with self.assertRaises(ValueError):pipeline.run(packet,bad,backend,cfg,folder,dict(online_seconds=1500))
        self.assertEqual(backend.calls,[])

    def test_empty_memory_has_no_organization_calls(self):
        packet,bundle=fixture();m=partial.new_memory('v',packet['captions'],'source');partial.rebuild_indexes(m)
        bundle['memory']=m
        import json
        prefix=bundle['qa_input']['prompts'][0].split('\n\nTemporal entity memory (')[0]
        bundle['qa_input']['prompts'][0]=prefix+'\n\nTemporal entity memory (original summary/caption assertions; identities are text inferences; not pixel verified):\n'+json.dumps(partial.memory_context(m),ensure_ascii=False,separators=(',',':'))
        backend=Backend()
        with tempfile.TemporaryDirectory() as folder:r=pipeline.run(packet,bundle,backend,load_config('configs/script_temporal_full.yaml'),folder,dict(online_seconds=1500))
        self.assertIsNone(r['error'],r['error']);self.assertEqual(r['entity_count'],0)
        self.assertEqual([op for op,inp in backend.calls],['temporal_qa_check','videotree_qa'])

    def test_identity_candidates_require_explicit_judgments_and_foreign_citations_fail_atomically(self):
        _,bundle=fixture();m=pipeline.project(bundle['memory']);inp=dict(pairs=pipeline.identity_pairs(m,['S0','S1']))
        before=deepcopy(m)
        for bad in [dict(decisions=[]),dict(decisions=[dict(pair_id='I0',relation='same_instance',
                left_caption_ids=['FOREIGN'],right_caption_ids=['C0'],reason='guess')])]:
            with self.assertRaises(ValueError):pipeline.commit_identities(m,inp,bad,'call')
            self.assertEqual(m,before)

    def test_competing_same_scene_identity_cannot_merge_via_another_scene(self):
        _,bundle=fixture();m=pipeline.project(bundle['memory'])
        clone=deepcopy(m['entities']['E0']);clone['id']='E2';m['entities']['E2']=clone;m['next_entity']=3
        pipeline.temporal.rebuild_indexes(m);pipeline.temporal.rebuild_temporal_network(m)
        pairs=pipeline.identity_pairs(m,['S0','S1']);inp=dict(pairs=pairs)
        report=dict(decisions=[dict(pair_id=p['id'],relation='same_instance',left_caption_ids=p['left']['caption_ids'][:1],
                right_caption_ids=p['right']['caption_ids'][:1],reason='text inference') for p in pairs])
        pipeline.commit_identities(m,inp,report,'call')
        self.assertEqual([x['decision'] for x in m['cross_matches']],['same','unknown'])


if __name__=='__main__':unittest.main()
