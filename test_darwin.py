"""Automated test suite for Darwin Evolving Teaching Assistant.

Tests:
1. MySQL connectivity with use_pure=True and terminal notice 'connected to mysql you can proceed'.
2. Active agents maintained in database.
3. Media file validation and transcription stubbing.
4. LangGraph 3-agent parallel generation producing actionable Teacher Classroom Guides.
5. Teacher registration, login, logout, and session handling.
6. Feedback recording with teacher_id and collective memory storage.
7. Web routes (/, /lesson, /feedback, /agents, /login, /register, /logout).
"""

import io
import json
import os
import sys
import unittest
from dotenv import load_dotenv

# Ensure environment is loaded
load_dotenv()

from app import app
import db
from pipeline import run_lesson_pipeline
from evolution import record_feedback, evolve_population
from transcription import validate_file, extract_or_transcribe


class DarwinTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        cls.client = app.test_client()

    def test_01_db_connection_pure_and_terminal_message(self):
        """Verify that MySQL connection is pure and prints 'connected to mysql you can proceed'."""
        captured_out = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_out
            conn = db.get_connection(include_database=True)
            self.assertTrue(conn.is_connected())
            config = db.get_db_config()
            self.assertTrue(config.get("use_pure"), "Database connection must specify use_pure=True")
            conn.close()
        finally:
            sys.stdout = old_stdout

        output_text = captured_out.getvalue()
        self.assertIn("connected to mysql you can proceed", output_text)

    def test_02_agents_seeded_minimum_three(self):
        """Verify that at least 3 active agents are maintained in the database."""
        agents = db.fetch_all("SELECT * FROM agents WHERE status = 'active'")
        self.assertGreaterEqual(len(agents), 3, "There must be at least 3 active agents maintained in DB")

    def test_03_transcription_validation(self):
        """Verify file validation rejects unsupported types and sizes."""
        valid, msg = validate_file("recording.mp4")
        self.assertTrue(valid)

        invalid_ext, msg = validate_file("malicious.exe")
        self.assertFalse(invalid_ext)
        self.assertIn("Unsupported file type", msg)

        too_large, msg = validate_file("large.mp4", file_size=50 * 1024 * 1024)
        self.assertFalse(too_large)
        self.assertIn("exceeds maximum", msg)

    def test_04_teacher_auth_flow(self):
        """Verify teacher registration, login with verification, session, and logout."""
        import uuid
        unique_user = f"testprof_{uuid.uuid4().hex[:6]}"
        test_pass = "SecurePass123!"
        full_name = "Dr. Test Professor"

        # 1. Register new teacher
        reg_resp = self.client.post("/register", data={
            "full_name": full_name,
            "username": unique_user,
            "password": test_pass
        }, follow_redirects=True)
        self.assertEqual(reg_resp.status_code, 200)

        # 2. Verify in database
        teacher_rec = db.verify_teacher(unique_user, test_pass)
        self.assertIsNotNone(teacher_rec)
        self.assertEqual(teacher_rec["full_name"], full_name)

        # 3. Invalid login check
        bad_login = self.client.post("/login", data={
            "username": unique_user,
            "password": "wrongpassword"
        })
        self.assertIn(b"Invalid username or password", bad_login.data)

        # 4. Valid login check
        good_login = self.client.post("/login", data={
            "username": unique_user,
            "password": test_pass
        }, follow_redirects=True)
        self.assertIn(b"Welcome back", good_login.data)

        # 5. Logout
        logout_resp = self.client.get("/logout", follow_redirects=True)
        self.assertIn(b"successfully logged out", logout_resp.data)

    def test_05_default_teacher_seeded(self):
        """Verify default teacher account exists and can authenticate."""
        teacher = db.verify_teacher("teacher", "password123")
        self.assertIsNotNone(teacher, "Default teacher account ('teacher' / 'password123') should exist")

    def test_06_langgraph_multi_agent_pipeline(self):
        """Verify LangGraph pipeline selects 3 agents and generates actionable Teacher Guides."""
        topic = "Newtonian Gravitation"
        context = "High school physics students familiar with kinematics and forces, but not orbits."
        result = run_lesson_pipeline(topic=topic, context_text=context)

        self.assertIn("student_profile", result)
        profile = result["student_profile"]
        self.assertIn("level", profile)

        self.assertIn("agent_plans", result)
        agent_plans = result["agent_plans"]
        self.assertGreaterEqual(len(agent_plans), 3, "Pipeline must generate proposals from at least 3 agents")

        # Verify each agent plan is an actionable Teacher Guide
        for plan in agent_plans:
            self.assertIn("agent", plan)
            self.assertIn("lesson_plan", plan)
            guide = plan["lesson_plan"]
            self.assertIn("Teacher's Step-by-Step Classroom Guide", guide)
            self.assertIn("Phase 1: Classroom Hook", guide)
            self.assertIn("Phase 3: Step-by-Step Teaching Script", guide)
            self.assertIn("Phase 4: Formative Comprehension Check", guide)

    def test_07_feedback_with_teacher_id(self):
        """Verify recording feedback with teacher_id updates collective memory and agent fitness."""
        agent = db.fetch_one("SELECT agent_id FROM agents WHERE status = 'active' LIMIT 1")
        self.assertIsNotNone(agent)
        agent_id = agent["agent_id"]

        teacher = db.fetch_one("SELECT teacher_id FROM teachers LIMIT 1")
        teacher_id = teacher["teacher_id"] if teacher else None

        result = record_feedback(
            agent_id=agent_id,
            topic="Test Gravitation",
            student_level="intermediate",
            score=5,
            comment="The classroom hook and worked example were directly usable.",
            teacher_id=teacher_id
        )

        self.assertIn("feedback_id", result)
        self.assertIn("knowledge_id", result)
        self.assertIn("outcome_summary", result)

        # Check teacher_id in feedback table
        fb_row = db.fetch_one("SELECT * FROM feedback WHERE feedback_id = %s", (result["feedback_id"],))
        self.assertEqual(fb_row["teacher_id"], teacher_id)

    def test_08_flask_lesson_route_displays_all_3_agents(self):
        """Verify POST /lesson presents comparison tabs for all 3 agents."""
        response = self.client.post("/lesson", data={
            "topic": "Photosynthesis and Light Reactions",
            "context_text": "Middle school biology students."
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Compare Teaching Agent Proposals", response.data)
        self.assertIn(b"Actionable Teacher", response.data)
        self.assertIn(b"Rate Pedagogical Quality", response.data)

    def test_09_flask_agents_dashboard(self):
        """Verify GET /agents displays the population dashboard."""
        response = self.client.get("/agents")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Teaching Agent Population", response.data)
        self.assertIn(b"Natural Selection Evolution Log", response.data)


if __name__ == "__main__":
    unittest.main()
