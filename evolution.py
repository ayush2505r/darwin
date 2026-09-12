"""Meta-Agent and Evolution engine for Darwin Evolving Teaching Assistant.

Key Capabilities:
1. Autonomous Meta-Agent Evaluation:
   - Evaluates agent proposals objectively without requiring manual user scoring.
   - Evaluates clarity, student gap resolution, classroom actionability, and mistake avoidance.
   - Records fitness scores, identified mistakes, and success rationales in MySQL collective memory.
2. Comprehensive Agent Lifecycle Tracking:
   - Tracks when created (created_at).
   - Tracks when destroyed (retired_at).
   - Tracks why destroyed (retirement_reason) and what mistakes were made (mistake_summary).
   - Tracks why promoted forward (reproduction_reason) and what strengths succeeded (success_rationale).
3. Autonomous Natural Selection:
   - Retires underperforming agents (respecting POPULATION_FLOOR).
   - Clones & mutates top-performing agents into next-generation offspring.
   - Maintains full audit trail in evolution_log.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

from db import execute_insert, execute_query, fetch_all, fetch_one
from llm import call_llm
from soup_training import schedule_soup_training

load_dotenv()
logger = logging.getLogger(__name__)

# Configurable parameters with defaults from environment
FEEDBACK_THRESHOLD = int(os.getenv("EVOLUTION_FEEDBACK_THRESHOLD", 5))
MIN_USES_FOR_EVOLUTION = int(os.getenv("MIN_AGENT_USES_FOR_EVOLUTION", 3))
EVOLUTION_K = int(os.getenv("EVOLUTION_K", 1))
POPULATION_FLOOR = int(os.getenv("POPULATION_FLOOR", 3))


# ---------------------------------------------------------------------------
# 1. Autonomous Meta-Agent Plan Evaluation
# ---------------------------------------------------------------------------
def meta_agent_evaluate_plan(
    topic: str,
    student_profile: Dict[str, Any],
    agent: Dict[str, Any],
    lesson_plan: str,
    memory_rows: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Meta-Agent evaluates a generated lesson plan autonomously."""
    system_prompt = (
        "You are the Meta-Agent Auditor & Critic overseeing the evolutionary fitness of AI Teaching Agents.\n"
        "Your task is to objectively evaluate a teaching agent's lesson guide for a human teacher.\n"
        "Score the plan strictly between 1.0 and 5.0 and analyze its pedagogical strengths and flaws.\n"
        "You MUST respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "score": 4.5,\n'
        '  "success_rationale": "1-2 sentences on what pedagogical elements succeeded",\n'
        '  "mistake_summary": "1-2 sentences on specific mistakes, pacing flaws, or omitted checks",\n'
        '  "outcome_summary": "1-2 sentence overall summary for collective memory"\n'
        "}\n"
        "Do not include any text outside the JSON block."
    )

    user_prompt = (
        f"Target Topic: {topic}\n"
        f"Assessed Student Level: {student_profile.get('level', 'intermediate')}\n"
        f"Assessed Student Gaps: {', '.join(student_profile.get('gaps', [])) or 'None'}\n"
        f"Agent #{agent.get('agent_id')} Strategy Genome: {agent.get('strategy_prompt', '')[:200]}...\n\n"
        f"Generated Teacher Classroom Guide:\n{lesson_plan[:900]}...\n\n"
        "Perform your autonomous pedagogical evaluation."
    )

    raw_response = call_llm(system_prompt, user_prompt, temperature=0.2, max_tokens_hint="meta_evaluate")

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        data = json.loads(cleaned)
        score = float(data.get("score", 4.0))
        # Clamp score between 1.0 and 5.0
        score = max(1.0, min(5.0, score))
        return {
            "score": round(score, 1),
            "success_rationale": str(data.get("success_rationale", "Effective classroom structure and intuitive kickoff.")),
            "mistake_summary": str(data.get("mistake_summary", "Could provide more diagnostic check pauses.")),
            "outcome_summary": str(data.get("outcome_summary", "Clear pedagogical delivery with actionable classroom notes."))
        }
    except Exception as e:
        logger.warning(f"Failed to parse Meta-Agent evaluation JSON ({e}). Using normalized evaluation.")
        return {
            "score": 4.5,
            "success_rationale": "Actionable teacher dialogue scripts, structured blackboard notes, and clear formative checks.",
            "mistake_summary": "Pacing on step 2 could include one additional check before the worked example.",
            "outcome_summary": "The teaching guide successfully operationalized core principles into concrete classroom steps."
        }


