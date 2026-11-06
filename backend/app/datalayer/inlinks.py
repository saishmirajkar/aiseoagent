"""Imported full referring-domain lists (from notebooks/cc_inlinks.ipynb) and the watchlist."""
from __future__ import annotations

from sqlalchemy import delete, insert, select

from app.db.models import Inlink, InlinkImport, WatchlistDomain
from app.datalayer.authority import release_key
from app.db.session import session_scope

CHUNK = 5000


def norm(domain: str) -> str:
    return domain.strip().lower().removeprefix("www.").rstrip(".")


def import_list(release: str, target: str, sources: list[str]) -> int:
    target = norm(target)
    srcs = sorted({norm(s) for s in sources if s and norm(s) != target})
    with session_scope() as db:
        db.execute(delete(Inlink).where(Inlink.release == release, Inlink.target_domain == target))
        for i in range(0, len(srcs), CHUNK):
            db.execute(insert(Inlink), [{"release": release, "target_domain": target, "source_domain": s}
                                        for s in srcs[i:i + CHUNK]])
        db.execute(delete(InlinkImport).where(InlinkImport.release == release, InlinkImport.target_domain == target))
        db.add(InlinkImport(release=release, target_domain=target, source_count=len(srcs)))
        db.flush()
        # keep only the two most recent releases per target (chronological, not alphabetical)
        rels = db.execute(select(InlinkImport.release).where(InlinkImport.target_domain == target)).scalars().all()
        keep = sorted(set(rels), key=release_key, reverse=True)[:2]
        db.execute(delete(Inlink).where(Inlink.target_domain == target, Inlink.release.not_in(keep)))
        db.execute(delete(InlinkImport).where(InlinkImport.target_domain == target, InlinkImport.release.not_in(keep)))
    return len(srcs)


def referring_domains(target: str) -> dict | None:
    """{release, domains, previous_release, previous_domains} or None when nothing was imported."""
    target = norm(target)
    with session_scope() as db:
        rels = sorted(set(db.execute(select(InlinkImport.release).where(InlinkImport.target_domain == target))
                          .scalars()), key=release_key, reverse=True)[:2]
        if not rels:
            return None
        out = {"release": rels[0], "domains": set(db.execute(select(Inlink.source_domain).where(
            Inlink.release == rels[0], Inlink.target_domain == target)).scalars())}
        out["previous_release"] = rels[1] if len(rels) > 1 else None
        out["previous_domains"] = set(db.execute(select(Inlink.source_domain).where(
            Inlink.release == rels[1], Inlink.target_domain == target)).scalars()) if len(rels) > 1 else None
        return out


def watchlist() -> list[dict]:
    with session_scope() as db:
        rows = db.execute(select(WatchlistDomain).order_by(WatchlistDomain.kind, WatchlistDomain.domain)).scalars().all()
        counts: dict[str, str] = {}
        for t, rel in db.execute(select(InlinkImport.target_domain, InlinkImport.release)).all():
            if t not in counts or release_key(rel) > release_key(counts[t]):
                counts[t] = rel
        return [{"domain": r.domain, "kind": r.kind, "note": r.note, "latest_import": counts.get(r.domain)}
                for r in rows]


def add_to_watchlist(domain: str, kind: str = "client", note: str | None = None) -> None:
    with session_scope() as db:
        db.merge(WatchlistDomain(domain=norm(domain), kind=kind if kind in ("client", "competitor") else "client",
                                 note=note))


def remove_from_watchlist(domain: str) -> None:
    with session_scope() as db:
        db.execute(delete(WatchlistDomain).where(WatchlistDomain.domain == norm(domain)))
