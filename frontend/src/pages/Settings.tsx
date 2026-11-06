import { useEffect, useState } from "react";
import { api, post, put } from "../api";

const SECTION_LABELS: Record<string, string> = {
  crawl: "Crawl limits",
  budgets: "Budgets (hard caps)",
  thresholds: "Check thresholds",
  scoring: "Scoring weights",
  retention: "Data retention",
  llm: "AI (LLM) analysis",
  data: "Data layer (keywords, authority, backlinks)",
};

const KEY_LABELS: Record<string, string> = {
  anthropic: "Anthropic (Claude)",
  brave_search: "Brave Search",
  pagespeed: "PageSpeed Insights",
  google_ads_keyword_planner: "Google Ads Keyword Planner",
  search_console: "Search Console",
  google_business_profile: "Google Business Profile",
  vertex_ai_gemini: "Vertex AI (Gemini)",
  admin_password: "ADMIN_PASSWORD",
  session_secret: "SESSION_SECRET",
  import_api_token: "IMPORT_API_TOKEN (Colab import)",
};

export default function Settings() {
  const [s, setS] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    api("/api/settings").then((d) => {
      setS(d);
      setDraft(structuredClone(d.runtime));
    });
  }, []);

  if (!s || !draft) return <p className="muted">Loading…</p>;

  const setVal = (section: string, key: string, value: any, sub?: string) =>
    setDraft((d: any) => {
      const n = structuredClone(d);
      if (sub) n[section][key][sub] = value;
      else n[section][key] = value;
      return n;
    });

  const save = async () => {
    setMsg("");
    setErr("");
    try {
      const r = await put("/api/settings", draft);
      setDraft(r.runtime);
      setMsg("Saved. New audits use these settings.");
    } catch (ex: any) {
      setErr(ex.message);
    }
  };

  const input = (section: string, key: string, value: any, sub?: string) => {
    if (typeof value === "boolean")
      return <input type="checkbox" checked={value} onChange={(e) => setVal(section, key, e.target.checked, sub)} />;
    if (typeof value === "number")
      return <input type="number" step="any" value={value}
                    onChange={(e) => setVal(section, key, e.target.value === "" ? "" : Number(e.target.value), sub)} />;
    return <input value={value} onChange={(e) => setVal(section, key, e.target.value, sub)} />;
  };

  return (
    <>
      <h1>Settings</h1>
      <h2>API keys</h2>
      <p className="muted small">Keys live in Replit Secrets. This page only shows whether each is set.</p>
      <div className="keys">
        {Object.entries(s.integrations).map(([k, v]) => (
          <div key={k} className="keyrow"><span className={v ? "dot on" : "dot"} />{KEY_LABELS[k] || k}
            <span className="muted small">{v ? "configured" : "not set"}</span></div>
        ))}
      </div>
      <p className="muted small">Report storage: {s.report_storage === "replit" ? "Replit Object Storage" : "PostgreSQL"}</p>

      <DataLayer />

      {Object.entries(draft).map(([section, fields]: [string, any]) => (
        <section key={section}>
          <h2>{SECTION_LABELS[section] || section}</h2>
          <div className="grid2 card">
            {Object.entries(fields).map(([key, value]: [string, any]) =>
              value && typeof value === "object" ? (
                <div key={key} className="span2">
                  <div className="label">{key.replace(/_/g, " ")}</div>
                  <div className="grid3">
                    {Object.entries(value).map(([sub, v]) => (
                      <label key={sub}>{sub}{input(section, key, v, sub)}</label>
                    ))}
                  </div>
                </div>
              ) : key === "render_mode" ? (
                <label key={key}>{key.replace(/_/g, " ")}
                  <select value={value} onChange={(e) => setVal(section, key, e.target.value)}>
                    <option value="auto">auto (render only when needed)</option>
                    <option value="never">never</option>
                    <option value="always">always</option>
                  </select>
                </label>
              ) : (
                <label key={key}>{key.replace(/_/g, " ")}{input(section, key, value)}</label>
              ),
            )}
          </div>
        </section>
      ))}
      <div className="row gap sticky-save">
        <button className="primary" onClick={save}>Save settings</button>
        <button onClick={() => setDraft(structuredClone(s.defaults))}>Reset to defaults</button>
        {msg && <span className="ok">{msg}</span>}
        {err && <span className="error">{err}</span>}
      </div>
    </>
  );
}


function DataLayer() {
  const [auth, setAuth] = useState<any>(null);
  const [watch, setWatch] = useState<any[]>([]);
  const [domain, setDomain] = useState("");
  const [kind, setKind] = useState("client");
  const [msg, setMsg] = useState("");

  const load = () => {
    api("/api/authority").then(setAuth);
    api<any[]>("/api/watchlist").then(setWatch);
  };
  useEffect(load, []);

  const refresh = async () => {
    const r = await post("/api/authority/refresh");
    setMsg(r.message);
    setTimeout(load, 2000);
  };
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!domain.trim()) return;
    setWatch(await post("/api/watchlist", { domain, kind }));
    setDomain("");
  };
  const remove = async (d: string) => setWatch(await api(`/api/watchlist/${encodeURIComponent(d)}`, { method: "DELETE" }));
  const current = auth?.releases?.find((r: any) => r.current);

  return (
    <>
      <h2>Authority table (Common Crawl)</h2>
      <div className="card stack">
        <div>
          {current
            ? <>Current release <b>{current.release}</b>: {current.rows.toLocaleString()} domains (top {current.top_n.toLocaleString()}), loaded {new Date(current.finished_at).toLocaleDateString()}.</>
            : <span className="muted">Not loaded yet — the worker loads it automatically on first start.</span>}
        </div>
        {auth?.releases?.filter((r: any) => !r.current).map((r: any) => (
          <div key={r.release} className="small muted">{r.release}: {r.status}{r.rows ? `, ${r.rows.toLocaleString()} domains` : ""}{r.error ? ` — ${r.error}` : ""}</div>
        ))}
        <div className="row gap"><button onClick={refresh}>Refresh now</button><span className="muted small">{msg || "Refreshes quarterly on its own."}</span></div>
      </div>

      <h2>Watchlist (full referring-domain import)</h2>
      <p className="muted small">Domains whose complete referring-domain lists the Colab notebook (notebooks/cc_inlinks.ipynb) imports from Common Crawl.</p>
      <form className="row gap wrapline" onSubmit={add}>
        <input className="grow" placeholder="example.com" value={domain} onChange={(e) => setDomain(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: "auto" }}>
          <option value="client">client</option>
          <option value="competitor">competitor</option>
        </select>
        <button className="primary">Add</button>
      </form>
      <div className="scroll"><table>
        <thead><tr><th>Domain</th><th>Type</th><th>Latest import</th><th></th></tr></thead>
        <tbody>{watch.map((w) => (
          <tr key={w.domain}><td>{w.domain}</td><td>{w.kind}</td><td>{w.latest_import || <span className="muted">never</span>}</td>
            <td><button className="link" onClick={() => remove(w.domain)}>remove</button></td></tr>
        ))}</tbody>
      </table></div>
    </>
  );
}
