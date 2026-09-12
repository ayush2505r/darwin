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
from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from db import fetch_all, fetch_one
from pipeline import run_lesson_pipeline
from evolution import record_feedback, FEEDBACK_THRESHOLD
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


@app.route("/", methods=["GET"])
def index():
    """Form to submit a new lesson request with topic and optional context."""
    return render_template("index.html")


@app.route("/lesson", methods=["POST"])
def generate_lesson_route():
    """Run assessment + teaching agent LangGraph pipeline and show result."""
    topic = request.form.get("topic", "").strip()
    if not topic:
        flash("Please enter a lesson topic to proceed.", "error")
        return redirect(url_for("index"))

    context_text = request.form.get("context_text", "").strip() or None
    media_path = None
    original_filename = None

    # Handle optional media file upload
    if "media_file" in request.files:
        file = request.files["media_file"]
        if file and file.filename and file.filename.strip():
            filename = secure_filename(file.filename)
            is_valid, err_msg = validate_file(filename)
            if not is_valid:
                flash(err_msg, "error")
                return redirect(url_for("index"))

            # Save with unique prefix to avoid collision
            unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            saved_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(saved_path)
            media_path = saved_path
            original_filename = filename

    try:
        # Execute LangGraph pipeline
        result = run_lesson_pipeline(
            topic=topic,
            context_text=context_text,
            media_path=media_path,
            original_filename=original_filename
        )

        agent = result.get("selected_agent")
        student_profile = result.get("student_profile", {})
        lesson_plan = result.get("lesson_plan", "")
        collective_memory = result.get("collective_memory", [])

        if not agent:
            flash("No active teaching agents found. Please run seed.py to seed the database.", "error")
            return redirect(url_for("index"))

        return render_template(
            "lesson.html",
            topic=topic,
            student_profile=student_profile,
            agent=agent,
            collective_memory=collective_memory,
            lesson_plan=lesson_plan
        )
    except Exception as e:
        logger.exception("Error executing lesson pipeline")
        flash(f"An error occurred while generating the lesson plan: {e}", "error")
        return redirect(url_for("index"))


@app.route("/feedback", methods=["POST"])
def submit_feedback_route():
    """Record teacher rating and feedback, update collective memory, and check evolution."""
    try:
        agent_id = int(request.form.get("agent_id"))
        topic = request.form.get("topic", "").strip()
        student_level = request.form.get("student_level", "intermediate").strip()
        score = int(request.form.get("score", 3))
        comment = request.form.get("comment", "").strip() or None
        lesson_excerpt = request.form.get("lesson_excerpt", "").strip() or None

        # Validate score range (1 to 5)
        if score < 1 or score > 5:
            flash("Score must be between 1 and 5.", "error")
            return redirect(url_for("index"))

        # Process feedback and check natural selection loop
        result = record_feedback(
            agent_id=agent_id,
            topic=topic,
            student_level=student_level,
            score=score,
            comment=comment,
            lesson_excerpt=lesson_excerpt
        )

        return render_template(
            "feedback_result.html",
            agent_id=agent_id,
            score=score,
            comment=comment,
            result=result
        )

    except Exception as e:
        logger.exception("Error processing feedback submission")
        flash(f"Could not record feedback: {e}", "error")
        return redirect(url_for("index"))


@app.route("/agents", methods=["GET"])
def agents_dashboard():
    """Read-only dashboard displaying agent population, status, fitness, and evolution history."""
    try:
        agents = fetch_all(
            "SELECT * FROM agents ORDER BY generation ASC, agent_id ASC"
        )
        active_count = sum(1 for a in agents if a["status"] == "active")
        retired_count = sum(1 for a in agents if a["status"] == "retired")
        max_gen = max((a["generation"] for a in agents), default=1)

        raw_logs = fetch_all(
            "SELECT * FROM evolution_log ORDER BY created_at DESC LIMIT 20"
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
