"""Shared answer parsing used by J/demo after retirement of G+GRPO."""
import unittest

from script_core.g_rl import parse_answer


class AnswerParserTests(unittest.TestCase):
    def test_person_c_is_not_an_option_letter(self):
        options = ["C's overall purpose in the video is to warm up.",
                   'cool down', 'play soccer', 'celebrate', 'reflect']
        parsed = parse_answer('prediction: ' + options[0], options)
        self.assertEqual(parsed['option'], 0)
        self.assertIsNone(parse_answer('prediction: C is making food.', options)['option'])

    def test_explicit_letter_and_exact_option_text(self):
        options = ['one', 'C chops cucumber', 'three', 'four', 'five']
        self.assertEqual(parse_answer('prediction: B\nexplanation: chopping', options)['option'], 1)
        self.assertEqual(parse_answer('prediction: E: five', options)['option'], 4)
        self.assertEqual(parse_answer('prediction: c CHOPS cucumber.', options)['option'], 1)

    def test_conflicting_predictions_are_unresolved(self):
        self.assertIsNone(parse_answer('prediction: A\nprediction: B', list('abcde'))['option'])

    def test_explanation_cannot_repair_an_explicit_letter(self):
        self.assertEqual(parse_answer('prediction: C\nexplanation: use a clay mould',
                                      ['one', 'use a clay mould', 'hands', 'four', 'five'])['option'], 2)


if __name__ == '__main__':
    unittest.main()
