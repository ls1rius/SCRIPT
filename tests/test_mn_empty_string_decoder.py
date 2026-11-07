import json
from pathlib import Path
import unittest

import torch
from transformers import AutoTokenizer
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data

from script_core.backend import constrained_prefix
from script_core.batch_extraction import substitute
from script_core.schemas import decoder_schema, output_schema


class EmptyStringDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = AutoTokenizer.from_pretrained(
            'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct', local_files_only=True)
        cls.data = build_token_enforcer_tokenizer_data(cls.tokenizer)

    def test_saved_failure_is_blocked_at_nonempty_gap_before_false_eos(self):
        call = json.loads(Path('tests/fixtures/m_empty_gap_false_eos.json').read_text())
        schema = output_schema('m_transition_next', call['inputs'])
        schema = substitute(schema, {v:k for k,v in call['output']['id_map'].items()})
        prefix = constrained_prefix(self.data, decoder_schema(schema), 'm_transition_next')
        state = torch.tensor([self.tokenizer.eos_token_id])
        blocked = None
        for token in call['output']['generated_token_ids']:
            if token not in prefix(0, state):
                blocked = token
                break
            state = torch.cat([state, torch.tensor([token])])
        self.assertIsNotNone(blocked)
        self.assertEqual(self.tokenizer.decode(state[1:]).splitlines()[-1], '  "gap": "')
        self.assertNotIn(self.tokenizer.eos_token_id, prefix(0, state))
        self.assertIn('specific', self.tokenizer.decode([blocked]))

    def test_empty_and_nonempty_branches_keep_exact_values(self):
        for maximum, text in [(0, ''), (5, 'hello')]:
            schema = {'type':'object', 'properties':{'gap':{'type':'string','maxLength':maximum}},
                      'required':['gap'], 'additionalProperties':False}
            original = json.loads(json.dumps(schema))
            prefix = constrained_prefix(self.data, decoder_schema(schema), 'm_transition_next')
            state = torch.tensor([self.tokenizer.eos_token_id])
            for char in json.dumps({'gap':text}, separators=(',', ':')):
                for token in self.tokenizer.encode(char, add_special_tokens=False):
                    self.assertNotIn(self.tokenizer.eos_token_id, prefix(0, state))
                    self.assertIn(token, prefix(0, state))
                    state = torch.cat([state, torch.tensor([token])])
            self.assertIn(self.tokenizer.eos_token_id, prefix(0, state))
            self.assertEqual(schema, original)


if __name__ == '__main__':
    unittest.main()
