import { useEffect, useState } from "react";
import { api, post } from "../api";

interface AuditRow {
  id: number;
  domain: string;
  url: string;
  status: string;
  overall: number | null;
  coverage: number | null;
  created_at: string;
  error: string | null;
}

const empty = {
  url: "", max_pages: "", business_type: "", country: "", language: "", keywords: "",
  competitors: ["", "", ""], business_name: "", address: "", phone: "", brand: "",
};

export default function Home() {
  const [form, setForm] = useState(empty);
  const [more, setMore] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [audits, setAudits] = useState<AuditRow[]>([]);

  const load = () => api<AuditRow[]>("/api/audits?limit=100").then(setAudits).catch(() => {});
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const set = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    const body: any = { url: form.url.trim() };
    if (form.max_pages) body.max_pages = Number(form.max_pages);
    for (const k of ["business_type", "country", "language", "business_name", "address", "phone", "brand"]) {
      const v = (form as any)[k].trim();
      if (v) body[k] = v;
    }
    body.keywords = form.keywords.split(",").map((s) => s.trim()).filter(Boolean);
    body.competitors = form.competitors.map((s) => s.trim()).filter(Boolean);
    try {
      const a = await post<AuditRow>("/api/audits", body);
      window.location.hash = `#/audit/${a.id}`;
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h1>New audit</h1>
      <form onSubmit={submit} className="card stack">
        <div className="row gap">
          <input className="grow" placeholder="https://example.com" value={form.url}
                 onChange={(e) => set("url", e.target.value)} required />
          <button className="primary" disabled={busy || !form.url.trim()}>{busy ? "Starting…" : "Run audit"}</button>
        </div>
        <button type="button" className="link" onClick={() => setMore(!more)}>
          {more ? "Hide" : "Show"} optional details
        </button>
        {more && (
          <div className="grid2">
            <label>Max pages<input type="number" min={1} max={5000} value={form.max_pages}
                                   onChange={(e) => set("max_pages", e.target.value)} placeholder="100" /></label>
            <label>Business type<input value={form.business_type} onChange={(e) => set("business_type", e.target.value)}
                                       placeholder="e.g. dental clinic" /></label>
            <label>Target country<input value={form.country} onChange={(e) => set("country", e.target.value)} placeholder="e.g. US" /></label>
            <label>Language<input value={form.language} onChange={(e) => set("language", e.target.value)} placeholder="e.g. en" /></label>
            <label className="span2">Primary keywords (comma separated)
              <input value={form.keywords} onChange={(e) => set("keywords", e.target.value)} /></label>
            {form.competitors.map((c, i) => (
              <label key={i}>Competitor {i + 1}
                <input value={c} placeholder="https://competitor.com" onChange={(e) => {
                  const next = [...form.competitors];
                  next[i] = e.target.value;
                  set("competitors", next);
                }} /></label>
            ))}
            <label>Brand name<input value={form.brand} onChange={(e) => set("brand", e.target.value)} /></label>
            <label>Business name<input value={form.business_name} onChange={(e) => set("business_name", e.target.value)} /></label>
            <label>Address<input value={form.address} onChange={(e) => set("address", e.target.value)} /></label>
            <label>Phone<input value={form.phone} onChange={(e) => set("phone", e.target.value)} /></label>
          </div>
        )}
        {err && <p className="error">{err}</p>}
      </form>

      <h2>Audit history</h2>
      {audits.length === 0 ? <p className="muted">No audits yet.</p> : (
        <div className="scroll">
          <table>
            <thead><tr><th>#</th><th>Site</th><th>Status</th><th>Score</th><th>Coverage</th><th>Started</th></tr></thead>
            <tbody>
              {audits.map((a) => (
                <tr key={a.id} className="clickable" onClick={() => (window.location.hash = `#/audit/${a.id}`)}>
                  <td>{a.id}</td>
                  <td className="url">{a.url}</td>
                  <td><span className={`badge st-${a.status}`}>{a.status}</span></td>
                  <td>{a.overall ?? "–"}</td>
                  <td>{a.coverage != null ? `${a.coverage}%` : "–"}</td>
                  <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
