"""Question-only scope guards and budget-aware, temporally spread inspection."""
from copy import deepcopy
import re


def observation_focus(task):
    """Carry query distinctions as questions to inspect, never as observed facts."""
    return {'question': task.get('question', task['goal']),
            'options': deepcopy(task.get('options', [])),
            'acceptance_checks': deepcopy(task.get('acceptance_checks', [])),
            'prior_observation_gaps': deepcopy(task.get('observation_gaps', []))}


def query_observation_gaps(memory, query_id):
    """Time-local observation limits, kept distinct from positive/negative facts."""
    return [{'interval': list(record['interval']), 'reason': gap}
            for record in memory.tables['build_records'].values() if record.get('query_id') == query_id
            for gap in record.get('observation_gaps', [])]


def option_distinctions(options):
    """Keep the differing clauses, including an option that adds no extra event."""
    tokens = [re.findall(r'\w+|[^\w\s]', text) for text in options]
    if not tokens:
        return []
    start = 0
    while all(len(t) > start for t in tokens) and len({t[start].casefold() for t in tokens}) == 1:
        start += 1
    end = 0
    while all(len(t)-start > end for t in tokens) and len({t[-end-1].casefold() for t in tokens}) == 1:
        end += 1
    result = []
    for i, parts in enumerate(tokens):
        difference = parts[start:len(parts)-end if end else len(parts)]
        meaningful = any(re.search(r'\w', t) for t in difference)
        result.append({'option_id': str(i), 'difference_text': ' '.join(difference) if meaningful else '',
                       'shared_steps_only': not meaningful})
    return result


def option_check_rows(gate):
    checks = gate.get('option_checks', [])
    if isinstance(checks, dict):
        return [dict(check, option_id=key) for key, check in checks.items()]
    return checks


def novel_claim_ids(memory, claim_ids):
    """Repeated wording at another time is coverage, but not a new distinction."""
    normalize = lambda text: ' '.join(re.findall(r'\w+', text.casefold()))
    proposed = set(claim_ids)
    known = {normalize(c.get('description', '')) for c in memory.tables['claims'].values()
             if c['id'] not in proposed and memory.is_valid(c['id'])}
    novel = []
    for ref in claim_ids:
        text = normalize(memory.get(ref).get('description', ''))
        if text and text not in known:
            novel.append(ref)
            known.add(text)
    return novel


def strict_question(question):
    text = question.lower()
    text = re.sub(r'\beach other\b', '', text)
    # "Summarize without listing all actions" explicitly rejects enumeration.
    text = re.sub(r"\b(?:without|rather than|instead of|do not|don't|don’t) (?:simply )?(?:list(?:ing)?|enumerat(?:e|ing)) (?:all|every|each)\b", '', text)
    text = re.sub(r'(?:不要|无需|不必)(?:逐一)?(?:列出|列举)(?:所有|全部|每一)', '', text)
    # Describing the purpose/use of each item is not exhaustive temporal
    # coverage. Explicit all/count/last/absence wording still survives below.
    text = re.sub(r'\b((?:purpose|significance|importance|role|usage|use|function|contribution) of) each\b', r'\1 the', text)
    if re.search(r'\b(recurring|repeated|repetitive) (?:steps|actions|pattern|process)\b', text):
        text = re.sub(r'\bprocessing each\b', 'processing a representative', text)
    return bool(re.search(r'\b(how many|count|all|every|each|last|latest|never|absence)\b|多少|计数|每一|所有|全部|最后|从未|是否没有', text))


def speech_question(question):
    return bool(re.search(r'\b(speech|spoken words|transcript|what .{0,20}say|what .{0,20}said|quote)\b|说了什么|原话|讲话内容|演讲', question.lower()))


def overview_question(question):
    text = question.lower()
    # Explicit exhaustive, counting, absence and terminal questions keep their proofs.
    if strict_question(question):
        return False
    if re.search(r'\bwhat (?:is the )?(?:colou?r|material)\b|什么颜色|什么材质', text):
        return False
    return bool(re.search(
        r'\b(throughout|overall|entire|central theme|recurring|repeatedly|pattern|multitasking|turning points?|'
        r'summarize|summarise|overarching|progression|transformation|key steps|key stages|key actions|concurrently|high.level transition|transition from)\b'
        r'|\b(?:primary|main|final) (?:\w+ ){0,2}(?:activity|activities|tasks?|objective|goal|purpose|reason|process|dish|components|tools|steps|focus|theme)\b'
        r'|\b(?:purpose|significance|role|contribute|facilitated)\b'
        r'|\bwhy\b[^?]{0,180}\b(?:essential|significant)\b'
        r'|sequence (?:in which|of actions|of interactions)|additional activity|总结|概括|主要|整体|全过程|贯穿|反复|转折|同时在做', text))


