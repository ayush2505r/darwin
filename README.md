# Darwin: Evolving Multi-Agent Teaching Assistant

An educational web application where AI "teaching agents" propose how to explain topics to specific student groups, a human teacher rates the results, and a **Meta-Agent** evolves the population over time through a natural selection loop (retiring low-performing agents and cloning/mutating high-performing ones).

All agents share a unified **MySQL database** as collective memory, allowing successive generations to learn from past successes and avoid past mistakes.

---

## Key Features

1. **Teacher Authentication (`/login`, `/register`, `/logout`)**:
   - Individual teacher accounts with session tracking.
   - Seeded with a default teacher account: `username: teacher` / `password: password123`.
2. **At Least 3 Agents Generated Per Lesson**:
   - For every lesson request, the system selects **at least 3 active teaching agents** from the population.
   - Each agent generates a distinct pedagogical proposal based on its unique strategy genome and the shared collective memory.
   - An interactive tab switcher allows the teacher to compare all 3 plans and rate each agent individually (1–5 stars + comments).
3. **Actionable Step-by-Step Teacher Classroom Guide**:
   - Rather than generic text, each agent outputs a concrete, 5-phase teaching roadmap that teachers can directly follow in class:
     - 🎯 **Phase 1: Classroom Hook & Intuitive Kickoff** (First 5–7 mins, exact dialogue to speak and demo to show)
     - 💡 **Phase 2: Addressing Assessed Student Gaps & Misconceptions** (Targeting student weaknesses identified by the Assessor)
     - 📋 **Phase 3: Step-by-Step Teaching Script & Blackboard Flow** (Core 20-min lesson breakdown with diagram notes)
     - ❓ **Phase 4: Formative Comprehension Check** (Diagnostic questions, expected student answers, and corrective hints)
     - 🚀 **Phase 5: Differentiated Practice & Wrap-Up** (Exercises for struggling vs advanced learners)
4. **Terminal Connection Notice**:
   - Whenever MySQL is connected, the message `connected to mysql you can proceed` is automatically printed to the terminal.

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
3. **3-Agent Selection & Collective Memory Injection**:
   - At least 3 active Teaching Agents are selected using a weighted selection rule that favors high fitness (`avg_score`) while granting exploration bonuses to newly evolved generation agents.
   - Relevant lessons learned (past successes and pitfalls) are fetched from the MySQL `knowledge_pool` and injected into each agent's prompt.
4. **Multi-Agent Lesson Generation**:
   - All 3 agents produce their respective Teacher Classroom Guides tailored to the assessed student level and present them in tabbed comparison format.
5. **Teacher Feedback & Evolutionary Loop (`POST /feedback`)**:
   - The teacher rates any of the agents (1–5 stars) and optionally adds comments.
   - An LLM generates a concise pedagogical reflection (`outcome_summary`) saved to `knowledge_pool`.
   - The agent's `avg_score` is recalculated and linked to the teacher.
   - Every **N** pieces of feedback, the **Meta-Agent** evaluates the population:
     - **Retires** bottom $k$ performers (subject to the population floor of 3).
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

Run the seed script to create the database schema, populate the default teacher account, and initialize the 3 foundational Generation-1 agents:

```bash
python seed.py
```

To reset the database tables and re-seed from scratch:
```bash
python seed.py --reset
```

Upon connecting to MySQL, the terminal will print:
```
connected to mysql you can proceed
```

---

## Running the Application

Start the Flask development server:
```bash
python app.py
```

Open your browser and navigate to:
- **Teacher Login**: [http://127.0.0.1:5000/login](http://127.0.0.1:5000/login) (Default: `teacher` / `password123`)
- **Lesson Submission & 3-Agent Comparison**: [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
- **Agent Population Dashboard**: [http://127.0.0.1:5000/agents](http://127.0.0.1:5000/agents)

---

## Running Automated Tests

Run the included automated test suite:
```bash
python test_darwin.py
```

The test suite validates:
1. Pure MySQL connectivity (`use_pure=True`) and terminal notice `connected to mysql you can proceed`.
2. Seeding of initial agent population (at least 3 active agents maintained).
3. Media file validation and transcription stubbing.
4. Teacher authentication flow (registration, login, verification, session, logout).
5. Default teacher account verification.
6. LangGraph 3-agent generation producing actionable 5-phase Teacher Classroom Guides.
7. Feedback recording with `teacher_id` and collective memory reflection.
8. Multi-agent comparison tabs on `/lesson`.
9. Population dashboard and audit logs on `/agents`.

---

## Configuration & Natural Selection Rules

The evolutionary behavior is governed by four configuration constants:

| Variable | Default | Purpose |
|---|---|---|
| `EVOLUTION_FEEDBACK_THRESHOLD` | `5` | Evolution cycle triggers every $N$ pieces of teacher feedback. |
| `MIN_AGENT_USES_FOR_EVOLUTION` | `3` | Minimum sample size before an agent is eligible for retirement or reproduction. |
| `EVOLUTION_K` | `1` | Number of bottom agents to retire and top agents to reproduce per cycle. |
| `POPULATION_FLOOR` | `3` | Minimum number of active agents guaranteed so the population never collapses. |
