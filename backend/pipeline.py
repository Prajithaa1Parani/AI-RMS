"""The complaint workflow (orchestration).

INPUT -> preprocess -> business pre-checks -> token/budget check -> [chunk + summarise]
      -> Stage 1 extract -> (clarification?) -> Stage 2 classify + verify in parallel
      -> (tie-break / human review?) -> Stage 3 draft reply + grounding check
      -> business rules -> database write -> audit -> result for the frontend
"""
import json
import re
import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor

import config
import db
import llm
import prompts
import rules
import schemas

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|above|prior|earlier) (instructions|rules|prompts?)",
    r"disregard (all |the )?(previous|above|prior)",
    r"you are now\b", r"\bsystem\s*:", r"\bassistant\s*:", r"</?\s*(system|untrusted)",
    r"(reveal|print|show) (your |the )?(system )?(prompt|instructions)",
    r"(set|mark|change) (the |this )?(priority|status|department) (to|as)\b",
    r"approve (the |my |a )?refund", r"act as\b", r"jailbreak", r"developer mode",
]


class Trace:
    """Collects workflow steps for the frontend and writes each one to the audit log."""

    def __init__(self, run_id, user_id, ticket_id=None):
        self.run_id, self.user_id, self.ticket_id = run_id, user_id, ticket_id
        self.steps = []

    def add(self, stage, status, detail=None):
        self.steps.append({"stage": stage, "status": status, "detail": detail})
        db.audit(stage, status, detail, self.run_id, self.ticket_id, self.user_id)

    @property
    def ctx(self):
        return {"run_id": self.run_id, "user_id": self.user_id}


# ---------------------------------------------------------------- helpers
def preprocess(text):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def scan_injection(text):
    low = text.lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, low)]


