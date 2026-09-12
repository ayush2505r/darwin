"""Feedback-to-Soup training integration.

Soup is intentionally invoked as a separate process because its training stack
includes PyTorch/Transformers and can require a GPU.  Feedback recording stays
fast and safe: the dataset/config are written first, a single run is reserved
in MySQL, and then ``soup train`` is launched without blocking the web request.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from db import execute_insert, execute_query, fetch_all, fetch_one

logger = logging.getLogger(__name__)

SOUP_FEEDBACK_THRESHOLD = int(os.getenv("SOUP_FEEDBACK_THRESHOLD", "1000"))
SOUP_ENABLED = os.getenv("SOUP_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
SOUP_BASE_MODEL = os.getenv("SOUP_BASE_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
SOUP_WORK_DIR = Path(os.getenv("SOUP_WORK_DIR", "training/soup"))
SOUP_CLI = os.getenv("SOUP_CLI", "soup")


def _work_dir() -> Path:
    path = SOUP_WORK_DIR
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _feedback_to_alpaca(row: Dict[str, Any]) -> Dict[str, str]:
    """Convert one Darwin feedback row into Soup's documented Alpaca format."""
    comment = (row.get("feedback_comment") or row.get("comment") or "").strip()
    outcome = (row.get("outcome_summary") or comment or "No outcome summary recorded.").strip()
    topic = (row.get("topic") or "the lesson topic").strip()
    level = (row.get("student_level") or "intermediate").strip()
    instruction = (
        "Analyze the classroom feedback and produce a concise, actionable teaching-quality "
        "assessment with one concrete improvement."
    )
    input_text = f"Topic: {topic}\nStudent level: {level}\nFeedback: {comment}"
    return {"instruction": instruction, "input": input_text, "output": outcome}


def export_feedback_dataset(rows: Iterable[Dict[str, Any]], output_path: Path) -> int:
    """Write feedback as JSONL and return the number of usable examples."""
    count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            item = _feedback_to_alpaca(row)
            if not item["output"].strip():
                continue
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_soup_config(dataset_path: Path, output_dir: Path, config_path: Path) -> None:
    """Write a strict Soup v0.75-compatible SFT config."""
    def yaml_quote(value: str) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    config = "\n".join([
        f"base: {yaml_quote(SOUP_BASE_MODEL)}",
        "task: sft",
        "data:",
        f"  train: {yaml_quote(str(dataset_path))}",
        "  format: alpaca",
        "  val_split: 0.1",
        "  max_length: 2048",
        "training:",
        "  epochs: 3",
        "  lr: 0.00002",
        "  batch_size: auto",
        "  lora:",
        "    r: 16",
        "    alpha: 32",
        "    target_modules: auto",
        "  quantization: 4bit",
        f"output: {yaml_quote(str(output_dir))}",
        "",
    ])
    config_path.write_text(config, encoding="utf-8")


def schedule_soup_training(total_feedback: int) -> Optional[Dict[str, Any]]:
    """Reserve and launch one Soup run at each 1,000-feedback boundary.

    The unique threshold row in ``soup_training_runs`` prevents duplicate
    processes when multiple feedback requests arrive concurrently.
    """
    if total_feedback <= 0 or total_feedback % SOUP_FEEDBACK_THRESHOLD != 0:
        return None

    existing = fetch_one(
        "SELECT run_id, status FROM soup_training_runs WHERE feedback_count = %s",
        (total_feedback,),
    )
    if existing:
        return {"run_id": existing["run_id"], "status": existing["status"], "duplicate": True}

    work_dir = _work_dir()
    dataset_path = work_dir / f"feedback-{total_feedback}.jsonl"
    config_path = work_dir / f"soup-{total_feedback}.yaml"
    output_dir = work_dir / f"model-{total_feedback}"
    rows = fetch_all(
        """
        SELECT f.topic, f.student_level, f.comment AS feedback_comment,
               k.outcome_summary
        FROM feedback f
        LEFT JOIN knowledge_pool k ON k.knowledge_id = (
            SELECT MAX(k2.knowledge_id) FROM knowledge_pool k2
            WHERE k2.agent_id = f.agent_id
              AND k2.topic = f.topic
              AND k2.student_level = f.student_level
        )
        ORDER BY f.feedback_id ASC
        """
    )
    example_count = export_feedback_dataset(rows, dataset_path)
    if example_count < 2:
        logger.warning("Soup training skipped: only %d usable feedback examples.", example_count)
        return None
    write_soup_config(dataset_path, output_dir, config_path)

    try:
        run_id = execute_insert(
            """
            INSERT INTO soup_training_runs
                (feedback_count, example_count, status, dataset_path, config_path, output_path)
            VALUES (%s, %s, 'queued', %s, %s, %s)
            """,
            (total_feedback, example_count, str(dataset_path), str(config_path), str(output_dir)),
        )
    except Exception:
        # A concurrent request may have won the unique reservation.
        existing = fetch_one(
            "SELECT run_id, status FROM soup_training_runs WHERE feedback_count = %s",
            (total_feedback,),
        )
        if existing:
            return {"run_id": existing["run_id"], "status": existing["status"], "duplicate": True}
        raise

    if not SOUP_ENABLED:
        execute_query(
            "UPDATE soup_training_runs SET status = 'disabled', finished_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (run_id,), commit=True,
        )
        return {"run_id": run_id, "status": "disabled"}

    soup_command = shutil.which(SOUP_CLI) if os.path.basename(SOUP_CLI) == SOUP_CLI else SOUP_CLI
    if not soup_command:
        message = f"Soup CLI '{SOUP_CLI}' was not found. Install Soup with: pip install -e 'vendor/Soup[train]'"
        execute_query(
            "UPDATE soup_training_runs SET status = 'failed', error_message = %s, finished_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (message, run_id), commit=True,
        )
        logger.error(message)
        return {"run_id": run_id, "status": "failed", "error": message}

    log_path = work_dir / f"run-{total_feedback}.log"
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [soup_command, "train", "--config", str(config_path)],
            cwd=str(work_dir), stdout=log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, close_fds=True,
        )
        log_handle.close()
        execute_query(
            "UPDATE soup_training_runs SET status = 'running', pid = %s, log_path = %s, started_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (process.pid, str(log_path), run_id), commit=True,
        )
        threading.Thread(
            target=_watch_training_process, args=(process, run_id), daemon=True,
        ).start()
        return {"run_id": run_id, "status": "running", "pid": process.pid}
    except Exception as exc:
        log_handle.close()
        execute_query(
            "UPDATE soup_training_runs SET status = 'failed', error_message = %s, finished_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (str(exc), run_id), commit=True,
        )
        return {"run_id": run_id, "status": "failed", "error": str(exc)}


def _watch_training_process(process: subprocess.Popen, run_id: int) -> None:
    """Persist completion/failure after the detached Soup process exits."""
    try:
        return_code = process.wait()
        status = "completed" if return_code == 0 else "failed"
        error = None if return_code == 0 else f"Soup exited with code {return_code}. See the run log."
        execute_query(
            "UPDATE soup_training_runs SET status = %s, error_message = %s, finished_at = CURRENT_TIMESTAMP WHERE run_id = %s",
            (status, error, run_id), commit=True,
        )
    except Exception:
        logger.exception("Could not update Soup training run %s status", run_id)


def list_soup_training_runs(limit: int = 10):
    return fetch_all(
        "SELECT * FROM soup_training_runs ORDER BY run_id DESC LIMIT %s", (limit,)
    )
