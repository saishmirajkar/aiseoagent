"""Authority table from Common Crawl's domain-level web graph ranks.

The ranks file (several GB gzip, sorted by harmonic-centrality rank) is streamed over HTTPS and
decompressed on the fly; only the top N rows are read, then the download stops. Nothing is
written to disk. Rows go to Postgres with COPY. Only the current and previous release are kept.

Authority Score (0-100, log scale):
    combined_rank = sqrt(harmonic_rank × pagerank_rank)
    score = round(100 × (1 − ln(combined_rank) / ln(REFERENCE)))   clamped to 1..100
with REFERENCE = 100,000,000 by default (≈ the number of domains in the graph): rank 1 → 100,
rank 100 → 75, 10,000 → 50, 1,000,000 → 25. Domains outside the loaded top N have no score
("Low authority (outside top N)").
"""
from __future__ import annotations

import json
import logging
import math
import zlib
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import delete, select, update

from app.config import ssl_context
from app.db.models import CCRelease, DomainAuthority
from app.db.session import get_engine, session_scope

log = logging.getLogger(__name__)
GRAPHINFO = "https://index.commoncrawl.org/graphinfo.json"
BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
SOURCE_LABEL = "Common Crawl"


def ranks_url(release: str) -> str:
    return f"{BASE}/{release}/domain/{release}-domain-ranks.txt.gz"


def score(harmonic_pos: int, pagerank_pos: int, reference: int = 100_000_000) -> int:
    combined = math.sqrt(max(1, harmonic_pos) * max(1, pagerank_pos))
    return max(1, min(100, round(100 * (1 - math.log(combined) / math.log(reference)))))


def rev_to_domain(host_rev: str) -> str:
    return ".".join(reversed(host_rev.strip().split(".")))


