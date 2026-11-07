from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from script import controlled_memory_io as io
from script import queue_grounded_reader_after_i as queue


class GroundedReaderQueueTests(unittest.TestCase):
    def fixture(self,root):
        project=Path(__file__).resolve().parents[1]
        predecessor,parent,demo,out,folder=(root/n for n in ['I_queue','I','demo','J','J_queue'])
        io.write(predecessor/'plan.json',dict(output_I=str(parent),planned=300))
        io.write(predecessor/'frozen_hashes.json',{'plan.json':io.sha(predecessor/'plan.json')})
        io.write(predecessor/'queue_status.json',dict(state='complete'))
        io.write(parent/'queue_status.json',dict(state='complete'))
        io.write(demo/'experiment.json',dict(arm='J_grounded_reader',version='test-J'))
        io.write(demo/'queue_status.json',dict(state='complete'))
        io.write(demo/'report.json',dict(completed=7,format_errors=0,correct=0,cases=[dict(J=dict(reader_calls=2)) for _ in range(7)]))
        io.write(demo/'validation/functional.json',dict(passed=True))
        for name in ['controlled_memory_io.py','grounded_reader.py','demo_evidence_revision.py','qa_evidence_view.py','g_rl.py','run_grounded_reader_experiment.py']:
            source=project/('script' if name in ['controlled_memory_io.py','run_grounded_reader_experiment.py'] else 'script_core')/name
            target=demo/'control'/name;target.parent.mkdir(exist_ok=True);target.write_bytes(source.read_bytes())
        io.write(demo/'frozen_hashes.json',{'experiment.json':io.sha(demo/'experiment.json')})
        queue.prepare(folder,predecessor,parent,demo,out)
        return folder,out,demo

    def test_functional_gate_ignores_accuracy_and_prepares_then_runs_j(self):
        with tempfile.TemporaryDirectory() as td:
            folder,out,demo=self.fixture(Path(td));commands=[]
            def launch(command,**kwargs):
                commands.append(command)
                io.write(out/'queue_status.json',dict(state='prepared' if len(commands)==1 else 'complete'))
                return SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
            with patch.object(queue.subprocess,'Popen',side_effect=launch):self.assertEqual(queue.run(folder),0)
            self.assertEqual([c[3] for c in commands],['prepare','queue'])
            self.assertFalse(io.read(folder/'functional_gate.json')['accuracy_used'])

    def test_format_failure_stops_before_formal_j(self):
        with tempfile.TemporaryDirectory() as td:
            folder,out,demo=self.fixture(Path(td))
            result=io.read(demo/'report.json');result['format_errors']=1;io.write(demo/'report.json',result)
            with patch.object(queue.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'functional gate failed'):queue.run(folder)
            launch.assert_not_called();self.assertFalse(out.exists())

    def detach_demo(self,folder,demo):
        plan=io.read(folder/'plan.json');plan.pop('demo');plan.pop('demo_manifest_sha256')
        io.write(folder/'plan.json',plan)
        io.write(folder/'prerequisite_gate.json',dict(version=plan['version'],passed=True,
            synthetic_functional_probe_passed=True,benchmark_labels_used=False,
            completed_cases=7,format_errors=0,reader_calls_per_case=[2]*7,accuracy_used=False))
        io.write(folder/'frozen_hashes.json',{name:io.sha(folder/name)
            for name in ['plan.json','prerequisite_gate.json']})
        shutil.rmtree(demo)

    def test_sealed_gate_runs_after_all_demo_records_are_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            folder,out,demo=self.fixture(Path(td));self.detach_demo(folder,demo);commands=[]
            def launch(command,**kwargs):
                commands.append(command)
                io.write(out/'queue_status.json',dict(state='prepared' if len(commands)==1 else 'complete'))
                return SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
            with patch.object(queue.subprocess,'Popen',side_effect=launch):self.assertEqual(queue.run(folder),0)
            self.assertEqual([c[3] for c in commands],['prepare','queue'])
            self.assertFalse(io.read(folder/'functional_gate.json')['accuracy_used'])

    def test_failed_or_tampered_sealed_gate_cannot_launch_j(self):
        with tempfile.TemporaryDirectory() as td:
            folder,out,demo=self.fixture(Path(td));self.detach_demo(folder,demo)
            gate=io.read(folder/'prerequisite_gate.json');gate['reader_calls_per_case']=[2]*6+[1]
            io.write(folder/'prerequisite_gate.json',gate)
            with patch.object(queue.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'Frozen queue artifact changed'):queue.run(folder)
                seals=io.read(folder/'frozen_hashes.json');seals['prerequisite_gate.json']=io.sha(folder/'prerequisite_gate.json')
                io.write(folder/'frozen_hashes.json',seals)
                with self.assertRaisesRegex(ValueError,'Sealed J prerequisite gate failed'):queue.run(folder)
            launch.assert_not_called();self.assertFalse(out.exists())


if __name__=='__main__':unittest.main()
