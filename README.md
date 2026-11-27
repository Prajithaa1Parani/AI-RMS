# AI-Powered RMS – Intelligent Student Grievance Management System

CSB312 Generative AI Foundations – CA4 final project.

Students describe a complaint in plain language. The backend runs it through a
multi-model AI workflow (extract → classify + verify → draft reply), validates every
AI output against a schema, applies fixed business rules, saves the ticket in the
database and records every step in an audit log.

## Run it

Backend (Python 3.10+):

```
cd backend
python -m venv .venv
.venv\Scripts\activate          # Linux/Nimbus: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Linux/Nimbus: cp .env.example .env
python app.py                   # http://localhost:5050
```

Put your API keys in `backend/.env` (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`)
and set `MOCK_LLM=0`. With `MOCK_LLM=1` an offline keyword-based test model answers
instead, so the app can be developed without keys.

Frontend (Node 18+):

```
cd frontend
npm install
npm run build      # Flask then serves the built app at http://localhost:5050
npm run dev        # or: live-reload dev server at http://localhost:5173
```

If the frontend is deployed on a different address from the backend, build it with
`VITE_API_URL=https://your-backend-address npm run build`.

Demo logins: `student1 / student123`, `hostel_staff | accounts_staff | exam_staff | it_staff / staff123`, `admin / admin123`.

## Test the 10 evaluation tasks

```
cd backend
python test_eval_tasks.py
```

This runs all ten tasks against the backend with the offline test model. For the
report, repeat them in the browser with real models: log in as `admin`, open
**Test lab** (sample inputs and failure switches), and submit as `student1`.

## Where things are

| File | What it does |
|---|---|
| `backend/config.py` | Models, prices, routing rules, limits, departments, business constants |
| `backend/llm.py` | LLM gateway: model selection, fallback, retries, budget, cost, cache, JSON validation |
| `backend/prompts.py` | Versioned system/user prompts, few-shot examples, untrusted-content wrapping |
| `backend/schemas.py` | Pydantic schemas for every AI output |
| `backend/pipeline.py` | The workflow (orchestration), chunking, grounding check, database write |
| `backend/rules.py` | Deterministic business rules R1–R9 and authorization |
| `backend/db.py` | SQLite schema, audit log, settings |
| `backend/app.py` | REST API and login |
| `backend/mock_llm.py` | Offline test model (development only) |
| `frontend/src/` | React interface for student, staff and admin |
