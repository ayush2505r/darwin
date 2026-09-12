# Darwin: Evolving Multi-Agent Teaching Assistant

Web app where AI teaching agents write classroom guides for a specific teacher and class, a **Meta-Agent** scores those guides, and natural selection retires weak agents and clones strong ones. All agents share one MySQL knowledge pool.

This is a functional MVP: Flask templates, LangGraph pipeline, Groq LLM, MySQL with `use_pure=True`.

---

## What a teacher actually does

1. Log in (`/login`) or register (`/register`).
2. Submit a topic plus optional student notes or media (`/`).
3. Read the student-level assessment and three classroom guides (`/lesson`).
4. Follow the Meta-Agent’s champion plan in class.

Teachers **do not** rate agents. Fitness, retirement, and reproduction are Meta-Agent decisions. Different teachers receive different agent mixes and different classroom scripts for the same topic.

---

## Pipeline

`transcribe → assess_student_level → select_agents → generate_lessons → meta_agent_evaluate`

- **Assessor** returns `level`, `reasoning`, `known_concepts`, `gaps`.
- **Three active teaching agents** each write a 5-phase Markdown classroom guide.
- **Meta-Agent** scores every guide (1–5), stores mistakes and strengths, and may trigger evolution after every `N` evaluations.

Evolution rule (plain ranking, not a full GA):

- Eligible agents need at least `MIN_AGENT_USES_FOR_EVOLUTION` uses.
- Retire the bottom `k` (never below `POPULATION_FLOOR`).
- Clone the top `k` with a mutated strategy prompt informed by the knowledge pool.
- Log birth, promotion, destruction, and reasons in `agents` + `evolution_log`.

If the active population drops below the floor, the Meta-Agent **spawns new agents**. It does not resurrect retired ones.

---

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Flask |
| Agents | LangChain + LangGraph |
| LLM | Groq (`GROQ_MODEL`, never hardcoded) |
| Database | MySQL |
| Driver | `mysql-connector-python` with `use_pure=True` |
| Frontend | Server-rendered Jinja templates + `static/style.css` |

On a successful MySQL connection the process prints once:

```
connected to mysql you can proceed
```

---

## Setup

Python 3.10+ and a running MySQL server.

```bash
cd darwin
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows: copy .env.example .env
```

Fill `.env` (see `.env.example`). Set `GROQ_MODEL` to a live Groq id (for example `openai/gpt-oss-20b`). `llama-3.3-70b-versatile` was shut down on Groq and will 404.

## Soup feedback training

The Soup repository is vendored at `vendor/Soup`. Every 1,000th feedback record creates an Alpaca JSONL dataset and a Soup SFT config under `training/soup`, then launches `soup train` in a detached process. Runs are recorded in the `soup_training_runs` MySQL table and duplicate launches for the same feedback boundary are prevented.

Soup requires Python 3.10–3.12 and its training extra. Install it in a compatible environment, then set `SOUP_CLI` to that environment's `soup` executable if it is not on `PATH`:

```powershell
py -3.12 -m venv .soup-venv
.soup-venv\Scripts\pip install -e "vendor/Soup[train]"
```

Configure `SOUP_BASE_MODEL`, `SOUP_ENABLED`, and `SOUP_WORK_DIR` in `.env.example` as needed. A CUDA GPU is recommended; CPU training is supported by Soup but is very slow.

If `GROQ_API_KEY` is empty, the app uses deterministic mock LLM responses so the pipeline still runs.

```bash
python seed.py
python app.py
```

Reset tables and re-seed:

```bash
python seed.py --reset
```

Default teacher: `teacher` / `password123`.

- Login: http://127.0.0.1:5000/login
- New lesson: http://127.0.0.1:5000/
- Lifecycle ledger: http://127.0.0.1:5000/agents

```bash
python test_darwin.py
```

---

## Config constants

| Variable | Default | Purpose |
|---|---|---|
| `EVOLUTION_FEEDBACK_THRESHOLD` | `5` | Evolution after every N Meta-Agent evaluations |
| `MIN_AGENT_USES_FOR_EVOLUTION` | `3` | Min uses before retire/reproduce |
| `EVOLUTION_K` | `1` | How many to retire and clone per cycle |
| `POPULATION_FLOOR` | `3` | Minimum active agents |

---

## Database tables

`teachers`, `agents` (including born/destroyed timestamps and reasons), `knowledge_pool`, `feedback` (Meta-Agent scores), `evolution_log`.
