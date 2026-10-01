import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Cluster, type ClusterStatus, type Me, type Role } from "./api";

// ---- data loading -------------------------------------------------------------------------------------

export function useApi<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!path) return;
    const id = ++seq.current;
    try {
      const result = await api.get<T>(path);
      if (id === seq.current) {
        setData(result);
        setError(null);
      }
    } catch (e) {
      if (id === seq.current) setError((e as Error).message);
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return;
    const timer = window.setInterval(load, refreshMs);
    return () => window.clearInterval(timer);
  }, [load, refreshMs]);

  return { data, error, loading, reload: load };
}

// ---- current user -------------------------------------------------------------------------------------

export const MeContext = createContext<Me | null>(null);
const ROLE_ORDER: Role[] = ["viewer", "engineer", "admin"];

export function useMe(): Me | null {
  return useContext(MeContext);
}

export function useCan(role: Role): boolean {
  const me = useMe();
  return !!me && ROLE_ORDER.indexOf(me.role) >= ROLE_ORDER.indexOf(role);
}

// ---- small presentational pieces ----------------------------------------------------------------------

export function PageHead({ crumbs, title, subtitle, children }: {
  crumbs: string[]; title: string; subtitle?: ReactNode; children?: ReactNode;
}) {
  return (
    <>
      <div className="crumbs">
        {crumbs.slice(0, -1).join(" / ")}
        {crumbs.length > 1 ? " / " : ""}
        <b>{crumbs[crumbs.length - 1]}</b>
      </div>
      <div className="head">
        <div>
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {children && <div className="actions">{children}</div>}
      </div>
    </>
  );
}

const STATUS_LABEL: Record<ClusterStatus, [string, string]> = {
  active: ["Healthy", "ok"],
  managed: ["Agent not installed", "warn"],
  pending: ["Waiting for approval", "warn"],
  offline: ["Agent offline", "bad"],
  unreachable: ["Unreachable", "bad"],
};

export function ClusterStatusPill({ status }: { status: ClusterStatus }) {
  const [label, tone] = STATUS_LABEL[status];
  return <span className={`pill ${tone}`}>{label}</span>;
}

export function Pill({ tone, children }: { tone: "ok" | "warn" | "bad" | "neutral"; children: ReactNode }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

export function TypeBadge({ type }: { type: Cluster["type"] }) {
  const label = { hub: "Hub", gpu: "GPU", cpu: "CPU only", burst: "Burst", unknown: "Unknown" }[type];
  return <span className={`typebadge ${type}`}>{label}</span>;
}

export function Bar({ value, max, tone }: { value: number; max: number; tone?: string }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="bar" role="img" aria-label={`${value} of ${max}`}>
      <i style={{ width: `${pct}%`, background: tone }} />
    </div>
  );
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Modal({ title, onClose, children, footer }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <h2>{title}</h2>
          <button className="btn small" onClick={onClose}>Close</button>
        </header>
        <div className="content">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function CopyBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <pre className="code">{text}</pre>
      <div>
        <button
          className="btn small"
          onClick={() => navigator.clipboard?.writeText(text).then(() => setCopied(true), () => setCopied(false))}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export function ago(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
