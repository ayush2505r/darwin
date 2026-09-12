'''
you are the ai engineer of the world and what you have to do is that you have to build an ai agent with given specifications 

problem statement : as we know ai cannot evolve on its own this is the biggest problem with ai why not to fix this problem
                you have to build an ai agent that takes some a video or audio or text as a input and what it does is that it takes that video and feeds that to agents now agents will make some assumptions
                now they will be provided a topic like 'what is quantum computing' now the ai had made some assumptions like students are at low level and we need to teach them from skretch or they are at good level and we should not teach them the basics that will be just time waste and this is it based on that video the ai will give best possible way to teach the students 
                now here comes the main part the uesr will come back that is a teacher and now he/she will give a kind of feed back and based on that feed back the main agent that is a meta agent will destroy some agents that does not perform well and multiply those agents that does the best job this process will go on and  the agents will kind of evolve because there is the process what we call natural selection and this is the whole thing

tech stack : you have to use a common mysql database so that everything can be kept on one place and every agent can see what was the mistake that the last agent made that lead him to the destruction and this will be a common knowledge pool that will tell the agents what mistakes the previous models did and what the model that are good are doing this will tell them what they can improve and what not to do 
            use mysql connector with setting use pure to true 
            use falsk for website 
            use langchain and langgraph for agent creation and you will be provided with groq api key you have to use that and what model to use will be provided soon 

things not to do : do not overcomplecate the project keep the ui simple and clean no 1000 things at one time 
                   do not try to add things from your side and mind all those things that are needed and nothing else
                    
note for llms :keep this prompt do not delete it 



'''

import json
import logging
import os
import uuid
from datetime import datetime
from functools import wraps
from flask import Flask, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import markdown as md_module

from db import fetch_all, fetch_one, create_teacher, verify_teacher, init_db
from pipeline import run_lesson_pipeline
from evolution import FEEDBACK_THRESHOLD
from transcription import validate_file

# Load environment configuration
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "darwin-default-secret-key-2026")

# Directory for uploaded media files
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB max request payload


# ---------------------------------------------------------------------------
# Jinja Markdown Filter (Renders Markdown to Beautiful Styled HTML)
# ---------------------------------------------------------------------------
@app.template_filter("markdown")
def render_markdown(text: str) -> Markup:
    """Render markdown strings as safe, formatted HTML."""
    if not text:
        return Markup("")
    html = md_module.markdown(
        text,
        extensions=["extra", "nl2br", "sane_lists"]
    )
    return Markup(html)


def login_required(view_fn):
    """Require a logged-in teacher for protected pages."""
    @wraps(view_fn)
    def wrapped(*args, **kwargs):
        if not session.get("teacher_id"):
            flash("Please log in to continue.", "info")
            return redirect(url_for("login_route"))
        return view_fn(*args, **kwargs)
    return wrapped


