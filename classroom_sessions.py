"""One-time classroom session materials and student checks."""

import json
import logging
import os
import re
import secrets
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import execute_insert, fetch_all, fetch_one
from llm import call_llm

logger = logging.getLogger(__name__)


def _new_session_id() -> str:
    return "DRW-" + secrets.token_hex(4).upper()


def _fallback_materials(topic: str, level: str) -> Dict[str, Any]:
    return {
        "flashcards": [
            {"front": f"What is the central idea of {topic}?", "back": f"Explain {topic} in one clear sentence and give one example."},
            {"front": f"What is a common misunderstanding about {topic}?", "back": "Connect the definition to the mechanism and check the conditions where it applies."},
            {"front": f"How should a {level} student use this idea?", "back": "Start with the simplest example, explain each step, and verify the result."},
        ],
        "mcqs": [
            {"question": f"Which approach best demonstrates understanding of {topic}?", "options": ["Memorizing a definition only", "Explaining the idea and applying it to a new example", "Copying an answer", "Skipping the example"], "answer": 1, "explanation": "Applying the idea to a new example demonstrates transfer."},
            {"question": "What should a student do when the first attempt is incorrect?", "options": ["Stop immediately", "Guess without checking", "Identify the step that failed and revise it", "Ignore the result"], "answer": 2, "explanation": "Finding the failed step makes the mistake useful for learning."},
        ],
        "notes": f"# {topic}\n\n## Key idea\nUnderstand the definition, mechanism, and one worked example.\n\n## Study checklist\n- Explain the central idea in your own words.\n- Solve one familiar example.\n- Try a new example and explain each step.\n- Check the result and describe any remaining question.\n",
    }


def _generate_materials(topic: str, level: str, lesson_plan: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = (
        "You create a compact student follow-up pack from a teacher's lesson. "
        "Return ONLY valid JSON with this exact shape: "
        "{\"flashcards\":[{\"front\":\"...\",\"back\":\"...\"}],"
        "\"mcqs\":[{\"question\":\"...\",\"options\":[\"...\",\"...\",\"...\",\"...\"],"
        "\"answer\":0,\"explanation\":\"...\"}],\"notes\":\"markdown...\"}. "
        "Create 5 flashcards and 5 multiple-choice questions. The answer is a zero-based option index. "
        "Questions must test understanding and application, not trivia. Notes should be clear, student-friendly Markdown."
    )
    user_prompt = (
        f"Topic: {topic}\nStudent level: {level}\nStudent profile: {json.dumps(profile, ensure_ascii=False)}\n"
        f"Teacher lesson:\n{lesson_plan[:10000]}"
    )
    raw = call_llm(system_prompt, user_prompt, temperature=0.25, max_tokens_hint="session_material")
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict) or not data.get("flashcards") or not data.get("mcqs") or not data.get("notes"):
        raise ValueError("Session material JSON is incomplete")
    return data


def create_classroom_session(
    teacher_id: int,
    topic: str,
    student_level: str,
    lesson_plan: str,
    student_profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate and persist one immutable student pack for a teacher lesson."""
    materials = _fallback_materials(topic, student_level)
    try:
        materials = _generate_materials(topic, student_level, lesson_plan, student_profile)
    except Exception as exc:
        logger.warning("Student pack generation failed; using deterministic pack: %s", exc)

    session_id = _new_session_id()
    execute_insert(
        """
        INSERT INTO classroom_sessions
            (session_id, teacher_id, topic, student_level, materials_json, notes_markdown)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (session_id, teacher_id, topic, student_level, json.dumps(materials, ensure_ascii=False), materials["notes"]),
    )
    return {"session_id": session_id, "topic": topic, "student_level": student_level, **materials}


def get_classroom_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    row = fetch_one("SELECT * FROM classroom_sessions WHERE session_id = %s", (session_id.strip().upper(),))
    if not row:
        return None
    try:
        materials = json.loads(row.get("materials_json") or "{}")
    except json.JSONDecodeError:
        materials = {}
    return {**row, **materials, "session_id": row["session_id"]}


def record_student_attempt(session_id: str, student_name: str, answers: List[int]) -> Dict[str, Any]:
    session = get_classroom_session(session_id)
    if not session:
        raise ValueError("That session ID was not found or has expired.")
    questions = session.get("mcqs", [])
    correct = sum(1 for index, question in enumerate(questions) if index < len(answers) and answers[index] == int(question.get("answer", -1)))
    total = len(questions)
    attempt_id = execute_insert(
        """
        INSERT INTO student_attempts (session_id, student_name, answers_json, score, total_questions)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (session_id.upper(), student_name[:120] or "Anonymous", json.dumps(answers), correct, total),
    )
    return {"attempt_id": attempt_id, "score": correct, "total": total, "percentage": round(correct / total * 100) if total else 0}


def get_session_attempts(session_id: str) -> List[Dict[str, Any]]:
    return fetch_all(
        """
        SELECT attempt_id, student_name, score, total_questions, submitted_at
        FROM student_attempts
        WHERE session_id = %s
        ORDER BY submitted_at DESC
        """,
        (session_id.upper(),),
    )


def create_notes_pdf(session: Dict[str, Any]) -> Path:
    """Create or reuse a stable PDF of the stored notes."""
    output_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "output" / "pdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"session_{session['session_id']}_notes.pdf"
    if output_path.exists():
        return output_path

    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        logger.warning("ReportLab is unavailable; creating a basic compatible PDF fallback.")
        _create_basic_pdf(output_path, session)
        return output_path

    styles = getSampleStyleSheet()
    title = ParagraphStyle("SessionTitle", parent=styles["Title"], textColor="#4338ca", spaceAfter=16)
    heading = ParagraphStyle("SessionHeading", parent=styles["Heading2"], textColor="#0f766e", spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("SessionBody", parent=styles["BodyText"], leading=15, spaceAfter=7)
    story = [Paragraph(escape(session["topic"]), title), Paragraph(f"Student study notes · Session {escape(session['session_id'])}", body)]
    for line in str(session.get("notes_markdown") or "").splitlines():
        clean = line.strip()
        if not clean:
            story.append(Spacer(1, 6))
        elif clean.startswith("#"):
            story.append(Paragraph(escape(clean.lstrip("# ")), heading))
        else:
            story.append(Paragraph(escape(clean.lstrip("- ")), body))
    doc = SimpleDocTemplate(str(output_path), pagesize=LETTER, rightMargin=.7 * inch, leftMargin=.7 * inch, topMargin=.65 * inch, bottomMargin=.65 * inch)
    doc.build(story)
    return output_path


def _create_basic_pdf(output_path: Path, session: Dict[str, Any]) -> None:
    """Dependency-free fallback so PDF download still works offline."""
    lines = [
        session["topic"],
        f"Student study notes - Session {session['session_id']}",
        "",
    ] + [line.lstrip("#- ") for line in str(session.get("notes_markdown") or "").splitlines()]
    pages = [lines[index:index + 42] for index in range(0, len(lines), 42)] or [[session["topic"]]]

    objects: List[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_refs = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{page_refs}] /Count {len(pages)} >>".encode())
    for index, page_lines in enumerate(pages):
        page_object = 3 + index * 2
        stream_object = page_object + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {3 + len(pages) * 2} 0 R >> >> /Contents {stream_object} 0 R >>".encode())
        commands = ["BT", "/F1 16 Tf", "50 750 Td"]
        for line_index, line in enumerate(page_lines):
            safe_line = line.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:105]
            if line_index:
                commands.append("0 -17 Td")
            commands.append(f"({safe_line}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    output_path.write_bytes(pdf)
