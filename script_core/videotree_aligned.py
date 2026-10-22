"""VideoTree execution with one clustering substitution: contiguous temporal clusters.

The original launch owns width growth, representatives, prompts, retries and scores.
Depth and QA use the original functions too. No CLIP text scoring or review heuristic.
"""
from copy import deepcopy
import sys


def defaults():
    from util import parse_args
    saved=sys.argv
    try:
        sys.argv=['videotree-defaults','--output_filename','unused.json']
        a=parse_args()
    finally:
        sys.argv=saved
    return {k:getattr(a,k) for k in ('init_cluster_num','max_cluster_num','default_adpative_rate',
                                    'iter_threshold','fps','num_words_in_sum','temperature')}


def contiguous_kmeans(X,num_clusters,distance='cosine',device='cpu',**kwargs):
    """Exact fixed-K temporal SSE partition; K is supplied by original VideoTree."""
    import numpy as np
    import torch
    x=X.detach().float().cpu().numpy().astype(np.float64)
    if x.ndim!=2 or not len(x) or not np.isfinite(x).all():
        raise ValueError('Invalid visual feature matrix')
    n=len(x);k=int(num_clusters)
    lengths=np.linalg.norm(x,axis=1,keepdims=True)
    if k<1 or k>n or (lengths<=0).any():raise ValueError('Invalid contiguous cluster count/features')
    z=x/lengths
    prefix=np.vstack([np.zeros(z.shape[1]),z.cumsum(axis=0)])
    costs=np.full((n,n+1),np.inf)
    for a in range(n):
        sums=prefix[a+1:]-prefix[a]
        sizes=np.arange(1,n-a+1)
        costs[a,a+1:]=np.maximum(0,sizes-np.einsum('ij,ij->i',sums,sums)/sizes)
    dp=np.full((k+1,n+1),np.inf);prev=np.full((k+1,n+1),-1,dtype=int)
    dp[0,0]=0
    for count in range(1,k+1):
        for end in range(count,n+1):
            candidates=dp[count-1,count-1:end]+costs[count-1:end,end]
            at=int(np.argmin(candidates))+count-1
            dp[count,end]=candidates[at-count+1];prev[count,end]=at
    bounds=[];end=n
    for count in range(k,0,-1):
        start=int(prev[count,end]);bounds.append((start,end));end=start
    labels=np.empty(n,dtype=np.int64);centers=[]
    for cid,(a,b) in enumerate(reversed(bounds)):
        labels[a:b]=cid
        # Raw mean and original Euclidean representative selector are retained.
        centers.append(X[a:b].mean(dim=0))
    return torch.tensor(labels,device=device),torch.stack(centers).to(device)


def width(inputs,model,device='cuda',seed=0):
    import adaptive_breath_expansion as original
    from .videotree_tools import run_width
    saved=original.kmeans
    try:
        original.kmeans=contiguous_kmeans
        result=run_width(inputs,model,device,seed)
    finally:
        original.kmeans=saved
    labels=result['width']['cluster_ids_x']
    if any(a>b or b>a+1 for a,b in zip(labels,labels[1:])):
        raise ValueError('Continuous width produced noncontiguous scene IDs')
    result['alignment']=dict(substitution='contiguous fixed-K visual clustering only',
        original_width_launch=True,original_representatives=True,original_scoring=True,defaults=defaults())
    return result


def directory(packet,features,width_result):
    """Final-width scene directory; no entities are built at this stage."""
    import torch
    from adaptive_breath_expansion import find_closest_points_per_cluster
    labels=width_result['width']['cluster_ids_x'];n=len(labels)
    if n!=len(packet['captions']) or n!=len(features):raise ValueError('Source grid mismatch')
    scenes=[]
    for cid in sorted(set(labels)):
        indices=[i for i,v in enumerate(labels) if v==cid]
        if indices!=list(range(indices[0],indices[-1]+1)):raise ValueError('Discontinuous scene')
        center=features[indices].mean(dim=0)
        distances=torch.norm(features[indices]-center,dim=1)
        order=sorted(indices,key=lambda i:(float(distances[i-indices[0]]),i))
        scenes.append(dict(id=f'S{cid}',cluster_id=cid,interval=[indices[0],indices[-1]+1],
            center=center.tolist(),representative_order=order,chronological_order=indices,
            caption_ids=[packet['captions'][i]['id'] for i in indices],description=None,
            relevance=width_result['relevance']['cluster_relevance'][cid],status='directory_only'))
    pointers=[]
    # Chronological adjacency and nearest other visual scene; bidirectional hints only.
    centers=torch.nn.functional.normalize(torch.stack([torch.tensor(s['center']) for s in scenes]),dim=1)
    for i,s in enumerate(scenes):
        targets=[(i+1,'temporal_next')] if i+1<len(scenes) else []
        others=[j for j in range(len(scenes)) if abs(i-j)>1]
        if others:targets.append((min(others,key=lambda j:(-float(centers[i]@centers[j]),j)),'similar_scene'))
        for j,kind in targets:
            a,b=sorted((i,j))
            if any(p['source']==f'S{a}' and p['target']==f'S{b}' and p['kind']==kind for p in pointers):continue
            pointers.append(dict(id=f'P{len(pointers)}',source=f'S{a}',target=f'S{b}',kind=kind,
                bidirectional=True,similarity=float(centers[i]@centers[j]),status='navigation_hint_not_identity_or_cause'))
    return dict(schema_version=1,method='videotree_aligned_contiguous',video_id=packet['question']['video_id'],
                scenes=scenes,pointers=pointers,captions=deepcopy(packet['captions']),alignment=width_result['alignment'])


def visit_order(d,selected):
    """All VideoTree-selected scenes are visited; its score orders pointer traversal."""
    active={s['id']:s for s in d['scenes'] if set(s['chronological_order']) & set(selected)}
    order=[]
    while len(order)<len(active):
        frontier=set()
        for p in d['pointers']:
            if p['source'] in order and p['target'] in active and p['target'] not in order:frontier.add(p['target'])
            if p['target'] in order and p['source'] in active and p['source'] not in order:frontier.add(p['source'])
        choices=frontier or (set(active)-set(order))
        sid=min(choices,key=lambda x:(-active[x]['relevance'],active[x]['interval'][0]))
        order.append(sid)
    return order
