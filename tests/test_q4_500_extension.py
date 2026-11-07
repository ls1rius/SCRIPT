import concurrent.futures,importlib.util,json,pathlib,tempfile,unittest,copy
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('q4extension',ROOT/'script/extend_q4_500.py');ext=importlib.util.module_from_spec(spec);spec.loader.exec_module(ext)
OLD=ROOT/'workdirs/Q4-adaptive-300-20261105-r1';OUT=ROOT/'workdirs/Q4-adaptive-500-20261105-r1'
class ExtensionContracts(unittest.TestCase):
 def test_exact_300_prefix_and_200_new_public_labels(self):
  ids=ext.read(OUT/'cohort.json');old=ext.read(OLD/'cohort.json');new=ext.read(OUT/'new_case_ids.json');self.assertEqual(ids[:300],old);self.assertEqual(ids[300:],new);self.assertEqual(len(set(ids)),500);self.assertFalse(set(old)&set(new));self.assertEqual(len(ext.read(OUT/'evaluation_only.json')),500)
 def test_inherited_results_are_exact_and_not_regenerated(self):
  for uid,row in ext.read(OUT/'inherited_results.json').items():self.assertEqual(ext.sha(OUT/'runs'/uid/'result.json'),row['sha256']);self.assertTrue((OUT/'runs'/uid).is_symlink())
 def test_concurrent_claims_never_reclaim_original_prefix(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);ext.write(p/'cohort.json',['old']+[f'new{i}' for i in range(8)]);ext.write(p/'claims.json',{'old':{'state':'complete','index':1,'inherited':True}})
   with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(lambda i:ext.claim(p,i),range(4)))
   self.assertEqual(len({u for _,u in rows}),4);self.assertNotIn('old',[u for _,u in rows]);self.assertEqual(ext.read(p/'claims.json')['old']['state'],'complete')
 def test_failure_halts_new_claims(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d);ext.write(p/'claims.json',{});ext.write(p/'cohort.json',['new']);ext.fail(p,0,ValueError('fixture'));self.assertIsNone(ext.claim(p,1));self.assertEqual(ext.read(p/'failure.json')['type'],'ValueError')
 def test_current_and_source_model_semantics_and_budgets(self):
  q=ext.read(OUT/'qa_config.json');self.assertEqual(q,ext.read(OLD/'qa_config.json'));n=ext.read(OUT/'source_config.json');self.assertEqual(q['models']['vlm'],n['models']['vlm']);self.assertEqual(n['query']['field_extraction_batch_size'],4);self.assertEqual(n['budget']['online_cap'],1500);self.assertEqual(n['budget']['tool_call_cap'],150);self.assertEqual(n['query']['tool_transient_retries'],0);self.assertEqual(n['query']['json_repair_retries'],0)
 def test_all_original_model_facing_runtime_bytes_unchanged(self):
  for rel,h in ext.read(OUT/'validation/parent_runtime_hashes.json').items():self.assertEqual(ext.sha(OUT/'runtime'/rel),h)
 def test_new_packets_have_no_labels_and_original_complete_sources(self):
  for uid in ext.read(OUT/'new_case_ids.json'):
   p=ext.read(OUT/'source_inputs'/f'{uid}.json');self.assertEqual(set(p['question']),{'question_id','video_id','question','options'});self.assertEqual(len(p['captions']),len(p['frames']));self.assertEqual([c['interval'] for c in p['captions']],[[i,i+1] for i in range(len(p['captions']))]);self.assertEqual([f['pts'] for f in p['frames']],list(range(len(p['captions']))))
 def test_source_adapter_matches_original_nonempty_and_empty_paths(self):
  ids=ext.read(OLD/'cohort.json');empty=next(u for u in ids if not ext.read(OLD/'cases'/u/'sentences.json'));uids=[ids[0],empty]
  for uid in uids:
   with self.subTest(uid=uid),tempfile.TemporaryDirectory() as d:
    p=pathlib.Path(d);(p/'runtime').symlink_to((OUT/'runtime').resolve(),target_is_directory=True);(p/'source_runs').mkdir();(p/'source_runs'/uid).symlink_to(pathlib.Path(ext.read(OLD/'cases'/uid/'source_binding.json')['original_source_audit']['parent']).resolve(),target_is_directory=True)
    question=ext.read(OLD/'cases'/uid/'question.json');ext.write(p/'source_inputs'/f'{uid}.json',{'question':question});ext.write(p/'cases'/uid/'question.json',question);case=ext.prepare_case(p,uid)
    self.assertEqual(ext.read(case/'sentences.json'),ext.read(OLD/'cases'/uid/'sentences.json'));self.assertEqual(ext.read(case/'original_qa.json'),ext.read(OLD/'cases'/uid/'original_qa.json'));self.assertTrue(ext.read(case/'source_binding.json')['original_source_audit']['N_answers_and_extracted_roles_excluded'])
 def test_partial_500_report_does_not_count_pending_as_wrong(self):
  # Read the current snapshot: the background run may already have new answers.
  report=ext.read(OUT/'report.json');self.assertGreaterEqual(report['completed'],300);self.assertEqual(report['groups']['original300']['arms']['Q']['correct'],193);self.assertEqual(report['groups']['all500']['videotree']['correct'],304);self.assertEqual(report['groups']['original300']['videotree']['correct'],189)
  for name,planned in [('additional200',200),('all500',500)]:
   group=report['groups'][name];arm=group['arms']['Q'];n=group['completed'];self.assertEqual(arm['completed'],n)
   self.assertEqual(arm['accuracy_among_completed'],arm['correct']/n if n else None)
   self.assertEqual(arm['accuracy'],arm['correct']/planned if n==planned else None)
if __name__=='__main__':unittest.main()
