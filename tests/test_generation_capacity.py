from types import SimpleNamespace
import unittest

from script_core.generation_capacity import context_preflight, planner_output_preflight


class CapacityTests(unittest.TestCase):
    def test_native_exact_fit_and_one_token_overflow(self):
        cfg = SimpleNamespace(max_position_embeddings=2048)
        audit = context_preflight(cfg, 1024, 1024, 4096, label='Test')
        self.assertEqual(audit['native_context_tokens'], 2048)
        with self.assertRaisesRegex(ValueError, '1025 \\+ 1024 > 2048'):
            context_preflight(cfg, 1025, 1024, 4096, label='Test')

    def test_nested_model_config_and_configured_input_limit(self):
        cfg = {'text_config': {'max_position_embeddings': 4096}}
        context_preflight(cfg, 2048, 1024, 2048, label='Test')
        with self.assertRaisesRegex(ValueError, 'input overflow'):
            context_preflight(cfg, 2049, 1024, 2048, label='Test')

    def test_unknown_and_invalid_native_capacity_fail_before_generation(self):
        for cfg in [None, {}, SimpleNamespace(), {'max_position_embeddings': 0},
                    {'max_position_embeddings': True}, {'max_position_embeddings': '4096'}]:
            with self.subTest(cfg=cfg), self.assertRaisesRegex(ValueError, 'unknown or invalid'):
                context_preflight(cfg, 1, 1024, 4096, label='Test')

    def test_planner_lower_bound_rejects_only_impossible_capacity(self):
        tokenizer = SimpleNamespace(encode=lambda text, **kwargs: list(text))
        # A character tokenizer's maximum literal output contribution is one;
        # the extra boundary-space margin makes our bound weaker, never unsafe.
        data = SimpleNamespace(regular_tokens=[(0, 'a', False)])
        schema = dict(type='object', properties={'R0':dict(type='string',enum=['keep','skip'])}, required=['R0'])
        audit = planner_output_preflight(schema, tokenizer, data, 100)
        lower = audit['necessary_output_token_lower_bound']
        planner_output_preflight(schema, tokenizer, data, lower)
        with self.assertRaisesRegex(ValueError, 'necessary token lower bound'):
            planner_output_preflight(schema, tokenizer, data, lower - 1)

    def test_worst_case_estimate_does_not_reject_feasible_short_output(self):
        tokenizer = SimpleNamespace(encode=lambda text, **kwargs: list(text))
        data = SimpleNamespace(regular_tokens=[(0, 'x' * 100, False)])
        schema = dict(type='object', properties={'R0':dict(type='string',enum=['keep','group:'+'x'*1000])}, required=['R0'])
        audit = planner_output_preflight(schema, tokenizer, data, 20)
        self.assertFalse(audit['worst_case_estimate_fits'])
        self.assertFalse(audit['worst_case_estimate_is_proof'])
        self.assertLess(audit['necessary_output_token_lower_bound'], 20)

    def test_unknown_tokenizer_pieces_do_not_invent_a_lower_bound(self):
        tokenizer = SimpleNamespace(encode=lambda text, **kwargs: list(text))
        schema = dict(type='object', properties={'R0':dict(type='string',enum=['keep'])}, required=['R0'])
        audit = planner_output_preflight(schema, tokenizer, SimpleNamespace(), 1)
        self.assertIsNone(audit['necessary_output_token_lower_bound'])


if __name__ == '__main__':
    unittest.main()
