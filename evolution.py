"""Meta-Agent and Evolution engine for Darwin Evolving Teaching Assistant.

Handles:
1. Logging teacher feedback (score 1-5 + optional comment).
2. Generating LLM outcome summary for collective memory (knowledge_pool).
3. Recomputing agent avg_score.
4. Natural selection loop:
   - Triggered every N feedbacks (EVOLUTION_FEEDBACK_THRESHOLD).
   - Ranks active agents meeting minimum sample size (MIN_AGENT_USES_FOR_EVOLUTION).
   - Maintains POPULATION_FLOOR.
   - Retires bottom k agents.
   - Reproduces top k agents with LLM prompt mutation informed by collective memory.
   - Logs events into evolution_log.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

from db import execute_insert, execute_query, fetch_all, fetch_one
from llm import call_llm

load_dotenv()
logger = logging.getLogger(__name__)

# Configurable parameters with defaults from environment
FEEDBACK_THRESHOLD = int(os.getenv("EVOLUTION_FEEDBACK_THRESHOLD", 5))
MIN_USES_FOR_EVOLUTION = int(os.getenv("MIN_AGENT_USES_FOR_EVOLUTION", 3))
EVOLUTION_K = int(os.getenv("EVOLUTION_K", 1))
POPULATION_FLOOR = int(os.getenv("POPULATION_FLOOR", 3))


def generate_outcome_summary(
    topic: str,
    student_level: str,
    score: int,
    comment: Optional[str] = None,
    lesson_excerpt: Optional[str] = None
) -> str:
    """Generate a concise LLM reflection on why the lesson succeeded or failed."""
    system_prompt = (
        "You are an Educational Quality Analyst. Analyze this teaching interaction and teacher feedback. "
        "Summarize in 1-2 concise sentences what pedagogical aspect succeeded or failed and why. "
        "Be direct and actionable."
    )
    user_prompt = (
        f"Topic: {topic}\n"
        f"Student Level: {student_level}\n"
        f"Teacher Score: {score}/5\n"
        f"Teacher Comment: {comment or 'None provided'}\n"
    )
    if lesson_excerpt:
        # Include snippet of lesson
        user_prompt += f"Lesson Snippet: {lesson_excerpt[:400]}...\n"

    outcome = call_llm(system_prompt, user_prompt, temperature=0.3)
    return outcome.strip()


def mutate_strategy_prompt(parent_agent: Dict[str, Any], knowledge_excerpts: List[Dict[str, Any]]) -> str:
    """Meta-Agent: Generate a mutated, improved teaching prompt based on parent and collective memory."""
    system_prompt = (
        "You are the Meta-Agent overseeing the evolutionary development of AI Teaching Assistants.\n"
        "Your role is to act as the genetic mutator: study a parent agent's strategy prompt alongside "
        "the collective memory of what worked (high scores) and what failed (low scores) across recent lessons.\n"
        "Produce an improved, mutated system prompt for the next-generation child agent. "
        "Keep the strong foundational qualities of the parent while addressing identified weaknesses and "
        "incorporating lessons from the collective memory. "
        "Respond ONLY with the complete text of the new mutated strategy prompt. No conversational filler."
    )

    memory_summary = "Collective Knowledge Pool (Lessons Learned):\n"
    if knowledge_excerpts:
        for item in knowledge_excerpts:
            tag = "HIGH SCORE" if item.get("feedback_score", 0) >= 4 else "LOW SCORE"
            memory_summary += (
                f"- [{tag}] Topic: {item.get('topic')}, Level: {item.get('student_level')}, "
                f"Outcome: {item.get('outcome_summary')}"
            )
            if item.get("feedback_comment"):
                memory_summary += f" | Teacher comment: \"{item.get('feedback_comment')}\""
            memory_summary += "\n"
    else:
        memory_summary += "- General guideline: Refine clarity, interactive checks, and adaptive analogies.\n"

    user_prompt = (
        f"Parent Agent ID: #{parent_agent['agent_id']} (Generation {parent_agent['generation']})\n"
        f"Parent Strategy Prompt:\n{parent_agent['strategy_prompt']}\n\n"
        f"{memory_summary}\n"
        "Generate the mutated strategy prompt for Generation "
        f"{parent_agent['generation'] + 1}."
    )

    mutated_prompt = call_llm(system_prompt, user_prompt, temperature=0.7)
    return mutated_prompt.strip()


def record_feedback(
    agent_id: int,
    topic: str,
    student_level: str,
    score: int,
    comment: Optional[str] = None,
    lesson_excerpt: Optional[str] = None,
    teacher_id: Optional[int] = None
) -> Dict[str, Any]:
    """Store teacher feedback, generate outcome summary, update agent stats, and check evolution."""
    # 1. Insert into feedback table
    feedback_id = execute_insert(
        """
        INSERT INTO feedback (agent_id, teacher_id, topic, student_level, score, comment)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, teacher_id, topic, student_level, score, comment)
    )

    # 2. Generate outcome summary via LLM and save to knowledge_pool
    outcome_summary = generate_outcome_summary(
        topic=topic,
        student_level=student_level,
        score=score,
        comment=comment,
        lesson_excerpt=lesson_excerpt
    )

    knowledge_id = execute_insert(
        """
        INSERT INTO knowledge_pool (agent_id, topic, student_level, feedback_score, feedback_comment, outcome_summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, topic, student_level, score, comment, outcome_summary)
    )

    # 3. Recalculate agent's average score and total ratings
    stats = fetch_one(
        "SELECT AVG(score) as avg_score, COUNT(*) as cnt FROM feedback WHERE agent_id = %s",
        (agent_id,)
    )
    new_avg = round(float(stats["avg_score"] or 0.0), 2)
    
    execute_query(
        "UPDATE agents SET avg_score = %s WHERE agent_id = %s",
        (new_avg, agent_id),
        commit=True
    )

    # 4. Check if evolution should trigger
    total_feedback_row = fetch_one("SELECT COUNT(*) as total FROM feedback")
    total_feedback = total_feedback_row["total"] if total_feedback_row else 0

    evolution_result = None
    if total_feedback > 0 and (total_feedback % FEEDBACK_THRESHOLD == 0):
        logger.info(f"Feedback threshold reached ({total_feedback} total). Triggering Meta-Agent evolution cycle.")
        evolution_result = evolve_population()

    return {
        "feedback_id": feedback_id,
        "knowledge_id": knowledge_id,
        "outcome_summary": outcome_summary,
        "agent_avg_score": new_avg,
        "total_feedback_count": total_feedback,
        "evolution_triggered": evolution_result is not None,
        "evolution_details": evolution_result
    }


def evolve_population() -> Dict[str, Any]:
    """Execute the natural selection evolution loop.
    
    1. Fetch all active agents.
    2. Filter eligible agents (times_used >= MIN_USES_FOR_EVOLUTION).
    3. Rank eligible agents by avg_score.
    4. Retire worst k agents (respecting POPULATION_FLOOR).
    5. Clone & mutate best k agents using LLM and collective memory.
    6. Record event in evolution_log.
    """
    active_agents = fetch_all(
        "SELECT * FROM agents WHERE status = 'active' ORDER BY avg_score DESC, times_used DESC"
    )
    total_active = len(active_agents)

    # Filter eligible agents based on minimum sample size
    eligible_agents = [
        a for a in active_agents if int(a.get("times_used", 0) or 0) >= MIN_USES_FOR_EVOLUTION
    ]

    log_details: Dict[str, Any] = {
        "active_population_before": total_active,
        "eligible_agents_count": len(eligible_agents),
        "retired": [],
        "reproduced": [],
        "notes": ""
    }

    if len(eligible_agents) < 2:
        log_details["notes"] = (
            f"Insufficient eligible agents with >= {MIN_USES_FOR_EVOLUTION} uses "
            f"(found {len(eligible_agents)}). Evolution cycle postponed."
        )
        execute_insert(
            "INSERT INTO evolution_log (event_type, details) VALUES (%s, %s)",
            ("evolution_postponed", json.dumps(log_details))
        )
        return log_details

    # Determine how many agents can be retired without violating POPULATION_FLOOR
    max_retireable = max(0, total_active - POPULATION_FLOOR)
    k_retire = min(EVOLUTION_K, max_retireable, len(eligible_agents) - 1)

    # 1. Retirement: Bottom k agents
    retired_agents = []
    if k_retire > 0:
        # Candidate worst agents are the tail of eligible agents
        worst_candidates = eligible_agents[-k_retire:]
        for agent in worst_candidates:
            execute_query(
                "UPDATE agents SET status = 'retired' WHERE agent_id = %s",
                (agent["agent_id"],),
                commit=True
            )
            retired_info = {
                "agent_id": agent["agent_id"],
                "generation": agent["generation"],
                "avg_score": float(agent.get("avg_score", 0.0) or 0.0),
                "times_used": agent["times_used"],
                "reason": f"Bottom performer with avg_score {agent.get('avg_score')}"
            }
            retired_agents.append(retired_info)
            log_details["retired"].append(retired_info)
    else:
        log_details["notes"] += f"No agents retired to preserve population floor of {POPULATION_FLOOR}. "

    # 2. Reproduction: Top k agents
    k_reproduce = min(EVOLUTION_K, len(eligible_agents))
    top_candidates = eligible_agents[:k_reproduce]

    # Fetch recent knowledge excerpts to inform mutation
    knowledge_excerpts = fetch_all(
        "SELECT topic, student_level, feedback_score, feedback_comment, outcome_summary "
        "FROM knowledge_pool ORDER BY created_at DESC LIMIT 6"
    )

    reproduced_agents = []
    for parent in top_candidates:
        mutated_prompt = mutate_strategy_prompt(parent, knowledge_excerpts)
        new_gen = int(parent["generation"]) + 1

        new_agent_id = execute_insert(
            """
            INSERT INTO agents (generation, parent_id, strategy_prompt, status, times_used, avg_score)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (new_gen, parent["agent_id"], mutated_prompt, "active", 0, 0.0)
        )

        reproduction_info = {
            "new_agent_id": new_agent_id,
            "generation": new_gen,
            "parent_id": parent["agent_id"],
            "parent_avg_score": float(parent.get("avg_score", 0.0) or 0.0),
            "mutated_prompt_snippet": mutated_prompt[:120] + "..."
        }
        reproduced_agents.append(reproduction_info)
        log_details["reproduced"].append(reproduction_info)

    # Active population after evolution
    active_after = fetch_one("SELECT COUNT(*) as count FROM agents WHERE status = 'active'")
    log_details["active_population_after"] = active_after["count"] if active_after else 0

    # 3. Log evolution event in evolution_log table
    execute_insert(
        "INSERT INTO evolution_log (event_type, details) VALUES (%s, %s)",
        ("natural_selection_cycle", json.dumps(log_details))
    )

    return log_details