def candidate_releases(today: date | None = None, months_back: int = 18) -> list[str]:
    """Release ids in Common Crawl's naming scheme, newest first (e.g. cc-main-2025-26-nov-dec-jan)."""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months_back):
        # window ending in month m: (m-2, m-1, m)
        idx = [(m - 3 + k) % 12 for k in range(3)]
        start_year = y if m >= 3 else y - 1
        names = [MONTHS[i] for i in idx]
        crosses = idx[0] > idx[2]
        yy = f"{start_year}-{str(start_year + 1)[2:]}" if crosses else f"{y}"
        out.append(f"cc-main-{yy}-{'-'.join(names)}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def latest_release(client: httpx.Client) -> str | None:
    try:
        r = client.get(GRAPHINFO, timeout=20)
        if r.status_code == 200:
            ids = [g["id"] for g in r.json() if str(g.get("id", "")).startswith("cc-main")]
            if ids:
                return ids[0]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.info("graphinfo.json unavailable (%s); probing release names", type(e).__name__)
    for rel in candidate_releases():
        try:
            if client.head(ranks_url(rel), timeout=15).status_code == 200:
                return rel
        except httpx.HTTPError:
            continue
    return None


def current_release() -> str | None:
    with session_scope() as db:
        return db.execute(select(CCRelease.release).where(CCRelease.is_current.is_(True))).scalar_one_or_none()


def load_release(release: str | None, top_n: int, reference: int = 100_000_000,
                 progress=None, chunk_rows: int = 20_000) -> dict:
    """Stream the ranks file and load the top N domains. Returns {release, rows}."""
    with httpx.Client(verify=ssl_context(), follow_redirects=True,
                      headers={"User-Agent": "SEOInspector authority loader"}) as client:
        release = release or latest_release(client)
        if not release:
            raise RuntimeError("Could not find a Common Crawl web-graph release")
        with session_scope() as db:
            db.execute(delete(DomainAuthority).where(DomainAuthority.release == release))
            db.merge(CCRelease(release=release, status="loading", rows=0, top_n=top_n, is_current=False,
                               started_at=datetime.now(timezone.utc), finished_at=None, error=None))
        rows = 0
        try:
            with client.stream("GET", ranks_url(release), timeout=httpx.Timeout(60, read=120)) as resp:
                resp.raise_for_status()
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                buf = b""
                raw = get_engine().raw_connection()
                try:
                    cur = raw.cursor()
                    with cur.copy("COPY domain_authority (release, domain, harmonic_pos, pagerank_pos, score) "
                                  "FROM STDIN") as cp:
                        for chunk in resp.iter_bytes(1 << 16):
                            buf += dec.decompress(chunk)
                            *lines, buf = buf.split(b"\n")
                            for line in lines:
                                if not line or line.startswith(b"#"):
                                    continue
                                parts = line.decode("utf-8", "replace").split("\t")
                                if len(parts) < 5:
                                    continue
                                h, pr = int(parts[0]), int(parts[2])
                                cp.write_row((release, rev_to_domain(parts[4])[:255], h, pr, score(h, pr, reference)))
                                rows += 1
                                if rows >= top_n:
                                    break
                            if progress and rows and rows % 100_000 < chunk_rows:
                                progress(rows)
                            if rows >= top_n:
                                break  # stop downloading: we only need the top N
                    raw.commit()
                finally:
                    raw.close()
        except Exception as e:
            with session_scope() as db:
                db.execute(update(CCRelease).where(CCRelease.release == release).values(
                    status="failed", error=f"{type(e).__name__}: {e}"[:1000], finished_at=datetime.now(timezone.utc)))
                db.execute(delete(DomainAuthority).where(DomainAuthority.release == release))
            raise
        with session_scope() as db:
            db.execute(update(CCRelease).values(is_current=False))
            db.execute(update(CCRelease).where(CCRelease.release == release).values(
                status="ready", rows=rows, is_current=True, finished_at=datetime.now(timezone.utc)))
            keep = db.execute(select(CCRelease.release).where(CCRelease.status == "ready")
                              .order_by(CCRelease.finished_at.desc()).limit(2)).scalars().all()
            old = db.execute(select(CCRelease.release).where(CCRelease.release.not_in(keep))).scalars().all()
            for rel in old:
                db.execute(delete(DomainAuthority).where(DomainAuthority.release == rel))
                db.execute(delete(CCRelease).where(CCRelease.release == rel))
        log.info("Authority table: loaded %s rows from %s", rows, release)
        return {"release": release, "rows": rows}


def _candidates(domain: str) -> list[str]:
    labels = domain.lower().strip(".").split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def lookup(domains: list[str]) -> dict[str, dict | None]:
    """Authority for each domain (registered-domain fallback: blog.example.co.uk → example.co.uk)."""
    with session_scope() as db:
        rels = db.execute(select(CCRelease).where(CCRelease.status == "ready")
                          .order_by(CCRelease.finished_at.desc()).limit(2)).scalars().all()
        if not rels:
            return {d: None for d in domains}
        cur = rels[0]
        prev = rels[1] if len(rels) > 1 else None
        cand = {d: _candidates(d) for d in domains}
        allc = {c for cs in cand.values() for c in cs}
        rows = db.execute(select(DomainAuthority).where(
            DomainAuthority.release.in_([r.release for r in rels]), DomainAuthority.domain.in_(allc))).scalars().all()
        found = {(r.release, r.domain): r for r in rows}
        out: dict[str, dict | None] = {}
        for d, cs in cand.items():
            hit = next((found[(cur.release, c)] for c in cs if (cur.release, c) in found), None)
            if hit is None:
                out[d] = {"score": None, "label": f"Low authority (outside top {cur.top_n:,})",
                          "release": cur.release}
                continue
            p = found.get((prev.release, hit.domain)) if prev else None
            out[d] = {"score": hit.score, "harmonic_rank": hit.harmonic_pos, "pagerank_rank": hit.pagerank_pos,
                      "matched_domain": hit.domain, "release": cur.release,
                      "previous_score": p.score if p else None}
        return out


def status() -> dict:
    with session_scope() as db:
        rels = db.execute(select(CCRelease).order_by(CCRelease.started_at.desc())).scalars().all()
        return {"releases": [{"release": r.release, "status": r.status, "rows": r.rows, "top_n": r.top_n,
                              "current": r.is_current, "error": r.error,
                              "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rels]}


def source_label(release: str | None) -> str:
    return f"{SOURCE_LABEL} {release}" if release else SOURCE_LABEL


def dumps(x) -> str:
    return json.dumps(x, default=str)