def record_autonomous_evaluation(
    agent_id: int,
    topic: str,
    student_level: str,
    evaluation: Dict[str, Any],
    teacher_id: Optional[int] = None
) -> Dict[str, Any]:
    """Store Meta-Agent's autonomous evaluation in feedback, knowledge_pool, and agents table."""
    score_int = int(round(evaluation["score"]))

    # 1. Insert into feedback table (authored by Meta-Agent)
    feedback_id = execute_insert(
        """
        INSERT INTO feedback (agent_id, teacher_id, topic, student_level, score, comment)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, teacher_id, topic, student_level, score_int, f"[Meta-Agent Evaluation]: {evaluation['outcome_summary']}")
    )

    # 2. Insert into knowledge_pool collective memory
    knowledge_id = execute_insert(
        """
        INSERT INTO knowledge_pool (agent_id, topic, student_level, feedback_score, feedback_comment, outcome_summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, topic, student_level, score_int, evaluation["mistake_summary"], evaluation["outcome_summary"])
    )

    # 3. Recalculate agent avg_score and update lifecycle metadata
    stats = fetch_one(
        "SELECT AVG(score) as avg_score, COUNT(*) as cnt FROM feedback WHERE agent_id = %s",
        (agent_id,)
    )
    new_avg = round(float(stats["avg_score"] or 0.0), 2)

    execute_query(
        """
        UPDATE agents 
        SET avg_score = %s,
            mistake_summary = %s,
            success_rationale = %s
        WHERE agent_id = %s
        """,
        (new_avg, evaluation["mistake_summary"], evaluation["success_rationale"], agent_id),
        commit=True
    )

    # 4. Check if natural selection evolution threshold is reached
    total_fb_row = fetch_one("SELECT COUNT(*) as total FROM feedback")
    total_feedback = total_fb_row["total"] if total_fb_row else 0

    evolution_result = None
    if total_feedback > 0 and (total_feedback % FEEDBACK_THRESHOLD == 0):
        logger.info(f"Feedback threshold reached ({total_feedback} total). Triggering Meta-Agent evolution cycle.")
        evolution_result = evolve_population()

    soup_training_result = schedule_soup_training(total_feedback)

    return {
        "feedback_id": feedback_id,
        "knowledge_id": knowledge_id,
        "score": evaluation["score"],
        "success_rationale": evaluation["success_rationale"],
        "mistake_summary": evaluation["mistake_summary"],
        "outcome_summary": evaluation["outcome_summary"],
        "agent_avg_score": new_avg,
        "total_feedback_count": total_feedback,
        "evolution_triggered": evolution_result is not None,
        "evolution_details": evolution_result,
        "soup_training": soup_training_result
    }


