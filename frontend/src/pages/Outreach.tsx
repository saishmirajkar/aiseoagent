import { useEffect, useMemo, useState } from "react";
import { api } from "../api";

interface Prospect {
  id: number;
  site_domain: string;
  prospect_domain: string;
  url: string;
  title: string | null;
  kind: string;
  authority: number | null;
  relevance: number | null;
  contact: { emails?: string[]; contact_page?: string };
  draft_email: string | null;
  status: string;
  notes: string | null;
}

const STATUSES = ["new", "contacted", "replied", "won", "lost", "skip"];
const KINDS: Record<string, string> = {
  unlinked_mention: "Unlinked mention",
  link_gap: "Links to competitors",
  guest_post: "Guest post",
  resource: "Resource / blog",
};

export default function Outreach() {
  const [rows, setRows] = useState<Prospect[]>([]);
  const [site, setSite] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [saved, setSaved] = useState<number | null>(null);

  useEffect(() => {
    api<Prospect[]>("/api/outreach").then(setRows);
  }, []);

  const sites = useMemo(() => Array.from(new Set(rows.map((r) => r.site_domain))), [rows]);
  const shown = rows.filter((r) => (!site || r.site_domain === site) && (!status || r.status === status));

  const patch = async (id: number, body: Partial<Prospect>) => {
    const updated = await api<Prospect>(`/api/outreach/${id}`, { method: "PATCH", body: JSON.stringify(body) });
    setRows((rs) => rs.map((r) => (r.id === id ? updated : r)));
    setSaved(id);
    setTimeout(() => setSaved(null), 1500);
  };

  return (
    <>
      <h1>Outreach tracker</h1>
      <p className="muted small">
        Prospects found by audits (Link Building module). Contact details are only those publicly listed on the prospect's
        own pages. Emails are drafts — nothing is ever sent by SEO Inspector.
      </p>
      <div className="row gap wrapline filters">
        <select value={site} onChange={(e) => setSite(e.target.value)}>
          <option value="">All sites</option>
          {sites.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s}>{s}</option>)}
        </select>
        <span className="muted small">{shown.length} prospect(s)</span>
      </div>
      {shown.length === 0 && <p className="muted">No prospects yet — run an audit with the Brave Search key configured.</p>}
      {shown.map((p) => (
        <div className="finding" key={p.id}>
          <div className="fhead">
            <select value={p.status} onChange={(e) => patch(p.id, { status: e.target.value })} style={{ width: "auto" }}>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
            <b className="clickable" onClick={() => setOpen(open === p.id ? null : p.id)}>{p.prospect_domain}</b>
            <span className="muted small">
              {KINDS[p.kind] || p.kind} · authority {p.authority ?? "low/unknown"} · relevance {p.relevance ?? "–"}
              {p.contact?.emails?.length ? ` · ${p.contact.emails.join(", ")}` : p.contact?.contact_page ? " · contact page" : ""}
            </span>
            {saved === p.id && <span className="ok small">saved</span>}
          </div>
          {open === p.id && (
            <div className="fbody stack">
              <div className="small"><a href={p.url} target="_blank" rel="noreferrer">{p.url}</a> — {p.title}</div>
              {p.contact?.contact_page && (
                <div className="small">Contact page: <a href={p.contact.contact_page} target="_blank" rel="noreferrer">{p.contact.contact_page}</a></div>
              )}
              <label>Email draft (edit freely; send from your own mailbox)
                <textarea rows={9} defaultValue={p.draft_email || ""} onBlur={(e) => patch(p.id, { draft_email: e.target.value })} />
              </label>
              <label>Notes
                <textarea rows={2} defaultValue={p.notes || ""} onBlur={(e) => patch(p.id, { notes: e.target.value })} />
              </label>
            </div>
          )}
        </div>
      ))}
    </>
  );
}
