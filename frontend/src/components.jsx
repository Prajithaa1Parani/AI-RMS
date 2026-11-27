import { useEffect, useState } from "react";
import { api, label } from "./api.js";

const TONES = {
  open: "blue", in_progress: "blue", resolved: "good", closed: "grey", rejected: "bad",
  needs_clarification: "warn", needs_review: "warn", pending_approval: "warn",
  low: "grey", medium: "blue", high: "warn", critical: "bad",
  // workflow step results
  completed: "good", ok: "good", agreed: "good", grounded: "good", committed: "good",
  passed: "good", applied: "blue", skipped: "grey", chunked_and_summarised: "blue",
  resolved_by_majority: "warn", required: "warn", flagged: "warn", template_used: "warn",
  unverified: "warn", disagreed: "bad", failed: "bad", failed_safely: "bad", blocked: "bad",
  rejected_step: "bad", exhausted: "warn",
};

export function Chip({ value }) {
  if (!value) return <span className="chip grey">—</span>;
  return <span className={"chip " + (TONES[value] || "grey")}>{label(value)}</span>;
}

const STAGE_NAMES = {
  preprocessing: "1. Preprocessing & injection scan",
  token_estimate: "2. Token & cost estimate",
  budget_check: "2. Budget check",
  context_management: "3. Long-input handling",
  extraction: "4. AI extraction (stage 1)",
  injection_guard: "Prompt-injection guard",
  clarification: "5. Clarification check",
  verification: "6. Classification + multi-model verification (stage 2)",
  business_rules: "7. Business rules",
  grounding_check: "Grounding check",
  response_draft: "8. Reply draft (stage 3)",
  database_write: "9. Database write",
  workflow: "Workflow",
};

