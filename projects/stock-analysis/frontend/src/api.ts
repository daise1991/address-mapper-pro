const TOKEN_KEY = "stock-app-token";

// start.bat が開く URL（/#token=...）からトークンを受け取り、保存してアドレスバーから消す
(function captureTokenFromHash() {
  const m = location.hash.match(/token=([^&]+)/);
  if (!m) return;
  setToken(decodeURIComponent(m[1]));
  history.replaceState(null, "", location.pathname + location.search);
})();

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(t: string) {
  try {
    localStorage.setItem(TOKEN_KEY, t);
  } catch {
    /* プライベートブラウズ等では保存しない */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { ...init, headers: { ...headers, ...(init.headers as Record<string, string>) } });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* noop */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const send = <T,>(path: string, method: string, body?: unknown) =>
  api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

export type Factor = { label: string; score: number | null; weight: number };
export type ScoreRow = {
  run_date: string;
  code: string;
  name: string;
  category: string;
  group_name: string;
  asset_type: string;
  listing_status: string;
  held: boolean;
  price: number | null;
  growth_score: number | null;
  buy_score: number | null;
  buy_score_k150: number | null;
  buy_status: string;
  sell_score: number | null;
  theoretical_value: number | null;
  target_price: number | null;
  future_price: number | null;
  buy_decision: string;
  sell_decision: string;
  exit_type: string | null;
  growth_detail: { coverage: number; factors: Record<string, Factor> };
  sell_detail: { coverage?: number; valuation_side?: number; fundamental_side?: number; factors: Record<string, Factor> };
  buy_detail: {
    reason: string; method: string; v_over_p: number | null; years: number | null; assumption_origin: string;
    assumption_status?: "unconfirmed" | "provisional" | "review_required" | "verified"; confidence?: string | null;
    scenarios?: Record<string, { eps: number; per: number; theoretical_value: number | null; target_price: number | null; buy_score: number | null }>;
    anchor_ratio?: number | null; data_issue?: string | null;
    inputs?: {
      target_year: number; fiscal_month_end: number; eps_forecast?: number | null; normalized_eps?: number | null;
      assumed_per?: number | null; required_return: number; safety_margin: number; reason: string;
    };
  };
  method_version?: string | null;
  indicators: Record<string, number | null> & { sources?: string[] };
};

export type Meta = {
  buy_labels: Record<string, string>;
  sell_labels: Record<string, string>;
  exit_labels: Record<string, string>;
  categories: Record<string, string>;
  category_defaults: Record<string, { required_return: number; safety_margin: number; label: string }>;
  horizons: number[];
  benchmarks: Record<string, string>;
};
