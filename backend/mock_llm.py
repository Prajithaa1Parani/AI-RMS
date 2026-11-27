"""Offline stand-in for the real providers (only used when MOCK_LLM=1 in .env).

It lets the whole workflow be developed and tested without API keys or cost.
It is keyword-based on purpose: it is a test double, not part of the AI workflow.
"""
import json
import re

KEYWORDS = [
    ("hostel", ["hostel", "room", "warden", "fan", "block"]),
    ("fees", ["fee", "refund", "payment", "paid", "fine"]),
    ("examination", ["exam", "marks", "result", "revaluation", "hall ticket"]),
    ("academics", ["attendance", "faculty", "lecture", "syllabus", "course"]),
    ("it_services", ["wifi", "wi-fi", "ums", "portal", "login", "internet"]),
    ("library", ["library", "book"]),
    ("transport", ["bus", "transport"]),
    ("mess_food", ["mess", "food", "canteen"]),
    ("placement", ["placement", "internship", "company"]),
]
DEPT = {"hostel": "hostel_office", "fees": "accounts", "examination": "examination_cell",
        "academics": "academic_affairs", "it_services": "it_helpdesk", "library": "library",
        "transport": "transport_office", "mess_food": "mess_committee",
        "placement": "placement_cell", "other": "student_welfare"}


def _untrusted(text):
    found = re.findall(r"<(untrusted_[a-z]+_[0-9a-f]{8})>\n(.*?)\n</\1>", text, re.S)
    return found[-1][1] if found else text


def _category(text):
    low = text.lower()
    for cat, words in KEYWORDS:
        if any(w in low for w in words):
            return cat
    return "other"


def _extract(text):
    marker = "Clarification from the student"
    if marker in text:                      # clarified answers replace earlier details
        head, tail = text.split(marker, 1)
        answers = " ".join(re.findall(r"Answer: (.*)", tail))
        text = answers + " " + re.sub(r"(?:rs\.?|₹|inr)\s*[\d,]{3,}", "", head, flags=re.I) \
            if re.search(r"(?:rs\.?|₹|inr)\s*[\d,]{3,}", answers, re.I) else answers + " " + head
    low = text.lower()
    amounts = sorted({a.replace(",", "") for a in
                      re.findall(r"(?:rs\.?|₹|inr)\s*([\d,]{3,})", low)})
    course = re.search(r"\b[A-Z]{3}\d{3}\b", text)
    date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    loc = re.search(r"\b((?:room|block|bh|gh)[\s-]*[\w-]*\d[\w-]*(?:[\s,]+(?:room|block)[\s-]*[\w-]*\d[\w-]*)?)",
                    text, re.I)
    ref = re.search(r"\b(?:TXN|REF|RCPT)[\w-]+\b", text)
    contradictions = []
    if len(amounts) > 1:
        contradictions.append({"field": "amount", "values": amounts,
                               "explanation": "Different amounts are stated for the same payment."})
    requests_ = []
    if "phone number" in low or "contact number" in low:
        requests_.append("phone number of a staff member")
    if "which date" in low or "exact date" in low:
        requests_.append("exact date of resolution")
    actionable = len(text.split()) >= 6
    return {
        "issue_summary": (text.strip()[:300] + " (summary)").ljust(10, "."),
        "category": _category(text),
        "fields": {"location": loc.group(1) if loc else None,
                   "incident_date": date.group(0) if date else None,
                   "amount": float(amounts[0]) if len(amounts) == 1 else None,
                   "course_code": course.group(0) if course else None,
                   "reference_id": ref.group(0) if ref else None},
        "is_actionable": actionable,
        "clarifying_questions": [] if actionable else ["What exactly is the problem and where did it happen?"],
        "contradictions": contradictions,
        "information_requests": requests_,
        "injection_suspected": bool(re.search(r"ignore (all|previous|the above)|system:", low)),
    }


def _classify(user_message, text):
    low = text.lower()
    cat = _category(text)
    priority = "medium"
    if any(w in low for w in ["unsafe", "harass", "ragging", "injur", "fire"]):
        priority = "critical"
    elif any(w in low for w in ["urgent", "tomorrow", "exam", "deadline"]):
        priority = "high"
    record = re.search(r'"amount": ([\d.]+)', user_message)
    refund = "refund" in low
    return {"category": cat, "priority": priority, "department": DEPT[cat],
            "recommended_action": "approve_refund" if refund else "assign_to_department",
            "refund_amount": float(record.group(1)) if refund and record else None,
            "confidence": 0.82, "reasoning": "Keyword based mock classification."}


def answer(task, system, messages):
    user_message = messages[-1]["content"]
    text = _untrusted(user_message)
    if task == "summarize":
        data = {"summary": " ".join(text.split())[:420]}
    elif task == "extract":
        data = _extract(text)
    elif task in ("classify", "verify", "tiebreak"):
        data = _classify(user_message, text)
    else:
        unavailable = json.loads(re.search(r"tell them:\n(\[.*?\])\n", user_message, re.S).group(1))
        data = {"response_text": "Thank you for writing to us. Your complaint has been recorded "
                                 "and sent to the concerned department, which will review it within "
                                 "its standard response time. Some details you asked for are not "
                                 "available in this system. You can track progress on your dashboard.",
                "unavailable_information": unavailable}
    out = json.dumps(data)
    prompt_chars = len(system) + sum(len(m["content"]) for m in messages)
    return out, prompt_chars // 4, len(out) // 4
