import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import type { Me, Overview as OverviewData } from "./api";
import { ErrorBox, MeContext, useApi } from "./components";
import Audit from "./pages/Audit";
import Catalog from "./pages/Catalog";
import Clusters from "./pages/Clusters";
import Deployments from "./pages/Deployments";
import Gpus from "./pages/Gpus";
import NewDeployment from "./pages/NewDeployment";
import Overview from "./pages/Overview";

function Logo() {
  return (
    <svg viewBox="0 0 28 28" aria-hidden="true">
      <rect x="1" y="1" width="26" height="26" rx="6" fill="#1A3152" />
      <rect x="6" y="6" width="4" height="16" rx="1" fill="#5B8DEF" />
      <rect x="12" y="10" width="4" height="12" rx="1" fill="#fff" />
      <rect x="18" y="14" width="4" height="8" rx="1" fill="#fff" opacity=".6" />
    </svg>
  );
}

export default function App() {
  const me = useApi<Me>("/me");
  const overview = useApi<OverviewData>("/overview", 30000);
  const k = overview.data?.kpis;
  const alertCount = overview.data?.alerts.length ?? 0;
  const initials = (me.data?.name ?? "?").split(/[.\s_-]/).map((p) => p[0]).join("").slice(0, 2);

  return (
    <MeContext.Provider value={me.data}>
      <div className="shell">
        <header className="mast">
          <NavLink to="/" className="logo"><Logo />Tensorhelm</NavLink>
          <div className="scope">Scope: <b>All clusters</b></div>
          <span className="hub">hub · {me.data?.hub ?? "…"}</span>
          <span className="sp" />
          <NavLink to="/" className="user" style={{ textDecoration: "none" }}>
            Alerts{alertCount > 0 && <span className="badge-count">{alertCount}</span>}
          </NavLink>
          <span className="user">
            {me.data && <span>{me.data.name} · {me.data.role}</span>}
            <span className="avatar" title={me.data?.name}>{initials}</span>
          </span>
        </header>
        <div className="body">
          <nav className="side" aria-label="Main">
            <div className="group">Operate</div>
            <NavLink to="/" end>Fleet overview</NavLink>
            <NavLink to="/clusters">Clusters <em>{k?.clusters ?? ""}</em></NavLink>
            <NavLink to="/gpus">GPU fleet <em>{k?.gpus_total ?? ""}</em></NavLink>
            <NavLink to="/catalog">Model catalog</NavLink>
            <NavLink to="/deployments">Deployments <em>{k?.deployments ?? ""}</em></NavLink>
            <div className="group">Govern</div>
            <NavLink to="/audit">Audit log</NavLink>
            <div className="foot">
              v{me.data?.version ?? "…"} · {me.data?.delivery_mode === "gitops" ? "GitOps delivery" : "direct delivery"}
            </div>
          </nav>
          <main className="main">
            <ErrorBox error={me.error && `Cannot reach the Tensorhelm API: ${me.error}`} />
            <Routes>
              <Route path="/" element={<Overview data={overview.data} error={overview.error} />} />
              <Route path="/clusters" element={<Clusters />} />
              <Route path="/gpus" element={<Gpus />} />
              <Route path="/catalog" element={<Catalog />} />
              <Route path="/deployments" element={<Deployments />} />
              <Route path="/deployments/new" element={<NewDeployment />} />
              <Route path="/audit" element={<Audit />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </main>
        </div>
      </div>
    </MeContext.Provider>
  );
}
