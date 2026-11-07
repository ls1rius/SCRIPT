import json
from pathlib import Path
import tempfile
import unittest
from script import controlled_memory_io as io
from script.upgrade_o_source_bound import recover_rows
from script.o_batched_matrix import packet_for
from script_core import o_sentence_filter as core
from script_core.config import digest


class UpgradeTests(unittest.TestCase):
    def test_durable_uncommitted_batch_recovered_without_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';folder=root/'new/runs/q'
            source=dict(id='S0',text='D opens drawer.',source_intervals=[[0,1]])
            io.write(old/'cases/q/sentences.json',[source])
            extracted=dict(extraction_status='extracted',actions=[dict(actor='D',verb='opens',roles=dict(patient='drawer'))],
                attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
            candidates=core.candidates(source,extracted)
            for area,op,req,data in [
                ('shared_extraction',core.EXTRACT,core.extraction_input(source),extracted),
                ('question_only',core.FILTER,core.filter_input(dict(question='What happens?'),source,candidates),
                 dict(observation='D opens drawer',relevance='action',labels={u['id']:1 for u in candidates}))]:
                packet=packet_for('q','source_only' if op==core.EXTRACT else 'question_only',op,[('S0',req)])
                row=dict(source_id='S0',scope=packet['scope'],request_hash=digest(req),data=data)
                target=folder/area
                io.write(target/'raw_calls/id-0.json',dict(inputs=packet,output=dict(items=[row])))
                (target/'calls.jsonl').write_text(json.dumps(dict(call_id='id',attempt=0,operation='o_independent_batch',error=None))+'\n')
            self.assertEqual(len(recover_rows(old,folder,'q')),2)
            selection=io.read(folder/'question_only/sentences/S0/selection.json')
            self.assertEqual(selection['selected'],candidates)
            self.assertEqual(recover_rows(old,folder,'q'),[])
            self.assertFalse(list(folder.rglob('composition.json')))

    def test_inherited_foreign_row_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=dict(id='S0',text='D moves.')
            io.write(root/'old/cases/q/sentences.json',[source])
            packet=packet_for('q','source_only',core.EXTRACT,[('S0',core.extraction_input(source))])
            target=root/'new/shared_extraction'
            io.write(target/'raw_calls/id-0.json',dict(inputs=packet,output=dict(items=[dict(source_id='S0',scope={'question_id':'foreign'},request_hash='bad')])) )
            (target/'calls.jsonl').write_text(json.dumps(dict(call_id='id',attempt=0,operation='o_independent_batch',error=None))+'\n')
            with self.assertRaisesRegex(ValueError,'row mismatch'):recover_rows(root/'old',root/'new','q')

if __name__=='__main__':unittest.main()
