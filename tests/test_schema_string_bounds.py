import unittest
from script_core.schemas import validate_output, decoder_schema


class StringBoundTests(unittest.TestCase):
    def test_nonempty_boundary_and_unicode_codepoints(self):
        schema={'type':'string','minLength':1,'maxLength':2}
        for value in ['a','ab','字','😀','e\u0301']:
            validate_output(value,schema)
        for value in ['', 'abc', '字😀a']:
            with self.assertRaises(ValueError):validate_output(value,schema)

    def test_nullable_minimum_does_not_reject_null(self):
        schema={'type':['string','null'],'minLength':1,'maxLength':3}
        validate_output(None,schema)
        validate_output('yes',schema)
        with self.assertRaisesRegex(ValueError,'minimum'):validate_output('',schema)

    def test_empty_string_contract_remains_valid(self):
        schema={'type':'string','maxLength':0}
        validate_output('',schema)
        validate_output('',decoder_schema(schema))
        with self.assertRaises(ValueError):validate_output('x',schema)

    def test_nested_anyof_enforces_each_branch(self):
        schema={'type':'object','required':['role'],'properties':{'role':{
            'anyOf':[{'type':'null'},{'type':'string','minLength':1}]}},'additionalProperties':False}
        validate_output({'role':None},schema)
        validate_output({'role':'hand'},schema)
        with self.assertRaisesRegex(ValueError,'role'):validate_output({'role':''},schema)

    def test_contradictory_zero_max_and_minimum_is_rejected(self):
        schema={'type':'string','minLength':1,'maxLength':0}
        for candidate in ['', 'x']:
            with self.assertRaises(ValueError):validate_output(candidate,decoder_schema(schema))


if __name__=='__main__':unittest.main()
