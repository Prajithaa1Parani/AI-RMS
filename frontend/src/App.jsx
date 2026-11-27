import { useEffect, useState } from "react";
import { api, getToken, setToken, label } from "./api.js";
import StudentView from "./StudentView.jsx";
import StaffView from "./StaffView.jsx";
import AdminView from "./AdminView.jsx";

function Login({ onLogin }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ username: "", password: "", full_name: "" });
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setError(""); setInfo(""); setBusy(true);
    try {
      if (mode === "register") {
        await api("/auth/register", { method: "POST", body: form });
        setInfo("Account created. You can log in now.");
        setMode("login");
      } else {
        const data = await api("/auth/login", { method: "POST", body: form });
        setToken(data.token);
        onLogin(data.user);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login" onSubmit={submit}>
        <div className="logo">RMS</div>
        <h1>AI-Powered RMS</h1>
        <p className="muted">Intelligent Student Grievance Management System</p>
        {mode === "register" && (
          <label>Full name<input value={form.full_name} onChange={set("full_name")} required /></label>
        )}
        <label>Username<input value={form.username} onChange={set("username")} required autoFocus /></label>
        <label>Password<input type="password" value={form.password} onChange={set("password")} required /></label>
        {error && <div className="banner bad">{error}</div>}
        {info && <div className="banner good">{info}</div>}
        <button className="primary" disabled={busy}>
          {busy ? "Please wait…" : mode === "login" ? "Log in" : "Create student account"}
        </button>
        <button type="button" className="link" onClick={() => setMode(mode === "login" ? "register" : "login")}>
          {mode === "login" ? "New student? Create an account" : "Back to log in"}
        </button>
        <div className="demo-box">
          <b>Demo accounts</b>
          <span>student1 / student123</span>
          <span>hostel_staff, accounts_staff, exam_staff, it_staff / staff123</span>
          <span>admin / admin123</span>
        </div>
      </form>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api("/meta").then(setMeta).catch(() => {});
    if (!getToken()) return setLoading(false);
    api("/me").then((d) => setUser(d.user)).catch(() => setToken(null)).finally(() => setLoading(false));
  }, []);

  function logout() {
    setToken(null);
    setUser(null);
  }

  if (loading) return <div className="center muted">Loading…</div>;
  if (!user) return <Login onLogin={setUser} />;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand"><span className="logo small">RMS</span> AI-Powered RMS</div>
        <div className="who">
          <span>{user.full_name}</span>
          <span className="chip role">{label(user.role)}{user.department ? " · " + label(user.department) : ""}</span>
          <button className="ghost" onClick={logout}>Log out</button>
        </div>
      </header>
      <main>
        {user.role === "student" && <StudentView user={user} meta={meta} />}
        {user.role === "staff" && <StaffView user={user} meta={meta} />}
        {user.role === "admin" && <AdminView user={user} meta={meta} />}
      </main>
    </div>
  );
}
