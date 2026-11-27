import { useEffect, useState } from "react";
import { api } from "./api.js";
import { TicketDetail, TicketTable, WorkflowSteps } from "./components.jsx";

const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

export default function StudentView({ user, meta }) {
  const [text, setText] = useState("");
  const [doc, setDoc] = useState({ name: "", text: "" });
  const [key, setKey] = useState(newKey());   // same key while the text is unchanged
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [estimate, setEstimate] = useState(null);
  const [error, setError] = useState("");
  const [tickets, setTickets] = useState([]);
  const [openId, setOpenId] = useState(null);

  const loadTickets = () => api("/tickets").then((d) => setTickets(d.tickets)).catch((e) => setError(e.message));
  useEffect(() => { loadTickets(); }, []);

  function changeText(value) {
    setText(value); setKey(newKey()); setEstimate(null);
  }

  function pickFile(e) {
    const file = e.target.files[0];
    if (!file) return setDoc({ name: "", text: "" });
    if (file.size > 200_000) return setError("The file is too large (limit 200 KB of text).");
    const reader = new FileReader();
    reader.onload = () => { setDoc({ name: file.name, text: String(reader.result) }); setKey(newKey()); setEstimate(null); };
    reader.readAsText(file);
  }

  const fullText = () => (doc.text ? text + "\n\nAttached document:\n" + doc.text : text);

  async function getEstimate() {
    setError("");
    try { setEstimate(await api("/estimate", { method: "POST", body: { text: fullText() } })); }
    catch (e) { setError(e.message); }
  }

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setError(""); setResult(null);
    try {
      const r = await api("/tickets", { method: "POST", body: { text, document_text: doc.text, idempotency_key: key } });
      setResult(r);
      if (r.ok) { setText(""); setDoc({ name: "", text: "" }); setKey(newKey()); }
      loadTickets();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (openId) {
    return <TicketDetail id={openId} user={user} meta={meta} onClose={() => { setOpenId(null); loadTickets(); }} onChanged={loadTickets} />;
  }

  return (
    <div className="stack">
      <form className="card" onSubmit={submit}>
        <h2>Submit a complaint or request</h2>
        <p className="muted">Describe the problem in your own words. Mention where and when it happened, and any amount, course code or receipt number involved.</p>
        <textarea rows={7} value={text} onChange={(e) => changeText(e.target.value)}
          placeholder="Example: The Wi-Fi in block BH-4, room 212 has not worked since 2026-11-20…" />
        <div className="row between">
          <label className="file">Attach a text document (optional, .txt)
            <input type="file" accept=".txt,text/plain" onChange={pickFile} />
          </label>
          <span className="muted small">{text.length} characters{doc.name ? " + " + doc.name : ""}</span>
        </div>
        {estimate && (
          <div className={"banner " + (estimate.too_large || estimate.minimum_cost_usd > estimate.budget.remaining_usd ? "bad" : "info")}>
            About {estimate.input_tokens} tokens · {estimate.llm_calls} AI calls · expected cost ${estimate.expected_cost_usd.toFixed(5)}
            {estimate.long_input && <> · long input: will be split into {estimate.chunks} chunks and summarised</>}
            <br />Budget left today: ${estimate.budget.remaining_usd.toFixed(5)} of ${estimate.budget.limit_usd.toFixed(2)}
            {estimate.budget.economy_mode && " (economy mode: cheapest models first)"}
          </div>
        )}
        {error && <div className="banner bad">{error}</div>}
        <div className="row">
          <button className="primary" disabled={busy || text.trim().length < 15}>{busy ? "Processing with AI…" : "Submit complaint"}</button>
          <button type="button" disabled={busy || text.trim().length < 15} onClick={getEstimate}>Estimate tokens and cost</button>
        </div>
      </form>

      {result && (
        <div className="card">
          <div className="row between">
            <h2>Workflow status</h2>
            {result.ticket_id && <button onClick={() => setOpenId(result.ticket_id)}>Open ticket #{result.ticket_id}</button>}
          </div>
          <WorkflowSteps result={result} />
        </div>
      )}

      <div className="card">
        <h2>My tickets</h2>
        <TicketTable tickets={tickets} onOpen={setOpenId} />
      </div>
    </div>
  );
}
