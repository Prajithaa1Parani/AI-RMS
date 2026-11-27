"""Flask API. Routes are thin: they check who is calling, then use pipeline / rules / db."""
import json
import os
import uuid
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import llm
import pipeline
import prompts
import rules
import schemas

FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
app = Flask(__name__, static_folder=None)
CORS(app)
signer = URLSafeTimedSerializer(config.SECRET_KEY)
db.init_db()

JSON_COLUMNS = ["clarifications", "pending_questions", "fields", "unavailable_info",
                "verification", "rule_notes"]


# ---------------------------------------------------------------- auth
def login_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            try:
                user_id = signer.loads(token, max_age=8 * 3600)
            except BadSignature:
                return jsonify(error="Please log in again."), 401
            user = db.query_one("SELECT id, username, full_name, role, department FROM users"
                                " WHERE id = ?", (user_id,))
            if not user:
                return jsonify(error="Please log in again."), 401
            if roles and user["role"] not in roles:
                db.audit("authorization", "denied", {"path": request.path, "role": user["role"]},
                         user_id=user["id"])
                return jsonify(error="You are not allowed to do this."), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.errorhandler(rules.RuleViolation)
def rule_violation(e):
    db.audit("business_rules", "rejected", {"rule": e.rule, "message": str(e),
                                            "path": request.path},
             user_id=getattr(g, "user", {}).get("id"))
    return jsonify(error=str(e), rule=e.rule), 403


@app.post("/api/auth/login")
def login():
    body = request.get_json(silent=True) or {}
    user = db.query_one("SELECT * FROM users WHERE username = ?",
                        (str(body.get("username", "")).strip().lower(),))
    if not user or not check_password_hash(user["password_hash"], str(body.get("password", ""))):
        return jsonify(error="Wrong username or password."), 401
    return jsonify(token=signer.dumps(user["id"]),
                   user={k: user[k] for k in ("id", "username", "full_name", "role", "department")})


@app.post("/api/auth/register")
def register():
    """Self-registration always creates a student. Staff/admin accounts are seeded."""
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", "")).strip().lower()
    password, name = str(body.get("password", "")), str(body.get("full_name", "")).strip()
    if not (3 <= len(username) <= 30 and username.isalnum()) or len(password) < 6 or not name:
        return jsonify(error="Username 3-30 letters/digits, password 6+ characters, name required."), 400
    if db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
        return jsonify(error="That username is taken."), 409
    db.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,'student')",
               (username, generate_password_hash(password), name[:80]))
    return jsonify(ok=True), 201


@app.get("/api/me")
@login_required()
def me():
    return jsonify(user=g.user, budget=llm.budget_status(g.user["id"]))


# ---------------------------------------------------------------- tickets
def _ticket_json(t):
    for col in JSON_COLUMNS:
        t[col] = json.loads(t[col]) if t.get(col) else None
    t["department_name"] = config.DEPARTMENTS.get(t["department"], {}).get("name")
    return t


def _get_ticket(ticket_id):
    t = db.query_one("SELECT t.*, u.full_name AS student_name FROM tickets t"
                     " JOIN users u ON u.id = t.student_id WHERE t.id = ?", (ticket_id,))
    if not t:
        return None
    rules.check_view(g.user, t)
    return t


@app.post("/api/estimate")
@login_required("student")
def estimate():
    text = pipeline.preprocess((request.get_json(silent=True) or {}).get("text", ""))
    return jsonify(pipeline.estimate_request(text, g.user["id"]))


@app.post("/api/tickets")
@login_required("student")
def create_ticket():
    body = request.get_json(silent=True) or {}
    text = pipeline.preprocess(str(body.get("text", "")))
    document = pipeline.preprocess(str(body.get("document_text", "")))
    if document:
        text = f"{text}\n\nAttached document:\n{document}"
    if len(text) < 15:
        return jsonify(ok=False, outcome="rejected", steps=[],
                       message="Please describe your complaint (at least 15 characters)."), 400
    key = str(body.get("idempotency_key") or uuid.uuid4())
    result = pipeline.process(g.user, text, key)
    return jsonify(result), 200 if result["ok"] else 422


