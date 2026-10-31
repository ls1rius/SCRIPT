import json
from pathlib import Path
import tempfile
import unittest

from script.recover_complete_memory_shard import publish


class ResourceRecoveryTests(unittest.TestCase):
    def test_overlay_preserves_failed_attempt_and_includes_its_cost(self):
        with tempfile.TemporaryDirectory() as temp:
            original = Path(temp)/'original'; recovery = original/'recovery'/'r2'
            recovery.mkdir(parents=True)
            def save(folder, name, value):
                path = folder/name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            save(original, 'cohort.json', ['q'])
            save(original, 'evaluation_only.json', {'q':0})
            save(original, 'historical.json', {'q':{'selected_option':0}})
            failed = {'selected_option':None, 'error':{'message':'CUDA out of memory'}}
            save(original, 'runs/original_replay/q/result.json', failed)
            save(original, 'runs/original_replay/q/calls.jsonl', {'actual_cost':5})
            first = publish(original, recovery)
            self.assertEqual(first['arms']['original_replay']['errors'], 1)
            save(recovery, 'runs/original_replay/q/result.json', {'selected_option':0,'error':None})
            save(recovery, 'runs/original_replay/q/calls.jsonl', {'actual_cost':7})
            final = publish(original, recovery)
            self.assertEqual(final['arms']['original_replay']['correct'], 1)
            self.assertEqual(final['arms']['original_replay']['errors'], 0)
            self.assertEqual(final['resource_recovery']['measured_seconds_all_attempts'], 12)
            self.assertEqual(json.loads((original/'runs/original_replay/q/result.json').read_text()), failed)
            self.assertEqual(json.loads((original/'current_report.json').read_text()), final)


if __name__ == '__main__': unittest.main()