def chunk_text(text, chunk_tokens=config.CHUNK_TOKENS):
    """Token-aware chunking on sentence boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    chunks, current = [], ""
    for s in sentences:
        while llm.estimate_tokens(s) > chunk_tokens:          # one giant sentence
            cut = chunk_tokens * 4
            sentences_part, s = s[:cut], s[cut:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentences_part)
        if current and llm.estimate_tokens(current + " " + s) > chunk_tokens:
            chunks.append(current)
            current = s
        else:
            current = (current + " " + s).strip()
    if current:
        chunks.append(current)
    return chunks


def estimate_request(text, user_id):
    """Token and cost estimate made before any expensive call."""
    tokens = llm.estimate_tokens(text)
    long_input = tokens > config.LONG_INPUT_TOKENS
    n_chunks = len(chunk_text(text)) if long_input else 0
    working = min(tokens, n_chunks * 130) if long_input else tokens
    plan = [("summarize", config.CHUNK_TOKENS)] * n_chunks + \
           [("extract", working), ("classify", working), ("verify", working), ("respond", working)]
    expected = cheapest = 0.0
    for task, t in plan:
        t_in, t_out = t + 900, config.GEN_PARAMS[task]["max_tokens"]   # ~900 prompt tokens
        expected += llm.cost_usd(config.ROUTING[task]["candidates"][0], t_in, t_out)
        cheapest += min(llm.cost_usd(k, t_in, t_out) for k in config.ROUTING[task]["candidates"])
    return {"input_tokens": tokens, "long_input": long_input, "chunks": n_chunks,
            "llm_calls": len(plan), "expected_cost_usd": round(expected, 6),
            "minimum_cost_usd": round(cheapest, 6), "budget": llm.budget_status(user_id),
            "too_large": tokens > config.HARD_INPUT_TOKENS}


def grounding_problems(response_text, sources):
    """Deterministic hallucination check: e-mails and numbers in the reply must
    exist in the complaint or in our own facts."""
    haystack = sources.lower()
    problems = []
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", response_text):
        if email.lower() not in haystack:
            problems.append(f"e-mail '{email}' is not in the known facts")
    for number in re.findall(r"\d[\d,\-\s]{2,}\d", response_text):
        digits = re.sub(r"\D", "", number)
        if len(digits) >= 4 and digits not in re.sub(r"[,\-\s]", "", haystack):
            problems.append(f"number '{number.strip()}' is not in the known facts")
    return problems


def template_reply(dept):
    return (f"Thank you for contacting us. Your complaint has been recorded and sent to the "
            f"{dept['name']}. The usual response time is {dept['sla_hours']} hours. You can follow "
            f"the status on your dashboard. For questions write to {dept['email']}.")


# ---------------------------------------------------------------- stages
def stage_long_input(text, trace):
    tokens = llm.estimate_tokens(text)
    if tokens <= config.LONG_INPUT_TOKENS:
        trace.add("context_management", "skipped",
                  {"input_tokens": tokens, "limit": config.LONG_INPUT_TOKENS})
        return text
    chunks = chunk_text(text)
    summaries, used = [], []
    for i, chunk in enumerate(chunks, 1):
        obj, meta = llm.call_json("summarize", prompts.SUMMARIZE_SYSTEM,
                                  [{"role": "user", "content": prompts.summarize_user(chunk, i, len(chunks))}],
                                  schemas.ChunkSummary, trace.ctx)
        summaries.append(obj.summary)
        used.append(meta["model_key"])
    combined = "\n".join(f"[Part {i}] {s}" for i, s in enumerate(summaries, 1))
    trace.add("context_management", "chunked_and_summarised",
              {"input_tokens": tokens, "limit": config.LONG_INPUT_TOKENS, "chunks": len(chunks),
               "chunk_tokens": [llm.estimate_tokens(c) for c in chunks],
               "tokens_after": llm.estimate_tokens(combined), "models": sorted(set(used))})
    return combined


def stage_extract(text, trace):
    obj, meta = llm.call_json("extract", prompts.EXTRACT_SYSTEM,
                              prompts.extract_messages(text), schemas.Extraction, trace.ctx)
    data = obj.model_dump()
    trace.add("extraction", "completed", {"output": data, "model": _model_info(meta)})
    return data


def _model_info(meta):
    return {k: meta[k] for k in ("model_key", "provider", "reason", "fallback_used",
                                 "errors_before", "cached", "cost_usd", "latency_ms")}


def _classify(task, user_msg, ctx, avoid=()):
    obj, meta = llm.call_json(task, prompts.CLASSIFY_SYSTEM,
                              [{"role": "user", "content": user_msg}],
                              schemas.Classification, ctx, avoid_providers=avoid)
    return obj.model_dump(), meta


def stage_classify_and_verify(extraction, text, trace):
    """Primary and verifier run in parallel on different providers, then are compared
    field by field. Returns (decision, verified, verification_record)."""
    user_msg = prompts.classify_user(extraction["issue_summary"], extraction["fields"], text)
    primary_provider = config.MODELS[config.ROUTING["classify"]["candidates"][0]]["provider"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_primary = pool.submit(_classify, "classify", user_msg, trace.ctx)
        f_verify = pool.submit(_classify, "verify", user_msg, trace.ctx, (primary_provider,))
        primary, p_meta = f_primary.result()      # primary failure stops the workflow
        try:
            verifier, v_meta = f_verify.result()
        except (llm.AllProvidersFailed, llm.SchemaValidationFailed) as e:
            verifier, v_meta = None, {"error": str(e)}

    # If the primary fell back onto the verifier's provider, verify again elsewhere.
    if verifier and v_meta["provider"] == p_meta["provider"]:
        try:
            verifier, v_meta = _classify("verify", user_msg, trace.ctx, (p_meta["provider"],))
        except (llm.AllProvidersFailed, llm.SchemaValidationFailed) as e:
            verifier, v_meta = None, {"error": str(e)}

    record = {"primary": {"output": primary, "model": _model_info(p_meta)}}
    if verifier is None:
        record.update(result="unverified", reason=v_meta["error"])
        trace.add("verification", "unverified", record)
        return primary, False, record

    if db.get_settings()["sim_force_disagreement"]:           # Test Lab (task 6)
        verifier = dict(verifier, priority="low" if primary["priority"] != "low" else "high",
                        category="other", department="student_welfare")
        record["simulated_disagreement"] = True

    record["verifier"] = {"output": verifier, "model": _model_info(v_meta)}
    diffs = {f: {"primary": primary[f], "verifier": verifier[f]}
             for f in schemas.COMPARE_FIELDS if primary[f] != verifier[f]}
    if not diffs:
        record.update(result="agreed", compared_fields=schemas.COMPARE_FIELDS)
        trace.add("verification", "agreed", record)
        return primary, True, record

    # Disagreement -> third model from another provider, then majority vote per field.
    record["differences"] = diffs
    try:
        third, t_meta = _classify("tiebreak", user_msg, trace.ctx,
                                  (p_meta["provider"], v_meta["provider"]))
    except (llm.AllProvidersFailed, llm.SchemaValidationFailed) as e:
        record.update(result="disagreed_unresolved", reason=f"No third model available: {e}")
        trace.add("verification", "disagreed", record)
        return primary, False, record

    record["tiebreak"] = {"output": third, "model": _model_info(t_meta)}
    decision, unresolved = dict(primary), []
    for f in diffs:
        votes = [primary[f], verifier[f], third[f]]
        winner = max(set(votes), key=votes.count)
        if votes.count(winner) >= 2:
            decision[f] = winner
        else:
            unresolved.append(f)
    if unresolved:
        record.update(result="disagreed_unresolved", unresolved_fields=unresolved)
        trace.add("verification", "disagreed", record)
        return primary, False, record
    record.update(result="resolved_by_majority", final=decision)
    trace.add("verification", "resolved_by_majority", record)
    return decision, True, record


def stage_respond(extraction, decision, department, text, trace):
    dept = config.DEPARTMENTS[department]
    facts = {"department": dept["name"], "department_email": dept["email"],
             "usual_response_time_hours": dept["sla_hours"], "category": decision["category"],
             "priority": decision["priority"],
             "next_step": "The department reviews the complaint and updates the ticket."}
    user_msg = prompts.respond_user(facts, extraction["information_requests"], text)
    sources = text + json.dumps(facts)
    try:
        for attempt in (1, 2):
            obj, meta = llm.call_json("respond", prompts.RESPOND_SYSTEM,
                                      [{"role": "user", "content": user_msg}],
                                      schemas.ResponseDraft, trace.ctx, use_cache=attempt == 1)
            problems = grounding_problems(obj.response_text, sources)
            if not problems:
                trace.add("response_draft", "grounded",
                          {"output": obj.model_dump(), "model": _model_info(meta)})
                return obj.response_text, obj.unavailable_information
            trace.add("grounding_check", "failed", {"attempt": attempt, "problems": problems,
                                                    "rejected_text": obj.response_text})
            user_msg += ("\n\nYour last reply contained unsupported details ("
                         + "; ".join(problems) + "). Remove anything not in KNOWN FACTS.")
        reason = "Reply still contained unsupported facts after 2 attempts"
    except (llm.AllProvidersFailed, llm.SchemaValidationFailed) as e:
        reason = str(e)
    trace.add("response_draft", "template_used", {"reason": reason})
    return template_reply(dept), list(extraction["information_requests"])


# ---------------------------------------------------------------- main workflow
def _build_text(original, clarifications):
    if not clarifications:
        return original
    lines = "\n".join(f"- Question: {c['question']}\n  Answer: {c['answer']}" for c in clarifications)
    return f"{original}\n\nClarification from the student (replaces conflicting details above):\n{lines}"


def _run(user, ticket, original, clarifications, trace):
    text = _build_text(original, clarifications)

    # 1. Preprocessing + injection scan (deterministic)
    patterns = scan_injection(text)
    trace.add("preprocessing", "completed",
              {"characters": len(text), "injection_patterns_found": patterns})

    # 2. Token and budget check before spending anything
    est = estimate_request(text, user["id"])
    if est["too_large"]:
        trace.add("token_estimate", "rejected", est)
        return {"outcome": "rejected", "message": f"Input is too long ({est['input_tokens']} tokens; "
                f"limit {config.HARD_INPUT_TOKENS}). Please shorten it."}
    if est["minimum_cost_usd"] > est["budget"]["remaining_usd"]:
        trace.add("budget_check", "blocked", est)
        raise llm.BudgetExceeded(
            f"AI budget reached ({est['budget']['scope']}): this request needs about "
            f"${est['minimum_cost_usd']:.5f} but only ${est['budget']['remaining_usd']:.5f} is left.")
    trace.add("token_estimate", "ok", est)

    # 3. Long input -> chunk + summarise
    working = stage_long_input(text, trace)

    # 4. Stage 1: extraction
    extraction = stage_extract(working, trace)
    injection = bool(patterns) or extraction["injection_suspected"]
    if injection:
        trace.add("injection_guard", "flagged",
                  {"patterns": patterns, "model_flag": extraction["injection_suspected"],
                   "action": "Text treated as data only; ticket will be sent for human review."})

    # 5. Conditional: incomplete or contradictory -> ask the student, do not assume
    questions = rules.missing_required_fields(extraction["category"], extraction["fields"])
    questions += [f"Conflicting {c['field']}: {' vs '.join(c['values'])}. Which one is correct?"
                  for c in extraction["contradictions"]]
    if not extraction["is_actionable"]:
        questions += extraction["clarifying_questions"] or ["Please describe the problem in more detail."]
    base = {"summary": extraction["issue_summary"], "category": extraction["category"],
            "fields": json.dumps(extraction["fields"]), "injection_flag": int(injection)}

    if questions and len(clarifications) < config.MAX_CLARIFICATION_ROUNDS:
        trace.add("clarification", "required",
                  {"questions": questions, "contradictions": extraction["contradictions"],
                   "round": len(clarifications) + 1})
        tid = save_ticket(user, ticket, original, clarifications, trace,
                          dict(base, status="needs_clarification",
                               pending_questions=json.dumps(questions)),
                          "Waiting for the student to clarify.")
        return {"outcome": "needs_clarification", "ticket_id": tid, "questions": questions,
                "message": "We need a little more information before this can be processed."}

    # 6. Stage 2: classification + independent verification (parallel)
    decision, verified, verification = stage_classify_and_verify(extraction, working, trace)
    if questions:      # still unclear after the allowed rounds -> a human decides
        verified = False
        trace.add("clarification", "exhausted", {"open_questions": questions})

    # 7. Deterministic business rules
    status, department, notes = rules.apply_to_decision(decision, extraction["fields"],
                                                        verified, injection)
    trace.add("business_rules", "applied",
              {"ai_recommendation": decision, "final_status": status,
               "final_department": department, "rule_notes": notes})

    # 8. Stage 3: draft reply for staff (grounded)
    reply, unavailable = stage_respond(extraction, decision, department, working, trace)

    # 9. Database write
    tid = save_ticket(user, ticket, original, clarifications, trace,
                      dict(base, status=status, category=decision["category"],
                           priority=decision["priority"], department=department,
                           suggested_response=reply, unavailable_info=json.dumps(unavailable),
                           ai_recommended_action=decision["recommended_action"],
                           verification=json.dumps(verification), rule_notes=json.dumps(notes),
                           pending_questions="[]"),
                      "Processed by AI workflow.")
    messages = {"open": "Complaint saved and assigned to the department.",
                "pending_approval": "Complaint saved. It needs administrator approval before action.",
                "needs_review": "Complaint saved. It was sent for human review."}
    return {"outcome": "saved", "ticket_id": tid, "status": status, "message": messages[status]}


def save_ticket(user, ticket, original, clarifications, trace, values, note):
    """Create or update the ticket and its history row in ONE transaction."""
    now = db.now()
    with db.transaction() as conn:
        if ticket is None:
            cols = dict(values, student_id=user["id"], original_text=original,
                        text_hash=rules.text_hash(original),
                        clarifications=json.dumps(clarifications), created_at=now, updated_at=now)
            sql = (f"INSERT INTO tickets ({', '.join(cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))})")
            tid, old = conn.execute(sql, list(cols.values())).lastrowid, None
        else:
            cols = dict(values, clarifications=json.dumps(clarifications), updated_at=now)
            sql = f"UPDATE tickets SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?"
            conn.execute(sql, list(cols.values()) + [ticket["id"]])
            tid, old = ticket["id"], ticket["status"]
        conn.execute("INSERT INTO ticket_events (ticket_id, actor, from_status, to_status, note,"
                     " created_at) VALUES (?,?,?,?,?,?)",
                     (tid, "ai_workflow", old, values["status"], note, now))
        conn.execute("UPDATE audit_logs SET ticket_id = ? WHERE run_id = ?", (tid, trace.run_id))
    trace.ticket_id = tid
    saved = db.query_one("SELECT id, status, department FROM tickets WHERE id = ?", (tid,))
    trace.add("database_write", "committed", {"ticket": saved})   # read back = proof it was stored
    return tid


def process(user, original, idempotency_key, ticket=None, clarifications=None):
    """Entry point used by the API. Always returns a truthful result dict."""
    # Idempotency: the same key never runs the workflow twice.
    try:
        run_id = db.execute("INSERT INTO workflow_runs (idempotency_key, user_id, ticket_id, status,"
                            " created_at) VALUES (?,?,?,?,?)",
                            (idempotency_key, user["id"], ticket["id"] if ticket else None,
                             "running", db.now()))
    except sqlite3.IntegrityError:
        prev = db.query_one("SELECT * FROM workflow_runs WHERE idempotency_key = ?", (idempotency_key,))
        if prev["user_id"] != user["id"]:
            return {"ok": False, "outcome": "rejected", "message": "Invalid request key.", "steps": []}
        if prev["result"]:
            return dict(json.loads(prev["result"]), replayed=True,
                        message="This request was already processed. Showing the saved result.")
        return {"ok": False, "outcome": "in_progress", "steps": [],
                "message": "This request is already being processed."}

    trace = Trace(run_id, user["id"], ticket["id"] if ticket else None)
    try:
        if ticket is None:
            rules.check_before_processing(user, rules.text_hash(original))
        result = _run(user, ticket, original, clarifications or [], trace)
    except rules.RuleViolation as e:
        trace.add("business_rules", "rejected", {"rule": e.rule, "message": str(e)})
        result = {"outcome": "rejected", "message": str(e)}
    except llm.BudgetExceeded as e:
        trace.add("budget_check", "blocked", {"message": str(e)})
        result = {"outcome": "budget_exceeded", "message": str(e)}
    except llm.SchemaValidationFailed as e:
        trace.add("workflow", "failed_safely", {"reason": "schema_validation", "message": str(e)})
        result = {"outcome": "failed", "message": "The AI output could not be validated, so nothing "
                                                  "was saved. Please try again later."}
    except llm.AllProvidersFailed as e:
        trace.add("workflow", "failed_safely", {"reason": "providers_unavailable", "message": str(e)})
        result = {"outcome": "failed", "message": "All AI providers are unavailable right now, so "
                                                  "nothing was saved. Please try again later."}
    except Exception as e:                                   # never report success on error
        trace.add("workflow", "failed_safely", {"reason": "internal_error"})
        db.audit("internal_error", "error",       # full details stay in the backend only
                 {"message": str(e), "trace": traceback.format_exc()[-1500:]},
                 run_id, trace.ticket_id, user["id"])
        result = {"outcome": "failed", "message": "Internal error. Nothing was saved."}

    result["ok"] = result["outcome"] in ("saved", "needs_clarification")
    result["run_id"] = run_id
    result["steps"] = trace.steps
    usage = db.query_one("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,"
                         " COALESCE(SUM(output_tokens),0) AS output_tokens,"
                         " COALESCE(SUM(cost_usd),0) AS cost_usd FROM llm_calls WHERE run_id = ?",
                         (run_id,))
    result["usage"] = usage
    db.execute("UPDATE workflow_runs SET status = ?, ticket_id = ?, result = ?, finished_at = ?"
               " WHERE id = ?",
               (result["outcome"], result.get("ticket_id"), json.dumps(result, default=str),
                db.now(), run_id))
    return result
