"""Chinese/English BM25, dense search and deterministic reciprocal-rank fusion."""
from collections import Counter
import math
import re


def tokenize(text):
    import jieba
    tokens = []
    for part in re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|[0-9]+', text.lower()):
        tokens.extend(jieba.cut(part, cut_all=False) if re.search(r'[\u4e00-\u9fff]', part) else [part])
    return tokens


def hybrid_search(documents, vectors, query_text, query_vector, scope, cfg):
    import numpy as np
    from .coverage import intersect
    legal = [i for i, doc in enumerate(documents) if intersect(doc['interval'], scope)]
    if not legal:
        return []
    counts = [Counter(tokenize(documents[i]['text'])) for i in legal]
    lengths = [sum(c.values()) for c in counts]
    avg = max(1., sum(lengths)/len(lengths))
    query_tokens = set(tokenize(query_text))
    df = {term: sum(term in c for c in counts) for term in query_tokens}
    lexical = []
    for i, c, length in zip(legal, counts, lengths):
        score = 0.
        for term in query_tokens:
            f = c[term]
            if f:
                idf = math.log(1+(len(counts)-df[term]+.5)/(df[term]+.5))
                score += idf*f*2.2/(f+1.2*(.25+.75*length/avg))
        lexical.append((i, score))
    x, q = np.asarray(vectors), np.asarray(query_vector)
    dense = [(i, float(x[i] @ q / max(1e-12, np.linalg.norm(x[i])*np.linalg.norm(q)))) for i in legal]
    key = lambda row: (-row[1], documents[row[0]]['interval'][0], documents[row[0]]['id'])
    rrf = {i: 0. for i in legal}
    for ranking, top in ((dense, cfg['dense_top_k']), (lexical, cfg['bm25_top_k'])):
        for rank, (i, _) in enumerate(sorted(ranking, key=key)[:top], 1):
            rrf[i] += 1/(cfg['rrf_constant']+rank)
    return [{'id': documents[i]['id'], 'score': score, 'interval': documents[i]['interval']}
            for i, score in sorted(rrf.items(), key=key)]


def frontier_split(candidates, limit=12):
    key = lambda c: (c['priority_class'], c['basis_rank'], c.get('navigation_rank', 0),
                     c['expected_cost'], c['interval'][0], c['id'])
    ordered = sorted(candidates, key=key)
    selected, routes = [], set()
    for c in ordered:
        if c['route'] not in routes and len(selected) < limit:
            selected.append(c)
            routes.add(c['route'])
    selected_ids = {c['id'] for c in selected}
    for c in ordered:
        if c['id'] not in selected_ids and len(selected) < limit:
            selected.append(c)
            selected_ids.add(c['id'])
    return sorted(selected, key=key), [c for c in ordered if c['id'] not in selected_ids]