@app.post("/api/tickets/<int:ticket_id>/clarify")
@login_required("student")
def clarify(ticket_id):
    ticket = _get_ticket(ticket_id)
    if not ticket:
        return jsonify(error="Ticket not found."), 404
    if ticket["status"] != "needs_clarification":
        raise rules.RuleViolation("R1", "This ticket is not waiting for clarification.")
    body = request.get_json(silent=True) or {}
    answer = pipeline.preprocess(str(body.get("answer", "")))
    if len(answer) < 2:
        return jsonify(error="Please type an answer."), 400
    clarifications = json.loads(ticket["clarifications"])
    clarifications.append({"question": " / ".join(json.loads(ticket["pending_questions"])),
                           "answer": answer})
    key = str(body.get("idempotency_key") or uuid.uuid4())
    result = pipeline.process(g.user, ticket["original_text"], key, ticket, clarifications)
    return jsonify(result), 200 if result["ok"] else 422


@app.get("/api/tickets")
@login_required()
def list_tickets():
    base = ("SELECT t.id, t.summary, t.category, t.priority, t.department, t.status, t.created_at,"
            " t.updated_at, t.injection_flag, u.full_name AS student_name FROM tickets t"
            " JOIN users u ON u.id = t.student_id")
    if g.user["role"] == "student":
        rows = db.query(base + " WHERE t.student_id = ? ORDER BY t.id DESC", (g.user["id"],))
    elif g.user["role"] == "staff":
        rows = db.query(base + " WHERE t.department = ? AND t.status IN"
                               " ('open','in_progress','resolved','closed') ORDER BY t.id DESC",
                        (g.user["department"],))
    else:
        rows = db.query(base + " ORDER BY t.id DESC")
    for r in rows:
        r["department_name"] = config.DEPARTMENTS.get(r["department"], {}).get("name")
    return jsonify(tickets=rows)


