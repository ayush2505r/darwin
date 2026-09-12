"""One-time classroom session materials and student checks."""

import json
import logging
import os
import re
import secrets
from html import escape
from html.parser import HTMLParser
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


def build_notes_html(session: Dict[str, Any]) -> str:
    """Build the complete student pack as a styled, self-contained HTML document."""
    notes = str(session.get("notes_markdown") or "")
    note_blocks = []
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("###"):
            note_blocks.append(f"<h3>{escape(line[3:].strip())}</h3>")
        elif line.startswith("##"):
            note_blocks.append(f"<h2>{escape(line[2:].strip())}</h2>")
        elif line.startswith("#"):
            note_blocks.append(f"<h1>{escape(line[1:].strip())}</h1>")
        elif line.startswith("-") or line.startswith("*"):
            note_blocks.append(f"<li>{escape(line[1:].strip())}</li>")
        else:
            note_blocks.append(f"<p>{escape(line)}</p>")

    flashcards = "".join(
        f'<article class="card"><div class="card-label">FLASHCARD {index}</div>'
        f'<h3>{escape(str(card.get("front", "")))}</h3>'
        f'<p class="answer"><strong>Answer:</strong> {escape(str(card.get("back", "")))}</p></article>'
        for index, card in enumerate(session.get("flashcards", []), start=1)
    )
    questions = "".join(
        f'<article class="question"><div class="card-label">QUESTION {index}</div>'
        f'<h3>{escape(str(question.get("question", "")))}</h3>'
        f'<ol type="A">{"".join(f"<li>{escape(str(option))}</li>" for option in question.get("options", []))}</ol>'
        f'<p class="hint"><strong>Why it matters:</strong> {escape(str(question.get("explanation", "")))}</p></article>'
        for index, question in enumerate(session.get("mcqs", []), start=1)
    )
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(str(session.get("topic", "Lesson")))}</title>
<style>
@page {{ size: Letter; margin: 0.65in 0.65in 0.7in; }}
* {{ box-sizing: border-box; }} body {{ font-family: Arial, sans-serif; color:#172033; line-height:1.45; margin:0; }}
.hero {{ background:#312e81; color:white; padding:24px 28px; border-radius:16px; margin-bottom:22px; }}
.eyebrow {{ color:#c4b5fd; text-transform:uppercase; letter-spacing:2px; font-size:9px; font-weight:bold; }}
h1 {{ color:#312e81; font-size:21px; margin:18px 0 8px; }} .hero h1 {{ color:white; font-size:27px; margin:7px 0; }}
h2 {{ color:#0f766e; font-size:17px; border-bottom:2px solid #99f6e4; padding-bottom:5px; margin-top:21px; }}
h3 {{ color:#25315b; font-size:12px; margin:5px 0 7px; }} p {{ font-size:10.5px; margin:6px 0 9px; }}
.meta {{ display:flex; gap:22px; color:#e0e7ff; font-size:10px; }} .section {{ margin-top:18px; }}
.card, .question {{ background:#f5f3ff; border:1px solid #ddd6fe; border-left:5px solid #8b5cf6; padding:11px 14px; margin:10px 0; border-radius:8px; }}
.question {{ background:#ecfeff; border-color:#a5f3fc; border-left-color:#0f766e; }} .card-label {{ color:#7c3aed; font-size:8px; font-weight:bold; letter-spacing:1px; }}
.answer {{ background:white; padding:7px 9px; border-radius:5px; }} .hint {{ color:#475569; font-size:9px; }} li {{ font-size:10px; margin:3px 0; }}
.footer {{ color:#64748b; font-size:8px; border-top:1px solid #cbd5e1; margin-top:25px; padding-top:7px; }}
</style></head><body>
<header class="hero"><div class="eyebrow">Darwin classroom study pack</div><h1>{escape(str(session.get("topic", "Lesson")))}</h1>
<div class="meta"><span>Session {escape(str(session.get("session_id", "")))}</span><span>Level: {escape(str(session.get("student_level", "")))}</span></div></header>
<main><section class="section"><h2>Lesson notes</h2>{''.join(note_blocks)}</section>
<section class="section"><h2>Review flashcards</h2>{flashcards or '<p>No flashcards were generated for this lesson.</p>'}</section>
<section class="section"><h2>Practice check</h2><p>Try each question before reading the explanation.</p>{questions or '<p>No practice questions were generated for this lesson.</p>'}</section></main>
<div class="footer">Generated for this classroom session. Use the practice check to explain your thinking, not just to choose an answer.</div>
</body></html>'''


class _NotesHTMLParser(HTMLParser):
    """Small HTML-to-reportlab adapter for the generated notes document."""

    def __init__(self):
        super().__init__()
        self.blocks = []
        self._tag = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "p", "li"}:
            self._tag, self._text = tag, []

    def handle_data(self, data):
        if self._tag:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == self._tag:
            text = " ".join("".join(self._text).split())
            if text:
                self.blocks.append((tag, text))
            self._tag, self._text = None, []


def _html_blocks(html: str):
    parser = _NotesHTMLParser()
    parser.feed(html)
    return parser.blocks


def create_notes_pdf(session: Dict[str, Any]) -> Path:
    """Render the complete generated HTML study pack into a student PDF."""
    output_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "output" / "pdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "output" / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / f"session_{session['session_id']}_notes.html"
    html_path.write_text(build_notes_html(session), encoding="utf-8")
    output_path = output_dir / f"session_{session['session_id']}_notes.pdf"

    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        logger.warning("ReportLab is unavailable; converting the generated HTML with the styled fallback.")
        _create_basic_pdf(output_path, html_path.read_text(encoding="utf-8"), session)
        return output_path

    styles = getSampleStyleSheet()
    title = ParagraphStyle("SessionTitle", parent=styles["Title"], textColor="#312e81", fontSize=25, leading=29, spaceAfter=8)
    h1 = ParagraphStyle("SessionH1", parent=styles["Heading1"], textColor="#312e81", fontSize=18, spaceBefore=16, spaceAfter=7)
    h2 = ParagraphStyle("SessionH2", parent=styles["Heading2"], textColor="#0f766e", fontSize=14, spaceBefore=14, spaceAfter=6)
    h3 = ParagraphStyle("SessionH3", parent=styles["Heading3"], textColor="#25315b", fontSize=10.5, leading=13, spaceBefore=4, spaceAfter=4)
    body = ParagraphStyle("SessionBody", parent=styles["BodyText"], fontSize=9.5, leading=13, spaceAfter=6)
    small = ParagraphStyle("SessionSmall", parent=body, fontSize=8.5, textColor="#475569")
    story = [Paragraph(escape(str(session.get("topic", "Lesson"))), title), Paragraph(f"Student study notes - Session {escape(str(session['session_id']))} - Level: {escape(str(session.get('student_level', '')))}", small), Spacer(1, 10)]
    for tag, text in _html_blocks(html_path.read_text(encoding="utf-8")):
        style = {"h1": h1, "h2": h2, "h3": h3, "p": body, "li": body}[tag]
        prefix = "• " if tag == "li" else ""
        story.append(Paragraph(escape(prefix + text), style))
    doc = SimpleDocTemplate(str(output_path), pagesize=LETTER, rightMargin=.7 * inch, leftMargin=.7 * inch, topMargin=.65 * inch, bottomMargin=.65 * inch)
    doc.build(story)
    return output_path


def _create_basic_pdf(output_path: Path, html: str, session: Dict[str, Any]) -> None:
    """Dependency-free fallback that converts visible HTML blocks into a styled PDF."""
    blocks = _html_blocks(html)
    page_count = max(1, (len(blocks) + 29) // 30)
    page_capacity = max(1, (len(blocks) + page_count - 1) // page_count)
    pages = [blocks[index:index + page_capacity] for index in range(0, len(blocks), page_capacity)] or [("h1", session["topic"])]

    objects: List[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_refs = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{page_refs}] /Count {len(pages)} >>".encode())
    for index, page_lines in enumerate(pages):
        page_object = 3 + index * 2
        stream_object = page_object + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {3 + len(pages) * 2} 0 R >> >> /Contents {stream_object} 0 R >>".encode())
        commands = ["BT", "50 750 Td"]
        for line_index, (tag, line) in enumerate(page_lines):
            size = {"h1": 18, "h2": 14, "h3": 11, "p": 10, "li": 10}.get(tag, 10)
            commands.append(f"/F1 {size} Tf")
            if tag in {"h1", "h2"}:
                commands.extend(["0.19 0.18 0.51 rg", "0 -3 Td", "0.19 0.18 0.51 rg"])
            else:
                commands.append("0.09 0.13 0.20 rg")
            safe_line = line.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:105]
            if line_index:
                commands.append(f"0 -{24 if tag in {'h1', 'h2'} else 17} Td")
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
