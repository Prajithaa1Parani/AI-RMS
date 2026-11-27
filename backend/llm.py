"""LLM gateway: the only place in the project that talks to AI providers.

Responsibilities
- model selection + fallback routing (config.ROUTING)
- budget check before every call, token and cost tracking after it
- retries for timeouts / rate limits / outages, no retry for bad credentials
- structured output: parse JSON, validate with Pydantic, bounded repair retries
- caching of validated answers
- every attempt is written to the llm_calls table
"""
import hashlib
import json
import math
import time

import requests
from pydantic import ValidationError

import config
import db
import mock_llm
from prompts import PROMPT_VERSIONS, REPAIR_NOTE


class LLMError(Exception):
    """One failed provider call."""

    def __init__(self, kind, message, retryable):
        super().__init__(message)
        self.kind = kind            # timeout | rate_limit | invalid_credentials | outage | network | bad_request
        self.retryable = retryable


class AllProvidersFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class SchemaValidationFailed(Exception):
    pass


# ---------------------------------------------------------------- tokens & cost
def estimate_tokens(text):
    """Rough estimate (about 4 characters per token) used before a call is made."""
    return math.ceil(len(text) / 4)


def cost_usd(model_key, input_tokens, output_tokens):
    m = config.MODELS[model_key]
    return (input_tokens * m["price_in"] + output_tokens * m["price_out"]) / 1_000_000


def budget_status(user_id):
    s = db.get_settings()
    project_left = s["project_budget_usd"] - db.spend_total()
    user_left = s["user_daily_budget_usd"] - db.spend_today(user_id)
    if project_left <= user_left:
        remaining, limit, scope = project_left, s["project_budget_usd"], "project"
    else:
        remaining, limit, scope = user_left, s["user_daily_budget_usd"], "user_daily"
    return {
        "remaining_usd": max(remaining, 0.0),
        "limit_usd": limit,
        "scope": scope,
        "economy_mode": limit <= 0 or remaining / limit < config.ECONOMY_THRESHOLD,
        "project_spent_usd": db.spend_total(),
        "user_spent_today_usd": db.spend_today(user_id),
        "project_budget_usd": s["project_budget_usd"],
        "user_daily_budget_usd": s["user_daily_budget_usd"],
    }


# ---------------------------------------------------------------- provider calls
def _raise_for_status(resp):
    if resp.status_code < 400:
        return
    body = resp.text[:300]
    if resp.status_code in (401, 403):
        raise LLMError("invalid_credentials", f"HTTP {resp.status_code}: {body}", False)
    if resp.status_code == 429:
        raise LLMError("rate_limit", f"HTTP 429: {body}", True)
    if resp.status_code >= 500:
        raise LLMError("outage", f"HTTP {resp.status_code}: {body}", True)
    raise LLMError("bad_request", f"HTTP {resp.status_code}: {body}", False)


def _post(url, headers, body):
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=config.REQUEST_TIMEOUT_S)
    except requests.Timeout:
        raise LLMError("timeout", "Provider did not answer in time", True)
    except requests.RequestException as e:
        raise LLMError("network", f"Network error: {e}", True)
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError:
        raise LLMError("outage", "Provider returned a non-JSON body", True)


def _call_gemini(m, system, messages, params):
    p = config.PROVIDERS["gemini"]
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "model" if x["role"] == "assistant" else "user",
                      "parts": [{"text": x["content"]}]} for x in messages],
        "generationConfig": {
            "temperature": params["temperature"],
            "topP": params["top_p"],
            # extra room because some Gemini models spend output tokens on "thinking"
            "maxOutputTokens": params["max_tokens"] + 1024,
            "responseMimeType": "application/json",
        },
    }
    data = _post(f"{p['base_url']}/models/{m['model']}:generateContent",
                 {"x-goog-api-key": p["api_key"]}, body)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = ""   # blocked or empty answer -> fails validation -> retried
    usage = data.get("usageMetadata", {})
    out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return text, usage.get("promptTokenCount", 0), out


def _call_openai_style(m, system, messages, params):
    """Groq and OpenAI share the same chat-completions API format."""
    p = config.PROVIDERS[m["provider"]]
    body = {
        "model": m["model"],
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": params["temperature"],
        "top_p": params["top_p"],
        "response_format": {"type": "json_object"},
    }
    limit_key = "max_completion_tokens" if m["provider"] == "openai" else "max_tokens"
    body[limit_key] = params["max_tokens"]
    headers = {"Authorization": f"Bearer {p['api_key']}"}
    url = f"{p['base_url']}/chat/completions"
    try:
        data = _post(url, headers, body)
    except LLMError as e:
        # Some newer models only accept default sampling values: resend without them.
        if e.kind == "bad_request" and ("temperature" in str(e) or "top_p" in str(e)):
            body.pop("temperature"), body.pop("top_p")
            data = _post(url, headers, body)
        else:
            raise
    text = (data["choices"][0]["message"].get("content") or "") if data.get("choices") else ""
    usage = data.get("usage", {})
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _simulated_failure(provider):
    """Test Lab: pretend a provider is failing (evaluation task 7)."""
    s = db.get_settings()
    if s["sim_fail_provider"] in (provider, "all"):
        kind = s["sim_fail_kind"]
        raise LLMError(kind, f"[SIMULATED] {provider} {kind}", kind != "invalid_credentials")


