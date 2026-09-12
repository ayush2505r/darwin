"""LangGraph Pipeline module for Darwin Evolving Teaching Assistant.

Graph flow:
  START -> transcribe_node -> assess_student_node -> select_agents_node -> generate_lessons_node -> meta_agent_evaluate_node -> END

Selects at least 3 active Teaching Agents for each lesson request, generates
actionable Teacher Classroom Guides, and executes an Autonomous Meta-Agent
Evaluation that scores fitness, records lessons learned, and drives natural selection.
"""

import json
import logging
import random
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from db import fetch_all, execute_query
from llm import call_llm
from transcription import extract_or_transcribe
from evolution import ensure_population_floor, meta_agent_evaluate_plan, record_autonomous_evaluation

logger = logging.getLogger(__name__)


class StudentProfile(TypedDict):
    level: str
    reasoning: str
    known_concepts: List[str]
    gaps: List[str]


class LessonState(TypedDict):
    topic: str
    context_text: Optional[str]
    media_path: Optional[str]
    original_filename: Optional[str]
    teacher_id: Optional[int]
    teacher_name: Optional[str]
    transcribed_text: str
    student_profile: Dict[str, Any]
    selected_agent: Optional[Dict[str, Any]]
    selected_agents: List[Dict[str, Any]]
    collective_memory: List[Dict[str, Any]]
    lesson_plan: str
    agent_plans: List[Dict[str, Any]]
    champion_plan: Optional[Dict[str, Any]]
    evolution_triggered: bool
    evolution_details: Optional[Dict[str, Any]]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Node 1: transcribe_node
# ---------------------------------------------------------------------------
def transcribe_node(state: LessonState) -> Dict[str, Any]:
    """Extract text from uploaded notes or audio/video recording."""
    media_path = state.get("media_path")
    original_filename = state.get("original_filename")
    context_text = state.get("context_text")

    try:
        extracted = extract_or_transcribe(
            file_path=media_path,
            original_filename=original_filename,
            raw_text=context_text
        )
        return {"transcribed_text": extracted}
    except Exception as e:
        logger.error(f"Error in transcribe_node: {e}")
        return {
            "transcribed_text": f"[Error extracting context: {e}]",
            "error": str(e)
        }


# ---------------------------------------------------------------------------
# Node 2: assess_student_node
# ---------------------------------------------------------------------------
def assess_student_node(state: LessonState) -> Dict[str, Any]:
    """Analyze context and topic to produce a structured student profile."""
    topic = state.get("topic", "").strip()
    context = state.get("transcribed_text", "").strip()

    system_prompt = (
        "You are an expert Educational Assessor. Your task is to evaluate the student's background "
        "and familiarity with the target topic based on provided context notes or classroom transcriptions.\n"
        "You MUST respond ONLY with a valid JSON object strictly matching this schema:\n"
        "{\n"
        '  "level": "beginner" | "intermediate" | "advanced",\n'
        '  "reasoning": "short 1-2 sentence explanation",\n'
        '  "known_concepts": ["concept1", "concept2"],\n'
        '  "gaps": ["gap1", "gap2"]\n'
        "}\n"
        "Do not include any explanation outside the JSON markdown block."
    )

    user_prompt = f"Topic to teach: {topic}\n\n"
    if context:
        user_prompt += f"Context & Student Background Material:\n{context}"
    else:
        user_prompt += "No student background provided. Infer an introductory baseline for this topic."

    raw_response = call_llm(system_prompt, user_prompt, temperature=0.2)

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        profile = json.loads(cleaned)
        if not isinstance(profile, dict):
            raise ValueError("Response is not a dict")
        profile.setdefault("level", "intermediate")
        profile.setdefault("reasoning", "Assessed based on provided topic and background.")
        profile.setdefault("known_concepts", [])
        profile.setdefault("gaps", [])
    except Exception as e:
        logger.warning(f"Failed to parse assessor response as JSON ({e}). Using normalized fallback.")
        level = "beginner" if "beginner" in cleaned.lower() else ("advanced" if "advanced" in cleaned.lower() else "intermediate")
        profile = {
            "level": level,
            "reasoning": "Determined baseline from lesson topic and available notes.",
            "known_concepts": ["Foundational terms"],
            "gaps": ["Practical application and deeper principles"]
        }

    return {"student_profile": profile}


