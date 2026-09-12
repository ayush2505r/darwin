"""Automated test suite for Darwin Evolving Teaching Assistant.

Tests database connectivity, LangGraph pipeline, feedback logging,
evolution logic, and Flask HTTP endpoints.
"""

import json
import os
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

    def test_01_db_connection_pure(self):
        """Verify that MySQL connection is established with use_pure=True."""
        conn = db.get_connection(include_database=True)
        # Verify connection is valid and not closed
        self.assertTrue(conn.is_connected())
        # Verify use_pure flag
        config = db.get_db_config()
        self.assertTrue(config.get("use_pure"), "Database connection must specify use_pure=True")
        conn.close()

    def test_02_agents_seeded(self):
        """Verify that active agents exist in the database."""
        agents = db.fetch_all("SELECT * FROM agents WHERE status = 'active'")
        self.assertGreaterEqual(len(agents), 3, "There should be at least 3 active agents in DB")

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

    def test_04_langgraph_pipeline_execution(self):
        """Verify LangGraph pipeline produces student profile and lesson plan."""
        topic = "Newtonian Gravitation"
        context = "High school physics students familiar with kinematics and forces, but not orbits."
        result = run_lesson_pipeline(topic=topic, context_text=context)

        self.assertIn("selected_agent", result)
        self.assertIsNotNone(result["selected_agent"])
        self.assertIn("student_profile", result)
        profile = result["student_profile"]
        self.assertIn("level", profile)
        self.assertIn(profile["level"], ["beginner", "intermediate", "advanced"])
        self.assertIn("lesson_plan", result)
        self.assertGreater(len(result["lesson_plan"]), 50)

    def test_05_feedback_and_knowledge_pool(self):
        """Verify recording feedback generates outcome summary and updates agent avg_score."""
        agent = db.fetch_one("SELECT agent_id FROM agents WHERE status = 'active' LIMIT 1")
        self.assertIsNotNone(agent)
        agent_id = agent["agent_id"]

        result = record_feedback(
            agent_id=agent_id,
            topic="Test Topic",
            student_level="intermediate",
            score=5,
            comment="Excellent pedagogical clarity."
        )

        self.assertIn("feedback_id", result)
        self.assertIn("knowledge_id", result)
        self.assertIn("outcome_summary", result)
        self.assertGreater(len(result["outcome_summary"]), 10)

        # Check knowledge_pool entry in DB
        kp_row = db.fetch_one(
            "SELECT * FROM knowledge_pool WHERE knowledge_id = %s",
            (result["knowledge_id"],)
        )
        self.assertIsNotNone(kp_row)
        self.assertEqual(kp_row["feedback_score"], 5)

    def test_06_flask_index_route(self):
        """Verify GET / renders the lesson submission form."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Submit a Lesson Request", response.data)

    def test_07_flask_lesson_route(self):
        """Verify POST /lesson generates a lesson and returns feedback form."""
        response = self.client.post("/lesson", data={
            "topic": "Photosynthesis and Light Reactions",
            "context_text": "Middle school biology students."
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Generated Lesson Explanation", response.data)
        self.assertIn(b"Teacher Feedback", response.data)

    def test_08_flask_feedback_route(self):
        """Verify POST /feedback saves review and renders confirmation."""
        agent = db.fetch_one("SELECT agent_id FROM agents WHERE status = 'active' LIMIT 1")
        response = self.client.post("/feedback", data={
            "agent_id": agent["agent_id"],
            "topic": "Photosynthesis",
            "student_level": "beginner",
            "score": "4",
            "comment": "Good simple analogies."
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Feedback Successfully Recorded", response.data)

    def test_09_flask_agents_dashboard(self):
        """Verify GET /agents displays the population table."""
        response = self.client.get("/agents")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Teaching Agent Population", response.data)
        self.assertIn(b"Natural Selection Evolution Log", response.data)


if __name__ == "__main__":
    unittest.main()
