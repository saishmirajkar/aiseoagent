import { useState } from "react";
import { post } from "../api";

export default function Login({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await post("/api/auth/login", { password: pw });
      onDone();
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="wrap narrow">
      <h1>SEO Inspector</h1>
      {!configured && (
        <p className="error">ADMIN_PASSWORD is not set. Add it in Replit Secrets and restart the app.</p>
      )}
      <form onSubmit={submit} className="card stack">
        <label>
          Password
          <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus />
        </label>
        <button className="primary" disabled={busy || !pw}>{busy ? "Signing in…" : "Sign in"}</button>
        {err && <p className="error">{err}</p>}
      </form>
    </div>
  );
}