# ---------------------------------------------------------------------------
# Node 3: select_agents_node (Selects at least 3 active Teaching Agents)
# ---------------------------------------------------------------------------
def select_agents_node(state: LessonState) -> Dict[str, Any]:
    """Select at least 3 active Teaching Agents from MySQL for comparative generation."""
    ensure_population_floor()
    agents = fetch_all("SELECT * FROM agents WHERE status = 'active' ORDER BY avg_score DESC, times_used DESC")

    if not agents:
        raise RuntimeError("No active teaching agents found in the database. Run seed.py first.")

    teacher_id = state.get("teacher_id") or 0
    topic = state.get("topic") or ""
    rng = random.Random(f"{teacher_id}:{topic.lower().strip()}")

    if len(agents) <= 3:
        selected_agents = list(agents)
        rng.shuffle(selected_agents)
    else:
        pool = list(agents)
        selected_agents = []
        for _ in range(3):
            weights = []
            for a in pool:
                avg_score = float(a.get("avg_score", 0.0) or 0.0)
                times_used = int(a.get("times_used", 0) or 0)
                bonus = 1.5 if times_used == 0 else 0.0
                weights.append(max(avg_score, 1.0) + bonus)
            chosen = rng.choices(pool, weights=weights, k=1)[0]
            selected_agents.append(chosen)
            pool.remove(chosen)

    return {
        "selected_agents": selected_agents,
        "selected_agent": selected_agents[0] if selected_agents else None
    }


