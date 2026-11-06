"""Small TF-IDF + cosine similarity (no external deps) for internal-link suggestions."""
from __future__ import annotations

import math
import re
from collections import Counter

_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
STOPWORDS = set("""
about above after again against all also and any are because been before being below between both but can
could did does doing down during each few for from further had has have having her here hers him his how
into its itself just more most must not now off once only other our ours out over own same she should some
such than that the their theirs them then there these they this those through too under until very was were
what when where which while who whom why will with would you your yours we us may might one two get got
also use used using via per new like make made see find read learn click home page site contact
""".split())


def terms(text: str) -> list[str]:
    return [w for w in (m.lower() for m in _WORD.findall(text)) if w not in STOPWORDS]


class TfIdf:
    def __init__(self, docs: dict[str, str], max_terms: int = 300, boilerplate_df: float = 0.5):
        self.tf: dict[str, Counter] = {k: Counter(terms(v)) for k, v in docs.items()}
        n = max(1, len(self.tf))
        df: Counter = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        if n >= 6:  # terms on most pages are navigation/footer boilerplate, not topics
            common = {t for t, d in df.items() if d / n > boilerplate_df}
            for c in self.tf.values():
                for t in common & c.keys():
                    del c[t]
            for t in common:
                del df[t]
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}
        self.vec: dict[str, dict[str, float]] = {}
        for k, c in self.tf.items():
            total = sum(c.values()) or 1
            w = {t: (cnt / total) * self.idf[t] for t, cnt in c.items()}
            top = dict(sorted(w.items(), key=lambda kv: -kv[1])[:max_terms])
            norm = math.sqrt(sum(x * x for x in top.values())) or 1.0
            self.vec[k] = {t: x / norm for t, x in top.items()}

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.vec.get(a, {}), self.vec.get(b, {})
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(x * vb.get(t, 0.0) for t, x in va.items())

    def top_terms(self, key: str, n: int = 5) -> list[str]:
        return [t for t, _ in sorted(self.vec.get(key, {}).items(), key=lambda kv: -kv[1])[:n]]
