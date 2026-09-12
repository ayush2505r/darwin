# Darwin Project Finalization Specification

## Problem

The Darwin Evolving Multi-Agent Teaching Assistant project has a codebase with many intended features already scaffolded, but the end users report that:
1. Agent lifecycle tracking (creation, destruction dates, reasons, mistakes, and promotion rationale) is not working end-to-end as described.
2. Teaching agent output is unreadable / formatted oddly.
3. Different teachers do not receive distinct, personalized guides for the same topic.
4. The Meta-Agent is supposed to autonomously evaluate and drive natural selection, not rely on manual teacher scoring.
5. At least 3 active teaching agents must always be maintained and used per lesson request.
6. Teachers must have separate login sessions.
7. On successful MySQL connection, the terminal must print exactly: `connected to mysql you can proceed`.

The goal of this spec is to bring the project to a reliably working end state where every acceptance criterion passes and the app runs end-to-end.

## Users

- **Teacher (primary):** Logs in, submits a lesson topic + optional context, receives 3 personalized classroom guides + a Meta-Agent champion recommendation, and follows the champion in class. Teachers do not score agents.
- **Auditor / Debugger:** Uses the `/agents` lifecycle ledger to inspect every teaching agent's birth, performance, promotion, or destruction decision with full reasons.
- **Maintainer:** Runs `seed.py`, `app.py`, and `test_darwin.py` with zero failures.

## Goals

1. End-to-end working Flask app with LangGraph pipeline.
2. All 10 automated tests in `test_darwin.py` pass.
3. Every feature from the original project spec is demonstrably functional.
4. Output legibility: lesson guides must render cleanly in the browser with consistent Markdown structure.

## Non-Goals

- No authentication redesign.
- No new tables or extra agent roles beyond what already exists in schema.sql.
- No frontend framework.
- No additional features beyond what is listed in the original spec + user amendments.
- No manual teacher scoring UI.

## Constraints / Dependencies / Assumptions

- Flask, MySQL + mysql-connector-python with `use_pure=True`, LangChain + LangGraph, Groq (via `.env`).
- MySQL server must be reachable and credentials set in `.env`.
- If `GROQ_API_KEY` is missing/empty, deterministic mock LLM responses still make the app fully functional.
- Python 3.10+.
- `POPULATION_FLOOR = 3` minimum active agents; never below.
- Teachers do NOT evaluate agents; the Meta-Agent does that autonomously.

## Open Questions

None at this stage; proceed with verifications as coded in `test_darwin.py` and inferred from the codebase.

---

## Acceptance Criteria

### AC1 (rule): MySQL connection prints exact terminal notice
- **Observable condition:** When `db.get_connection()` is invoked and the connection succeeds, stdout contains exactly the string `connected to mysql you can proceed` (exact lowercase match). The notice is printed only once per process.
- **Evidence source:** `test_darwin.py::test_01_db_connection_pure_and_terminal_message` passes; `db.py` `get_connection()` function.

### AC2 (rule): MySQL connections always instantiate with `use_pure=True`
- **Observable condition:** `db.get_db_config()["use_pure"]` is `True`.
- **Evidence source:** `test_darwin.py::test_01_db_connection_pure_and_terminal_message` passes; `db.py` `get_db_config()`.

### AC3 (rule): At least 3 active teaching agents always exist in DB after seeding
- **Observable condition:** `SELECT COUNT(*) FROM agents WHERE status='active'` returns a value >= 3.
- **Evidence source:** `test_darwin.py::test_02_agents_seeded_minimum_three` passes; `evolution.py::ensure_population_floor`.

### AC4 (rule): Teacher registration, login, session, logout work
- **Observable condition:** Registration creates a new row in `teachers`; login with correct credentials sets `session["teacher_id"]`; logout clears session; wrong password returns invalid-credentials flash.
- **Evidence source:** `test_darwin.py::test_04_teacher_auth_flow` and `test_05_default_teacher_seeded` pass.

### AC5 (rule): Unauthenticated access to `/` and `/lesson` redirects to `/login`
- **Observable condition:** `POST /lesson` without session returns HTTP 302 (redirect). Same for `GET /agents` and `GET /`.
- **Evidence source:** `test_darwin.py::test_08_flask_lesson_route_requires_login_and_shows_guides` and `test_09_flask_agents_dashboard` pass.

### AC6 (rule): The LangGraph lesson pipeline produces at least 3 agent proposals
- **Observable condition:** `run_lesson_pipeline()` returns `agent_plans` list with `len(agent_plans) >= 3`.
- **Evidence source:** `test_darwin.py::test_06_langgraph_multi_agent_pipeline` passes.

