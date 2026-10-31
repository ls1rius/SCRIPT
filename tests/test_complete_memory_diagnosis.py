import unittest
from script.analyze_complete_memory_errors import audited_option


class DiagnosisParserTests(unittest.TestCase):
    def test_character_c_with_exact_full_option_text_is_not_letter_c(self):
        options=['C uses the book as a surface to draw on.','C reads a book.','C writes a note.']
        self.assertEqual(audited_option('prediction: C uses the book as a surface to draw on.\nexplanation: drawing.',options),
            dict(option='0',method='unique_exact_option_text'))

    def test_ambiguous_paraphrase_is_not_guessed_from_the_first_character(self):
        self.assertEqual(audited_option('prediction: C often moves around the room.',['C works.','C walks.','C sits.'])['option'],None)

    def test_explicit_letter_has_priority_over_explanation(self):
        self.assertEqual(audited_option('prediction: B\nexplanation: I might choose A.',['a','b','c'])['option'],'1')

    def test_duplicate_option_text_and_multiple_predictions_stay_unresolved(self):
        self.assertIsNone(audited_option('prediction: C works.',['C works.','C works.'])['option'])
        self.assertIsNone(audited_option('prediction: C works.\nprediction: C reads.',['C works.','C reads.'])['option'])


if __name__ == '__main__': unittest.main()
