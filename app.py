import asyncio
import builtins
import csv
import os
import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, jsonify, render_template, request, send_from_directory

from src.agent import run_agent
from src.config import DATA_DIR, LLM_PROVIDER
from src.llm import get_active_model

app = Flask(__name__)
os.makedirs(DATA_DIR, exist_ok=True)

# Global single-job lock because BROWSER_MANAGER and PAGE_CACHE in src/ are module-level singletons
_ACTIVE_JOB_LOCK = threading.Lock()
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_ABORT_SENTINEL = object()


def _safe_csv_filename(raw_name: str) -> str:
    """Sanitize filename to prevent directory traversal outside DATA_DIR."""
    base = os.path.basename((raw_name or "output.csv").strip()) or "output.csv"
    if not base.lower().endswith(".csv"):
        base += ".csv"
    return base


def _safe_csv_path(raw_name: str) -> str:
    filename = _safe_csv_filename(raw_name)
    full_path = os.path.realpath(os.path.join(DATA_DIR, filename))
    data_root = os.path.realpath(DATA_DIR)
    if not (full_path == data_root or full_path.startswith(data_root + os.sep)):
        raise ValueError("Invalid CSV filename.")
    return full_path


class _ThreadScopedStdout:
    """Capture stdout only from the active job thread while mirroring to the real console."""

    def __init__(self, target_thread_id: int, job: dict, original_stdout):
        self._tid = target_thread_id
        self._job = job
        self._orig = original_stdout
        self._buf = ""

    def write(self, data: str) -> int:
        if threading.get_ident() == self._tid:
            self._orig.write(data)
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    with _JOBS_LOCK:
                        self._job["logs"].append(line)
        else:
            self._orig.write(data)
        return len(data)

    def flush(self) -> None:
        if threading.get_ident() == self._tid and self._buf.strip():
            with _JOBS_LOCK:
                self._job["logs"].append(self._buf.strip())
            self._buf = ""
        self._orig.flush()


def _run_job_thread(job_id: str, url: str, user_goal: str, csv_filename: str, max_steps: int) -> None:
    job = _JOBS[job_id]
    tid = threading.get_ident()
    orig_stdout = sys.stdout
    orig_input = builtins.input
    input_queue: queue.Queue[object] = queue.Queue()
    cancel_event = threading.Event()

    def _web_input(prompt: str = "") -> str:
        if cancel_event.is_set():
            raise asyncio.CancelledError("Job aborted by user.")

        clean_prompt = (prompt or "Input required by agent:").strip()
        with _JOBS_LOCK:
            job["logs"].append(f"[INPUT REQUIRED] {clean_prompt}")
            job["pending_input"] = clean_prompt
            job["input_queue"] = input_queue

        reply = input_queue.get()  # Blocks until user submits or aborts

        with _JOBS_LOCK:
            job["pending_input"] = None
            job["input_queue"] = None

        if reply is _ABORT_SENTINEL or cancel_event.is_set():
            raise asyncio.CancelledError("Job aborted by user.")

        with _JOBS_LOCK:
            job["logs"].append("[INPUT RECEIVED] Resuming agent...")
        return str(reply)

    start_perf = time.perf_counter()
    loop = asyncio.new_event_loop()
    try:
        sys.stdout = _ThreadScopedStdout(tid, job, orig_stdout)
        builtins.input = _web_input
        asyncio.set_event_loop(loop)

        task = loop.create_task(
            run_agent(
                url=url,
                user_goal=user_goal,
                csv_filename=csv_filename,
                max_steps=max_steps,
            )
        )

        with _JOBS_LOCK:
            job["loop"] = loop
            job["task"] = task
            job["cancel_event"] = cancel_event
            if job.get("abort_requested"):
                cancel_event.set()
                task.cancel()

        result = loop.run_until_complete(task)
        elapsed = round(time.perf_counter() - start_perf, 2)
        with _JOBS_LOCK:
            job["status"] = "completed"
            job["result"] = result
            job["elapsed_seconds"] = elapsed
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    except asyncio.CancelledError:
        elapsed = round(time.perf_counter() - start_perf, 2)
        with _JOBS_LOCK:
            job["status"] = "aborted"
            job["error"] = "Execution aborted by user."
            job["elapsed_seconds"] = elapsed
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["logs"].append("[ABORTED] Execution stopped by user.")

    except Exception as exc:
        elapsed = round(time.perf_counter() - start_perf, 2)
        with _JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["elapsed_seconds"] = elapsed
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["logs"].append(f"[ERROR] {type(exc).__name__}: {exc}")

    finally:
        with _JOBS_LOCK:
            job["loop"] = None
            job["task"] = None
            job["input_queue"] = None
            job["pending_input"] = None
        try:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        sys.stdout.flush()
        sys.stdout = orig_stdout
        builtins.input = orig_input
        _ACTIVE_JOB_LOCK.release()


