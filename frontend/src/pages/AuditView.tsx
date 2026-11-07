import { useEffect, useMemo, useState } from "react";
import { Agent, api, Finding, fixCode, fmtEvidence, post } from "../api";

const RUNNING = ["queued", "running", "cancel_requested"];

export default function AuditView({ id }: { id: number }) {
  const [a, setA] = useState<any>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"summary" | "agents" | "pages">("summary");
  const [agent, setAgent] = useState("");

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
            {([["summary", "Summary"], ["agents", "What each agent found"], ["pages", "Pages"]] as const).map(([t, label]) => (
              <button key={t} className={tab === t ? "active" : ""} onClick={() => { setTab(t); setAgent(""); }}>
                {label}
              </button>
            ))}
          </div>
          {tab === "summary" && <Overview a={a} onAgent={(k) => { setAgent(k); setTab("agents"); }} />}
          {tab === "agents" && <Findings auditId={id} agents={a.agents || []} agent={agent} setAgent={setAgent} />}
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

function Overview({ a, onAgent }: { a: any; onAgent: (key: string) => void }) {
  const plain = a.plain || {};
  return (
    <>
      <div className="card hero">
        <div className="big">{a.scores?.overall ?? "–"}<small> / 100</small></div>
        <div>
          <div className="grade">{plain.grade?.label}</div>
          <div>{plain.grade?.sentence}</div>
          <div className="muted small">
            {plain.counts_sentence}
            {a.coverage != null && a.coverage < 100 && ` We could test ${a.coverage}% of what we look at; the rest needs extra access.`}
          </div>
          <div className="legend"><span>🔴 Needs fixing</span><span>🟠 Could be better</span><span>🟢 Looks good</span><span>⚪ Couldn't check</span></div>
        </div>
      </div>
      <p className="muted small">
        The score is like a health check: how many checks your site passed, weighted by how much each one matters.
        It is not your position in Google.
      </p>

      <h2>Meet your agents</h2>
      <div className="agents">
        {(a.agents || []).map((ag: Agent) => (
          <div key={ag.key} className={`card agent t-${ag.tone} clickable`} onClick={() => onAgent(ag.key)}>
            <b><span className="aicon">{ag.icon}</span>{ag.name}</b>
            <div className="muted small">{ag.job}</div>
            <span className={`pill p-${ag.tone}`}>{ag.verdict}</span>
          </div>
        ))}
      </div>

      <h2>Fix these first</h2>
      {a.top_fixes?.length ? (
        <ol className="fixes">
          {a.top_fixes.map((f: Finding) => (
            <li key={f.id} className="card"><PlainBody f={f} showAgent /></li>
          ))}
        </ol>
      ) : <p className="muted">Nothing urgent. Well done!</p>}
      <p className="muted small">
        API usage for this audit: ${a.cost?.total_usd ?? 0} total · ${a.cost?.llm_usd ?? 0} AI · {a.cost?.search_queries ?? 0} web
        searches · {a.cost?.pagespeed_calls ?? 0} speed tests (free; cached results not counted).
      </p>
    </>
  );
}

function PlainBody({ f, showAgent = false }: { f: Finding; showAgent?: boolean }) {
  const problem = f.status === "FAIL" || f.status === "WARNING";
  return (
    <>
      <b>{f.plain_icon} {f.plain_title || f.title}</b>
      {showAgent && <div className="muted small">{f.agent_icon} {f.agent} · {f.plain_status}</div>}
      <div className="kv"><b>What we found</b><span>{f.plain_found || f.summary}</span></div>
      {problem && <div className="kv"><b>Why it matters</b><span>{f.plain_why || f.why}</span></div>}
      {problem && <div className="kv"><b>What to do</b><span>{f.plain_do || f.fix}</span></div>}
      {problem && f.plain_who && <div className="muted small">👤 Who can do it: {f.plain_who}</div>}
    </>
  );
}

export function FindingRow({ f, open: initial = false }: { f: Finding; open?: boolean }) {
  const [tech, setTech] = useState(initial && !f.plain_title);
  const problem = f.status === "FAIL" || f.status === "WARNING";
  return (
    <div className="finding">
      {problem ? (
        <div className="fbody"><PlainBody f={f} /></div>
      ) : (
        <div className="fbody">
          {f.plain_icon} <b>{f.plain_title || f.title}</b>{" "}
          <span className="muted small">· {f.plain_status}{f.status !== "PASS" && `: ${f.plain_found}`}</span>
        </div>
      )}
      <button className="link small" onClick={() => setTech(!tech)}>
        {tech ? "Hide technical details" : "Technical details for your web developer"}
      </button>
      {tech && <TechDetails f={f} />}
    </div>
  );
}

function TechDetails({ f }: { f: Finding }) {
  return (
    <div className="fbody tech">
      <div>
        <span className={`badge s-${f.status.replace("/", "")}`}>{f.status}</span> <b>{f.title}</b>
        <span className="muted small"> {f.check_id}
          {(f.status === "FAIL" || f.status === "WARNING") && ` · ${f.affected}${f.total ? `/${f.total}` : ""} affected · impact ${f.impact} · effort ${f.effort}`}
        </span>
      </div>
      {f.reason && <p><b>Not measured:</b> {f.reason}</p>}
      {f.summary && <p>{f.summary}</p>}
      {(f.status === "FAIL" || f.status === "WARNING") && <p><b>Why:</b> {f.why}<br /><b>Fix:</b> {f.fix}</p>}
      {f.generated_fix && <><div className="label">Ready-made fix</div><pre>{fixCode(f.generated_fix)}</pre></>}
      {f.items.length > 0 && (
        <div className="scroll">
          <table>
            <thead><tr><th>Page</th><th>Details</th><th>Ready-made fix</th></tr></thead>
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
  );
}

const ORDER: Record<string, number> = { FAIL: 0, WARNING: 1, PASS: 2, NOT_CHECKED: 3, "N/A": 4 };

function Findings({ auditId, agents, agent, setAgent }: {
  auditId: number; agents: Agent[]; agent: string; setAgent: (k: string) => void;
}) {
  const [all, setAll] = useState<Finding[]>([]);
  const [show, setShow] = useState("problems");
  const [q, setQ] = useState("");

  useEffect(() => {
    api<Finding[]>(`/api/audits/${auditId}/findings`).then(setAll);
  }, [auditId]);

  const shown = useMemo(() => all.filter((f) =>
    (!agent || f.module === agent) &&
    (show === "all" || (show === "problems" ? ["FAIL", "WARNING"].includes(f.status) : f.status === show)) &&
    (!q || [f.plain_title, f.title, f.check_id, f.summary, ...f.items.map((i) => i.url)].join(" ").toLowerCase().includes(q.toLowerCase()))),
  [all, agent, show, q]);

  return (
    <>
      <div className="row gap wrapline filters">
        <select value={agent} onChange={(e) => setAgent(e.target.value)}>
          <option value="">All agents</option>
          {agents.map((a) => <option key={a.key} value={a.key}>{a.icon} {a.name}</option>)}
        </select>
        <select value={show} onChange={(e) => setShow(e.target.value)}>
          <option value="problems">Problems only</option>
          <option value="all">Everything</option>
          <option value="PASS">Looks good</option>
          <option value="NOT_CHECKED">Couldn't check</option>
        </select>
        <input className="grow" placeholder="Search checks or pages" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {agents.filter((a) => !agent || a.key === agent).map((a) => {
        const fs = shown.filter((f) => f.module === a.key).sort((x, y) => (ORDER[x.status] ?? 9) - (ORDER[y.status] ?? 9) || y.priority - x.priority);
        if (!fs.length && (agent !== a.key)) return null;
        return (
          <div key={a.key} className="agent-section">
            <div className="row gap wrapline">
              <span className="aicon big-icon">{a.icon}</span><h2 style={{ margin: 0 }}>{a.name}</h2>
              <span className={`pill p-${a.tone}`}>{a.verdict}</span>
            </div>
            <p className="muted small">{a.job}</p>
            {a.planned ? <p className="muted">This agent is still being built and will join in a later update.</p>
              : fs.length ? fs.map((f) => <FindingRow key={f.id} f={f} />)
              : <p className="muted">Nothing to show with the current filter.</p>}
          </div>
        );
      })}
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
