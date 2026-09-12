"""Seed script to initialize Darwin database schema and populate initial teaching agent population."""

import sys
from db import init_db, fetch_one, execute_insert

INITIAL_AGENTS = [
    {
        "strategy_prompt": (
            "You are an inquiry-driven Socratic Teaching Assistant. Your strategy is to lead students to understanding "
            "through carefully sequenced questions, guided thought experiments, and interactive mental models. Rather "
            "than lecturing passively, you deconstruct complex concepts into accessible sub-questions, encouraging the "
            "learner to infer answers based on their prior knowledge before revealing formal explanations. Tailor your "
            "pacing and depth strictly to the assessed student level."
        )
    },
    {
        "strategy_prompt": (
            "You are a First-Principles Teaching Assistant. Your strategy is to deconstruct complex ideas down to their "
            "fundamental axioms and physical or logical truths, deliberately stripping away intimidating jargon at the outset. "
            "You anchor every abstract concept in vivid real-world analogies and intuitive everyday phenomena, and only build "
            "up to rigorous terminology once the core conceptual intuition is firmly established."
        )
    },
    {
        "strategy_prompt": (
            "You are a Pragmatic Scaffolding Teaching Assistant. Your strategy is to structure lesson explanations into clear, "
            "modular milestones: (1) The Big Picture in 60 seconds, (2) Step-by-Step Walkthrough with concrete walkthrough examples, "
            "(3) Common Pitfalls & Traps to Avoid, and (4) Self-Check Exercises. You provide clear signposts and checkpoints so "
            "students can verify their understanding at every stage of the explanation."
        )
    }
]


def seed_database(force: bool = False, reset: bool = False) -> None:
    print("Initializing database and tables...")
    try:
        init_db()
        print("Schema verified and initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")
        raise

    if reset:
        print("Reset requested. Clearing existing tables...")
        from db import get_db_cursor
        with get_db_cursor(commit=True, dictionary=False) as (cursor, conn):
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            cursor.execute("DELETE FROM evolution_log")
            cursor.execute("DELETE FROM feedback")
            cursor.execute("DELETE FROM knowledge_pool")
            cursor.execute("DELETE FROM agents")
            cursor.execute("DELETE FROM teachers")
            cursor.execute("ALTER TABLE evolution_log AUTO_INCREMENT = 1")
            cursor.execute("ALTER TABLE feedback AUTO_INCREMENT = 1")
            cursor.execute("ALTER TABLE knowledge_pool AUTO_INCREMENT = 1")
            cursor.execute("ALTER TABLE agents AUTO_INCREMENT = 1")
            cursor.execute("ALTER TABLE teachers AUTO_INCREMENT = 1")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        print("All tables cleared.")

    # Seed default teacher if not exists
    from db import fetch_one, create_teacher
    existing_teacher = fetch_one("SELECT teacher_id FROM teachers WHERE username = 'teacher'")
    if not existing_teacher:
        teacher_id = create_teacher(
            username="teacher",
            password="password123",
            full_name="Prof. Darwin (Default Teacher)"
        )
        print(f"  [+] Created default teacher account: 'teacher' / 'password123' (ID: #{teacher_id})")

    existing_count = fetch_one("SELECT COUNT(*) as count FROM agents WHERE status = 'active'")
    count = existing_count["count"] if existing_count else 0

    if count > 0 and not force and not reset:
        print(f"Database already contains {count} active agents. Skipping agent seed (use --reset to reseed).")
        return

    print(f"Seeding {len(INITIAL_AGENTS)} initial Generation-1 teaching agents...")
    for i, agent in enumerate(INITIAL_AGENTS, start=1):
        agent_id = execute_insert(
            """
            INSERT INTO agents (generation, parent_id, strategy_prompt, status, times_used, avg_score)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (1, None, agent["strategy_prompt"], "active", 0, 0.0)
        )
        print(f"  [+] Created Agent #{agent_id} (Generation 1)")

    print("Initial population and teacher account successfully seeded!")


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    force_flag = "--force" in sys.argv or reset_flag
    seed_database(force=force_flag, reset=reset_flag)

