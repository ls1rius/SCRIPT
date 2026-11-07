from copy import deepcopy
import json
import unittest
from lmformatenforcer import JsonSchemaParser
from script_core.json_unique_array_guard import UniqueStringArrayParser
from script_core import p_sentence_filter as p
from script_core.schemas import decoder_schema


def feed(parser,text):
    for char in text:
        if char not in parser.get_allowed_characters():
            raise ValueError('Forbidden character: '+repr(char))
        parser=parser.add_character(char)
    return parser


def request(ids=('S1','S10','S2')):
    return p.review_input([dict(source_id=s,intervals=[[i,i+1]],information=[]) for i,s in enumerate(ids)],
        dict(question='Which observation?',options=['First.','Second.','Third.','Fourth.','Fifth.']))


def review(refs):
    return dict(assessments={k:dict(main_support=list(refs),main_counter=[],main_status='unknown',
        detail_support=[],detail_counter=[],details_status='unknown') for k in 'ABCDE'})


class UniqueReviewTests(unittest.TestCase):
    def parser(self,ids=('S1','S10','S2')):
        return UniqueStringArrayParser(JsonSchemaParser(decoder_schema(p.review_schema(request(ids)))),ids)

    def test_arbitrary_order_and_cross_option_reference_reuse_allowed(self):
        value=review(['S2','S1','S10'])
        parser=feed(self.parser(),json.dumps(value,separators=(',',':')))
        self.assertTrue(parser.can_end());p.validate_review(request(),value)

    def test_duplicate_in_same_array_blocked_before_it_is_completed(self):
        parser=feed(self.parser(),'{"assessments":{"A":{"main_support":["S1","S1')
        self.assertNotIn('"',parser.get_allowed_characters())
        self.assertIn('0',parser.get_allowed_characters())

    def test_same_reference_in_another_list_is_allowed(self):
        value=review(['S1']);value['assessments']['A']['detail_support']=['S1']
        self.assertTrue(feed(self.parser(),json.dumps(value,separators=(',',':'))).can_end())

    def test_empty_arrays_and_exhausted_allowlist_close_normally(self):
        value=review([])
        self.assertTrue(feed(self.parser(),json.dumps(value,separators=(',',':'))).can_end())
        parser=feed(self.parser(),'{"assessments":{"A":{"main_support":["S1","S10","S2"')
        self.assertIn(']',parser.get_allowed_characters());self.assertNotIn(',',parser.get_allowed_characters())

    def test_unicode_and_escaped_reference_strings(self):
        ids=('研究','A"B','back\\slash')
        value=review(list(ids));text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
        # LMFE's enum parser itself cannot encode embedded quotes. Exercise
        # escaped literals against its ordinary string parser; this guard still
        # supplies the finite allowlist. Real production IDs are internal S<n>.
        schema=p.review_schema(request(ids))
        for entry in schema['properties']['assessments']['properties'].values():
            for key in ['main_support','main_counter','detail_support','detail_counter']:
                entry['properties'][key]['items']={'type':'string'}
        inner=JsonSchemaParser(decoder_schema(schema))
        inner.config.alphabet+=''.join(ids)
        inner.context.alphabet_without_quotes=inner.config.alphabet.replace('"','')
        self.assertTrue(feed(UniqueStringArrayParser(inner,ids),text).can_end())

    def test_parser_state_is_immutable_between_independent_branches(self):
        start=feed(self.parser(),'{"assessments":{"A":{"main_support":[')
        left=feed(start,'"S1",');right=feed(start,'"S2",')
        with self.assertRaises(ValueError):feed(left,'"S1"')
        feed(right,'"S1"')

    def test_postvalidation_still_rejects_duplicate_and_foreign_ids(self):
        for refs in [['S1','S1'],['other']]:
            with self.assertRaises(ValueError):p.validate_review(request(),review(refs))

    def test_real_tokenizer_blocks_duplicate_sequence_and_keeps_local_ids(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.o_generation import constrained_prefix
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        data=build_token_enforcer_tokenizer_data(tokenizer)
        def walk(value,should_pass):
            prefix=constrained_prefix(data,decoder_schema(p.review_schema(request())),unique_string_arrays=['S1','S10','S2'])
            tokens=tokenizer.encode(json.dumps(value,separators=(',',':')),add_special_tokens=False)
            state=[tokenizer.eos_token_id];prefix(0,torch.tensor(state))
            for token in tokens:
                allowed=prefix(0,torch.tensor(state))
                if token not in allowed:
                    self.assertFalse(should_pass);return
                state.append(token)
            self.assertTrue(should_pass)
            self.assertIn(tokenizer.eos_token_id,prefix(0,torch.tensor(state)))
        walk(review(['S2','S1','S10']),True)
        walk(review(['S1','S1']),False)


if __name__=='__main__':unittest.main()
