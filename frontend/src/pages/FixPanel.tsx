import { useEffect, useMemo, useState } from "react";
import { api, post, put } from "../api";

type Question = { id: string; question: string; required?: boolean };
type Item = {
  id: string; check_id: string; agent: string; agent_icon: string; kind: "auto" | "review" | "input" | "manual";
  action: string; label: string; page_url?: string; before?: string | null; after?: string | null;
  questions: Question[]; data: any;
};
type Run = {
  id: number; status: string; selection: string[]; answers: Record<string, string>; result: any;
  pr_url: string | null; error: string | null; created_at: string;
};

const KIND: Record<string, { label: string; cls: string; help: string }> = {
  auto: { label: "Automatic", cls: "p-PASS", help: "Mechanical change, checked in code" },
  review: { label: "You'll review the writing", cls: "p-WARNING", help: "AI-written text; read it in the pull request" },
  input: { label: "Needs your answer", cls: "p-FAIL", help: "Only you know these facts" },
  manual: { label: "Developer instructions", cls: "", help: "Listed in the pull request; not changed automatically" },
};
const DONE = ["ready", "failed", "published"];

export default function FixPanel({ auditId }: { auditId: number }) {
  const [plan, setPlan] = useState<{ connection: any; items: Item[]; runs: Run[] } | null>(null);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api(`/api/audits/${auditId}/fix-plan`).then((p) => {
      setPlan(p);
      setSelected(new Set(p.items.filter((i: Item) => i.kind !== "manual").map((i: Item) => i.id)));
      if (p.runs?.length) setRun(p.runs[0]);
    }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [auditId]);

  useEffect(() => {
    if (!run || DONE.includes(run.status)) return;
    const t = setTimeout(() => api<Run>(`/api/fixes/${run.id}`).then(setRun).catch((e) => setErr(e.message)), 2000);
    return () => clearTimeout(t);
  }, [run]);

  const groups = useMemo(() => {
    const g: Record<string, Item[]> = {};
    for (const i of plan?.items || []) (g[`${i.agent_icon} ${i.agent}`] ||= []).push(i);
    return g;
  }, [plan]);
  const questions = useMemo(() => {
    const seen = new Set<string>();
    const out: (Question & { item: string })[] = [];
    for (const i of plan?.items || []) {
      if (!selected.has(i.id)) continue;
      for (const q of i.questions || []) if (!seen.has(q.id)) { seen.add(q.id); out.push({ ...q, item: i.label }); }
    }
    return out;
  }, [plan, selected]);

  if (err && !plan) return <p className="error">{err}</p>;
  if (!plan) return <p className="muted">Loading…</p>;

  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const prepare = async () => {
    setBusy(true); setErr("");
    try { setRun(await post<Run>(`/api/audits/${auditId}/fixes`, { selected: [...selected], answers })); }
    catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  const publish = async () => {
    if (!run) return;
    setBusy(true); setErr("");
    try { setRun(await post<Run>(`/api/fixes/${run.id}/publish`)); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };

  return (
    <div className="fix">
      <p className="muted">
        The AI prepares changes from this audit and opens a <b>pull request</b> on GitHub: a change request you review
        and merge yourself. Nothing is published or merged automatically.
      </p>
      {err && <p className="error">{err}</p>}

      <h2>1. Connect your website's GitHub repository</h2>
      <Connect auditId={auditId} connection={plan.connection} onDone={load} />

      <h2>2. Choose the fixes</h2>
      <p className="muted small">
        {plan.items.filter((i) => i.kind !== "manual").length} fixes available ·{" "}
        {plan.items.filter((i) => i.kind === "manual").length} developer instructions (included in the pull request)
      </p>
      {Object.entries(groups).map(([agent, items]) => (
        <details key={agent} className="card" open={items.some((i) => i.kind !== "manual")}>
          <summary><b>{agent}</b> <span className="muted small">· {items.length}</span></summary>
          {items.map((i) => (
            <label key={i.id} className={`fixrow ${i.kind === "manual" ? "muted" : ""}`}>
              <input type="checkbox" disabled={i.kind === "manual"} checked={selected.has(i.id)} onChange={() => toggle(i.id)} />
              <span>
                {i.label} <span className={`pill ${KIND[i.kind].cls}`} title={KIND[i.kind].help}>{KIND[i.kind].label}</span>
                {i.after && i.kind !== "manual" && (
                  <div className="small muted">
                    {i.before ? <><s>{i.before}</s> → </> : null}{String(i.after).slice(0, 200)}
                  </div>
                )}
                {i.kind === "manual" && i.data?.do && <div className="small muted">{i.data.do}</div>}
              </span>
            </label>
          ))}
        </details>
      ))}

      {questions.length > 0 && (
        <>
          <h2>3. Answer a few questions</h2>
          <p className="muted small">Fixes that need an answer are skipped until you give one. The AI never makes these facts up.</p>
          {questions.map((q) => (
            <label key={q.id} className="stack q">
              <span>{q.question}{q.required ? " *" : " (optional)"}</span>
              <textarea rows={2} value={answers[q.id] || ""} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
            </label>
          ))}
        </>
      )}

      <div className="row gap" style={{ marginTop: 16 }}>
        <button className="primary" disabled={busy || !plan.connection || !selected.size || (run !== null && !DONE.includes(run.status))}
                onClick={prepare}>
          Prepare changes ({selected.size})
        </button>
        {!plan.connection && <span className="muted small">Connect a repository first.</span>}
      </div>

      {run && <RunView run={run} items={plan.items} onPublish={publish} busy={busy} />}
    </div>
  );
}

function Connect({ auditId, connection, onDone }: { auditId: number; connection: any; onDone: () => void }) {
  const [edit, setEdit] = useState(!connection);
  const [repo, setRepo] = useState(connection?.repo || "");
  const [token, setToken] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  if (connection && !edit)
    return (
      <div className="card row between wrapline">
        <span>✅ Connected to <b>{connection.repo}</b>{connection.default_branch && <> (branch {connection.default_branch})</>}</span>
        <span className="row gap">
          <button onClick={() => setEdit(true)}>Change</button>
          <button onClick={() => api(`/api/audits/${auditId}/repo`, { method: "DELETE" }).then(onDone)}>Disconnect</button>
        </span>
      </div>
    );
  const save = async () => {
    setBusy(true); setMsg("");
    try { await put(`/api/audits/${auditId}/repo`, { repo, token }); setToken(""); setEdit(false); onDone(); }
    catch (e: any) { setMsg(e.message); }
    setBusy(false);
  };
  return (
    <div className="card stack">
      <label>Repository link<input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/your-name/your-website" /></label>
      <label>Access key (fine-grained personal access token)
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="github_pat_…" autoComplete="off" />
      </label>
      <details>
        <summary className="small">How do I create the access key? (about 5 minutes)</summary>
        <ol className="small">
          <li>On GitHub, open <b>Settings → Developer settings → Personal access tokens → Fine-grained tokens</b>.</li>
          <li>Click <b>Generate new token</b>, give it a name like "SEO Inspector" and an expiry date.</li>
          <li>Under <b>Repository access</b> choose <b>Only select repositories</b> and pick your website's repository.</li>
          <li>Under <b>Permissions → Repository permissions</b> set <b>Contents: Read and write</b> and <b>Pull requests: Read and write</b>. Leave everything else as "No access".</li>
          <li>Click <b>Generate token</b>, copy it and paste it above.</li>
        </ol>
        <p className="small muted">The key is stored encrypted and only used to read the code and open a pull request. It can't merge or deploy anything, and you can delete it on GitHub at any time.</p>
      </details>
      {msg && <p className="error">{msg}</p>}
      <div className="row gap">
        <button className="primary" disabled={busy || !repo} onClick={save}>{busy ? "Checking…" : "Connect"}</button>
        {connection && <button onClick={() => setEdit(false)}>Cancel</button>}
      </div>
    </div>
  );
}

function RunView({ run, items, onPublish, busy }: { run: Run; items: Item[]; onPublish: () => void; busy: boolean }) {
  const res = run.result || {};
  const st = res.items || {};
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  const counts: Record<string, number> = {};
  for (const v of Object.values<any>(st)) counts[v.status] = (counts[v.status] || 0) + 1;
  if (!DONE.includes(run.status) && run.status !== "publishing")
    return <div className="card"><b>⏳ Preparing changes…</b> <span className="muted small">reading the repository and applying the fixes</span></div>;
  return (
    <div>
      <h2>4. Review the changes</h2>
      {run.error && <p className="error">{run.error}</p>}
      {res.framework && (
        <p className="muted small">
          Repository {res.repo} · detected {res.framework.label}
          {res.framework.notes?.map((n: string) => <span key={n}> · {n}</span>)}
        </p>
      )}
      <p>
        <span className="pill p-PASS">{counts.done || 0} ready</span>{" "}
        {counts.skipped ? <span className="pill">{counts.skipped} skipped</span> : null}{" "}
        {counts.waiting ? <span className="pill p-WARNING">{counts.waiting} waiting for the AI</span> : null}{" "}
        {counts.failed ? <span className="pill p-FAIL">{counts.failed} failed checks</span> : null}
      </p>
      {Object.entries<any>(st).map(([id, v]) => {
        const it = byId[id];
        if (!it) return null;
        return (
          <div key={id} className={`finding fbody ${v.status}`}>
            <div>{v.status === "done" ? "✅" : v.status === "waiting" ? "⏳" : v.status === "failed" ? "❌" : "➖"} {it.label}</div>
            {v.reason && <div className="small muted">{v.reason}</div>}
            {v.text && <pre className="written">{v.text}</pre>}
            {v.issues?.map((x: string) => <div key={x} className="small" style={{ color: "var(--warn)" }}>⚠️ {x}</div>)}
          </div>
        );
      })}
      {(res.files || []).map((f: any) => (
        <details key={f.path} className="card">
          <summary><b>{f.path}</b> {f.new && <span className="pill p-PASS">new file</span>}
            {f.problems?.length > 0 && <span className="pill p-FAIL">failed checks, not included</span>}</summary>
          {f.problems?.map((p: string) => <div key={p} className="error small">{p}</div>)}
          <Diff text={f.diff} />
        </details>
      ))}
      <div className="row gap" style={{ marginTop: 12 }}>
        {run.pr_url ? (
          <a className="button primary" href={run.pr_url.startsWith("http") ? run.pr_url : undefined} target="_blank" rel="noreferrer">
            Open the pull request ↗
          </a>
        ) : (
          <button className="primary" disabled={busy || run.status !== "ready" || !(res.commits || []).length} onClick={onPublish}>
            {run.status === "publishing" ? "Creating pull request…" : "Create pull request on GitHub"}
          </button>
        )}
        {run.pr_url && !run.pr_url.startsWith("http") && <span className="muted small">{run.pr_url}</span>}
      </div>
      <p className="muted small">After you merge and your site updates, run a new audit to confirm the fixes worked.</p>
    </div>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {(text || "").split("\n").map((l, i) => (
        <span key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : ""}>
          {l}{"\n"}
        </span>
      ))}
    </pre>
  );
}
