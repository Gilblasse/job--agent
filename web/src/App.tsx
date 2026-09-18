import { useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { UNAUTHORIZED_EVENT, getPassword } from "./api";
import Login from "./components/Login";
import Dismissed from "./pages/Dismissed";
import Find from "./pages/Find";
import SavedJobs from "./pages/SavedJobs";
import Sources from "./pages/Sources";

export default function App() {
  const [authed, setAuthed] = useState(() => Boolean(getPassword()));

  useEffect(() => {
    const onUnauthorized = () => setAuthed(false);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  return (
    <div className="app">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link to="/find" className="brand">
          Jobagent
        </Link>
        <nav aria-label="Primary">
          <NavLink to="/find">Find jobs</NavLink>
          <NavLink to="/saved">Saved jobs</NavLink>
        </nav>
      </header>
      <main id="main" className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/find" replace />} />
          <Route path="/find" element={<Find />} />
          <Route path="/saved" element={<SavedJobs />} />
          <Route path="/dismissed" element={<Dismissed />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="*" element={<Navigate to="/find" replace />} />
        </Routes>
      </main>
      <footer className="footer">
        <Link to="/dismissed">Dismissed</Link>
        <span aria-hidden="true">·</span>
        <Link to="/sources">Sources &amp; registry</Link>
      </footer>
    </div>
  );
}
