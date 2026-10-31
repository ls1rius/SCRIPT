"""Prepare final QA inputs from saved memories without GPU or answer generation."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=_PROJECT_ROOT
sys.path.insert(0,str(ROOT))
from script_core.memory_evidence import prepare_memory_input, qa_inputs
from script_core.typed_memory import attach_observation_types, INFORMATION_TYPES
from script_core.config import digest
from script_core.state import write_json


def preview(run, output, maximum=12, per_fact=2, typed=False, excluded_types=(), cohort=None):
    run=Path(run).resolve();output=Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new output directory for a reproducible preview')
    paths=sorted((run/'runs/script').glob('*/result.json'))
    if not paths:
        raise ValueError('No completed SCRIPT results found')
    if cohort is not None:
        available={p.parent.name:p for p in paths}
        if (not isinstance(cohort,list) or any(not isinstance(uid,str) for uid in cohort)
                or len(set(cohort))!=len(cohort) or not cohort or not set(cohort)<=set(available)):
            raise ValueError('Cohort must contain unique completed question IDs')
        paths=[available[uid] for uid in cohort]
    if excluded_types and not typed:
        raise ValueError('Category filtering requires --typed')
    if not set(excluded_types)<=set(INFORMATION_TYPES):
        raise ValueError('Unknown excluded information type')
    # Resolve each completed-result target before creating output. Input packets,
    # QA memories and accepted visual observations are the only prompt sources;
    # saved predictions and evaluation labels never enter the assembled prompt.
    output.mkdir(parents=True)
    counts=Counter();items=[];type_counts=Counter();visual_type_counts=Counter();field_counts=Counter()
    mode='typed_memory_quotes' if typed else 'memory_quotes'
    included=[t for t in INFORMATION_TYPES if t not in excluded_types]
    for result_path in paths:
        uid=result_path.parent.name
        directory=result_path.resolve().parent
        context_path=directory/'qa_context.json'
        if not context_path.exists():
            context_path=directory/'partial_temporal/qa_context.json'
        packet=json.loads((run/'inputs'/f'{uid}.json').read_text())
        context=json.loads(context_path.read_text())
        # Old paired runs saved accepted visual observations on their result;
        # read only that field, never the answer or its explanation.
        visual=json.loads(result_path.read_text()).get('supplemental_visual',{}).get('observations',[])
        memory=context['context'];selected=context['selected_seconds']
        if typed:
            memory_path=context_path.parent/'network.json'
            source_memory=json.loads(memory_path.read_text())
            memory=attach_observation_types(memory,source_memory)
        final_memory,final_visual,audit=prepare_memory_input(packet['captions'],selected,memory,visual,
            maximum,per_fact,mode,included)
        query=dict(packet['question'],id=packet['question']['question_id'])
        prompt=qa_inputs(query,final_memory,audit,final_visual)
        write_json(output/'inputs'/f'{uid}.json',dict(qa_input=prompt,
            final_memory=final_memory,visual_information=final_visual,
            evidence_selection=audit,source_context=str(context_path),
            source_context_hash=digest(context),source_packet_hash=digest(packet)))
        if typed:
            types=audit['information_types']
            type_counts.update(types['retained_caption_counts'])
            visual_type_counts.update(types['retained_visual_counts'])
            field_counts.update(types['attribute_fields'])
            counts['questions_with_cross_type_source_overlap']+=bool(types['retained_sources_overlapping_excluded_types'])
        counts.update(questions=1,original_selected_captions=len(selected),
            quoted_captions=len(audit['quotes']),empty_memories=int(audit['empty_memory']),
            without_quotes=int(audit['no_supporting_quotes']),accepted_visual_observations=len(visual))
        items.append(dict(question_id=uid,quote_count=len(audit['quotes']),
            empty_memory=audit['empty_memory'],without_quotes=audit['no_supporting_quotes'],
            information_types=audit.get('information_types')))
    summary=dict(source_run=str(run),mode=mode,max_quotes=maximum,quotes_per_fact=per_fact,
        counts=dict(counts),questions=items,
        caption_information_counts=dict(type_counts),visual_information_counts=dict(visual_type_counts),
        attribute_fields=dict(field_counts),included_types=included if typed else None,
        inference_calls=0,accuracy=None,
        note='CPU prompt preview only; no new answers, retrieval, extraction or visual checks.')
    write_json(output/'summary.json',summary)
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--max-quotes',type=int,default=12)
    parser.add_argument('--quotes-per-fact',type=int,default=2)
    parser.add_argument('--typed',action='store_true',help='Separate actions, attributes, states and other information types')
    parser.add_argument('--exclude-type',choices=INFORMATION_TYPES,action='append',default=[],
        help='Omit a category from typed prompt inputs; report shared-source overlap')
    parser.add_argument('--cohort',type=Path,
        help='Saved preview summary.json or a JSON list of IDs; fixes paired comparisons')
    args=parser.parse_args(argv)
    cohort=None
    if args.cohort:
        saved=json.loads(args.cohort.read_text())
        cohort=[row['question_id'] for row in saved['questions']] if isinstance(saved,dict) else saved
    summary=preview(args.run,args.out,args.max_quotes,args.quotes_per_fact,args.typed,args.exclude_type,cohort)
    print(json.dumps({k:v for k,v in summary.items() if k!='questions'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
