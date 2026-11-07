"""Finite, group-local O+ renderings. No model-generated factual text is accepted."""
import json

POLICY = 'Oplus-source-bound-renderings-v1'
PROMPT = '''Render each selected evidence group independently using only its supplied
identity_text_choices. Choose the code for a faithful short rendering, or empty text
only if the group contains no useful information. These are complete output choices,
not examples to extend. Preserve useful partial evidence and explicit identities.
The question (and optional options) guide relevance only and supply NO facts.
Never borrow a value or a choice from another group. Never add a purpose, frequency,
location, cause, action, participant, explanation or inference. Do not answer the
question. Return {"groups":{"group_id":{"text":"supplied code or empty"}}}.
The caller restores the exact chosen text and joins groups in their source order.
No free-form output is permitted.'''


def renderings(units):
    """Return only lossless renderings; all factual strings come from this group.

    Labels/punctuation are presentation metadata. Roles remain explicitly labelled
    so that, for example, a location cannot silently become a destination. Omission
    is whole-group only: no new actor/object binding or partial negation deletion.
    """
    if not units:
        raise ValueError('Empty composition group')
    if len({u['id'] for u in units}) != len(units):
        raise ValueError('Duplicate composition unit ID')
    if len({u['group'] for u in units}) != 1:
        raise ValueError('Mixed composition groups')
    if all(u['kind'] == 'action_field' for u in units):
        fields = {}
        for u in units:
            if not isinstance(u['value'], str) or not u['value'].strip():
                raise ValueError('Invalid selected action value')
            fields.setdefault(u['field'], []).append(u['value'])
        if set(fields) == {'actor'}:
            identities = fields['actor']
            unknown = {'someone', 'somebody', 'a person', 'an unknown person', 'unknown'}
            return [''] if all(v.lower().strip() in unknown for v in identities) else [
                '', 'Actor: ' + ', '.join(identities) + '.']
        if any(len(v) != 1 for v in fields.values()):
            raise ValueError('Conflicting action fields in one event')
        fields = {k: v[0] for k, v in fields.items()}
        labelled = '; '.join(k + ': ' + v for k, v in fields.items())
        choices = ['', labelled]
        # Only an explicit verb permits a subject/verb/object surface form.
        # The other roles retain their exact names and values, never inferred
        # prepositions, determiners, tense, or grammatical rewrites.
        if 'verb' in fields:
            text = ' '.join(fields[k] for k in ('actor', 'verb', 'patient') if k in fields)
            roles = '; '.join(k + ': ' + v for k, v in fields.items()
                              if k not in ('actor', 'verb', 'patient'))
            if roles:
                text += ' [' + roles + ']'
            choices.append(text)
        return list(dict.fromkeys(choices))
    if len(units) != 1:
        raise ValueError('Non-action composition requires one intact record')
    unit = units[0]
    if unit['kind'] == 'raw_sentence':
        return ['', unit['text']]
    # Keep uncertainty, negation, before/after and event-link fields together.
    # Exact JSON string values also escape any punctuation inside source values.
    return ['', json.dumps(unit['record'], ensure_ascii=False, separators=(',', ':'))]
