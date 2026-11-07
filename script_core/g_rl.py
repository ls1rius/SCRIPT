"""Shared gold-blind answer parser retained for J and demo comparisons.

The historical module name is kept for frozen preparation compatibility.
G+GRPO training, reward, adapter and optimization code has been removed.
"""
import re


def canonical(text):
    return re.sub(r'\s+', ' ', text.replace('**', '').replace('`', '')).strip().casefold().rstrip('.!?')


def parse_answer(text, options):
    """Gold-blind parser: a person named C must never be interpreted as option C."""
    clean = text.replace('**', '').replace('`', '').strip()
    declared = re.findall(r'(?im)^\s*prediction\s*:\s*([^\n]*)$', clean)
    if not declared:
        first = clean.split('\n', 1)[0].strip()
        if re.fullmatch(r'[A-E][.]?', first, re.I):
            declared = [first]
    if len(declared) != 1:
        return {'option': None, 'method': 'unresolved'}
    value = declared[0].strip()
    letter = re.fullmatch(r'([A-E])\s*[.)]?', value, re.I)
    # An explicit "E: <answer>" is unambiguous; "C is ..." is not.
    prefixed = re.fullmatch(r'([A-E])\s*:\s*\S.*', value, re.I)
    if letter or prefixed:
        return {'option': ord((letter or prefixed).group(1).upper()) - 65,
                'method': 'explicit_letter'}
    matches = [i for i, option in enumerate(options) if canonical(option) == canonical(value)]
    if len(matches) == 1:
        return {'option': matches[0], 'method': 'unique_exact_option_text'}
    return {'option': None, 'method': 'unresolved'}
