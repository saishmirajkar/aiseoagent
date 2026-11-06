import { useEffect, useMemo, useState } from "react";
import { api, CATEGORY_LABELS, Finding, fixCode, fmtEvidence, post } from "../api";

const RUNNING = ["queued", "running", "cancel_requested"];

export default function AuditView({ id }: { id: number }) {
  const [a, setA] = useState<any>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"overview" | "findings" | "pages">("overview");

  useEffect(() => {
    let stop = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const data = await api(`/api/audits/${id}`);
        if (stop) return;
        setA(data);
        if (RUNNING.includes(data.status)) timer = window.setTimeout(tick, 2000);
      } catch (ex: any) {
        setErr(ex.message);
      }
    };
    tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [id]);

  if (err) return <p className="error">{err}</p>;
  if (!a) return <p className="muted">Loading…</p>;

  return (
    <>
      <div className="row between wrapline">
        <div>
          <h1>{a.domain}</h1>
          <div className="muted">{a.url} · audit #{a.id} · <span className={`badge st-${a.status}`}>{a.status}</span></div>
        </div>
        {a.status === "completed" && (
          <div className="row gap">
            <a className="button" href={`/api/audits/${id}/report.html`} target="_blank" rel="noreferrer">HTML report</a>
            <a className="button" href={`/api/audits/${id}/report.csv`}>CSV</a>
            <a className="button" href={`/api/audits/${id}/report.json`}>JSON</a>
          </div>
        )}
      </div>
      {RUNNING.includes(a.status) && <Progress a={a} />}
      {a.status === "failed" && <p className="error card">Audit failed: {a.error}</p>}
      {a.status === "cancelled" && <p className="muted card">This audit was cancelled.</p>}
      {a.status === "completed" && (
        <>
          <div className="tabs">
            {(["overview", "findings", "pages"] as const).map((t) => (
              <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
          {tab === "overview" && <Overview a={a} onShowFindings={() => setTab("findings")} />}
          {tab === "findings" && <Findings auditId={id} />}
          {tab === "pages" && <Pages auditId={id} />}
        </>
      )}
    </>
  );
}

function Progress({ a }: { a: any }) {
  const p = a.progress || {};
  const crawl = p.crawl || {};
  const modules = Object.entries(p.modules || {}) as [string, any][];
  const pct = crawl.limit ? Math.min(100, Math.round((100 * (crawl.fetched || 0)) / crawl.limit)) : 0;
  return (
    <div className="card stack">
      <div className="row between">
        <b>Stage: {p.stage || a.status}</b>
        <button onClick={() => post(`/api/audits/${a.id}/cancel`)}>Cancel</button>
      </div>
      <div className="muted ellipsis">{p.message}</div>
      {crawl.limit && (
        <div>
          <div className="bar"><div style={{ width: `${pct}%` }} /></div>
          <div className="muted small">
            {crawl.fetched || 0} / {crawl.limit} pages fetched · {crawl.rendered || 0} rendered with JS
          </div>
        </div>
      )}
      <div className="modgrid">
        {modules.sort((x, y) => x[1].number - y[1].number).map(([k, m]) => (
          <div key={k} className={`mod mod-${m.status}`}>
            <span>{m.number}. {m.name}</span>
            <span className="small">{m.status === "planned" ? `planned (phase ${m.planned_phase})` : m.status}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Overview({ a, onShowFindings }: { a: any; onShowFindings: () => void }) {
  const cats = (a.scores?.categories || {}) as Record<string, any>;
  const counts = a.summary?.status_counts || {};
  return (
    <>
      <div className="cards">
        <div className="card score-card">
          <div className="label">Overall</div>
          <div className="score">{a.scores?.overall ?? "–"}</div>
        </div>
        <div className="card score-card">
          <div className="label">Coverage</div>
          <div className="score">{a.coverage ?? 0}%</div>
          <div className="small muted">of the audit actually measured</div>
        </div>
        {Object.keys(CATEGORY_LABELS).filter((k) => cats[k]).map((k) => [k, cats[k]] as [string, any]).map(([k, c]) => (
          <div className="card score-card" key={k}>
            <div className="label">{CATEGORY_LABELS[k] || k}</div>
            <div className="score">{c.score ?? "–"}</div>
            <div className="small muted">coverage {c.coverage}%</div>
          </div>
        ))}
      </div>
      <p className="muted small">
        {Object.entries(counts).map(([k, v]) => <span key={k} className={`badge s-${k.replace("/", "")}`}>{k} {String(v)}</span>)}
        {" "}NOT_CHECKED items are excluded from scores and reduce coverage instead.
      </p>

      <h2>Missing on this site</h2>
      {a.missing?.length ? <ul className="missing">{a.missing.map((m: string) => <li key={m}>{m}</li>)}</ul>
        : <p className="muted">Nothing essential missing among the checks run.</p>}

      <h2>Top 10 fixes</h2>
      {a.top_fixes?.length ? (
        <ol className="top">
          {a.top_fixes.map((f: Finding) => (
            <li key={f.id}>
              <span className={`badge s-${f.status}`}>{f.status}</span> <b>{f.title}</b>
              <span className="muted"> · {f.affected}{f.total ? `/${f.total}` : ""} affected · impact {f.impact} · effort {f.effort}</span>
              <div className="muted">{f.summary}</div>
              <div>{f.fix}</div>
            </li>
          ))}
        </ol>
      ) : <p className="muted">No issues found.</p>}
      <button className="link" onClick={onShowFindings}>See all findings →</button>
      <p className="muted small">
        API usage for this audit: ${a.cost?.total_usd ?? 0} total · ${a.cost?.llm_usd ?? 0} LLM · {a.cost?.search_queries ?? 0} search
        queries · {a.cost?.pagespeed_calls ?? 0} PageSpeed calls (free; cached results not counted).
      </p>
    </>
  );
}

export function FindingRow({ f, open: initial = false }: { f: Finding; open?: boolean }) {
  const [open, setOpen] = useState(initial);
  return (
    <div className="finding">
      <div className="fhead clickable" onClick={() => setOpen(!open)}>
        <span className={`badge s-${f.status.replace("/", "")}`}>{f.status}</span>
        <b>{f.title}</b>
        <span className="muted small">
          {f.module_number}. {f.module_name || f.module}
          {(f.status === "FAIL" || f.status === "WARNING") && ` · ${f.affected}${f.total ? `/${f.total}` : ""} affected · impact ${f.impact} · effort ${f.effort}`}
        </span>
      </div>
      {open && (
        <div className="fbody">
          {f.reason && <p><b>Not measured:</b> {f.reason}</p>}
          {f.summary && <p>{f.summary}</p>}
          {(f.status === "FAIL" || f.status === "WARNING") && (
            <p><b>Why it matters:</b> {f.why}<br /><b>Fix:</b> {f.fix}</p>
          )}
          {f.generated_fix && <><div className="label">Generated fix</div><pre>{fixCode(f.generated_fix)}</pre></>}
          {f.items.length > 0 && (
            <div className="scroll">
              <table>
                <thead><tr><th>URL</th><th>Evidence</th><th>Generated fix</th></tr></thead>
                <tbody>
                  {f.items.slice(0, 100).map((it, i) => (
                    <tr key={i}>
                      <td className="url">{it.url || "—"}</td>
                      <td className="ev">{fmtEvidence(it.evidence)}</td>
                      <td>{it.generated_fix && <pre>{fixCode(it.generated_fix)}</pre>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(f.items.length > 100 || f.items_truncated > 0) && (
                <p className="muted small">Showing 100 of {f.items.length + f.items_truncated}. Use the CSV export for all.</p>
              )}
            </div>
          )}
          {f.sources?.length > 0 && <div className="small muted">Source: {f.sources.join(", ")}</div>}
        </div>
      )}
    </div>
  );
}

function Findings({ auditId }: { auditId: number }) {
  const [all, setAll] = useState<Finding[]>([]);
  const [cat, setCat] = useState("");
  const [status, setStatus] = useState("FAIL,WARNING");
  const [q, setQ] = useState("");

  useEffect(() => {
    api<Finding[]>(`/api/audits/${auditId}/findings`).then(setAll);
  }, [auditId]);

  const shown = useMemo(() => all.filter((f) =>
    (!cat || f.category === cat) &&
    (!status || status.split(",").includes(f.status)) &&
    (!q || (f.title + f.check_id + (f.summary || "") + f.items.map((i) => i.url).join(" ")).toLowerCase().includes(q.toLowerCase()))),
  [all, cat, status, q]);

  return (
    <>
      <div className="row gap wrapline filters">
        <select value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">All categories</option>
          {Object.entries(CATEGORY_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="FAIL,WARNING">Issues (FAIL + WARNING)</option>
          <option value="FAIL">FAIL</option>
          <option value="WARNING">WARNING</option>
          <option value="PASS">PASS</option>
          <option value="NOT_CHECKED">NOT_CHECKED</option>
          <option value="">All statuses</option>
        </select>
        <input className="grow" placeholder="Search checks or URLs" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <p className="muted small">{shown.length} of {all.length} checks</p>
      {shown.map((f) => <FindingRow key={f.id} f={f} />)}
    </>
  );
}

function Pages({ auditId }: { auditId: number }) {
  const [data, setData] = useState<{ total: number; pages: any[] }>({ total: 0, pages: [] });
  const [q, setQ] = useState("");
  const [only, setOnly] = useState("");

  useEffect(() => {
    const t = setTimeout(() => {
      const params = new URLSearchParams({ limit: "500" });
      if (q) params.set("q", q);
      if (only) params.set("only", only);
      api(`/api/audits/${auditId}/pages?${params}`).then(setData);
    }, 250);
    return () => clearTimeout(t);
  }, [auditId, q, only]);

  return (
    <>
      <div className="row gap wrapline filters">
        <input className="grow" placeholder="Filter by URL or title" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={only} onChange={(e) => setOnly(e.target.value)}>
          <option value="">All pages</option>
          <option value="errors">Errors (4xx/5xx/failed)</option>
          <option value="nonindexable">Non-indexable</option>
        </select>
      </div>
      <p className="muted small">{data.total} pages</p>
      <div className="scroll">
        <table>
          <thead><tr><th>URL</th><th>Status</th><th>Issues</th><th>Depth</th><th>Inlinks</th><th>Words</th><th>Indexable</th><th>Title</th></tr></thead>
          <tbody>
            {data.pages.map((p) => (
              <tr key={p.id} className="clickable" onClick={() => (window.location.hash = `#/audit/${auditId}/page/${p.id}`)}>
                <td className="url">{p.url}</td>
                <td>{p.status ?? "err"}</td>
                <td>{p.issues || ""}</td>
                <td>{p.depth ?? "—"}</td>
                <td>{p.inlinks}</td>
                <td>{p.word_count ?? ""}</td>
                <td>{p.indexable ? "yes" : "no"}</td>
                <td className="ellipsis">{p.title}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
