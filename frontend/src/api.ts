export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new Event("auth-required"));
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T = any>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export type Status = "PASS" | "WARNING" | "FAIL" | "NOT_CHECKED" | "N/A";

export interface Finding {
  id: number;
  module: string;
  module_name?: string;
  module_number: number;
  check_id: string;
  category: string;
  status: Status;
  title: string;
  summary: string | null;
  why: string | null;
  fix: string | null;
  reason: string | null;
  impact: string;
  effort: string;
  priority: number;
  score: number | null;
  affected: number;
  total: number | null;
  items: { url: string | null; evidence: any; generated_fix?: any }[];
  items_truncated: number;
  generated_fix: any;
  sources: string[];
}

export const CATEGORY_LABELS: Record<string, string> = {
  technical: "Technical",
  onpage: "On-page",
  performance: "Performance",
  schema: "Schema",
  offpage: "Off-page",
  local: "Local",
  geo: "AI Search & GEO",
};

export function fmtEvidence(v: any, depth = 0): string {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) {
    const shown = v.slice(0, 10).map((x) => fmtEvidence(x, depth + 1));
    return "[" + shown.join(", ") + (v.length > 10 ? ` … +${v.length - 10}` : "") + "]";
  }
  if (typeof v === "object") {
    const parts = Object.entries(v)
      .filter(([, x]) => x !== null && x !== "" && !(Array.isArray(x) && x.length === 0))
      .map(([k, x]) => `${k}: ${fmtEvidence(x, depth + 1)}`);
    return depth === 0 ? parts.join("; ") : "{" + parts.join(", ") + "}";
  }
  return String(v);
}

export function fixCode(fix: any): string {
  if (!fix) return "";
  if (typeof fix === "object") {
    if (fix.code) return String(fix.code);
    if (fix.content) return String(fix.content);
    if (fix.type === "redirect_rule") return `# Apache\n${fix.apache}\n# Nginx\n${fix.nginx}`;
    if (fix.note) return String(fix.note);
    return JSON.stringify(fix, null, 2);
  }
  return String(fix);
}
