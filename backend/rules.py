"""Deterministic business rules. No AI in this file.

The AI only *recommends*. These functions decide what is actually allowed, and they
can reject or override an AI recommendation. Every rule has an id (R1..R9) that is
written to the audit log and shown in the report.
"""
import hashlib
import re

import config
import db


class RuleViolation(Exception):
    def __init__(self, rule, message):
        super().__init__(message)
        self.rule = rule


def text_hash(text):
    normalised = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha256(normalised.encode()).hexdigest()


# ---------------------------------------------------------------- before any AI call
def check_before_processing(user, thash):
    """R1 role, R2 duplicate, R3 daily limit. Runs before money is spent on AI."""
    if user["role"] != "student":
        raise RuleViolation("R1", "Only students can file a complaint.")
    dup = db.query_one(
        "SELECT id, status FROM tickets WHERE student_id = ? AND text_hash = ?"
        " AND status NOT IN ('closed','rejected')", (user["id"], thash))
    if dup:
        raise RuleViolation("R2", f"Duplicate: you already filed this complaint as ticket "
                                  f"#{dup['id']} (status: {dup['status']}).")
    today = db.query_one(
        "SELECT COUNT(*) AS n FROM tickets WHERE student_id = ? AND created_at >= date('now')",
        (user["id"],))["n"]
    if today >= config.DAILY_TICKET_LIMIT:
        raise RuleViolation("R3", f"Daily limit reached: at most {config.DAILY_TICKET_LIMIT} "
                                  "complaints per day.")


# ---------------------------------------------------------------- required fields
def missing_required_fields(category, fields):
    """R4: category-specific fields must be present. Returns questions to ask."""
    return [config.FIELD_QUESTIONS[f] for f in config.REQUIRED_FIELDS.get(category, [])
            if fields.get(f) in (None, "")]


# ---------------------------------------------------------------- after the AI decision
def apply_to_decision(decision, fields, verified, injection_flag):
    """Turn an AI recommendation into what will really be stored.

    Returns (status, department, notes). `notes` lists every rule that changed or
    rejected something the AI recommended.
    """
    notes = []
    category = decision["category"]
    allowed = config.CATEGORY_DEPARTMENTS[category]
    department = decision["department"]

    # R5: department must be one that is allowed to handle this category.
    if department not in allowed:
        notes.append({"rule": "R5", "result": "ai_overridden",
                      "message": f"AI chose department '{department}' which may not handle "
                                 f"'{category}' complaints. Backend assigned '{allowed[0]}'."})
        department = allowed[0]

    status = "open"

    # R6: the AI can never approve a refund. Above the limit only an admin can.
    if decision["recommended_action"] == "approve_refund":
        amount = decision.get("refund_amount") or fields.get("amount") or 0
        stated = fields.get("amount")
        if stated is not None and amount > stated:
            notes.append({"rule": "R6", "result": "ai_rejected",
                          "message": f"AI suggested refunding {amount:.0f} but the complaint only "
                                     f"mentions {stated:.0f}. Amount capped to the stated value."})
            amount = stated
        if amount > config.REFUND_LIMIT:
            notes.append({"rule": "R6", "result": "ai_rejected",
                          "message": f"AI recommended approving a refund of Rs {amount:.0f}. Rejected: "
                                     f"above the Rs {config.REFUND_LIMIT} limit. No refund was "
                                     "executed; an administrator must approve."})
        else:
            notes.append({"rule": "R6", "result": "ai_rejected",
                          "message": "AI recommended approving a refund. Rejected: refunds are never "
                                     "executed automatically. Sent for human approval."})
        status = "pending_approval"

    # R7: critical priority and escalations need a human to confirm.
    if decision["priority"] == "critical" or decision["recommended_action"] == "escalate_to_admin":
        notes.append({"rule": "R7", "result": "needs_approval",
                      "message": "Critical / escalated complaints need administrator confirmation."})
        status = "pending_approval"

    # R8: the AI cannot throw a complaint away. Suspected injection is also reviewed.
    if decision["recommended_action"] == "reject_invalid":
        notes.append({"rule": "R8", "result": "ai_rejected",
                      "message": "AI recommended rejecting the complaint. Only a human may reject."})
        status = "needs_review"
    if injection_flag:
        notes.append({"rule": "R8", "result": "needs_review",
                      "message": "Possible prompt injection in the text. Sent for human review."})
        status = "needs_review"

    # R9: a decision the two models could not agree on is never auto-executed.
    if not verified:
        notes.append({"rule": "R9", "result": "needs_review",
                      "message": "Models did not agree (or verification was unavailable)."})
        status = "needs_review"

    return status, department, notes


# ---------------------------------------------------------------- human actions
def check_view(user, ticket):
    if user["role"] == "admin":
        return
    if user["role"] == "student" and ticket["student_id"] == user["id"]:
        return
    if user["role"] == "staff" and ticket["department"] == user["department"]:
        return
    raise RuleViolation("R1", "You are not allowed to view this ticket.")


def check_status_change(user, ticket, new_status):
    """Staff may move only their own department's tickets along the allowed flow."""
    if user["role"] not in ("staff", "admin"):
        raise RuleViolation("R1", "Only staff can change ticket status.")
    if user["role"] == "staff" and ticket["department"] != user["department"]:
        raise RuleViolation("R1", "This ticket belongs to another department.")
    if new_status not in config.STATUS_FLOW.get(ticket["status"], []):
        raise RuleViolation("R1", f"Status cannot go from '{ticket['status']}' to '{new_status}'.")


def check_review(user, ticket, decision, category, department):
    """Admin approval / rejection of tickets waiting for a human."""
    if user["role"] != "admin":
        raise RuleViolation("R1", "Only an administrator can review this ticket.")
    if ticket["status"] not in ("needs_review", "pending_approval"):
        raise RuleViolation("R1", f"Ticket is '{ticket['status']}', nothing to review.")
    if decision not in ("approve", "reject"):
        raise RuleViolation("R1", "Decision must be 'approve' or 'reject'.")
    if decision == "approve":
        if category not in config.CATEGORY_DEPARTMENTS:
            raise RuleViolation("R5", "Unknown category.")
        if department not in config.CATEGORY_DEPARTMENTS[category]:
            raise RuleViolation("R5", f"'{department}' may not handle '{category}' complaints.")
