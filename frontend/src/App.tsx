import { useEffect, useState } from "react";
import { api, post } from "./api";
import Login from "./pages/Login";
import Home from "./pages/Home";
import AuditView from "./pages/AuditView";
import PageView from "./pages/PageView";
import Settings from "./pages/Settings";

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export default function App() {
  const [auth, setAuth] = useState<{ authenticated: boolean; configured: boolean } | null>(null);
  const hash = useHash();

  const refresh = () => api("/api/auth/me").then(setAuth).catch(() => setAuth({ authenticated: false, configured: true }));
  useEffect(() => {
    refresh();
    const on = () => setAuth((a) => (a ? { ...a, authenticated: false } : a));
    window.addEventListener("auth-required", on);
    return () => window.removeEventListener("auth-required", on);
  }, []);

  if (!auth) return <div className="wrap muted">Loading…</div>;
  if (!auth.authenticated) return <Login configured={auth.configured} onDone={refresh} />;

  const parts = hash.replace(/^#\/?/, "").split("/");
  let view;
  if (parts[0] === "audit" && parts[2] === "page") view = <PageView auditId={+parts[1]} pageId={+parts[3]} />;
  else if (parts[0] === "audit") view = <AuditView id={+parts[1]} />;
  else if (parts[0] === "settings") view = <Settings />;
  else view = <Home />;

  return (
    <>
      <header className="top">
        <div className="wrap row between">
          <a href="#/" className="brand">SEO Inspector</a>
          <nav>
            <a href="#/">Audits</a>
            <a href="#/settings">Settings</a>
            <button className="link" onClick={() => post("/api/auth/logout").then(refresh)}>Log out</button>
          </nav>
        </div>
      </header>
      <main className="wrap">{view}</main>
    </>
  );
}
