"""Online L: H/I source memory followed by L's audited evidence selector."""
from . import i_evidence_planner as core, merged_lazy_memory as merged
from .accounting import metered_cpu
from .state import write_json

MODE = 'planned_lazy_script'
VERSION = 'L-online-I-evidence-planner-v1'
PRESENTATION_VERSION = 'L-frames-adjacent-v1-no-source-label'


def render(pool, plan):
    """Keep L's partition/source audit and the user's compact frame notation."""
    _, audit = core.render(pool, plan)
    records = {r['id']: r for r in pool['records']}
    lines = [core.POLICY]
    for group in audit['groups']:
        statements = []
        for display in group['interval_display']:
            row = records[display['record_id']]
            times = ', '.join('['+','.join(str(t) for t in interval)+')'
                              for interval in display['display_intervals']) or 'time unspecified'
            label = '' if row['qualification'] == 'source assertion' else ' ('+row['qualification']+')'
            statements.append('Frames '+times+label+': '+row['text'])
        lines.append(' '.join(statements))
    audit.update(presentation_version=PRESENTATION_VERSION, interval_label='Frames',
        display_qualifications={rid:records[rid]['qualification']
                               for group in audit['groups'] for rid in group['record_ids']})
    return '\n'.join(lines), audit


@metered_cpu('l_evidence_materialization')
def project_evidence(qa, payload, query, meter, target):
    from script.controlled_memory_io import parts
    target.mkdir(parents=True, exist_ok=True)
    archive = merged.materialize(payload)
    pool = core.build_pool(archive, qa, parts)
    inputs = core.planner_input(query, pool, query.get('id') or query['question_id'])
    write_json(target/'pool.json', pool)
    write_json(target/'planner_input.json', inputs)
    output, _ = meter.call('l_evidence_plan', dict(inputs=inputs, schema=core.plan_schema(pool)))
    write_json(target/'planner_output.json', output)
    plan = core.decode_plan(output['raw'], pool)
    description, audit = render(pool, plan)
    final = core.final_input(qa, description, parts)
    write_json(target/'plan.json', plan)
    write_json(target/'audit.json', audit)
    write_json(target/'final_input.json', final)
    return final, dict(version=VERSION, planner_version=core.VERSION, parent='I',
                       pool_records=len(pool['records']), audit=audit)