# ---------------------------------------------------------------------------
# Helper: fetch collective memory from knowledge_pool
# ---------------------------------------------------------------------------
def fetch_collective_memory(topic: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Retrieve high-signal lessons learned from MySQL collective memory."""
    try:
        query = """
            SELECT knowledge_id, agent_id, topic, student_level, feedback_score, feedback_comment, outcome_summary
            FROM knowledge_pool
            ORDER BY 
                CASE WHEN LOWER(topic) = LOWER(%s) THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT %s
        """
        rows = fetch_all(query, (topic, limit))
        return rows or []
    except Exception as e:
        logger.warning(f"Could not fetch collective memory: {e}")
        return []


def format_lesson_plan(raw: str, topic: str) -> str:
    """Normalize LLM output into readable Markdown a teacher can follow."""
    text = (raw or "").strip()
    if not text:
        return f"### Teacher's Classroom Guide: {topic}\n\nThe agent did not return a usable plan. Please generate again."

    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md|text)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        maybe_json = json.loads(text)
        if isinstance(maybe_json, dict):
            text = maybe_json.get("lesson_plan") or maybe_json.get("guide") or json.dumps(maybe_json, indent=2)
    except Exception:
        pass

    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if "Phase 1" not in text and "###" not in text:
        text = f"### Teacher's Step-by-Step Classroom Guide: {topic}\n\n{text}"

    return text.strip()


# ---------------------------------------------------------------------------
# Node 4: generate_lessons_node
# ---------------------------------------------------------------------------
def generate_lessons_node(state: LessonState) -> Dict[str, Any]:
    """Generate detailed Teacher's Classroom Guides for each of the selected 3 agents."""
    agents = state.get("selected_agents") or []
    if not agents and state.get("selected_agent"):
        agents = [state["selected_agent"]]

    if not agents:
        raise ValueError("No agents selected for lesson generation")

    topic = state.get("topic", "")
    profile = state.get("student_profile", {})
    teacher_name = state.get("teacher_name") or "the classroom teacher"
    teacher_id = state.get("teacher_id") or 0
    memory_rows = fetch_collective_memory(topic, limit=4)

    memory_section = ""
    if memory_rows:
        memory_lines = ["\n### Shared Collective Memory (Lessons learned by past agents):"]
        for row in memory_rows:
            score = row.get("feedback_score")
            tag = "SUCCESS" if score >= 4 else ("MISTAKE" if score <= 2 else "FEEDBACK")
            comment = f" (Note: \"{row.get('feedback_comment')}\")" if row.get("feedback_comment") else ""
            memory_lines.append(
                f"- [{tag}] For student level '{row.get('student_level')}': {row.get('outcome_summary')}{comment}"
            )
        memory_lines.append("Take these past outcomes into account to avoid repeated mistakes and repeat successful patterns.")
        memory_section = "\n".join(memory_lines)

    agent_plans: List[Dict[str, Any]] = []

    for agent in agents:
        agent_prompt = agent.get("strategy_prompt", "")
        temperature = 0.55 + ((int(agent.get("agent_id") or 0) + int(teacher_id)) % 5) * 0.06

        system_prompt = (
            f"{agent_prompt}\n\n"
            "You are an expert Educational Consultant preparing a practical CLASSROOM TEACHING GUIDE "
            f"that {teacher_name} will follow live with students.\n"
            "Write clean Markdown only. Use headings, short paragraphs, and bullet lists. "
            "Do not output JSON, XML, or code fences. Do not dump a wall of unformatted text.\n\n"
            "Use exactly these 5 heading levels:\n"
            "### Teacher's Step-by-Step Classroom Guide: <topic>\n"
            "#### Phase 1: Classroom Hook & Intuitive Kickoff\n"
            "#### Phase 2: Addressing Assessed Student Gaps & Misconceptions\n"
            "#### Phase 3: Step-by-Step Teaching Script & Blackboard Flow\n"
            "#### Phase 4: Formative Comprehension Check\n"
            "#### Phase 5: Differentiated Practice & Wrap-Up\n\n"
            "Under each phase include: what to say (quoted teacher lines), what to write on the board, "
            "timing, and what to do if students get stuck.\n"
            f"Tailor strictly to assessed student level: {profile.get('level', 'intermediate')}.\n"
            f"Personalize examples and classroom dialogue for this teacher's session (teacher id {teacher_id}). "
            "Do not reuse generic filler that another teacher would receive unchanged.\n"
            f"{memory_section}"
        )

        user_prompt = (
            f"Target Topic to Teach: {topic}\n\n"
            f"Teacher receiving this guide: {teacher_name}\n"
            f"Assessed Student Profile:\n"
            f"- Assessed Level: {profile.get('level', 'intermediate')}\n"
            f"- Reasoning: {profile.get('reasoning', 'N/A')}\n"
            f"- Known Concepts: {', '.join(profile.get('known_concepts', [])) or 'None specified'}\n"
            f"- Knowledge Gaps: {', '.join(profile.get('gaps', [])) or 'None specified'}\n\n"
            "Generate the comprehensive, step-by-step Teacher Classroom Guide."
        )

        lesson_plan = format_lesson_plan(call_llm(system_prompt, user_prompt, temperature=temperature), topic)

        try:
            execute_query(
                "UPDATE agents SET times_used = times_used + 1 WHERE agent_id = %s",
                (agent["agent_id"],),
                commit=True
            )
            agent["times_used"] = int(agent.get("times_used") or 0) + 1
        except Exception as e:
            logger.warning(f"Could not increment times_used for agent #{agent['agent_id']}: {e}")

        agent_plans.append({
            "agent": agent,
            "collective_memory": memory_rows,
            "lesson_plan": lesson_plan
        })

    return {
        "agent_plans": agent_plans,
        "collective_memory": memory_rows
    }


# ---------------------------------------------------------------------------
# Node 5: meta_agent_evaluate_node (Autonomous Meta-Agent Evaluation)
# ---------------------------------------------------------------------------
def meta_agent_evaluate_node(state: LessonState) -> Dict[str, Any]:
    """Autonomous Meta-Agent evaluates all agent proposals and updates natural selection."""
    topic = state.get("topic", "")
    profile = state.get("student_profile", {})
    agent_plans = state.get("agent_plans") or []
    teacher_id = state.get("teacher_id")

    last_evolution_details = None
    evolution_triggered = False

    evaluated_plans = []
    for plan in agent_plans:
        agent = plan["agent"]
        lesson_plan = plan["lesson_plan"]

        # Meta-Agent autonomous evaluation
        evaluation = meta_agent_evaluate_plan(
            topic=topic,
            student_profile=profile,
            agent=agent,
            lesson_plan=lesson_plan,
            memory_rows=plan.get("collective_memory")
        )

        # Record evaluation into MySQL
        db_res = record_autonomous_evaluation(
            agent_id=agent["agent_id"],
            topic=topic,
            student_level=profile.get("level", "intermediate"),
            evaluation=evaluation,
            teacher_id=teacher_id
        )

        if db_res.get("evolution_triggered"):
            evolution_triggered = True
            last_evolution_details = db_res.get("evolution_details")

        # Attach evaluation to plan
        evaluated_plans.append({
            "agent": agent,
            "collective_memory": plan.get("collective_memory", []),
            "lesson_plan": lesson_plan,
            "evaluation": evaluation,
            "score": evaluation["score"]
        })

    # Sort plans by score descending to find Champion Agent
    evaluated_plans.sort(key=lambda p: p["score"], reverse=True)
    champion_plan = evaluated_plans[0] if evaluated_plans else None

    return {
        "agent_plans": evaluated_plans,
        "champion_plan": champion_plan,
        "selected_agent": champion_plan["agent"] if champion_plan else None,
        "lesson_plan": champion_plan["lesson_plan"] if champion_plan else "",
        "evolution_triggered": evolution_triggered,
        "evolution_details": last_evolution_details
    }


# ---------------------------------------------------------------------------
# Build and Compile LangGraph
# ---------------------------------------------------------------------------
def build_lesson_graph():
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(LessonState)

    workflow.add_node("transcribe", transcribe_node)
    workflow.add_node("assess_student_level", assess_student_node)
    workflow.add_node("select_agents", select_agents_node)
    workflow.add_node("generate_lessons", generate_lessons_node)
    workflow.add_node("meta_agent_evaluate", meta_agent_evaluate_node)

    workflow.add_edge(START, "transcribe")
    workflow.add_edge("transcribe", "assess_student_level")
    workflow.add_edge("assess_student_level", "select_agents")
    workflow.add_edge("select_agents", "generate_lessons")
    workflow.add_edge("generate_lessons", "meta_agent_evaluate")
    workflow.add_edge("meta_agent_evaluate", END)

    return workflow.compile()


lesson_graph = build_lesson_graph()


def run_lesson_pipeline(
    topic: str,
    context_text: Optional[str] = None,
    media_path: Optional[str] = None,
    original_filename: Optional[str] = None,
    teacher_id: Optional[int] = None,
    teacher_name: Optional[str] = None
) -> Dict[str, Any]:
    """Execute the compiled LangGraph pipeline end-to-end with autonomous evaluation."""
    initial_state: LessonState = {
        "topic": topic,
        "context_text": context_text,
        "media_path": media_path,
        "original_filename": original_filename,
        "teacher_id": teacher_id,
        "teacher_name": teacher_name,
        "transcribed_text": "",
        "student_profile": {},
        "selected_agent": None,
        "selected_agents": [],
        "collective_memory": [],
        "lesson_plan": "",
        "agent_plans": [],
        "champion_plan": None,
        "evolution_triggered": False,
        "evolution_details": None,
        "error": None
    }

    result = lesson_graph.invoke(initial_state)
    return result
