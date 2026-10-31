"""Checks for reward integrity, fixed cohorts, gradient alignment and real LoRA updates."""
import unittest
import torch
from script_core import g_rl as g


class RewardTests(unittest.TestCase):
    def test_person_c_is_not_option_c(self):
        options = ["C's overall purpose in the video is to warm up.", 'cool down', 'play soccer', 'celebrate', 'reflect']
        text = "prediction: C's overall purpose in the video is to warm up.\nexplanation: kicking"
        self.assertEqual(g.parse_answer(text, options)['option'], 0)
        self.assertEqual(g.answer_reward(text, options, 2)[0], 0)

    def test_no_semantic_guess_or_first_letter_fallback(self):
        self.assertIsNone(g.parse_answer('prediction: C is apparently making food.', ['a','b','c','d','e'])['option'])

    def test_explicit_and_unique_option_parsing(self):
        self.assertEqual(g.parse_answer('prediction: B\nexplanation: mould', ['a','b','c','d','e'])['option'], 1)
        self.assertEqual(g.parse_answer('prediction: E: The correct description.', ['a','b','c','d','e'])['option'], 4)
        self.assertEqual(g.parse_answer('prediction: c CHOPS cucumber.', ['a','C chops cucumber','c','d','e'])['option'], 1)

    def test_conflicting_answers_unresolved(self):
        self.assertIsNone(g.parse_answer('prediction: A\nprediction: B', ['a','b','c','d','e'])['option'])

    def test_wrong_letter_is_not_repaired_using_explanation(self):
        text = 'prediction: C\nexplanation: use a clay mould'
        self.assertEqual(g.answer_reward(text, ['a','use a clay mould','hands','d','e'], 1)[0], 0)

    def test_incomplete_generation_not_rewarded(self):
        self.assertEqual(g.answer_reward('prediction: A', ['a','b','c','d','e'], 0, False)[0], -0.1)


class CohortTests(unittest.TestCase):
    def test_exact_200_300_order_and_source_overlap_disclosure(self):
        ids = [f'q{i}' for i in range(500)]
        original = list(reversed(ids[:300]))
        mapping = {u: {'video_uid': f'v{i % 250}'} for i,u in enumerate(ids)}
        splits, audit = g.remaining_200_split(ids, original, mapping)
        self.assertEqual(splits['validation'], original)
        self.assertEqual(set(splits['train']), set(ids[300:]))
        self.assertFalse(set(splits['train']) & set(splits['validation']))
        self.assertTrue(audit['shared_original_video_ids'])
        self.assertEqual(g.remaining_200_split(list(reversed(ids)), original, mapping)[0], splits)

    def test_invalid_cohort_rejected(self):
        with self.assertRaises(ValueError):
            g.remaining_200_split(['x']*500, ['x']*300, {})


class GRPOTests(unittest.TestCase):
    def test_advantage_signal_and_zero_variance(self):
        a = g.relative_advantages([0, 0, 1, 1])
        self.assertTrue(torch.all(a[:2] < 0) and torch.all(a[2:] > 0))
        self.assertAlmostEqual(a.mean().item(), 0)
        self.assertTrue(torch.equal(g.relative_advantages([1]*4), torch.zeros(4)))

    def test_policy_gradient_and_clipping(self):
        p = torch.tensor([[-1.0]], requires_grad=True)
        loss, kl = g.grpo_loss(p, p.detach(), p.detach(), 1.0, beta=0)
        loss.backward()
        self.assertLess(p.grad.item(), 0)  # Gradient descent raises rewarded log-probability.
        p = torch.tensor([[0.0]], requires_grad=True)
        loss, _ = g.grpo_loss(p, torch.tensor([[-2.0]]), p.detach(), 1.0, beta=0)
        loss.backward()
        self.assertEqual(p.grad.item(), 0)  # PPO clips an already over-improved action.

    def test_real_model_alignment_and_adapter_only_update(self):
        from transformers import Qwen2Config, Qwen2ForCausalLM
        from peft import LoraConfig, TaskType, get_peft_model
        torch.manual_seed(7)
        model = Qwen2ForCausalLM(Qwen2Config(vocab_size=37, hidden_size=32, intermediate_size=64,
                        num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                        attention_dropout=0, max_position_embeddings=128))
        model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM,
                                  r=2, lora_alpha=4, target_modules=['q_proj','v_proj'], lora_dropout=0))
        model.eval()
        prompt = torch.tensor([[1,2,3,4]])
        completion = torch.tensor([[5,6,7]])
        ids = torch.cat([prompt, completion], 1)
        expected = model(input_ids=ids, use_cache=False).logits[:,prompt.shape[1]-1:-1].float().log_softmax(-1)
        expected = expected.gather(-1, completion.unsqueeze(-1)).squeeze(-1)
        actual = g.token_logprobs(model, prompt, completion)
        torch.testing.assert_close(actual, expected)
        with torch.no_grad(), model.disable_adapter():
            ref = g.token_logprobs(model, prompt, completion).detach()
        before = {n: p.detach().clone() for n,p in model.named_parameters()}
        params = [p for n,p in model.named_parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(params, lr=1e-2)
        loss, _ = g.grpo_loss(actual, actual.detach(), ref, 1.0)
        loss.backward()
        optimizer.step()
        changed = [n for n,p in model.named_parameters() if not torch.equal(p, before[n])]
        self.assertTrue(changed)
        self.assertTrue(all('lora_' in n for n in changed))

    def test_qwen_vl_real_architecture_peft_forward_and_checkpointing(self):
        from transformers.models.qwen2_5_vl.configuration_qwen2_5_vl import Qwen2_5_VLConfig
        from transformers import Qwen2_5_VLForConditionalGeneration
        from peft import LoraConfig, TaskType, get_peft_model
        cfg = Qwen2_5_VLConfig(
            text_config=dict(vocab_size=37, hidden_size=32, intermediate_size=64, num_hidden_layers=1,
                num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=128,
                rope_scaling={'type':'mrope', 'mrope_section':[1,1,2]}),
            vision_config=dict(depth=1, hidden_size=32, intermediate_size=64, num_heads=4,
                out_hidden_size=32, patch_size=2, spatial_merge_size=1, temporal_patch_size=1,
                fullatt_block_indexes=[0]))
        model = Qwen2_5_VLForConditionalGeneration(cfg)
        model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM,
                         r=2, lora_alpha=4, target_modules=['q_proj','v_proj'], lora_dropout=0))
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
        model.enable_input_require_grads()
        model.train()
        logp = g.token_logprobs(model, torch.tensor([[1,2,3]]), torch.tensor([[4,5]]))
        (-logp.mean()).backward()
        grads = [p.grad for n,p in model.named_parameters() if p.requires_grad]
        self.assertTrue(any(v is not None and v.abs().sum() > 0 for v in grads))


if __name__ == '__main__':
    unittest.main()
