import { useEffect, useState } from "react";
import { api, put } from "../api";

const SECTION_LABELS: Record<string, string> = {
  crawl: "Crawl limits",
  budgets: "Budgets (hard caps)",
  thresholds: "Check thresholds",
  scoring: "Scoring weights",
  retention: "Data retention",
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
