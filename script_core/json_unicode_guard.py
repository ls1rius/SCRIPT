"""Navigation JSON: forbid surrogate escape code units; literal Unicode remains usable.

UTF-8 logs cannot contain lone UTF-16 surrogates. BMP escapes and ordinary JSON
escapes remain legal; supplementary characters must be emitted literally.
"""
import re
from lmformatenforcer import CharacterLevelParser


def tokenizer_data(data):
    if not hasattr(data, '_scalar_escape_data'):
        from lmformatenforcer.tokenenforcer import TokenEnforcerTokenizerData
        # LMFE's free-text shortcut bypasses character traversal for complete
        # escaped tokens. Exclude those tokens as well as guarding split escapes.
        rows = [r for r in data.regular_tokens if not re.search(r'\\u[dD][89a-fA-F]', r[1])]
        result = TokenEnforcerTokenizerData(rows, data.decoder, data.eos_token_id,
                                           data.use_bitmask, data.vocab_size)
        result.tokenizer_alphabet = data.tokenizer_alphabet
        data._scalar_escape_data = result
    return data._scalar_escape_data


class ScalarEscapeParser(CharacterLevelParser):
    def __init__(self, inner, escape=''):
        self.inner = inner
        self.escape = escape

    @property
    def config(self):
        return self.inner.config

    @config.setter
    def config(self, value):
        self.inner.config = value

    def get_allowed_characters(self):
        chars = self.inner.get_allowed_characters()
        if self.escape.lower() == 'ud':
            chars = ''.join(c for c in chars if c not in '89abcdefABCDEF')
        return chars

    def add_character(self, char):
        escape = self.escape
        if escape == '\\':
            escape = 'u' if char == 'u' else ''
        elif escape.startswith('u'):
            escape = escape + char if len(escape) < 4 else ''
        elif char == '\\':
            escape = '\\'
        return ScalarEscapeParser(self.inner.add_character(char), escape)

    def can_end(self):
        return not self.escape and self.inner.can_end()

    def shortcut_key(self):
        return None if self.escape else self.inner.shortcut_key()
