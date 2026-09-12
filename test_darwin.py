"""Automated test suite for Darwin Evolving Teaching Assistant."""

import io
import os
import sys
import unittest
import uuid
from dotenv import load_dotenv

load_dotenv()

from app import app
import db
from pipeline import format_lesson_plan, run_lesson_pipeline
from evolution import record_feedback
from transcription import validate_file


class DarwinTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def _login_default_teacher(self):
        return self.client.post("/login", data={
            "username": "teacher",
            "password": "password123"
        }, follow_redirects=True)

    def test_01_db_connection_pure_and_terminal_message(self):
        """Verify MySQL uses use_pure=True and prints the connection notice."""
        db.reset_mysql_notice()
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
        """Verify teacher registration, login, session, and logout."""
        unique_user = f"testprof_{uuid.uuid4().hex[:6]}"
        test_pass = "SecurePass123!"
        full_name = "Dr. Test Professor"

        reg_resp = self.client.post("/register", data={
            "full_name": full_name,
            "username": unique_user,
            "password": test_pass
        }, follow_redirects=True)
        self.assertEqual(reg_resp.status_code, 200)

        teacher_rec = db.verify_teacher(unique_user, test_pass)
        self.assertIsNotNone(teacher_rec)
        self.assertEqual(teacher_rec["full_name"], full_name)

        self.client.get("/logout", follow_redirects=True)

        bad_login = self.client.post("/login", data={
            "username": unique_user,
            "password": "wrongpassword"
        })
        self.assertIn(b"Invalid username or password", bad_login.data)

        good_login = self.client.post("/login", data={
            "username": unique_user,
            "password": test_pass
        }, follow_redirects=True)
        self.assertIn(b"Welcome back", good_login.data)

        logout_resp = self.client.get("/logout", follow_redirects=True)
        self.assertIn(b"successfully logged out", logout_resp.data)

    def test_05_default_teacher_seeded(self):
        """Verify default teacher account exists and can authenticate."""
        teacher = db.verify_teacher("teacher", "password123")
        self.assertIsNotNone(teacher, "Default teacher account ('teacher' / 'password123') should exist")

    def test_06_langgraph_multi_agent_pipeline(self):
        """Verify LangGraph selects 3 agents, formats guides, and Meta-Agent scores them."""
        topic = "Newtonian Gravitation"
        context = "High school physics students familiar with kinematics and forces, but not orbits."
        result = run_lesson_pipeline(
            topic=topic,
            context_text=context,
            teacher_id=1,
            teacher_name="Prof. Ada"
        )

        self.assertIn("student_profile", result)
        profile = result["student_profile"]
        self.assertIn("level", profile)

        agent_plans = result["agent_plans"]
        self.assertGreaterEqual(len(agent_plans), 3, "Pipeline must generate proposals from at least 3 agents")
        self.assertIsNotNone(result.get("champion_plan"))

        for plan in agent_plans:
            self.assertIn("agent", plan)
            self.assertIn("lesson_plan", plan)
            self.assertIn("evaluation", plan)
            guide = plan["lesson_plan"]
            self.assertIn("Teacher's Step-by-Step Classroom Guide", guide)
            self.assertIn("Phase 1: Classroom Hook", guide)
            self.assertIn("Phase 3: Step-by-Step Teaching Script", guide)
            self.assertIn("Phase 4: Formative Comprehension Check", guide)
            self.assertIn("Prof. Ada", guide)

    def test_07_feedback_with_teacher_id(self):
        """Verify knowledge-pool recording still stores teacher_id for audit."""
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

        fb_row = db.fetch_one("SELECT * FROM feedback WHERE feedback_id = %s", (result["feedback_id"],))
        self.assertEqual(fb_row["teacher_id"], teacher_id)

    def test_08_flask_lesson_route_requires_login_and_shows_guides(self):
        """Unauthenticated /lesson redirects; logged-in teachers see Meta-Agent guides."""
        anon = self.client.post("/lesson", data={
            "topic": "Photosynthesis and Light Reactions",
            "context_text": "Middle school biology students."
        }, follow_redirects=False)
        self.assertEqual(anon.status_code, 302)

        self._login_default_teacher()
        response = self.client.post("/lesson", data={
            "topic": "Photosynthesis and Light Reactions",
            "context_text": "Middle school biology students."
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Teacher's Step-by-Step Classroom Guide", response.data)
        self.assertIn(b"Meta-Agent Autonomous Audit Scorecard", response.data)
        self.assertNotIn(b"Rate Pedagogical Quality", response.data)

    def test_09_flask_agents_dashboard(self):
        """Verify GET /agents displays the lifecycle ledger after login."""
        self.client.get("/logout")
        anon = self.client.get("/agents", follow_redirects=False)
        self.assertEqual(anon.status_code, 302)

        self._login_default_teacher()
        response = self.client.get("/agents")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Agent Lifecycle", response.data)
        self.assertIn(b"Natural Selection Evolution Log", response.data)

    def test_10_lesson_plan_formatter_strips_fences(self):
        raw = "```markdown\n### Guide\nHello\n```"
        cleaned = format_lesson_plan(raw, "Gravity")
        self.assertNotIn("```", cleaned)
        self.assertIn("Hello", cleaned)

    def test_11_resources_page_requires_login(self):
        """Teacher resource search is protected and renders without a query."""
        self.client.get("/logout")
        anon = self.client.get("/resources", follow_redirects=False)
        self.assertEqual(anon.status_code, 302)

        self._login_default_teacher()
        response = self.client.get("/resources")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Find Teaching Resources", response.data)


if __name__ == "__main__":
    unittest.main()
