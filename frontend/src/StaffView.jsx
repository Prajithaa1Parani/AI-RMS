import { useEffect, useState } from "react";
import { api, label } from "./api.js";
import { TicketDetail, TicketTable } from "./components.jsx";

export default function StaffView({ user, meta }) {
  const [tickets, setTickets] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [error, setError] = useState("");

  const load = () => api("/tickets").then((d) => setTickets(d.tickets)).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  if (openId) {
    return <TicketDetail id={openId} user={user} meta={meta} onClose={() => { setOpenId(null); load(); }} onChanged={load} />;
  }
  const active = tickets.filter((t) => ["open", "in_progress"].includes(t.status));
  const done = tickets.filter((t) => !["open", "in_progress"].includes(t.status));
  return (
    <div className="stack">
      {error && <div className="banner bad">{error}</div>}
      <div className="card">
        <h2>{label(user.department)} — tickets to handle ({active.length})</h2>
        <TicketTable tickets={active} onOpen={setOpenId} showStudent />
      </div>
      <div className="card">
        <h2>Resolved and closed ({done.length})</h2>
        <TicketTable tickets={done} onOpen={setOpenId} showStudent />
      </div>
    </div>
  );
}
