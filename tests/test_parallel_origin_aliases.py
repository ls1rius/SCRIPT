import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import preserve_parallel_call_paths as aliases


class AliasReadinessTests(unittest.TestCase):
    def setup_case(self,root):
        out=root/'N';control=root/'control';run=out/'runs/q'
        io.write(control/'claims.json',dict(q=dict(state='complete')))
        io.write(run/'result.json',dict(answer='x',error=None))
        reference=control/'attempts/q/run/partial_temporal/raw_calls/call.json'
        io.write(run/'partial_temporal/raw_calls/call.json',dict(output=dict(raw='source')))
        (run/'partial_temporal/calls.jsonl').write_text(json.dumps(dict(error=None,output_ref=str(reference)))+'\n')
        return out,control,run

    def test_missing_script_is_rejected_before_process_launch(self):
        with tempfile.TemporaryDirectory() as td:
            out,control,_=self.setup_case(Path(td))
            with patch.object(aliases.subprocess,'Popen') as spawn:
                with self.assertRaises(FileNotFoundError):aliases.launch(out,control,os.getpid(),helper=control/'missing.py')
            spawn.assert_not_called();self.assertFalse((control/'origin_aliases_launch.json').exists())

    def test_broken_reference_is_detected_then_alias_repairs_without_result_change(self):
        with tempfile.TemporaryDirectory() as td:
            out,control,run=self.setup_case(Path(td));before=io.sha(run/'result.json')
            bad=aliases.audit_references(out,control);self.assertFalse(bad['passed']);self.assertEqual(len(bad['missing']),1)
            self.assertEqual(aliases.preserve(out,control),['q'])
            good=aliases.audit_references(out,control);self.assertTrue(good['passed']);self.assertEqual(good['successful_call_references'],1)
            self.assertEqual(io.sha(run/'result.json'),before)

    def test_real_helper_launch_requires_live_process_and_first_scan(self):
        with tempfile.TemporaryDirectory() as td:
            out,control,run=self.setup_case(Path(td));before=io.sha(run/'result.json')
            data=aliases.launch(out,control,os.getpid(),helper=Path(aliases.__file__))
            try:
                self.assertTrue(data['ready']);self.assertTrue(data['startup_status']['first_scan_complete'])
                self.assertEqual(aliases.process_identity(data['pid']),data['process_identity'])
                self.assertEqual(io.sha(run/'result.json'),before)
            finally:
                os.kill(data['pid'],signal.SIGTERM);aliases._CHILDREN.pop(data['pid']).wait(timeout=5)

    def test_pid_from_immediately_exiting_helper_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            out,control,_=self.setup_case(Path(td));helper=control/'bad_helper.py'
            helper.write_text("import sys\nif '--help' in sys.argv: sys.exit(0)\nsys.exit(7)\n")
            with self.assertRaisesRegex(RuntimeError,'exited'):
                aliases.launch(out,control,os.getpid(),helper=helper)
            record=io.read(control/'origin_aliases_launch.json')
            self.assertFalse(record['ready']);self.assertIn('exited',record['startup_error'])


if __name__=='__main__':unittest.main()
