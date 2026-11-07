import json
from pathlib import Path
import unittest
from lmformatenforcer import JsonSchemaParser
from script_core.json_unicode_guard import ScalarEscapeParser
from script_core.schemas import record,short_text

class ScalarTests(unittest.TestCase):
    def check_text(self, text):
        parser=ScalarEscapeParser(JsonSchemaParser(record(dict(value=short_text(100)))))
        for char in text:
            if char not in parser.get_allowed_characters(): return False
            parser=parser.add_character(char)
        return parser.can_end()
    def test_normal_escapes_and_bmp(self):
        for value in ['{"value":"\\u7e6b"}', '{"value":"escaped \\\" quote"}', '{"value":"line\\nnext"}', '{"value":"\\\\ud83c"}']:
            self.assertTrue(self.check_text(value), value)
    def test_surrogate_escapes_rejected(self):
        for code in ['d800','d83c','dfff','D800','DFFF']:
            self.assertFalse(self.check_text('{"value":"\\u'+code+'"}'))
    def test_saved_model_tokens_blocked_before_bad_escape(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.backend import constrained_prefix
        from script_core.m_scene_memory import output_schema
        from script_core.batch_extraction import substitute
        t=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        data=build_token_enforcer_tokenizer_data(t)
        call=json.loads(Path('tests/fixtures/m_sufficiency_invalid_saved_call.json').read_text())
        schema=output_schema('m_sufficiency_check',call['inputs'])
        schema=substitute(schema,{v:k for k,v in call['output']['id_map'].items()})
        fn=constrained_prefix(data,schema,'m_sufficiency_check')
        state=torch.tensor([t.eos_token_id]);blocked=None
        for token in call['output']['generated_token_ids']:
            if token not in fn(0,state):blocked=token;break
            state=torch.cat([state,torch.tensor([token])])
        self.assertIsNotNone(blocked)
        self.assertIn('\\u',t.decode(state.tolist()[1:],skip_special_tokens=True)+t.decode([blocked]))
    def test_real_tokenizer_valid_bmp_and_literal_unicode(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.backend import constrained_prefix
        t=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        data=build_token_enforcer_tokenizer_data(t)
        for raw in ['{"value":"中文"}', '{"value":"\\u7e6b"}', '{"value":"normal\\ntext"}']:
            fn=constrained_prefix(data,record(dict(value=short_text(100))),'m_candidate_query');state=torch.tensor([t.eos_token_id])
            # Character segmentation isolates grammar behavior from greedy BPE legality.
            for c in raw:
                for token in t.encode(c,add_special_tokens=False):
                    self.assertIn(token,fn(0,state));state=torch.cat([state,torch.tensor([token])])
            self.assertIn(t.eos_token_id,fn(0,state))

if __name__=='__main__':unittest.main()
