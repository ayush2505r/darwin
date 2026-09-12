# Darwin: Evolving Multi-Agent Teaching Assistant

An educational web application where AI "teaching agents" propose how to explain topics to specific student groups, a human teacher rates the results, and a **Meta-Agent** evolves the population over time through a natural selection loop (retiring low-performing agents and cloning/mutating high-performing ones).

All agents share a unified **MySQL database** as collective memory, allowing successive generations to learn from past successes and avoid past mistakes.

---

## Architecture & Core User Flow

1. **Teacher Submits Lesson Request (`GET /` & `POST /lesson`)**:
   - Inputs: Lesson topic (required) + student context (optional text notes or uploaded media: `.txt`, `.mp3`, `.wav`, `.mp4`, etc.).
   - Input is transcribed/extracted to plain text.
2. **Student Assessment Step (`LangGraph`)**:
   - An LLM Assessor evaluates student background and returns a structured profile:
     ```json
     {
       "level": "beginner | intermediate | advanced",
       "reasoning": "...",
       "known_concepts": ["..."],
       "gaps": ["..."]
     }
     ```
3. **Agent Selection & Collective Memory Injection**:
   - An active Teaching Agent is chosen using an explainable weighted selection rule that favors high fitness (`avg_score`) while granting an exploration bonus to newly spawned generation agents.
   - Relevant lessons learned (past successes and pitfalls) are fetched from the MySQL `knowledge_pool` and injected into the agent's prompt.
4. **Lesson Generation**:
   - The selected Teaching Agent produces a comprehensive explanation tailored to the student level and presents it to the teacher.
5. **Teacher Feedback & Evolutionary Loop (`POST /feedback`)**:
   - The teacher rates the lesson (1–5 stars) and optionally adds comments.
   - An LLM generates a concise pedagogical reflection (`outcome_summary`) saved to `knowledge_pool`.
   - The agent's `avg_score` is recalculated.
   - Every **N** pieces of feedback, the **Meta-Agent** evaluates the population:
     - **Retires** bottom $k$ performers (subject to the population floor).
     - **Clones & Mutates** top $k$ performers into next-generation agents with mutated strategies informed by collective memory.
     - Logs events in `evolution_log`.
6. **Agent Population Dashboard (`GET /agents`)**:
   - View all active/retired agents, lineage, generations, usage counts, fitness scores, strategy prompts, and evolution audit logs.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend Framework | Flask |
| Agent Orchestration | LangChain + LangGraph |
| LLM Provider | Groq API (`langchain-groq`, model dynamically loaded from `GROQ_MODEL`) |
| Database | MySQL Server |
| DB Driver | `mysql-connector-python` (with `use_pure=True` mandatory constraint) |
| Frontend | Plain Server-Rendered HTML (Flask templates) + minimal CSS |

---

## Setup & Installation

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.14)
- MySQL Server (running on localhost or accessible network host)

### 2. Clone & Create Virtual Environment
```bash
cd darwin
python -m venv .venv

# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# On macOS/Linux:
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Configure your parameters in `.env`:
```env
# Groq API Configuration
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile

# MySQL Database Configuration
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=darwin_db

# Flask Security
FLASK_SECRET_KEY=your-secret-key

# Natural Selection Evolution Parameters
EVOLUTION_FEEDBACK_THRESHOLD=5
MIN_AGENT_USES_FOR_EVOLUTION=3
EVOLUTION_K=1
POPULATION_FLOOR=3
```

> **Note**: If `GROQ_API_KEY` is not provided or in testing mode, the system automatically falls back to deterministic mock responses so the complete LangGraph pipeline and UI can be developed and verified offline.

---

## Database Initialization & Seeding

Run the seed script to create the database schema and populate the initial Generation-1 population (3 agents with distinct pedagogical strategies):

```bash
python seed.py
```

To reset the database tables and re-seed from scratch:
```bash
python seed.py --reset
```

---

## Running the Application

Start the Flask development server:
```bash
python app.py
```

Open your browser and navigate to:
- **Lesson Submission & Feedback**: [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
- **Agent Population Dashboard**: [http://127.0.0.1:5000/agents](http://127.0.0.1:5000/agents)

---

## Running Automated Tests

Run the included automated test suite:
```bash
python test_darwin.py
```

The test suite validates:
1. Pure MySQL connectivity (`use_pure=True`).
2. Seeding of initial agent population.
3. Media file validation and transcription stubbing.
4. LangGraph workflow nodes and state transitions.
5. Feedback recording, LLM outcome summary reflection, and collective memory storage.
6. Flask routes (`/`, `/lesson`, `/feedback`, `/agents`).

---

## Configuration & Natural Selection Rules

The evolutionary behavior is governed by four configuration constants:

| Variable | Default | Purpose |
|---|---|---|
| `EVOLUTION_FEEDBACK_THRESHOLD` | `5` | Evolution cycle triggers every $N$ pieces of teacher feedback. |
| `MIN_AGENT_USES_FOR_EVOLUTION` | `3` | Minimum sample size before an agent is eligible for retirement or reproduction (prevents killing agents on bad luck). |
| `EVOLUTION_K` | `1` | Number of bottom agents to retire and top agents to reproduce per cycle. |
| `POPULATION_FLOOR` | `3` | Minimum number of active agents guaranteed so the population never collapses. |

### Selection & Mutation Loop:
1. **Selection at Request Time**:
   $$\text{weight} = \max(\text{avg\_score}, 1.0) + (1.5 \text{ if times\_used} == 0 \text{ else } 0)$$
   This rewards high performance while encouraging exploration of newly evolved agents.
2. **Mutation during Reproduction**:
   When an agent reproduces, the Meta-Agent prompts the LLM with the parent's `strategy_prompt` alongside recent successes ($\ge 4$ stars) and mistakes ($\le 2$ stars) from the `knowledge_pool`. The resulting mutated prompt is inserted as a child agent with `generation = parent.generation + 1`.
3. **Audit Trail**:
   Every evolution event (which agents were retired, which reproduced, parent ID, mutation details) is recorded in `evolution_log`.