### AC7 (rule): Each lesson plan renders as a readable 5-phase teacher guide
- **Observable condition:** Each `plan["lesson_plan"]` string contains the exact headings:
  - `Teacher's Step-by-Step Classroom Guide`
  - `Phase 1: Classroom Hook`
  - `Phase 3: Step-by-Step Teaching Script`
  - `Phase 4: Formative Comprehension Check`
  No code fences (triple backticks) remain in the rendered/plain text output of `format_lesson_plan`.
- **Evidence source:** `test_darwin.py::test_06_langgraph_multi_agent_pipeline` and `test_10_lesson_plan_formatter_strips_fences` pass.

### AC8 (rule): Different teachers receive personalized/different lesson guides
- **Observable condition:** When the same topic is run through `run_lesson_pipeline` for `teacher_id=1, teacher_name="Prof. Ada"` vs a different teacher_id/name, the generated `lesson_plan` text includes the respective teacher's name and differs (different LLM temperature seeds). The test `test_06_langgraph_multi_agent_pipeline` asserts `"Prof. Ada"` appears in the guide.
- **Evidence source:** `test_darwin.py::test_06_langgraph_multi_agent_pipeline` passes (checks presence of `Prof. Ada` string).

### AC9 (rule): Meta-Agent autonomously evaluates every plan (no teacher scoring UI)
- **Observable condition:** `/lesson` rendered output contains `Meta-Agent Autonomous Audit Scorecard` and does NOT contain any `Rate Pedagogical Quality` / 1-5 star manual rating form for the teacher.
- **Evidence source:** `test_darwin.py::test_08_flask_lesson_route_requires_login_and_shows_guides` assertion `assertNotIn(b"Rate Pedagogical Quality", ...)` passes.

### AC10 (rule): Agent lifecycle data is tracked and displayed on `/agents`
- **Observable condition:**
  - `agents` table has columns: `created_at`, `retired_at`, `retirement_reason`, `mistake_summary`, `reproduction_reason`, `success_rationale`.
  - The `/agents` dashboard template displays `Born:` / `Destroyed:` timestamps, plus colored `lifecycle-alert-danger` (why destroyed + mistakes) and `lifecycle-alert-success` (why promoted + strengths) blocks.
  - The evolution_log table records every natural selection cycle with retired and reproduced agent lists.
- **Evidence source:** `schema.sql` columns present; `test_darwin.py::test_09_flask_agents_dashboard` passes (page contains `Agent Lifecycle` and `Natural Selection Evolution Log` text markers).

### AC11 (rule): Evolution retire/reproduce populates lifecycle reason fields
- **Observable condition:** `evolve_population()` in `evolution.py` writes `retired_at`, `retirement_reason`, and `mistake_summary` for retired agents, and writes `reproduction_reason` for both the child (spawned note) and the parent (promoted note).
- **Evidence source:** Code review of `evolution.py::evolve_population` lines 442–511; population_floor_restore and natural_selection_cycle events logged to `evolution_log`.

### AC12 (rule): File upload validates type and size
- **Observable condition:** `validate_file` rejects `.exe` and files >16 MB with clear messages.
- **Evidence source:** `test_darwin.py::test_03_transcription_validation` passes.

### AC13 (rubric): Rendered lesson guide visual legibility (0–2; threshold ≥ 1.5)
- **Dimension:** Browser visual quality of the rendered classroom guide.
- **Scale anchors:**
  - `0`: Walls of unformatted text, no headings, unreadable contrast.
  - `1`: Headings present but mis-styled; text dense but navigable.
  - `2`: Clear 5-phase Markdown rendered with distinct h3/h4, bullets, readable contrast, printed output legible via `@media print`.
- **Evidence source:** Manual browser check of `/lesson` with the fallback LLM, and CSS review of `.lesson-guide-rendered`, `.champion-banner`, `.meta-scorecard`, and `@media print` rules in `style.css`.

### AC14 (rubric): Lifecycle ledger explainability (0–2; threshold ≥ 1.5)
- **Dimension:** Clarity with which a human auditor can reconstruct "when each agent was born, why it was promoted, why it was destroyed, what mistakes it made" purely from the `/agents` page.
- **Scale anchors:**
  - `0`: No lifecycle data visible beyond active/retired status.
  - `1`: Some dates and reasons present but scattered; mistakes and promotion rationales not always paired with the triggering action.
  - `2`: For every agent card, a clear Born/Destroyed timestamp row; explicit danger/success alerts showing the Meta-Agent's reason text and mistake/success strings; evolution log table below summarizing each cycle.
- **Evidence source:** Manual `/agents` page review after running the pipeline with evolution cycles, plus code review of `templates/agents.html` and `evolution.py::evolve_population`.