@app.get("/api/tickets/<int:ticket_id>")
@login_required()
def get_ticket(ticket_id):
    ticket = _get_ticket(ticket_id)
    if not ticket:
        return jsonify(error="Ticket not found."), 404
    events = db.query("SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,))
    out = {"ticket": _ticket_json(ticket), "events": events}
    if g.user["role"] == "student":       # students do not see internal AI details
        for hidden in ("verification", "suggested_response", "ai_recommended_action"):
            out["ticket"].pop(hidden, None)
    else:
        logs = db.query("SELECT * FROM audit_logs WHERE ticket_id = ? ORDER BY id", (ticket_id,))
        for row in logs:
            row["detail"] = json.loads(row["detail"]) if row["detail"] else None
        out["audit"] = logs
    return jsonify(out)


@app.post("/api/tickets/<int:ticket_id>/status")
@login_required("staff", "admin")
def change_status(ticket_id):
    ticket = _get_ticket(ticket_id)
    if not ticket:
        return jsonify(error="Ticket not found."), 404
    body = request.get_json(silent=True) or {}
    new_status, response = str(body.get("status", "")), str(body.get("response", "")).strip()
    rules.check_status_change(g.user, ticket, new_status)
    if new_status == "resolved" and len(response or ticket["staff_response"] or "") < 10:
        raise rules.RuleViolation("R1", "A reply to the student is required to resolve a ticket.")
    with db.transaction() as conn:
        conn.execute("UPDATE tickets SET status = ?, staff_response = COALESCE(NULLIF(?, ''),"
                     " staff_response), updated_at = ? WHERE id = ?",
                     (new_status, response[:2000], db.now(), ticket_id))
        conn.execute("INSERT INTO ticket_events (ticket_id, actor, from_status, to_status, note,"
                     " created_at) VALUES (?,?,?,?,?,?)",
                     (ticket_id, g.user["username"], ticket["status"], new_status,
                      "Status changed by staff.", db.now()))
    db.audit("status_change", "committed", {"from": ticket["status"], "to": new_status},
             ticket_id=ticket_id, user_id=g.user["id"])
    return jsonify(ok=True, status=new_status)


@app.post("/api/tickets/<int:ticket_id>/review")
@login_required("admin")
def review(ticket_id):
    """Human-in-the-loop: admin approves (optionally correcting the routing) or rejects."""
    ticket = _get_ticket(ticket_id)
    if not ticket:
        return jsonify(error="Ticket not found."), 404
    body = request.get_json(silent=True) or {}
    decision = str(body.get("decision", ""))
    category = body.get("category") or ticket["category"]
    department = body.get("department") or ticket["department"]
    priority = body.get("priority") or ticket["priority"] or "medium"
    rules.check_review(g.user, ticket, decision, category, department)
    if priority not in config.PRIORITIES:
        raise rules.RuleViolation("R1", "Unknown priority.")
    new_status = "open" if decision == "approve" else "rejected"
    with db.transaction() as conn:
        conn.execute("UPDATE tickets SET status = ?, category = ?, department = ?, priority = ?,"
                     " updated_at = ? WHERE id = ?",
                     (new_status, category, department, priority, db.now(), ticket_id))
        conn.execute("INSERT INTO ticket_events (ticket_id, actor, from_status, to_status, note,"
                     " created_at) VALUES (?,?,?,?,?,?)",
                     (ticket_id, g.user["username"], ticket["status"], new_status,
                      str(body.get("note", ""))[:300] or f"Admin decision: {decision}.", db.now()))
    db.audit("human_review", decision,
             {"from": ticket["status"], "to": new_status, "category": category,
              "department": department, "priority": priority},
             ticket_id=ticket_id, user_id=g.user["id"])
    return jsonify(ok=True, status=new_status)


# ---------------------------------------------------------------- admin
@app.get("/api/admin/audit")
@login_required("admin")
def audit_log():
    rows = db.query("SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id"
                    " ORDER BY a.id DESC LIMIT 300")
    for r in rows:
        r["detail"] = json.loads(r["detail"]) if r["detail"] else None
    return jsonify(audit=rows)


@app.get("/api/admin/usage")
@login_required("admin")
def usage():
    by_model = db.query(
        "SELECT model_key, provider, COUNT(*) AS calls,"
        " SUM(outcome = 'success') AS successes, SUM(outcome LIKE 'error_%') AS errors,"
        " SUM(cached) AS cache_hits, SUM(input_tokens) AS input_tokens,"
        " SUM(output_tokens) AS output_tokens, ROUND(SUM(cost_usd), 6) AS cost_usd,"
        " CAST(AVG(CASE WHEN outcome = 'success' THEN latency_ms END) AS INTEGER) AS avg_latency_ms"
        " FROM llm_calls GROUP BY model_key ORDER BY calls DESC")
    calls = db.query("SELECT c.*, u.username FROM llm_calls c LEFT JOIN users u ON u.id = c.user_id"
                     " ORDER BY c.id DESC LIMIT 150")
    return jsonify(by_model=by_model, calls=calls, budget=llm.budget_status(g.user["id"]),
                   cache_entries=db.query_one("SELECT COUNT(*) AS n FROM llm_cache")["n"])


@app.get("/api/admin/settings")
@login_required("admin")
def get_settings():
    return jsonify(settings=db.get_settings(), providers=llm.provider_status(),
                   models=config.MODELS, routing=config.ROUTING, gen_params=config.GEN_PARAMS,
                   prompt_versions=prompts.PROMPT_VERSIONS, mock_mode=config.MOCK_LLM,
                   limits={"long_input_tokens": config.LONG_INPUT_TOKENS,
                           "chunk_tokens": config.CHUNK_TOKENS,
                           "hard_input_tokens": config.HARD_INPUT_TOKENS,
                           "refund_limit": config.REFUND_LIMIT,
                           "daily_ticket_limit": config.DAILY_TICKET_LIMIT,
                           "max_schema_retries": config.MAX_SCHEMA_RETRIES,
                           "max_api_retries": config.MAX_API_RETRIES})


@app.post("/api/admin/settings")
@login_required("admin")
def update_settings():
    body = request.get_json(silent=True) or {}
    checks = {
        "project_budget_usd": lambda v: isinstance(v, (int, float)) and 0 <= v <= 100,
        "user_daily_budget_usd": lambda v: isinstance(v, (int, float)) and 0 <= v <= 100,
        "sim_fail_provider": lambda v: v in ("", "gemini", "groq", "openai", "all"),
        "sim_fail_kind": lambda v: v in ("timeout", "rate_limit", "invalid_credentials", "outage"),
        "sim_malformed_attempts": lambda v: isinstance(v, int) and 0 <= v <= 5,
        "sim_force_disagreement": lambda v: isinstance(v, bool),
    }
    for key, value in body.items():
        if key not in checks or not checks[key](value):
            return jsonify(error=f"Invalid value for {key}."), 400
    for key, value in body.items():
        db.set_setting(key, value)
    db.audit("settings", "updated", body, user_id=g.user["id"])
    return jsonify(settings=db.get_settings())


@app.post("/api/admin/clear-cache")
@login_required("admin")
def clear_cache():
    db.execute("DELETE FROM llm_cache")
    db.audit("settings", "cache_cleared", user_id=g.user["id"])
    return jsonify(ok=True)


@app.post("/api/admin/compare")
@login_required("admin")
def compare_models():
    """Run the same classification on every model and show the answers side by side."""
    text = pipeline.preprocess(str((request.get_json(silent=True) or {}).get("text", "")))
    if len(text) < 15:
        return jsonify(error="Enter a sample complaint (at least 15 characters)."), 400
    user_msg = prompts.classify_user(text[:300], {}, text)
    rows = []
    for model_key in config.MODELS:
        row = {"model_key": model_key, "provider": config.MODELS[model_key]["provider"],
               "model": config.MODELS[model_key]["model"]}
        try:
            obj, meta = llm.call_json("classify", prompts.CLASSIFY_SYSTEM,
                                      [{"role": "user", "content": user_msg}],
                                      schemas.Classification, {"user_id": g.user["id"]},
                                      force_model=model_key, use_cache=False, simulate=False)
            row.update(ok=True, output=obj.model_dump(), latency_ms=meta["latency_ms"],
                       input_tokens=meta["input_tokens"], output_tokens=meta["output_tokens"],
                       cost_usd=meta["cost_usd"], schema_attempts=meta["schema_attempts"])
        except (llm.AllProvidersFailed, llm.SchemaValidationFailed, llm.BudgetExceeded) as e:
            row.update(ok=False, error=str(e))
        rows.append(row)
    return jsonify(results=rows)


@app.get("/api/health")
def health():
    """Used after deployment to verify frontend -> backend -> database -> LLM config."""
    try:
        users = db.query_one("SELECT COUNT(*) AS n FROM users")["n"]
        database = {"ok": True, "users": users}
    except Exception as e:
        database = {"ok": False, "error": str(e)}
    return jsonify(backend="ok", database=database, providers=llm.provider_status(),
                   mock_mode=config.MOCK_LLM)


@app.get("/api/meta")
def meta():
    return jsonify(categories=config.CATEGORIES, priorities=config.PRIORITIES,
                   departments={k: v["name"] for k, v in config.DEPARTMENTS.items()},
                   category_departments=config.CATEGORY_DEPARTMENTS)


# ---------------------------------------------------------------- frontend (production build)
@app.get("/")
@app.get("/<path:path>")
def frontend(path="index.html"):
    if path.startswith("api/"):
        return jsonify(error="Not found."), 404
    if not os.path.exists(os.path.join(FRONTEND_DIST, path)):
        path = "index.html"
    if not os.path.exists(os.path.join(FRONTEND_DIST, path)):
        return "Frontend not built yet. Run 'npm run build' inside the frontend folder.", 200
    return send_from_directory(FRONTEND_DIST, path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5050)), debug=os.getenv("FLASK_DEBUG") == "1")
