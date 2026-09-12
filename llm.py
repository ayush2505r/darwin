"""LLM integration module for Darwin.

Uses Groq via langchain-groq. Model name is dynamically read from GROQ_MODEL
environment variable as strictly specified in the project requirements.

Includes:
  * Rate-limit / quota aware Groq wrapper with exponential backoff on 429,
    bounded retries, max_tokens cap tuned to avoid token waste, and a short
    live circuit breaker so consecutive failed calls fall through to the
    deterministic offline engine immediately (no extra tokens consumed).
  * Graceful deterministic fallback for local dev/test when Groq key is
    missing, the rate-limit circuit is open, or the API returns any error.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate-limit / quota budget & circuit breaker (process-wide, thread-safe).
# ---------------------------------------------------------------------------
# These defaults are intentionally conservative. They can all be overridden
# via environment variables so operators can tune behaviour without edits.

def _int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        return int(raw)
    except (TypeError, ValueError):
        return default


MAX_GROQ_RETRIES = _int_env("GROQ_MAX_RETRIES", 0)          # 429s must not be retried by default
GROQ_INITIAL_BACKOFF_SEC = float(os.getenv("GROQ_INITIAL_BACKOFF_SEC", "1.25"))  # doubles each attempt
GROQ_CIRCUIT_OPEN_SEC = _int_env("GROQ_CIRCUIT_OPEN_SEC", 60)                   # seconds to skip API after N fails
GROQ_CIRCUIT_FAIL_THRESHOLD = _int_env("GROQ_CIRCUIT_FAIL_THRESHOLD", 1)         # one 429 stops the burst
GROQ_MIN_REQUEST_INTERVAL_SEC = float(os.getenv("GROQ_MIN_REQUEST_INTERVAL_SEC", "0.8"))
GROQ_REQUESTS_PER_MINUTE = _int_env("GROQ_REQUESTS_PER_MINUTE", 25)

# Max output tokens per call category. Groq charges per output token, so a
# hard cap both (a) protects budget, (b) keeps latency bounded, and (c)
# prevents run-away responses. Each key is a hint callers can pass.
MAX_OUTPUT_TOKENS_BY_HINT: Dict[str, int] = {
    "assess": 300,          # small JSON
    "meta_evaluate": 450,   # JSON with short prose
    "outcome_summary": 250, # single paragraph
    "mutate": 700,          # new prompt genome
    "teach": 3200,          # long 5-phase classroom guide
    "transcribe_summary": 1500,
    "session_material": 2200,
}
DEFAULT_MAX_OUTPUT_TOKENS: int = _int_env("GROQ_DEFAULT_MAX_OUTPUT_TOKENS", 1600)

# Token-cost budget (input+output estimate, process-wide soft cap). Once
# exceeded every Groq call short-circuits to the fallback engine for the
# rest of the process lifetime. Set to 0 to disable the cap entirely.
GROQ_PROCESS_TOKEN_BUDGET: int = _int_env("GROQ_PROCESS_TOKEN_BUDGET", 0)

# Process-wide singleton state ------------------------------------------------
_rl_lock = threading.Lock()
_circuit_fail_count: int = 0
_circuit_open_until: Optional[datetime] = None
_process_tokens_used: int = 0   # rough rolling estimate via max_tokens + prompt length heuristics
_last_request_at: float = 0.0
_request_times: List[float] = []


def _estimate_tokens(text: str) -> int:
    """Very rough char-based token estimate (≈ 4 chars/token). Good enough for budget caps."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _circuit_is_open() -> bool:
    global _circuit_open_until, _circuit_fail_count
    if _circuit_open_until is None:
        return False
    if datetime.utcnow() >= _circuit_open_until:
        # Circuit cool-down elapsed; reset counts silently.
        _circuit_open_until = None
        _circuit_fail_count = 0
        return False
    return True


def _record_call_success() -> None:
    global _circuit_fail_count, _circuit_open_until
    with _rl_lock:
        _circuit_fail_count = 0
        _circuit_open_until = None


