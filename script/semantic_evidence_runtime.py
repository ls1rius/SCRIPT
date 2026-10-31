"""Shared source-only extraction checks for controlled evidence experiments."""
from script_core.semantic_memory import normalize, view, COLLECTIONS, binding_supported, clean_unit


def probe_bundle():
    sentences=['#C C wipes the red book with a cloth.',
        '#C C draws on the drawing book with a pencil.', '#C The book is wet.',
        '#C C moves the book from the shelf into the bag with her left hand.',
        '#C The book changes from wet to dry.', '#C The machine produces a basket.',
        '#C C wipes the book then C moves the book.', '#C C never wipes the book.',
        '#C The book is made of bamboo.', '#C The book is on the table.']
    return dict(units=[dict(id=f'U{i}',text=t,source_type='caption',status='caption_asserted',
        source_caption_ids=[f'C{i}'],source_frame_ids=[],source_intervals=[[i,i+1]]) for i,t in enumerate(sentences)],
        read_caption_ids=[f'C{i}' for i in range(len(sentences))],structural_memory={})


def functional_gate(memory,audit):
    failures=[]
    sources={u['id']:clean_unit(u)['text'] for u in probe_bundle()['units']}
    for kind,collection in COLLECTIONS.items():
        for f in memory[collection]:
            fact=dict(kind=kind,**{k:v for k,v in f.items() if k not in ('kind','id')})
            if fact.get('source_id') not in sources:
                failures.append('invalid_fact_source:'+collection);continue
            valid,reason=binding_supported(fact,sources[fact['source_id']])
            if not valid:failures.append('invalid_probe_fact:'+collection+':'+reason)
    for name in COLLECTIONS.values():
        if not memory[name]:failures.append('missing_category:'+name)
    def canonical(value):
        words=value.lower().strip().rstrip('.').split()
        if words and words[0] in ('a','an','the'):words=words[1:]
        return ' '.join(words)
    def has(collection,source_id,**fields):
        return any(f.get('source_id')==source_id and all(canonical(f.get(k,''))==canonical(v)
            for k,v in fields.items()) for f in memory[collection])
    checks=[
        ('wipe_actor_patient_instrument','actions','U0',dict(actor='C',verb='wipes',patient='red book',instrument='cloth')),
        ('drawing_surface_instrument','actions','U1',dict(actor='C',verb='draws',surface='drawing book',instrument='pencil')),
        ('origin_destination_body_part','actions','U3',dict(actor='C',verb='moves',patient='book',source_location='shelf',destination='bag',body_part='left hand')),
        ('color_attribute','attributes','U0',dict(entity='book',property='color',value='red')),
        ('material_attribute','attributes','U8',dict(entity='book',property='material',value='bamboo')),
        ('temporary_wetness','states','U2',dict(entity='book',dimension='wetness',value='wet')),
        ('explicit_change','changes','U4',dict(entity='book',before='wet',after='dry')),
        ('explicit_result','results','U5',dict(entity='machine',predicate='produces',value='basket')),
        ('explicit_event_order','event_links','U6',dict(left_event='C wipes the book',connector='then',right_event='C moves the book',link_type='before')),
    ]
    for name,collection,uid,fields in checks:
        if not has(collection,uid,**fields):failures.append(name)
    if not any(has('relations','U9',left='book',predicate=p,right='table',relation_type='spatial')
            for p in ('on','is on')):failures.append('explicit_spatial_relation')
    negative=probe_bundle()['units'][7]['text'].removeprefix('#C ')
    if not any(r['source_id']=='U7' and r['text']==negative for r in memory['residuals']):failures.append('negative_clause')
    if any(f.get('source_id')=='U7' for name in COLLECTIONS.values() for f in memory[name]):
        failures.append('negative_clause_has_positive_fact')
    if audit['batch_failures']:failures.append('extraction_format_or_budget_failure')
    for kind in COLLECTIONS:
        coverage=audit.get('category_coverage',{}).get(kind,{})
        if coverage.get('checked_units')!=len(probe_bundle()['units']) or coverage.get('failed_units',0) or coverage.get('unattempted_units',0):
            failures.append('incomplete_category_check:'+kind)
    return dict(passed=not failures,failures=failures,benchmark_accuracy_used=False)
