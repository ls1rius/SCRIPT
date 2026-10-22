"""Lossless short keys for temporal observation output; stored facts stay canonical."""

KEYS = dict(entities='e', entity_ref='r', label='l', attributes='a', field='k',
            value='v', source_frame_ids='f', observations='o', frame_id='i',
            state='s', continuity='c', cause='u', action='x', actor='p', tool='t',
            basis='b', relations='j', subject='h', object='d', predicate='q',
            update='w', more_entities='m', unresolved='n')
INVERSE = {v: k for k, v in KEYS.items()}


def _keys(value, mapping):
    if isinstance(value, dict):
        return {mapping.get(k, k): _keys(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_keys(v, mapping) for v in value]
    return value


def encode_report(value):
    return _keys(value, KEYS)


def decode_report(value):
    return _keys(value, INVERSE)


def encode_schema(schema):
    """Rename object properties and required keys, never enum/reference values."""
    if isinstance(schema, list):
        return [encode_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    result={}
    for key,value in schema.items():
        if key=='properties':
            result[key]={KEYS.get(k,k): encode_schema(v) for k,v in value.items()}
        elif key=='required':
            result[key]=[KEYS.get(k,k) for k in value]
        else:
            result[key]=encode_schema(value)
    return result