def normalize_plan(report, query):
    report = deepcopy(report)
    text_only = speech_question(query['question'])
    local_attribute = bool(re.search(r'\bwhat (?:is the )?(?:colou?r|material)\b|什么颜色|什么材质', query['question'].lower()))
    model_overview = (report.get('task_type') in ('overview', 'temporal_overview')
                      and not strict_question(query['question']) and not local_attribute)
    if (overview_question(query['question']) or model_overview) and not text_only:
        report['requirements'] = [{'key': 'overview', 'predicate': 'other', 'roles': {},
            'scope': list(query['scope']), 'quantifier': 'global', 'prerequisites': [],
            'acceptable_modalities': ['visual'], 'answer_slot': 'temporal_pattern' if report.get('task_type') == 'temporal_overview' or re.search(
                r'\b(recurring|repeatedly|pattern|turning points?|sequence|progression|transition)\b|反复|转折|顺序', query['question'].lower())
                else 'activity_and_context'}]
        report['variables'] = []
    else:
        # Duplicate temporary names are repairable only when nobody depends on them.
        requirements = report['requirements']
        keys = [r.get('key') for r in requirements]
        if len(set(keys)) != len(keys) and not any(r['prerequisites'] for r in requirements):
            for i, requirement in enumerate(requirements):
                requirement['key'] = f'requirement_{i}'
        # Roles already name these variables. Declaring a missing symbol does not
        # bind an entity or assert a fact; keep it explicitly unconstrained.
        variables = {}
        for variable in report['variables']:
            name = variable['name']
            if name not in variables:
                variables[name] = variable
            else:
                variables[name]['constraints'] = list(dict.fromkeys(
                    variables[name]['constraints'] + variable['constraints']))
                if variable['description'] != variables[name]['description']:
                    variables[name]['description'] += '; ' + variable['description']
        for requirement in requirements:
            for name in requirement.get('roles', {}).values():
                if name not in variables:
                    variables[name] = {'name': name, 'description': f'{name} (unbound query variable)', 'constraints': []}
        # A parser may reuse its placeholder for actor/object or object/location.
        # Separate query symbols without asserting any entity, identity or fact.
        # Keep genuine local/count/last requirements and all their prerequisites.
        from .schemas import VARIABLES
        reserved = set(variables)
        replacements = {}
        for requirement in requirements:
            if requirement['predicate'] == 'other':
                continue
            used = set()
            for role, name in requirement.get('roles', {}).items():
                if role == 'content':
                    continue
                if name in used:
                    key = (name, role)
                    replacement = replacements.get(key)
                    if replacement is None or replacement in used:
                        replacement = next((v for v in [role, *VARIABLES]
                                            if v in VARIABLES and v not in reserved), None)
                        if replacement is None:
                            raise ValueError('No distinct unbound variable available for parser roles')
                        replacements[key] = replacement
                        reserved.add(replacement)
                        variables[replacement] = {'name': replacement,
                            'description': f'Unbound {role} in the question', 'constraints': []}
                    requirement['roles'][role] = replacement
                    name = replacement
                used.add(name)
        report['variables'] = list(variables.values())
    return report


def retain_plan_context(repaired, original):
    """A structural JSON repair must not silently erase task classification."""
    repaired = deepcopy(repaired)
    for key in ('task_type', 'evidence_gaps'):
        if key not in repaired and key in original:
            repaired[key] = deepcopy(original[key])
    return repaired


def plan_uncertainties(report, question):
    """Keep only grounded textual ambiguities; missing observations are leads."""
    blocking, leads = [], list(report.get('evidence_gaps', []))
    for item in report.get('ambiguities', []):
        if (isinstance(item, dict) and item.get('kind') in ('reference', 'scope')
                and item.get('question_span', '').strip()
                and item['question_span'].lower() in question.lower()
                and len(set(item.get('alternatives', []))) == 2):
            blocking.append(item)
        else:
            leads.append(item if isinstance(item, str) else item.get('description', str(item)))
    return blocking, list(dict.fromkeys(leads))


def checked_ambiguities(gate, ambiguities, claims):
    if not ambiguities:
        return True
    valid = {c['id'] for c in claims}
    checks = gate.get('ambiguity_checks', [])
    return (len(checks) == len(ambiguities)
        and {c['ambiguity_index'] for c in checks} == {str(i) for i in range(len(ambiguities))}
        and all(c['resolved'] and c['reason'].strip() and c['claim_ids']
                and set(c['claim_ids']) <= valid for c in checks))


def spread_order(values):
    """Inspect beginning, middle and end before filling the largest remaining gaps."""
    values = list(values)
    chosen = []
    remaining = set(range(len(values)))
    for index in (0, len(values)//2, len(values)-1):
        if index in remaining:
            chosen.append(index)
            remaining.remove(index)
    while remaining:
        index = max(remaining, key=lambda i: (min(abs(values[i]-values[j]) for j in chosen), -i))
        chosen.append(index)
        remaining.remove(index)
    return chosen


def global_checkpoints(scope, cfg):
    # Leave two existing rounds for discriminating observations or recovery.
    bins = min(cfg['coverage']['global_summary_time_bins'], max(1, cfg['query']['global_task_limit']-2))
    start, end = scope
    return [start+(i+.5)*(end-start)/bins for i in range(bins)]


def checked_choice(gate, options, claims):
    """A model's passed flag alone cannot establish a multiple-choice distinction."""
    if not options:
        return True
    ids = {str(i) for i in range(len(options))}
    checks = option_check_rows(gate)
    if len(checks) != len(ids) or {c['option_id'] for c in checks} != ids:
        return False
    supported = [c for c in checks if c['result'] == 'supported']
    valid = {c['id'] for c in claims}
    return (len(supported) == 1 and gate.get('selected_option') == supported[0]['option_id']
            and all(c['result'] in ('supported', 'refuted') and c['claim_ids']
                    and c.get('basis') == ('direct_support' if c['result'] == 'supported' else 'direct_contradiction')
                    and not (c['result'] == 'refuted' and re.search(
                        r'\b(?:no evidence|not (?:shown|observed|seen)|no (?:indication|sign) of|absence of evidence)\b|未见|没(?:有)?看到|缺乏证据|没有证据',
                        c['reason'], re.I))
                    and set(c['claim_ids']) <= valid and c['reason'].strip() for c in checks))