@app.route("/")
def index():
    return render_template(
        "index.html",
        llm_provider=LLM_PROVIDER.upper(),
        active_model=get_active_model(),
    )


@app.route("/api/start", methods=["POST"])
def start_scrape():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("\\")
    user_goal = (data.get("user_goal") or "").strip()
    csv_filename = _safe_csv_filename(data.get("csv_filename") or "output.csv")

    try:
        max_steps = max(1, min(int(data.get("max_steps") or 40), 100))
    except (TypeError, ValueError):
        max_steps = 40

    if not url:
        return jsonify({"error": "Target URL is required."}), 400
    if not user_goal:
        return jsonify({"error": "Extraction goal / prompt is required."}), 400

    if not _ACTIVE_JOB_LOCK.acquire(blocking=False):
        return jsonify({"error": "A scraping job is already running. Please wait for it to finish."}), 409

    job_id = uuid.uuid4().hex[:10]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "user_goal": user_goal,
            "csv_filename": csv_filename,
            "max_steps": max_steps,
            "status": "running",
            "abort_requested": False,
            "logs": [f"[Started] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Target: {url}"],
            "pending_input": None,
            "input_queue": None,
            "cancel_event": None,
            "loop": None,
            "task": None,
            "result": None,
            "error": None,
            "elapsed_seconds": None,
            "finished_at": None,
        }

    thread = threading.Thread(
        target=_run_job_thread,
        args=(job_id, url, user_goal, csv_filename, max_steps),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id, "csv_filename": csv_filename}), 202


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job_status(job_id: str):
    after = request.args.get("after", default=0, type=int) or 0
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        logs = job["logs"][after:]
        return jsonify({
            "id": job["id"],
            "status": job["status"],
            "abort_requested": job["abort_requested"],
            "csv_filename": job["csv_filename"],
            "logs": logs,
            "next_index": after + len(logs),
            "pending_input": job["pending_input"],
            "result": job["result"],
            "error": job["error"],
            "elapsed_seconds": job["elapsed_seconds"],
            "finished_at": job["finished_at"],
        })


@app.route("/api/jobs/<job_id>/abort", methods=["POST"])
def abort_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        if job["status"] != "running":
            return jsonify({"error": f"Job is already {job['status']}."}), 400
        if job.get("abort_requested"):
            return jsonify({"ok": True, "status": "aborting"}), 202

        job["abort_requested"] = True
        job["logs"].append("[ABORT REQUESTED] Stopping execution...")

        cancel_event = job.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()

        q = job.get("input_queue")
        if q is not None:
            q.put(_ABORT_SENTINEL)

        loop = job.get("loop")
        task = job.get("task")
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    return jsonify({"ok": True, "status": "aborting"}), 202


@app.route("/api/jobs/<job_id>/input", methods=["POST"])
def submit_job_input(job_id: str):
    data = request.get_json(silent=True) or {}
    value = str(data.get("value") or "")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        q = job.get("input_queue")
        if q is None:
            return jsonify({"error": "No input prompt is currently waiting."}), 400
        q.put(value)
    return jsonify({"ok": True})


@app.route("/api/files", methods=["GET"])
def list_csv_files():
    files = []
    for name in sorted(os.listdir(DATA_DIR)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(DATA_DIR, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        row_count = 0
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                row_count = sum(1 for _ in reader)
        except Exception:
            pass
        files.append({
            "name": name,
            "rows": row_count,
            "size_kb": round(stat.st_size / 1024, 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"files": files})


@app.route("/api/files/<path:filename>/preview", methods=["GET"])
def preview_csv(filename: str):
    limit = max(1, min(request.args.get("limit", default=100, type=int) or 100, 500))
    try:
        path = _safe_csv_path(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not os.path.isfile(path):
        return jsonify({"error": "CSV file not found."}), 404

    headers: list[str] = []
    rows: list[list[str]] = []
    total_rows = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        for row in reader:
            total_rows += 1
            if len(rows) < limit:
                rows.append(row)

    return jsonify({
        "name": os.path.basename(path),
        "headers": headers,
        "rows": rows,
        "total_rows": total_rows,
        "showing": len(rows),
    })


@app.route("/api/files/<path:filename>/download", methods=["GET"])
def download_csv(filename: str):
    try:
        safe_name = os.path.basename(_safe_csv_path(filename))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return send_from_directory(DATA_DIR, safe_name, as_attachment=True)


@app.route("/api/files/<path:filename>", methods=["DELETE"])
def delete_csv(filename: str):
    try:
        path = _safe_csv_path(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "CSV file not found."}), 404
    os.remove(path)
    return jsonify({"deleted": os.path.basename(path)})


if __name__ == "__main__":
    # use_reloader=False prevents Werkzeug from spawning duplicate Playwright processes
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)