def _call_provider(task, model_key, system, messages):
    m = config.MODELS[model_key]
    _simulated_failure(m["provider"])
    params = config.GEN_PARAMS[task]
    if config.MOCK_LLM:
        return mock_llm.answer(task, system, messages)
    if not config.PROVIDERS[m["provider"]]["api_key"]:
        raise LLMError("invalid_credentials", f"No API key configured for {m['provider']}", False)
    if m["provider"] == "gemini":
        return _call_gemini(m, system, messages, params)
    return _call_openai_style(m, system, messages, params)


# ---------------------------------------------------------------- logging
def _log(ctx, task, model_key, attempt, outcome, reason, error=None,
         tokens_in=0, tokens_out=0, cost=0.0, latency_ms=0, cached=0):
    m = config.MODELS.get(model_key, {})
    db.execute(
        "INSERT INTO llm_calls (run_id, user_id, task, model_key, provider, model, attempt,"
        " outcome, error, selection_reason, input_tokens, output_tokens, cost_usd, latency_ms,"
        " cached, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctx.get("run_id"), ctx.get("user_id"), task, model_key, m.get("provider"),
         m.get("model"), attempt, outcome, error, reason, tokens_in, tokens_out, cost,
         latency_ms, cached, PROMPT_VERSIONS.get(_prompt_name(task)), db.now()),
    )


def _prompt_name(task):
    return "classify" if task in ("verify", "tiebreak") else task


# ---------------------------------------------------------------- model selection
def select_models(task, input_tokens, user_id, avoid_providers=()):
    """Return (ordered candidate list, reason). Implements the routing rules."""
    route = config.ROUTING[task]
    reason = route["reason"]
    candidates = [k for k in route["candidates"]
                  if config.MODELS[k]["provider"] not in avoid_providers]
    # Context rule: drop models whose context window is too small for this input.
    need = input_tokens + config.GEN_PARAMS[task]["max_tokens"]
    fits = [k for k in candidates if config.MODELS[k]["context_tokens"] >= need]
    if len(fits) < len(candidates):
        reason += " Models with too small a context window were removed."
    candidates = fits
    # Cost rule: when the budget is nearly used up, cheapest model goes first.
    if budget_status(user_id)["economy_mode"]:
        out = config.GEN_PARAMS[task]["max_tokens"]
        candidates.sort(key=lambda k: cost_usd(k, input_tokens, out))
        reason += " Economy mode (budget below 25%): candidates re-ordered by cost."
    return candidates, reason


# ---------------------------------------------------------------- plain call
def call(task, system, messages, ctx, avoid_providers=(), force_model=None):
    """Call the best available model for `task`. Returns a dict with text + usage."""
    prompt_text = system + "".join(m["content"] for m in messages)
    est_in = estimate_tokens(prompt_text)
    max_out = config.GEN_PARAMS[task]["max_tokens"]
    if force_model:
        candidates, reason = [force_model], "Model forced for side-by-side comparison."
    else:
        candidates, reason = select_models(task, est_in, ctx["user_id"], avoid_providers)

    skipped_for_budget, errors = 0, []
    for position, model_key in enumerate(candidates):
        why = reason if position == 0 else f"Fallback #{position} after: {errors[-1] if errors else 'budget'}"
        estimate = cost_usd(model_key, est_in, max_out)
        if estimate > budget_status(ctx["user_id"])["remaining_usd"]:
            skipped_for_budget += 1
            _log(ctx, task, model_key, 0, "skipped_budget", why,
                 error=f"Estimated ${estimate:.6f} is more than the remaining budget")
            continue

        for attempt in range(1, config.MAX_API_RETRIES + 2):
            started = time.time()
            try:
                text, t_in, t_out = _call_provider(task, model_key, system, messages)
            except LLMError as e:
                latency = int((time.time() - started) * 1000)
                _log(ctx, task, model_key, attempt, f"error_{e.kind}", why,
                     error=str(e), latency_ms=latency)
                errors.append(f"{model_key}: {e.kind}")
                if e.retryable and attempt <= config.MAX_API_RETRIES:
                    time.sleep(0.4 * 2 ** (attempt - 1))   # exponential backoff
                    continue
                break   # give up on this model, try the next candidate
            latency = int((time.time() - started) * 1000)
            t_in = t_in or est_in
            t_out = t_out or estimate_tokens(text)
            cost = cost_usd(model_key, t_in, t_out)
            _log(ctx, task, model_key, attempt, "success", why,
                 tokens_in=t_in, tokens_out=t_out, cost=cost, latency_ms=latency)
            m = config.MODELS[model_key]
            return {"text": text, "model_key": model_key, "provider": m["provider"],
                    "model": m["model"], "input_tokens": t_in, "output_tokens": t_out,
                    "cost_usd": cost, "latency_ms": latency, "reason": why,
                    "fallback_used": position > 0, "errors_before": list(errors)}

    if candidates and skipped_for_budget == len(candidates):
        raise BudgetExceeded("AI budget reached: this request would exceed the "
                             f"{budget_status(ctx['user_id'])['scope']} budget.")
    raise AllProvidersFailed(f"No model could complete '{task}'. Tried: {', '.join(errors) or 'none available'}")