/** Timeline of what the backend actually did for one request. */
export function WorkflowSteps({ result }) {
  if (!result) return null;
  const tone = { saved: "good", needs_clarification: "warn" }[result.outcome] || "bad";
  return (
    <div className="workflow">
      <div className={"banner " + tone}>
        <b>{label(result.outcome)}</b> — {result.message}
        {result.ticket_id && <span> (Ticket #{result.ticket_id})</span>}
      </div>
      <ol className="steps">
        {(result.steps || []).map((s, i) => (
          <li key={i}>
            <div className="step-head">
              <span>{STAGE_NAMES[s.stage] || label(s.stage)}</span>
              <Chip value={s.status === "rejected" ? "rejected_step" : s.status} />
            </div>
            {s.detail && (
              <details>
                <summary>Details</summary>
                <pre>{JSON.stringify(s.detail, null, 2)}</pre>
              </details>
            )}
          </li>
        ))}
      </ol>
      {result.usage && (
        <p className="muted small">
          AI calls: {result.usage.calls} · tokens in/out: {result.usage.input_tokens}/{result.usage.output_tokens} ·
          estimated cost: ${Number(result.usage.cost_usd).toFixed(6)} · run #{result.run_id}
        </p>
      )}
    </div>
  );
}

export function TicketTable({ tickets, onOpen, showStudent }) {
  if (!tickets.length) return <p className="muted">No tickets here yet.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Summary</th>{showStudent && <th>Student</th>}<th>Category</th>
            <th>Priority</th><th>Department</th><th>Status</th><th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.id} className="clickable" onClick={() => onOpen(t.id)}>
              <td>{t.id}</td>
              <td className="wide">{t.summary || "—"}{t.injection_flag ? <span className="chip bad tiny">injection flag</span> : null}</td>
              {showStudent && <td>{t.student_name}</td>}
              <td>{label(t.category) || "—"}</td>
              <td><Chip value={t.priority} /></td>
              <td>{t.department_name || "—"}</td>
              <td><Chip value={t.status} /></td>
              <td className="nowrap">{t.updated_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Verification({ v }) {
  if (!v) return null;
  const rows = ["category", "priority", "department", "recommended_action", "confidence"];
  const cols = [["Primary", v.primary], ["Verifier", v.verifier], ["Tie-break", v.tiebreak]].filter((c) => c[1]);
  return (
    <div>
      <h4>Multi-model verification <Chip value={v.result === "disagreed_unresolved" ? "disagreed" : v.result} /></h4>
      {v.simulated_disagreement && <p className="muted small">Disagreement was simulated from the Test Lab.</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Field</th>{cols.map(([name, c]) => <th key={name}>{name}<br /><span className="muted small">{c.model.model_key}</span></th>)}</tr>
          </thead>
          <tbody>
            {rows.map((f) => (
              <tr key={f} className={v.differences && v.differences[f] ? "diff" : ""}>
                <td>{label(f)}</td>
                {cols.map(([name, c]) => <td key={name}>{String(c.output[f])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Full ticket page. What is shown and which buttons appear depends on the role. */
export function TicketDetail({ id, user, meta, onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState("");
  const [answer, setAnswer] = useState("");
  const [review, setReview] = useState({});
  const [result, setResult] = useState(null);

  async function load() {
    try {
      const d = await api("/tickets/" + id);
      setData(d);
      setReply(d.ticket.staff_response || d.ticket.suggested_response || "");
      setReview({ category: d.ticket.category || "other", department: d.ticket.department || "student_welfare",
                  priority: d.ticket.priority || "medium" });
    } catch (e) { setError(e.message); }
  }
  useEffect(() => { load(); }, [id]);

  async function act(fn) {
    setBusy(true); setError("");
    try { await fn(); await load(); onChanged && onChanged(); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }

  if (error && !data) return <div className="card"><div className="banner bad">{error}</div><button onClick={onClose}>Back</button></div>;
  if (!data) return <div className="card muted">Loading ticket…</div>;
  const t = data.ticket;
  const next = { open: ["in_progress"], in_progress: ["resolved"], resolved: ["closed", "in_progress"] }[t.status] || [];
  const canStaffAct = (user.role === "staff" || user.role === "admin") && next.length > 0;
  const canReview = user.role === "admin" && ["needs_review", "pending_approval"].includes(t.status);

  return (
    <div className="card detail">
      <div className="row between">
        <h2>Ticket #{t.id} <Chip value={t.status} /></h2>
        <button className="ghost" onClick={onClose}>← Back</button>
      </div>
      {error && <div className="banner bad">{error}</div>}
      <div className="grid2">
        <div><span className="k">Category</span>{label(t.category) || "—"}</div>
        <div><span className="k">Priority</span><Chip value={t.priority} /></div>
        <div><span className="k">Department</span>{t.department_name || "Not assigned yet"}</div>
        <div><span className="k">Student</span>{t.student_name}</div>
        <div><span className="k">Created</span>{t.created_at}</div>
        <div><span className="k">Updated</span>{t.updated_at}</div>
      </div>
      <h4>Summary</h4>
      <p>{t.summary || "—"}</p>
      <h4>Original complaint</h4>
      <p className="quote">{t.original_text}</p>
      {t.fields && <p className="muted small">Extracted details: {Object.entries(t.fields).filter(([, v]) => v !== null).map(([k, v]) => `${label(k)}: ${v}`).join(" · ") || "none"}</p>}
      {t.clarifications?.length > 0 && (
        <><h4>Clarifications</h4>{t.clarifications.map((c, i) => <p key={i} className="quote"><b>Q:</b> {c.question}<br /><b>A:</b> {c.answer}</p>)}</>
      )}

      {t.status === "needs_clarification" && user.role === "student" && (
        <div className="panel warn">
          <h4>We need more information</h4>
          <ul>{t.pending_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
          <textarea rows={3} value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Type your answer here" />
          <button className="primary" disabled={busy || answer.trim().length < 2}
            onClick={() => act(async () => {
              const r = await api(`/tickets/${t.id}/clarify`, { method: "POST", body: { answer } });
              setResult(r); setAnswer("");
            })}>{busy ? "Processing…" : "Send answer"}</button>
        </div>
      )}
      {result && <WorkflowSteps result={result} />}

      {t.staff_response && (<><h4>Reply from the department</h4><p className="quote good">{t.staff_response}</p></>)}
      {t.unavailable_info?.length > 0 && (
        <p className="muted small">Information you asked for that this system does not have: {t.unavailable_info.join("; ")}</p>
      )}

      {user.role !== "student" && (
        <>
          {t.rule_notes?.length > 0 && (
            <div className="panel">
              <h4>Business-rule decisions</h4>
              <ul>{t.rule_notes.map((n, i) => <li key={i}><b>{n.rule}</b> <Chip value={n.result === "ai_rejected" ? "rejected" : n.result} /> {n.message}</li>)}</ul>
              <p className="muted small">AI recommended action: {label(t.ai_recommended_action)}</p>
            </div>
          )}
          <Verification v={t.verification} />
        </>
      )}

      {canReview && (
        <div className="panel warn">
          <h4>Human review required</h4>
          <div className="row">
            <label>Category
              <select value={review.category} onChange={(e) => setReview({ ...review, category: e.target.value, department: meta.category_departments[e.target.value][0] })}>
                {meta.categories.map((c) => <option key={c} value={c}>{label(c)}</option>)}
              </select>
            </label>
            <label>Department
              <select value={review.department} onChange={(e) => setReview({ ...review, department: e.target.value })}>
                {Object.entries(meta.departments).map(([k, name]) => <option key={k} value={k}>{name}</option>)}
              </select>
            </label>
            <label>Priority
              <select value={review.priority} onChange={(e) => setReview({ ...review, priority: e.target.value })}>
                {meta.priorities.map((p) => <option key={p} value={p}>{label(p)}</option>)}
              </select>
            </label>
          </div>
          <div className="row">
            <button className="primary" disabled={busy} onClick={() => act(() => api(`/tickets/${t.id}/review`, { method: "POST", body: { decision: "approve", ...review } }))}>Approve and assign</button>
            <button className="danger" disabled={busy} onClick={() => act(() => api(`/tickets/${t.id}/review`, { method: "POST", body: { decision: "reject" } }))}>Reject ticket</button>
          </div>
        </div>
      )}

      {canStaffAct && (
        <div className="panel">
          <h4>Reply to student {t.suggested_response && !t.staff_response && <span className="muted small">(AI draft — edit before sending)</span>}</h4>
          <textarea rows={5} value={reply} onChange={(e) => setReply(e.target.value)} />
          <div className="row">
            {next.map((s) => (
              <button key={s} className="primary" disabled={busy}
                onClick={() => act(() => api(`/tickets/${t.id}/status`, { method: "POST", body: { status: s, response: reply } }))}>
                Mark as {label(s).toLowerCase()}
              </button>
            ))}
          </div>
        </div>
      )}

      <h4>History</h4>
      <ul className="history">
        {data.events.map((e) => (
          <li key={e.id}><span className="nowrap muted">{e.created_at}</span> <b>{e.actor}</b>: {e.from_status ? label(e.from_status) + " → " : ""}{label(e.to_status)} <span className="muted">{e.note}</span></li>
        ))}
      </ul>

      {data.audit && (
        <details>
          <summary>Audit trail ({data.audit.length} records)</summary>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Time</th><th>Stage</th><th>Result</th><th>Detail</th></tr></thead>
              <tbody>
                {data.audit.map((a) => (
                  <tr key={a.id}><td className="nowrap">{a.created_at}</td><td>{label(a.stage)}</td><td>{label(a.status)}</td>
                    <td><details><summary>view</summary><pre>{JSON.stringify(a.detail, null, 2)}</pre></details></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