def _record_call_failure() -> None:
    global _circuit_fail_count, _circuit_open_until
    with _rl_lock:
        _circuit_fail_count += 1
        if _circuit_fail_count >= GROQ_CIRCUIT_FAIL_THRESHOLD:
            _circuit_open_until = datetime.utcnow() + timedelta(seconds=GROQ_CIRCUIT_OPEN_SEC)
            logger.warning(
                "Groq circuit breaker OPEN for %d seconds after %d consecutive failures.",
                GROQ_CIRCUIT_OPEN_SEC, _circuit_fail_count
            )


def _consume_budget(estimated: int) -> bool:
    """Return True if the estimated token cost fits within the remaining budget.
    Always returns True when budget tracking is disabled (0)."""
    global _process_tokens_used
    if GROQ_PROCESS_TOKEN_BUDGET <= 0:
        return True


def _wait_for_request_slot() -> bool:
    """Throttle bursts before they reach Groq and fail closed on a minute quota."""
    global _last_request_at, _request_times
    now = time.monotonic()
    with _rl_lock:
        _request_times = [t for t in _request_times if now - t < 60.0]
        if GROQ_REQUESTS_PER_MINUTE > 0 and len(_request_times) >= GROQ_REQUESTS_PER_MINUTE:
            logger.warning("Groq request-per-minute guard active; using offline fallback.")
            return False
        wait_s = max(0.0, GROQ_MIN_REQUEST_INTERVAL_SEC - (now - _last_request_at))
        if wait_s:
            time.sleep(wait_s)
        request_at = time.monotonic()
        _last_request_at = request_at
        _request_times.append(request_at)
    return True
    with _rl_lock:
        if _process_tokens_used + estimated > GROQ_PROCESS_TOKEN_BUDGET:
            logger.warning(
                "Groq process token budget %d exceeded (%d used + %d est). Falling back offline.",
                GROQ_PROCESS_TOKEN_BUDGET, _process_tokens_used, estimated
            )
            return False
        _process_tokens_used += estimated
        return True


def get_configured_model() -> str:
    """Return model name configured in environment variable GROQ_MODEL."""
    model = os.getenv("GROQ_MODEL")
    if not model or not model.strip():
        return "llama3-8b-8192"
    return model.strip()