# ---------------------------------------------------------------- structured call
def parse_json(text):
    """Extract one JSON object from model text. Raises ValueError if impossible."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Top-level JSON value is not an object")
    return data


def _corrupt(text):
    """Test Lab: damage a model reply to prove validation works (evaluation task 4)."""
    return text[: max(len(text) // 2, 1)] + ' "priority": 12345, oops'


def call_json(task, system, messages, schema, ctx, avoid_providers=(), force_model=None,
              use_cache=True, simulate=True):
    """Call a model and return (validated_object, meta).

    Tries at most 1 + MAX_SCHEMA_RETRIES times. Each failed attempt is audited and the
    validation error is sent back to the model. If all attempts fail, raises
    SchemaValidationFailed - the caller must stop, nothing unvalidated is ever used.
    """
    malformed_n = int(db.get_settings()["sim_malformed_attempts"]) if simulate else 0
    key_src = json.dumps([task, PROMPT_VERSIONS.get(_prompt_name(task)), force_model,
                          system, _strip_tag_ids(messages)])
    cache_key = hashlib.sha256(key_src.encode()).hexdigest()

    if use_cache and malformed_n == 0:
        hit = db.query_one("SELECT response FROM llm_cache WHERE cache_key = ?", (cache_key,))
        if hit:
            cached = json.loads(hit["response"])
            obj = schema.model_validate(cached["data"])
            _log(ctx, task, cached["model_key"], 0, "cache_hit",
                 "Identical request answered from cache (cost 0).", cached=1)
            return obj, {**cached["meta"], "cached": True, "cost_usd": 0.0,
                         "input_tokens": 0, "output_tokens": 0}

    messages = [dict(m) for m in messages]
    original_last = messages[-1]["content"]
    last_error = ""
    for attempt in range(1, config.MAX_SCHEMA_RETRIES + 2):
        resp = call(task, system, messages, ctx, avoid_providers, force_model)
        text = _corrupt(resp["text"]) if attempt <= malformed_n else resp["text"]
        try:
            obj = schema.model_validate(parse_json(text))
        except (ValueError, ValidationError) as e:
            last_error = str(e)[:500]
            db.audit("schema_validation", "failed",
                     {"task": task, "schema": schema.__name__, "attempt": attempt,
                      "model": resp["model_key"], "error": last_error,
                      "raw_reply": text[:300], "simulated": attempt <= malformed_n},
                     run_id=ctx.get("run_id"), user_id=ctx.get("user_id"))
            messages[-1]["content"] = original_last + REPAIR_NOTE.format(error=last_error)
            continue
        db.audit("schema_validation", "passed",
                 {"task": task, "schema": schema.__name__, "attempt": attempt,
                  "model": resp["model_key"]},
                 run_id=ctx.get("run_id"), user_id=ctx.get("user_id"))
        meta = {k: resp[k] for k in ("model_key", "provider", "model", "input_tokens",
                                     "output_tokens", "cost_usd", "latency_ms", "reason",
                                     "fallback_used", "errors_before")}
        meta.update(cached=False, schema_attempts=attempt)
        if use_cache:
            db.execute("INSERT OR REPLACE INTO llm_cache (cache_key, response, created_at)"
                       " VALUES (?,?,?)",
                       (cache_key, json.dumps({"data": obj.model_dump(), "meta": meta,
                                               "model_key": resp["model_key"]}), db.now()))
        return obj, meta

    raise SchemaValidationFailed(
        f"{schema.__name__} output was still invalid after "
        f"{config.MAX_SCHEMA_RETRIES + 1} attempts. Last error: {last_error}")


def _strip_tag_ids(messages):
    """The random ids in <untrusted_x_ab12cd34> tags must not change the cache key."""
    import re
    return [re.sub(r"(untrusted_[a-z]+)_[0-9a-f]{8}", r"\1", m["content"]) for m in messages]


# ---------------------------------------------------------------- health
def provider_status():
    return {name: {"configured": bool(p["api_key"]) or config.MOCK_LLM}
            for name, p in config.PROVIDERS.items()}
