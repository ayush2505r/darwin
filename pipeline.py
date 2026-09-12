"""LangGraph Pipeline module for Darwin Evolving Teaching Assistant.

Graph flow:
  START -> transcribe_node -> assess_student_node -> select_agent_node -> generate_lesson_node -> END

Each node is self-contained and testable in isolation.
"""

import json
import logging
import random
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from db import fetch_all, fetch_one, execute_query
from llm import call_llm
from transcription import extract_or_transcribe

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
    transcribed_text: str
    student_profile: Dict[str, Any]
    selected_agent: Optional[Dict[str, Any]]
    collective_memory: List[Dict[str, Any]]
    lesson_plan: str
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

    # Clean and parse JSON
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        profile = json.loads(cleaned)
        # Ensure mandatory fields
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
# Node 3: select_agent_node
# ---------------------------------------------------------------------------
def select_agent_node(state: LessonState) -> Dict[str, Any]:
    """Select one active Teaching Agent from MySQL using an explainable rule.
    
    Rule: Weighted random selection where weight = max(avg_score, 1.0) + (1.5 if times_used == 0 else 0).
    This balances exploitation of high-rated agents while providing exploration
    for newly mutated generation agents that haven't been used yet.
    """
    agents = fetch_all("SELECT * FROM agents WHERE status = 'active'")
    if not agents:
        raise RuntimeError("No active teaching agents found in the database. Run seed.py first.")

    # Calculate weights
    weights = []
    for agent in agents:
        avg_score = float(agent.get("avg_score", 0.0) or 0.0)
        times_used = int(agent.get("times_used", 0) or 0)
        # Exploration bonus for unrated agents
        exploration_bonus = 1.5 if times_used == 0 else 0.0
        weight = max(avg_score, 1.0) + exploration_bonus
        weights.append(weight)

    selected = random.choices(agents, weights=weights, k=1)[0]
    return {"selected_agent": selected}


# ---------------------------------------------------------------------------
# Helper: fetch collective memory from knowledge_pool
# ---------------------------------------------------------------------------
def fetch_collective_memory(topic: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Retrieve high-signal lessons learned (top successes and top mistakes) from MySQL."""
    try:
        # First check topic-specific successes (score >= 4) and mistakes (score <= 2)
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


# ---------------------------------------------------------------------------
# Node 4: generate_lesson_node
# ---------------------------------------------------------------------------
def generate_lesson_node(state: LessonState) -> Dict[str, Any]:
    """Generate the lesson plan using selected agent's strategy and collective memory."""
    agent = state.get("selected_agent")
    if not agent:
        raise ValueError("No selected agent in state")

    topic = state.get("topic", "")
    profile = state.get("student_profile", {})
    agent_prompt = agent.get("strategy_prompt", "")

    # Retrieve collective memory
    memory_rows = fetch_collective_memory(topic, limit=4)

    # Format collective memory context
    memory_section = ""
    if memory_rows:
        memory_lines = ["\n### Shared Collective Memory (Lessons learned by past agents):"]
        for row in memory_rows:
            score = row.get("feedback_score")
            tag = "SUCCESS" if score >= 4 else ("MISTAKE" if score <= 2 else "FEEDBACK")
            comment = f" (Teacher comment: \"{row.get('feedback_comment')}\")" if row.get("feedback_comment") else ""
            memory_lines.append(
                f"- [{tag}] For student level '{row.get('student_level')}': {row.get('outcome_summary')}{comment}"
            )
        memory_lines.append("Take these past outcomes into account to avoid repeated mistakes and repeat successful patterns.")
        memory_section = "\n".join(memory_lines)

    system_prompt = (
        f"{agent_prompt}\n\n"
        "You are generating an explanation and lesson plan for a teacher to use with a student.\n"
        "Pitch the explanation precisely at the assessed student level. "
        "Address their knowledge gaps directly while building on what they already know.\n"
        f"{memory_section}"
    )

    user_prompt = (
        f"Target Topic: {topic}\n\n"
        f"Assessed Student Profile:\n"
        f"- Assessed Level: {profile.get('level', 'intermediate')}\n"
        f"- Reasoning: {profile.get('reasoning', 'N/A')}\n"
        f"- Known Concepts: {', '.join(profile.get('known_concepts', [])) or 'None specified'}\n"
        f"- Knowledge Gaps: {', '.join(profile.get('gaps', [])) or 'None specified'}\n\n"
        "Please generate a comprehensive, structured lesson explanation and pedagogical guide for this topic."
    )

    lesson_plan = call_llm(system_prompt, user_prompt, temperature=0.7)

    # Update agent times_used counter in DB
    try:
        execute_query(
            "UPDATE agents SET times_used = times_used + 1 WHERE agent_id = %s",
            (agent["agent_id"],),
            commit=True
        )
    except Exception as e:
        logger.warning(f"Could not increment times_used for agent #{agent['agent_id']}: {e}")

    return {
        "collective_memory": memory_rows,
        "lesson_plan": lesson_plan
    }


# ---------------------------------------------------------------------------
# Build and Compile LangGraph
# ---------------------------------------------------------------------------
def build_lesson_graph():
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(LessonState)

    # Add nodes
    workflow.add_node("transcribe", transcribe_node)
    workflow.add_node("assess_student_level", assess_student_node)
    workflow.add_node("select_agent", select_agent_node)
    workflow.add_node("generate_lesson", generate_lesson_node)

    # Define edges: transcribe -> assess_student_level -> select_agent -> generate_lesson -> END
    workflow.add_edge(START, "transcribe")
    workflow.add_edge("transcribe", "assess_student_level")
    workflow.add_edge("assess_student_level", "select_agent")
    workflow.add_edge("select_agent", "generate_lesson")
    workflow.add_edge("generate_lesson", END)

    return workflow.compile()


# Singleton compiled graph instance
lesson_graph = build_lesson_graph()


def run_lesson_pipeline(
    topic: str,
    context_text: Optional[str] = None,
    media_path: Optional[str] = None,
    original_filename: Optional[str] = None
) -> Dict[str, Any]:
    """Execute the compiled LangGraph pipeline end-to-end."""
    initial_state: LessonState = {
        "topic": topic,
        "context_text": context_text,
        "media_path": media_path,
        "original_filename": original_filename,
        "transcribed_text": "",
        "student_profile": {},
        "selected_agent": None,
        "collective_memory": [],
        "lesson_plan": "",
        "error": None
    }

    result = lesson_graph.invoke(initial_state)
    return result