@app.template_filter("fmt_dt")
def format_datetime(value) -> str:
    """Render timestamps in a compact, readable form."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


# ---------------------------------------------------------------------------
# Terminal Database Connection Check & Migration
# ---------------------------------------------------------------------------
def verify_mysql_connection():
    """Verify MySQL connectivity, run any pending migrations, and print terminal message."""
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to verify/migrate MySQL database: {e}")


# Run check on startup
verify_mysql_connection()


# ---------------------------------------------------------------------------
# Authentication Routes (Teacher Login, Register, Logout)
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_route():
    """Teacher login endpoint."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please provide both username and password.", "error")
            return render_template("login.html")

        teacher = verify_teacher(username, password)
        if teacher:
            session["teacher_id"] = teacher["teacher_id"]
            session["teacher_name"] = teacher["full_name"]
            session["username"] = teacher["username"]
            flash(f"Welcome back, {teacher['full_name']}!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid username or password. Please try again.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register_route():
    """Teacher registration endpoint."""
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not full_name or not username or not password:
            flash("All fields are required.", "error")
            return render_template("register.html")

        existing = fetch_one("SELECT teacher_id FROM teachers WHERE LOWER(username) = LOWER(%s)", (username,))
        if existing:
            flash(f"Username '{username}' is already taken. Please choose another.", "error")
            return render_template("register.html")

        try:
            teacher_id = create_teacher(username=username, password=password, full_name=full_name)
            session["teacher_id"] = teacher_id
            session["teacher_name"] = full_name
            session["username"] = username
            flash("Account registered successfully! You are now logged in.", "success")
            return redirect(url_for("index"))
        except Exception as e:
            flash(f"Registration failed: {e}", "error")

    return render_template("register.html")


@app.route("/logout", methods=["GET"])
def logout_route():
    """Log out teacher."""
    session.clear()
    flash("You have been successfully logged out.", "info")
    return redirect(url_for("login_route"))


# ---------------------------------------------------------------------------
# Core Lesson & Feedback Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
@login_required
def index():
    """Form to submit a new lesson request with topic and optional context."""
    return render_template("index.html")


@app.route("/lesson", methods=["POST"])
@login_required
def generate_lesson_route():
    """Run assessment + 3-agent generation + autonomous Meta-Agent evaluation."""
    topic = request.form.get("topic", "").strip()
    if not topic:
        flash("Please enter a lesson topic to proceed.", "error")
        return redirect(url_for("index"))

    context_text = request.form.get("context_text", "").strip() or None
    youtube_url = request.form.get("youtube_url", "").strip() or None
    if youtube_url:
        from transcription import extract_youtube_video_id
        if not extract_youtube_video_id(youtube_url):
            flash("Please enter a valid YouTube video link.", "error")
            return redirect(url_for("index"))
    media_path = None
    original_filename = None

    # Handle optional media file upload
    if "media_file" in request.files:
        file = request.files["media_file"]
        if file and file.filename and file.filename.strip():
            filename = secure_filename(file.filename)
            file.seek(0, os.SEEK_END)
            upload_size = file.tell()
            file.seek(0)
            is_valid, err_msg = validate_file(filename, file_size=upload_size)
            if not is_valid:
                flash(err_msg, "error")
                return redirect(url_for("index"))

            unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            saved_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(saved_path)
            media_path = saved_path
            original_filename = filename

    try:
        teacher_id = session.get("teacher_id")
        teacher_name = session.get("teacher_name")

        result = run_lesson_pipeline(
            topic=topic,
            context_text=context_text,
            media_path=media_path,
            original_filename=original_filename,
            youtube_url=youtube_url,
            teacher_id=teacher_id,
            teacher_name=teacher_name
        )

        student_profile = result.get("student_profile", {})
        agent_plans = result.get("agent_plans", [])
        champion_plan = result.get("champion_plan") or (agent_plans[0] if agent_plans else None)

        if not agent_plans:
            flash("No active teaching agents found. Please run seed.py to seed the database.", "error")
            return redirect(url_for("index"))

        return render_template(
            "lesson.html",
            topic=topic,
            student_profile=student_profile,
            agent_plans=agent_plans,
            champion_plan=champion_plan,
            evolution_triggered=result.get("evolution_triggered", False),
            evolution_details=result.get("evolution_details")
        )
    except Exception as e:
        logger.exception("Error executing lesson pipeline")
        flash(f"An error occurred while generating the lesson plan: {e}", "error")
        return redirect(url_for("index"))


@app.route("/agents", methods=["GET"])
@login_required
def agents_dashboard():
    """Dashboard displaying agent population, status, fitness, and lifecycle audit ledger."""
    try:
        agents = fetch_all(
            """
            SELECT 
                agent_id, generation, parent_id, strategy_prompt, status, 
                created_at, retired_at, retirement_reason, mistake_summary, 
                reproduction_reason, success_rationale, times_used, avg_score 
            FROM agents 
            ORDER BY generation DESC, avg_score DESC, agent_id DESC
            """
        )
        active_count = sum(1 for a in agents if a["status"] == "active")
        retired_count = sum(1 for a in agents if a["status"] == "retired")
        max_gen = max((a["generation"] for a in agents), default=1)

        raw_logs = fetch_all(
            "SELECT * FROM evolution_log ORDER BY created_at DESC LIMIT 25"
        )
        logs = []
        for log in raw_logs:
            parsed = None
            try:
                parsed = json.loads(log["details"])
            except Exception:
                pass
            logs.append({
                "log_id": log["log_id"],
                "event_type": log["event_type"],
                "details": log["details"],
                "parsed_details": parsed,
                "created_at": log["created_at"]
            })

        return render_template(
            "agents.html",
            agents=agents,
            active_count=active_count,
            retired_count=retired_count,
            max_generation=max_gen,
            logs=logs,
            feedback_threshold=FEEDBACK_THRESHOLD
        )
    except Exception as e:
        logger.exception("Error loading agent dashboard")
        flash(f"Could not load agents dashboard: {e}", "error")
        return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
