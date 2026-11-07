"""Incremental uniqueness for finite string-reference arrays during decoding.

Used for P reader review only. Lists retain arbitrary order and may reuse a
reference in another list/option. No generated output is silently deduplicated.
"""
import json
from lmformatenforcer import CharacterLevelParser


class UniqueStringArrayParser(CharacterLevelParser):
    def __init__(self, inner, values=(), *, choices=None, stack=(), in_string=False,
                 escaped=False, literal='', active=None):
        self.inner=inner
        self.choices=choices if choices is not None else tuple(json.dumps(v,ensure_ascii=False) for v in values)
        self.stack=stack
        self.in_string=in_string
        self.escaped=escaped
        self.literal=literal
        self.active=active

    @property
    def config(self):
        return self.inner.config

    @config.setter
    def config(self,value):
        self.inner.config=value

    def get_allowed_characters(self):
        chars=self.inner.get_allowed_characters()
        if self.active is not None:
            next_chars={v[len(self.literal)] for v in self.active if len(v)>len(self.literal)}
            return ''.join(c for c in chars if c in next_chars)
        if not self.in_string and self.stack and self.stack[-1][0]=='array':
            if len(self.stack[-1][1])==len(self.choices):
                chars=''.join(c for c in chars if c not in ',"')
        return chars

    def add_character(self,char):
        stack=self.stack;inside=self.in_string;escaped=self.escaped;literal=self.literal;active=self.active
        if inside:
            if active is not None:
                literal+=char
                active=tuple(v for v in active if v.startswith(literal))
                if not active:
                    raise ValueError('Repeated or nonlocal JSON array reference')
            if char=='"' and not escaped:
                if active is not None:
                    if literal not in active:
                        raise ValueError('Incomplete JSON reference')
                    kind,used=stack[-1]
                    stack=stack[:-1]+((kind,used|{literal}),)
                inside=False;literal='';active=None
            escaped=(not escaped) if char=='\\' else False
        elif char=='"':
            inside=True;escaped=False;literal='"'
            if stack and stack[-1][0]=='array':
                active=tuple(v for v in self.choices if v not in stack[-1][1])
                if not active:
                    raise ValueError('No unused local reference remains')
        elif char in '[{':
            stack=stack+(('array' if char=='[' else 'object',frozenset()),)
        elif char in ']}':
            stack=stack[:-1]
        return UniqueStringArrayParser(self.inner.add_character(char),choices=self.choices,
            stack=stack,in_string=inside,escaped=escaped,literal=literal,active=active)

    def can_end(self):
        return not self.in_string and self.inner.can_end()

    def shortcut_key(self):
        # LMFE shortcuts may bypass per-character uniqueness checks.
        return None
