import { useEffect, useState } from "react";
import { api } from "../api";
import { FindingRow } from "./AuditView";

export default function PageView({ auditId, pageId }: { auditId: number; pageId: number }) {
  const [p, setP] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api(`/api/audits/${auditId}/pages/${pageId}`).then(setP).catch((e) => setErr(e.message));
  }, [auditId, pageId]);

  if (err) return <p className="error">{err}</p>;
  if (!p) return <p className="muted">Loading…</p>;
  const d = p.data || {};
  const issues = p.findings.filter((f: any) => f.status === "FAIL" || f.status === "WARNING");

  return (
    <>
      <a href={`#/audit/${auditId}`} className="muted">← Back to audit</a>
      <h1 className="url">{p.url}</h1>
      <div className="grid2 card">
        <Field k="HTTP status" v={p.status} />
        <Field k="Final URL" v={d.final_url !== p.url ? d.final_url : "same"} />
        <Field k="Title" v={d.title} />
        <Field k="Meta description" v={d.meta_description} />
        <Field k="Canonical" v={d.canonical} />
        <Field k="Meta robots / X-Robots-Tag" v={[d.meta_robots, d.x_robots_tag].filter(Boolean).join(" | ") || "—"} />
        <Field k="Click depth" v={d.depth ?? "not linked from homepage"} />
        <Field k="Inbound internal links" v={d.inlinks} />
        <Field k="Words" v={d.word_count} />
        <Field k="Response time" v={d.response_ms != null ? `${d.response_ms} ms` : "—"} />
        <Field k="Rendered with JS" v={d.rendered ? (d.js_dependent ? "yes – content depends on JS" : "yes") : "no"} />
        <Field k="In sitemap" v={d.in_sitemap ? "yes" : "no"} />
        <Field k="Language / viewport" v={`${d.lang || "—"} / ${d.viewport || "—"}`} />
        <Field k="Structured data" v={(d.structured_data?.types || []).join(", ") || "none"} />
      </div>

      <h2>Issues on this page ({issues.length})</h2>
      {issues.length ? issues.map((f: any) => <FindingRow key={f.id} f={f} open />) : <p className="muted">No issues.</p>}

      <h2>Heading outline</h2>
      {(d.headings || []).length ? (
        <ul className="outline">
          {d.headings.map(([lvl, t]: [number, string], i: number) => (
            <li key={i} style={{ marginLeft: (lvl - 1) * 16 }}><span className="muted">H{lvl}</span> {t}</li>
          ))}
        </ul>
      ) : <p className="muted">No headings.</p>}

      <h2>Images ({p.images.length})</h2>
      {p.images.length > 0 && (
        <div className="scroll"><table>
          <thead><tr><th>Image</th><th>Alt</th><th>Format</th><th>KB</th><th>Intrinsic</th><th>Display</th><th>Loading</th></tr></thead>
          <tbody>{p.images.map((i: any, n: number) => (
            <tr key={n}>
              <td className="url">{i.filename}</td>
              <td>{i.has_alt ? (i.alt || <span className="muted">(empty)</span>) : <span className="s-FAIL">missing</span>}</td>
              <td>{i.format || "?"}</td>
              <td>{i.bytes != null ? Math.round(i.bytes / 1024) : "?"}</td>
              <td>{i.intrinsic_w ? `${i.intrinsic_w}×${i.intrinsic_h}` : "?"}</td>
              <td>{i.rendered_w ? `${i.rendered_w}×${i.rendered_h}` : i.width_attr ? `${i.width_attr}×${i.height_attr ?? "?"} (attr)` : "?"}</td>
              <td>{i.loading || ""}</td>
            </tr>))}
          </tbody></table></div>
      )}

      <h2>Outgoing links ({p.links.length})</h2>
      <div className="scroll"><table>
        <thead><tr><th>Target</th><th>Anchor</th><th>Type</th><th>Status</th></tr></thead>
        <tbody>{p.links.map((l: any, n: number) => (
          <tr key={n}>
            <td className="url">{l.target}</td>
            <td>{l.anchor}</td>
            <td>{l.internal ? "internal" : "external"}{l.nofollow ? " · nofollow" : ""}{l.sponsored ? " · sponsored" : ""}{l.ugc ? " · ugc" : ""}</td>
            <td>{l.status ?? "—"}</td>
          </tr>))}
        </tbody></table></div>

      <h2>Linked from ({p.inlinks.length})</h2>
      <ul>{p.inlinks.map((l: any, n: number) => <li key={n}><span className="url">{l.source}</span> <span className="muted">“{l.anchor}”</span></li>)}</ul>

      <h2>Visible text</h2>
      {p.text_retained ? <pre className="text">{p.text_excerpt}</pre>
        : <p className="muted">Full text is kept only for the most recent audits of a site.</p>}
    </>
  );
}

function Field({ k, v }: { k: string; v: any }) {
  return <div><div className="label">{k}</div><div className="url">{v === null || v === undefined || v === "" ? "—" : String(v)}</div></div>;
}