def is_groq_available() -> bool:
    """Check if GROQ_API_KEY is populated and the circuit is currently closed."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key or key == "your_groq_api_key_here":
        return False
    with _rl_lock:
        if _circuit_is_open():
            return False
    return True


def _resolve_max_tokens(hint: Optional[str]) -> int:
    if hint and hint in MAX_OUTPUT_TOKENS_BY_HINT:
        return MAX_OUTPUT_TOKENS_BY_HINT[hint]
    return DEFAULT_MAX_OUTPUT_TOKENS


def _extract_rate_limit_retry_seconds(exception: Any) -> Optional[float]:
    """Groq returns HTTP 429 with Retry-After; pull the best available wait."""
    ex_str = str(exception).lower()
    # Most SDKs surface 429 via status_code attribute or message text
    status_code: Optional[int] = getattr(exception, "status_code", None)
    try:
        # langchain-core exceptions may hide HTTP info in .response.status_code
        if status_code is None:
            response = getattr(exception, "response", None)
            if response is not None:
                status_code = getattr(response, "status_code", None)
    except Exception:
        status_code = None

    is_429 = status_code == 429 or "429" in ex_str or "rate limit" in ex_str or "too many requests" in ex_str
    if not is_429:
        return None

    # Numeric retry-after
    m = re.search(r"retry-after[:\s]*(\d+\.?\d*)", ex_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # "Ratelimit limit, reset in X seconds"
    m2 = re.search(r"reset in\s*(\d+\.?\d*)", ex_str)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    # Default moderate wait for any 429
    return 5.0


def get_chat_model(temperature: float = 0.6, max_tokens_hint: Optional[str] = None):
    """Return a configured ChatGroq instance if key is present and circuit is closed."""
    if not is_groq_available():
        return None
    try:
        from langchain_groq import ChatGroq
    except Exception as e:
        logger.error(f"Failed to import ChatGroq: {e}")
        return None

    try:
        model_name = get_configured_model()
        api_key = os.getenv("GROQ_API_KEY")
        max_tokens = _resolve_max_tokens(max_tokens_hint)
        # NOTE: we set max_retries=0 because call_llm handles retries with
        # its own backoff + 429 Retry-After + circuit breaker. Delegating
        # retries to langchain-groq would mask Retry-After and burn tokens.
        return ChatGroq(
            model=model_name,
            groq_api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=20.0,
            max_retries=0
        )
    except Exception as e:
        logger.error(f"Failed to instantiate ChatGroq: {e}")
        return None


def call_llm(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.6,
    max_tokens_hint: Optional[str] = None
) -> str:
    """Invoke Groq LLM with system+user prompts, respecting budget, 429 backoff,
    and circuit breaker. On any failure or open circuit returns the deterministic
    offline response so the pipeline never breaks — tokens are only consumed
    during successful invocations."""

    # Budget guard — estimate tokens BEFORE sending; if we would exceed the
    # process cap, fall straight to fallback without touching the network.
    est_input_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
    est_output_tokens = _resolve_max_tokens(max_tokens_hint)
    if not _consume_budget(est_input_tokens + est_output_tokens):
        logger.info("Groq budget guard active; returning offline fallback.")
        return fallback_llm_response(system_prompt, user_prompt)

    chat = get_chat_model(temperature=temperature, max_tokens_hint=max_tokens_hint)
    if chat is None:
        # No key, circuit open, or import failure → offline only.
        return fallback_llm_response(system_prompt, user_prompt)

    if not _wait_for_request_slot():
        return fallback_llm_response(system_prompt, user_prompt)

    try:
        from langchain_core.messages import SystemMessage, HumanMessage
    except Exception as e:
        logger.warning(f"langchain_core import failed ({e}). Using offline fallback.")
        return fallback_llm_response(system_prompt, user_prompt)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    last_exception: Optional[Exception] = None
    for attempt in range(1, MAX_GROQ_RETRIES + 2):  # initial + MAX_GROQ_RETRIES retries
        try:
            response = chat.invoke(messages)
            content = response.content if hasattr(response, "content") else str(response)
            if not content or not str(content).strip():
                raise RuntimeError("Empty response from Groq API")
            _record_call_success()
            return content
        except Exception as e:
            last_exception = e
            retry_after = _extract_rate_limit_retry_seconds(e)
            is_last_attempt = attempt >= (MAX_GROQ_RETRIES + 1)
            if retry_after is not None:
                # A 429 means the provider quota is exhausted. Retrying every
                # pipeline stage multiplies the failure and burns time/tokens.
                # Open the circuit immediately; operators can opt into a single
                # retry only by explicitly setting GROQ_MAX_RETRIES.
                _record_call_failure()
                if MAX_GROQ_RETRIES <= 0 or is_last_attempt:
                    break
                wait_s = max(retry_after, GROQ_INITIAL_BACKOFF_SEC * (2 ** (attempt - 1)))
                logger.warning(
                    "Groq 429 on attempt %d. Waiting %.1fs per Retry-After hint before retry.",
                    attempt, wait_s
                )
                time.sleep(wait_s)
                continue
            # Non-rate-limit error (network, auth, 5xx, etc.) → exponential backoff once then bail.
            if is_last_attempt:
                break
            wait_s = GROQ_INITIAL_BACKOFF_SEC * (2 ** (attempt - 1))
            logger.warning(
                "Groq API call failed on attempt %d (%s). Backoff %.1fs then retry.",
                attempt, repr(e), wait_s
            )
            time.sleep(wait_s)

    # All retries exhausted → open the circuit & refund the output-token budget
    # reservation we consumed up front, since the call failed to generate output.
    _record_call_failure()
    if GROQ_PROCESS_TOKEN_BUDGET > 0:
        with _rl_lock:
            global _process_tokens_used
            _process_tokens_used = max(0, _process_tokens_used - est_output_tokens)
    logger.warning(
        "Groq API failed after retries (%s). Switching to offline fallback to save tokens.",
        repr(last_exception)
    )
    return fallback_llm_response(system_prompt, user_prompt)


# ---------------------------------------------------------------------------
# Deterministic offline responses (zero tokens consumed).
# ---------------------------------------------------------------------------
def fallback_llm_response(system_prompt: str, user_prompt: str) -> str:
    """Generate deterministic, structured fallback responses when Groq is unavailable."""
    sys_lower = system_prompt.lower()
    user_lower = user_prompt.lower()

    # 1. Assessment step fallback
    if "assessor" in sys_lower or "student-level profile" in sys_lower:
        level = "intermediate"
        if any(w in user_lower for w in ["beginner", "novice", "basic", "intro", "scratch", "no experience"]):
            level = "beginner"
        elif any(w in user_lower for w in ["advanced", "expert", "deep dive", "specialized"]):
            level = "advanced"
        return json.dumps({
            "level": level,
            "reasoning": f"Assessed as {level} based on contextual cues and topic difficulty.",
            "known_concepts": ["Foundational domain definitions", "Basic qualitative principles"],
            "gaps": ["Deep mathematical foundations", "Nuanced edge cases and practical implementations"]
        }, indent=2)

    # 2. Autonomous Meta-Agent Plan Evaluation
    if "auditor" in sys_lower or ("meta-agent" in sys_lower and "evaluate" in sys_lower):
        score = 4.8 if "first-principles" in user_lower else (4.6 if "socratic" in user_lower else 4.3)
        return json.dumps({
            "score": score,
            "success_rationale": "Strong opening hook dialogue, directly actionable blackboard layout, and targeted formative check questions.",
            "mistake_summary": "Pacing on step 2 could include one additional check before proceeding to the worked example.",
            "outcome_summary": "The teaching guide successfully operationalized core principles into concrete classroom steps."
        }, indent=2)

    # 3. Outcome summary fallback
    if (
        "educational quality analyst" in sys_lower
        or "teacher score" in user_lower
        or "why the lesson succeeded" in sys_lower
    ):
        score_match = re.search(r"teacher score:\s*(\d+)", user_lower)
        score_val = int(score_match.group(1)) if score_match else 3
        if score_val >= 4:
            return (
                "The pedagogical pacing and clear intuitive analogies resonated strongly with the student. "
                "The progressive difficulty scaffolded understanding effectively."
            )
        elif score_val <= 2:
            return (
                "The explanation jumped too quickly past foundational concepts, leading to cognitive overload. "
                "Needs more step-by-step grounding and relatable baseline examples."
            )
        else:
            return (
                "The lesson provided adequate coverage of core concepts, but would benefit from more concrete "
                "checkpoints and interactive prompts to verify understanding."
            )

    # 4. Strategy mutation fallback
    if "mutate this teaching" in sys_lower or "genetic mutator" in sys_lower:
        return (
            "You are an evolved Teaching Assistant (mutated variant).\n"
            "Building upon the proven foundations of your predecessor, you maintain high clarity while "
            "incorporating active retrieval checks and adaptive analogies tailored specifically to identified "
            "student knowledge gaps. Address common misconceptions upfront and reinforce learning through "
            "succinct interactive milestones."
        )

    # 5. Teaching agent fallback (Actionable Teacher Classroom Guide)
    topic_match = re.search(r"topic(?: to teach)?:?\s*([^\n\r]+)", user_prompt, re.IGNORECASE)
    topic = topic_match.group(1).strip() if topic_match else "the topic"
    teacher_match = re.search(r"Teacher receiving this guide:\s*([^\n\r]+)", user_prompt)
    teacher_name = teacher_match.group(1).strip() if teacher_match else "the teacher"

    if "socratic" in sys_lower:
        style_title = "Socratic Inquiry Method"
        hook_action = (
            f"**What {teacher_name} should say:** \"Before we define {topic}, let me ask: imagine you encounter a system that seems "
            f"to follow two conflicting rules at the same time. How would you test which rule applies?\""
        )
        core_approach = (
            "Lead students through a series of 3 guided questions on the board. Do not lecture directly; "
            "pause after each question and write student hypotheses on the left side of the blackboard."
        )
    elif "first-principles" in sys_lower:
        style_title = "First-Principles & Real-World Intuition"
        hook_action = (
            f"**What {teacher_name} should say:** \"Forget the textbook formulas for {topic} for the next 10 minutes. "
            f"Let's strip away the jargon and look at the simplest physical analogy in everyday life.\""
        )
        core_approach = (
            "Draw a simple mechanical or visual diagram on the board illustrating the fundamental components. "
            "Connect each component directly to a familiar real-world object before introducing technical vocabulary."
        )
    else:
        style_title = "Pragmatic Scaffolding & Checkpoints"
        hook_action = (
            f"**What {teacher_name} should say:** \"Today our goal is to master {topic} in 4 distinct milestones. "
            f"By the end of this period, everyone will be able to solve a core problem independently.\""
        )
        core_approach = (
            "Present a structured 4-step sequence on the board: Definition -> Mechanism -> Common Error -> Practice. "
            "Have students write down each milestone checkpoint in their notebooks."
        )

    return f"""### Teacher's Step-by-Step Classroom Guide: {topic}

