"""Central configuration: API keys, model catalogue, limits and domain rules.

Everything a viva examiner may ask "where is this decided?" lives here.
API keys are read from the backend environment only - never sent to the frontend.
"""
import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "rms.db"))
MOCK_LLM = os.getenv("MOCK_LLM", "0") == "1"  # offline development only

# ---------------------------------------------------------------- providers
PROVIDERS = {
    "gemini": {
        "api_key": os.getenv("GEMINI_API_KEY", ""),
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "groq": {
        "api_key": os.getenv("GROQ_API_KEY", ""),
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    },
}

# ---------------------------------------------------------------- models
# Prices are USD per 1 million tokens. They are estimates used for budgeting;
# change them in .env if the provider's price list changes.
def _f(name, default):
    return float(os.getenv(name, default))


MODELS = {
    "gemini-flash": {
        "provider": "gemini",
        "model": os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        "price_in": _f("GEMINI_PRICE_IN", 0.30),
        "price_out": _f("GEMINI_PRICE_OUT", 2.50),
        "context_tokens": 1_000_000,
        "quality": 3,      # 1 = basic, 3 = strongest
        "speed": 2,        # 1 = slow, 3 = fastest
        "open_weight": False,
    },
    "groq-llama-70b": {
        "provider": "groq",
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "price_in": _f("GROQ_PRICE_IN", 0.59),
        "price_out": _f("GROQ_PRICE_OUT", 0.79),
        "context_tokens": 128_000,
        "quality": 2,
        "speed": 3,
        "open_weight": True,   # Llama is an open-weight model, hosted by Groq
    },
    "groq-llama-8b": {
        "provider": "groq",
        "model": os.getenv("GROQ_SMALL_MODEL", "llama-3.1-8b-instant"),
        "price_in": _f("GROQ_SMALL_PRICE_IN", 0.05),
        "price_out": _f("GROQ_SMALL_PRICE_OUT", 0.08),
        "context_tokens": 128_000,
        "quality": 1,
        "speed": 3,
        "open_weight": True,
    },
    "openai-mini": {
        "provider": "openai",
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "price_in": _f("OPENAI_PRICE_IN", 0.15),
        "price_out": _f("OPENAI_PRICE_OUT", 0.60),
        "context_tokens": 128_000,
        "quality": 2,
        "speed": 2,
        "open_weight": False,
    },
}

# Model-selection rules: for every task, an ordered list of candidates
# (first = preferred, the rest = fallbacks) and the reason for the choice.
ROUTING = {
    "summarize": {
        "candidates": ["groq-llama-8b", "openai-mini", "gemini-flash"],
        "reason": "Simple, high-volume task: cheapest and fastest model first.",
    },
    "extract": {
        "candidates": ["gemini-flash", "openai-mini", "groq-llama-70b"],
        "reason": "Needs careful reading and strict JSON: strongest model first.",
    },
    "classify": {
        "candidates": ["gemini-flash", "openai-mini", "groq-llama-70b"],
        "reason": "Important decision: strongest model is the primary classifier.",
    },
    "verify": {
        "candidates": ["groq-llama-70b", "openai-mini", "gemini-flash"],
        "reason": "Independent check: open-weight model from a different provider.",
    },
    "tiebreak": {
        "candidates": ["openai-mini", "groq-llama-70b", "gemini-flash"],
        "reason": "Third opinion from a provider not used by primary or verifier.",
    },
    "respond": {
        "candidates": ["groq-llama-70b", "gemini-flash", "openai-mini"],
        "reason": "Short text generation: low latency matters most.",
    },
}

# Generation parameters per task (temperature, max output tokens, top-p).
GEN_PARAMS = {
    "summarize": {"temperature": 0.2, "max_tokens": 400, "top_p": 0.9},
    "extract": {"temperature": 0.0, "max_tokens": 700, "top_p": 1.0},
    "classify": {"temperature": 0.0, "max_tokens": 400, "top_p": 1.0},
    "verify": {"temperature": 0.0, "max_tokens": 400, "top_p": 1.0},
    "tiebreak": {"temperature": 0.0, "max_tokens": 400, "top_p": 1.0},
    "respond": {"temperature": 0.4, "max_tokens": 500, "top_p": 0.9},
}

# ---------------------------------------------------------------- limits
REQUEST_TIMEOUT_S = 25        # per HTTP call to a provider
MAX_API_RETRIES = 2           # retries per model for timeout / rate limit / 5xx
MAX_SCHEMA_RETRIES = 2        # extra attempts when JSON fails validation
LONG_INPUT_TOKENS = int(os.getenv("LONG_INPUT_TOKENS", 1200))  # above this we chunk
CHUNK_TOKENS = 600            # size of each chunk
HARD_INPUT_TOKENS = 20_000    # above this the request is refused outright
ECONOMY_THRESHOLD = 0.25      # below 25% budget left -> cheapest models first

DEFAULT_SETTINGS = {
    "project_budget_usd": 1.00,       # total spend allowed for the whole project
    "user_daily_budget_usd": 0.05,    # spend allowed per user per day
    # Test Lab switches (admin only) used to demonstrate the evaluation tasks
    "sim_fail_provider": "",          # "gemini" | "groq" | "openai" | "all" | ""
    "sim_fail_kind": "timeout",       # timeout | rate_limit | invalid_credentials | outage
    "sim_malformed_attempts": 0,      # corrupt the first N structured responses
    "sim_force_disagreement": False,  # make the verifier disagree with the primary
}

# ---------------------------------------------------------------- domain
CATEGORIES = ["hostel", "fees", "examination", "academics", "it_services",
              "library", "transport", "mess_food", "placement", "other"]
PRIORITIES = ["low", "medium", "high", "critical"]
ACTIONS = ["assign_to_department", "escalate_to_admin", "approve_refund", "reject_invalid"]

DEPARTMENTS = {
    "hostel_office": {"name": "Hostel Office", "email": "hostel@rms.example.edu", "sla_hours": 48},
    "accounts": {"name": "Accounts Section", "email": "accounts@rms.example.edu", "sla_hours": 72},
    "examination_cell": {"name": "Examination Cell", "email": "exams@rms.example.edu", "sla_hours": 72},
    "academic_affairs": {"name": "Academic Affairs", "email": "academics@rms.example.edu", "sla_hours": 96},
    "it_helpdesk": {"name": "IT Helpdesk", "email": "ithelp@rms.example.edu", "sla_hours": 24},
    "library": {"name": "Central Library", "email": "library@rms.example.edu", "sla_hours": 72},
    "transport_office": {"name": "Transport Office", "email": "transport@rms.example.edu", "sla_hours": 48},
    "mess_committee": {"name": "Mess Committee", "email": "mess@rms.example.edu", "sla_hours": 24},
    "placement_cell": {"name": "Placement Cell", "email": "placements@rms.example.edu", "sla_hours": 96},
    "student_welfare": {"name": "Student Welfare", "email": "welfare@rms.example.edu", "sla_hours": 48},
}

# Business rule: which departments may handle each category (first = default).
CATEGORY_DEPARTMENTS = {
    "hostel": ["hostel_office"],
    "fees": ["accounts"],
    "examination": ["examination_cell"],
    "academics": ["academic_affairs"],
    "it_services": ["it_helpdesk"],
    "library": ["library"],
    "transport": ["transport_office"],
    "mess_food": ["mess_committee", "hostel_office"],
    "placement": ["placement_cell"],
    "other": ["student_welfare"],
}

# Fields that must be present before a complaint of that category can proceed.
REQUIRED_FIELDS = {
    "hostel": ["location"],
    "fees": ["amount"],
    "examination": ["course_code"],
    "academics": ["course_code"],
    "mess_food": ["location"],
}
FIELD_QUESTIONS = {
    "location": "Which hostel block / room / place is this about?",
    "amount": "What is the exact amount (in rupees) involved?",
    "course_code": "Which course code is this about (for example CSB312)?",
    "incident_date": "On which date did this happen?",
    "reference_id": "What is the receipt / transaction / reference number?",
}

REFUND_LIMIT = 10_000          # refunds above this always need admin approval
DAILY_TICKET_LIMIT = 5         # complaints one student may file per day
MAX_CLARIFICATION_ROUNDS = 2

STATUS_FLOW = {                # allowed staff transitions
    "open": ["in_progress"],
    "in_progress": ["resolved"],
    "resolved": ["closed", "in_progress"],
}