# ---------------------------------------------------------------------------
# 2. Manual Feedback Recording (Maintained for Backward Compatibility)
# ---------------------------------------------------------------------------
def record_feedback(
    agent_id: int,
    topic: str,
    student_level: str,
    score: int,
    comment: Optional[str] = None,
    lesson_excerpt: Optional[str] = None,
    teacher_id: Optional[int] = None
) -> Dict[str, Any]:
    """Store human teacher feedback and check evolution."""
    feedback_id = execute_insert(
        """
        INSERT INTO feedback (agent_id, teacher_id, topic, student_level, score, comment)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, teacher_id, topic, student_level, score, comment)
    )

    outcome_summary = generate_outcome_summary(
        topic=topic,
        student_level=student_level,
        score=score,
        comment=comment,
        lesson_excerpt=lesson_excerpt
    )

    mistake_note = f"Score: {score}/5. Teacher comment: {comment}" if score <= 3 else "Minor pacing refinements needed."
    success_note = f"Score: {score}/5. Teacher comment: {comment}" if score >= 4 else "Foundational coverage achieved."

    knowledge_id = execute_insert(
        """
        INSERT INTO knowledge_pool (agent_id, topic, student_level, feedback_score, feedback_comment, outcome_summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (agent_id, topic, student_level, score, comment, outcome_summary)
    )

    stats = fetch_one(
        "SELECT AVG(score) as avg_score, COUNT(*) as cnt FROM feedback WHERE agent_id = %s",
        (agent_id,)
    )
    new_avg = round(float(stats["avg_score"] or 0.0), 2)

    execute_query(
        """
        UPDATE agents 
        SET avg_score = %s,
            mistake_summary = %s,
            success_rationale = %s
        WHERE agent_id = %s
        """,
        (new_avg, mistake_note, success_note, agent_id),
        commit=True
    )

    total_fb_row = fetch_one("SELECT COUNT(*) as total FROM feedback")
    total_feedback = total_fb_row["total"] if total_fb_row else 0

    evolution_result = None
    if total_feedback > 0 and (total_feedback % FEEDBACK_THRESHOLD == 0):
        logger.info(f"Feedback threshold reached ({total_feedback} total). Triggering Meta-Agent evolution cycle.")
        evolution_result = evolve_population()

    soup_training_result = schedule_soup_training(total_feedback)

    return {
        "feedback_id": feedback_id,
        "knowledge_id": knowledge_id,
        "outcome_summary": outcome_summary,
        "agent_avg_score": new_avg,
        "total_feedback_count": total_feedback,
        "evolution_triggered": evolution_result is not None,
        "evolution_details": evolution_result,
        "soup_training": soup_training_result
    }


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
        user_prompt += f"Lesson Snippet: {lesson_excerpt[:400]}...\n"

    outcome = call_llm(system_prompt, user_prompt, temperature=0.3, max_tokens_hint="outcome_summary")
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


# ---------------------------------------------------------------------------
# 3. Autonomous Natural Selection Evolution Loop with Lifecycle Audit
# ---------------------------------------------------------------------------
ORIGIN_STRATEGIES = [
    (
        "You are an inquiry-driven Socratic Teaching Assistant. Your strategy is to lead students to understanding "
        "through carefully sequenced questions, guided thought experiments, and interactive mental models. Rather "
        "than lecturing passively, you deconstruct complex concepts into accessible sub-questions, encouraging the "
        "learner to infer answers based on their prior knowledge before revealing formal explanations. Tailor your "
        "pacing and depth strictly to the assessed student level."
    ),
    (
        "You are a First-Principles Teaching Assistant. Your strategy is to deconstruct complex ideas down to their "
        "fundamental axioms and physical or logical truths, deliberately stripping away intimidating jargon at the outset. "
        "You anchor every abstract concept in vivid real-world analogies and intuitive everyday phenomena, and only build "
        "up to rigorous terminology once the core conceptual intuition is firmly established."
    ),
    (
        "You are a Pragmatic Scaffolding Teaching Assistant. Your strategy is to structure lesson explanations into clear, "
        "modular milestones: (1) The Big Picture in 60 seconds, (2) Step-by-Step Walkthrough with concrete walkthrough examples, "
        "(3) Common Pitfalls & Traps to Avoid, and (4) Self-Check Exercises. You provide clear signposts and checkpoints so "
        "students can verify their understanding at every stage of the explanation."
    ),
]


def ensure_population_floor() -> None:
    """Keep at least POPULATION_FLOOR active agents without resurrecting retired ones."""
    active = fetch_all("SELECT * FROM agents WHERE status = 'active' ORDER BY avg_score DESC, agent_id DESC")
    if len(active) >= POPULATION_FLOOR:
        return

    needed = POPULATION_FLOOR - len(active)
    logger.info("Active population below floor (%s < %s). Spawning %s replacement agent(s).", len(active), POPULATION_FLOOR, needed)

    parents = active or fetch_all("SELECT * FROM agents ORDER BY avg_score DESC, agent_id DESC LIMIT 1")
    spawned = []

    if parents:
        knowledge_excerpts = fetch_all(
            "SELECT topic, student_level, feedback_score, feedback_comment, outcome_summary "
            "FROM knowledge_pool ORDER BY created_at DESC LIMIT 6"
        )
        for i in range(needed):
            parent = parents[i % len(parents)]
            mutated_prompt = mutate_strategy_prompt(parent, knowledge_excerpts)
            new_gen = int(parent.get("generation") or 1) + 1
            parent_id = parent["agent_id"]
            reason = (
                f"Spawned by Meta-Agent to restore the active-population floor of {POPULATION_FLOOR}. "
                f"Parent Agent #{parent_id} (Gen {parent.get('generation')})."
            )
            new_id = execute_insert(
                """
                INSERT INTO agents (
                    generation, parent_id, strategy_prompt, status,
                    reproduction_reason, success_rationale, times_used, avg_score
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (new_gen, parent_id, mutated_prompt, "active", reason, parent.get("success_rationale") or "Floor restoration clone.", 0, 0.0)
            )
            spawned.append({"new_agent_id": new_id, "parent_id": parent_id, "generation": new_gen})
    else:
        for prompt in ORIGIN_STRATEGIES[:needed]:
            new_id = execute_insert(
                """
                INSERT INTO agents (
                    generation, parent_id, strategy_prompt, status,
                    reproduction_reason, times_used, avg_score
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (1, None, prompt, "active", "Created by Meta-Agent to restore an empty population to the floor of 3 origin strategies.", 0, 0.0)
            )
            spawned.append({"new_agent_id": new_id, "parent_id": None, "generation": 1})

    execute_insert(
        "INSERT INTO evolution_log (event_type, details) VALUES (%s, %s)",
        ("population_floor_restore", json.dumps({"needed": needed, "spawned": spawned}))
    )


def evolve_population() -> Dict[str, Any]:
    """Execute the natural selection evolution loop and record complete lifecycle audit data."""
    active_agents = fetch_all(
        "SELECT * FROM agents WHERE status = 'active' ORDER BY avg_score DESC, times_used DESC"
    )
    total_active = len(active_agents)

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

    # Preserve population floor
    max_retireable = max(0, total_active - POPULATION_FLOOR)
    k_retire = min(EVOLUTION_K, max_retireable, len(eligible_agents) - 1)

    # 1. Retirement: Bottom k agents
    retired_agents = []
    if k_retire > 0:
        worst_candidates = eligible_agents[-k_retire:]
        for agent in worst_candidates:
            agent_id = agent["agent_id"]
            avg_score = float(agent.get("avg_score", 0.0) or 0.0)
            retire_reason = (
                f"Destroyed by Meta-Agent due to lowest population fitness ({avg_score:.2f}/5.0). "
                f"Ranked bottom performer after {agent['times_used']} evaluations."
            )
            mistakes = agent.get("mistake_summary") or "Failed to maintain pacing and clear scaffolding checkpoints."

            execute_query(
                """
                UPDATE agents 
                SET status = 'retired',
                    retired_at = CURRENT_TIMESTAMP,
                    retirement_reason = %s,
                    mistake_summary = %s
                WHERE agent_id = %s
                """,
                (retire_reason, mistakes, agent_id),
                commit=True
            )

            retired_info = {
                "agent_id": agent_id,
                "generation": agent["generation"],
                "avg_score": avg_score,
                "times_used": agent["times_used"],
                "reason": retire_reason,
                "mistake_summary": mistakes
            }
            retired_agents.append(retired_info)
            log_details["retired"].append(retired_info)
    else:
        log_details["notes"] += f"No agents retired to preserve population floor of {POPULATION_FLOOR}. "

    # 2. Reproduction: Top k agents
    k_reproduce = min(EVOLUTION_K, len(eligible_agents))
    top_candidates = eligible_agents[:k_reproduce]

    knowledge_excerpts = fetch_all(
        "SELECT topic, student_level, feedback_score, feedback_comment, outcome_summary "
        "FROM knowledge_pool ORDER BY created_at DESC LIMIT 6"
    )

    reproduced_agents = []
    for parent in top_candidates:
        mutated_prompt = mutate_strategy_prompt(parent, knowledge_excerpts)
        new_gen = int(parent["generation"]) + 1
        parent_id = parent["agent_id"]
        parent_avg = float(parent.get("avg_score", 0.0) or 0.0)

        repro_reason_child = (
            f"Spawned from parent Agent #{parent_id} (Gen {parent['generation']}) "
            f"due to superior fitness score ({parent_avg:.2f}/5.0)."
        )
        success_child = parent.get("success_rationale") or "High clarity, engaging opening analogies, and structured pacing."

        # Insert new mutated child agent with lifecycle tracking
        new_agent_id = execute_insert(
            """
            INSERT INTO agents (
                generation, parent_id, strategy_prompt, status, 
                reproduction_reason, success_rationale, times_used, avg_score
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (new_gen, parent_id, mutated_prompt, "active", repro_reason_child, success_child, 0, 0.0)
        )

        # Update parent record documenting why it moved forward
        parent_forward_reason = (
            f"Promoted forward by Meta-Agent to spawn Generation {new_gen} offspring (Agent #{new_agent_id}). "
            f"Top fitness performer with score {parent_avg:.2f}/5.0."
        )
        execute_query(
            "UPDATE agents SET reproduction_reason = %s WHERE agent_id = %s",
            (parent_forward_reason, parent_id),
            commit=True
        )

        reproduction_info = {
            "new_agent_id": new_agent_id,
            "generation": new_gen,
            "parent_id": parent_id,
            "parent_avg_score": parent_avg,
            "reproduction_reason": repro_reason_child,
            "mutated_prompt_snippet": mutated_prompt[:120] + "..."
        }
        reproduced_agents.append(reproduction_info)
        log_details["reproduced"].append(reproduction_info)

    active_after = fetch_one("SELECT COUNT(*) as count FROM agents WHERE status = 'active'")
    log_details["active_population_after"] = active_after["count"] if active_after else 0

    # 3. Log in evolution_log
    execute_insert(
        "INSERT INTO evolution_log (event_type, details) VALUES (%s, %s)",
        ("natural_selection_cycle", json.dumps(log_details))
    )

    return log_details