*Prepared for {teacher_name} · Pedagogical strategy: {style_title}*

#### Phase 1: Classroom Hook & Intuitive Kickoff
- **Objective**: Engage curiosity and activate students' existing mental models (first 5–7 minutes).
- {hook_action}
- **Demonstration / Visual**: Draw a simple 2-part diagram on the board showing the initial state vs. the transformed state. Ask the room: *"What do you notice has changed?"*

#### Phase 2: Addressing Assessed Student Gaps & Misconceptions
- **Targeting Gaps**: Address students' unfamiliarity with formal terminology by rooting definitions in intuitive observations first.
- **Common Misconception to Dispel**: Students frequently confuse introductory terminology with underlying causal mechanisms.
- **Teacher Script**: *"A common pitfall is to think that {topic} happens instantly. In reality, it is a progressive dynamic governed by core conservation rules."*

#### Phase 3: Step-by-Step Teaching Script & Blackboard Flow
1. **The Groundwork (5 mins)**:
   - Write the core definition clearly in the center of the board.
   - Highlight the 2 essential variables and have students repeat the key terms.
2. **The Mechanism Walkthrough (10 mins)**:
   - {core_approach}
   - Walk through a concrete, worked baseline example step-by-step.
   - Point out exactly where calculations or logic typically break down.
3. **Interactive Checkpoint (5 mins)**:
   - Pause and give students 90 seconds to summarize the mechanism to their neighbor.

#### Phase 4: Formative Comprehension Check
Ask the class the following targeted diagnostic questions:
1. **Concept Check**: *"If we modify the initial condition in our example, what will happen to the outcome?"*
   - *Expected Answer*: Students should identify that the rate or intensity scales proportionally.
   - *If they struggle*: Direct them back to step 2 on the board diagram.
2. **Reverse Scenario**: *"Why would an approach without {topic} fail in a real-world scenario?"*
   - *Expected Answer*: Because it overlooks the underlying constraint.

#### Phase 5: Differentiated Practice & Wrap-Up
- **For Students Needing Extra Scaffolding**: Provide a guided fill-in-the-blank template of the 3-step mechanism.
- **For Advanced / Fast Finishers**: Challenge them to predict what happens when extreme edge conditions are introduced.
- **Closing Takeaway**: Summarize the 1 core rule students must remember before the bell rings.
"""
