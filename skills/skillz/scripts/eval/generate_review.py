#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""Generate and serve a review page for eval results.

Reads an iteration directory, discovers runs (directories with outputs/),
embeds all output data into one HTML page, and serves it on 127.0.0.1.
Feedback auto-saves to feedback.json in the iteration directory. If the
requested port is busy the server binds a free port instead; it never
touches processes it did not start.

Usage:
    uv run generate_review.py <iteration-dir> --skill-name NAME --benchmark benchmark.json
    uv run generate_review.py <iteration-dir> --previous-workspace <iteration-dir-N-1>
    uv run generate_review.py <iteration-dir> --static review.html
"""

import base64
import json
import mimetypes
import re
import sys
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import click

VIEWER_TEMPLATE = Path(__file__).resolve().parents[2] / "assets" / "viewer.html"

# Role order: with_skill first, because the viewer colours configurations by first appearance.
CONFIG_ORDER = {"with_skill": 0, "without_skill": 1}

# Files to exclude from output listings
METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

# Extensions we render as inline text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

# Extensions we render as inline images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# MIME type overrides for common types
MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def find_runs(workspace: Path) -> list[dict]:
    """Recursively find directories that contain an outputs/ subdirectory."""
    runs: list[dict] = []
    _find_runs_recursive(workspace, workspace, runs)
    runs.sort(key=_run_sort_key)
    return runs


def _run_sort_key(run: dict) -> tuple:
    eval_id = run.get("eval_id")
    return (
        eval_id if isinstance(eval_id, int) else float("inf"),
        CONFIG_ORDER.get(run.get("configuration"), len(CONFIG_ORDER)),
        run.get("run_number") if isinstance(run.get("run_number"), int) else float("inf"),
        run["id"],
    )


def _find_runs_recursive(root: Path, current: Path, runs: list[dict]) -> None:
    if not current.is_dir():
        return

    outputs_dir = current / "outputs"
    if outputs_dir.is_dir():
        run = build_run(root, current)
        if run:
            runs.append(run)
        return

    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    for child in sorted(current.iterdir()):
        if child.is_dir() and child.name not in skip:
            _find_runs_recursive(root, child, runs)


def build_run(root: Path, run_dir: Path) -> dict | None:
    """Build a run dict with prompt, outputs, and grading data."""
    prompt = ""
    eval_id = None
    eval_name = None
    baseline_label = None

    # eval_metadata.json lives at the eval directory (two levels above the run
    # directory); search upward from the run directory to the workspace root.
    for directory in [run_dir, *run_dir.parents]:
        candidate = directory / "eval_metadata.json"
        if candidate.is_file():
            try:
                metadata = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                metadata = None
            if isinstance(metadata, dict):
                prompt = metadata.get("prompt") or ""
                eval_id = metadata.get("eval_id")
                eval_name = metadata.get("eval_name")
                baseline_label = metadata.get("baseline_label")
                break
        if directory == root:
            break

    # Fall back to transcript.md
    if not prompt:
        for candidate in [run_dir / "transcript.md", run_dir / "outputs" / "transcript.md"]:
            if candidate.exists():
                try:
                    text = candidate.read_text()
                    match = re.search(r"## Eval Prompt\n\n([\s\S]*?)(?=\n##|$)", text)
                    if match:
                        prompt = match.group(1).strip()
                except OSError:
                    pass
                if prompt:
                    break

    if not prompt:
        prompt = "(No prompt found)"

    run_id = str(run_dir.relative_to(root)).replace("/", "-").replace("\\", "-")

    # Collect output files
    outputs_dir = run_dir / "outputs"
    output_files: list[dict] = []
    if outputs_dir.is_dir():
        for f in sorted(outputs_dir.iterdir()):
            if f.is_file() and f.name not in METADATA_FILES:
                output_files.append(embed_file(f))

    # Load grading if present
    grading = None
    grading_path = run_dir / "grading.json"
    if grading_path.is_file():
        try:
            grading = json.loads(grading_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    run_number_match = re.fullmatch(r"run-(\d+)", run_dir.name)
    configuration = run_dir.parent.name if run_dir.parent != root else None

    return {
        "id": run_id,
        "prompt": prompt,
        "eval_id": eval_id,
        "eval_name": eval_name,
        "baseline_label": baseline_label,
        "configuration": configuration,
        "run_number": int(run_number_match.group(1)) if run_number_match else None,
        "outputs": output_files,
        "grading": grading,
    }


def embed_file(path: Path) -> dict:
    """Read a file and return an embedded representation."""
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(errors="replace")
        except OSError:
            content = "(Error reading file)"
        return {
            "name": path.name,
            "type": "text",
            "content": content,
        }
    elif ext in IMAGE_EXTENSIONS:
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "image",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".pdf":
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "pdf",
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".xlsx":
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "xlsx",
            "data_b64": b64,
        }
    else:
        # Binary / unknown — base64 download link
        try:
            raw = path.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError:
            return {"name": path.name, "type": "error", "content": "(Error reading file)"}
        return {
            "name": path.name,
            "type": "binary",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }


def load_previous_iteration(workspace: Path) -> dict[str, dict]:
    """Load previous iteration's feedback and outputs.

    Returns a map of run_id -> {"feedback": str, "outputs": list[dict]}.
    """
    result: dict[str, dict] = {}

    # Load feedback
    feedback_map: dict[str, str] = {}
    feedback_path = workspace / "feedback.json"
    if feedback_path.exists():
        try:
            data = json.loads(feedback_path.read_text())
            feedback_map = {
                r["run_id"]: r["feedback"]
                for r in data.get("reviews", [])
                if r.get("feedback", "").strip()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Load runs (to get outputs)
    prev_runs = find_runs(workspace)
    for run in prev_runs:
        result[run["id"]] = {
            "feedback": feedback_map.get(run["id"], ""),
            "outputs": run.get("outputs", []),
        }

    # Also add feedback for run_ids that had feedback but no matching run
    for run_id, fb in feedback_map.items():
        if run_id not in result:
            result[run_id] = {"feedback": fb, "outputs": []}

    return result


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
) -> str:
    """Generate the complete standalone HTML page with embedded data."""
    template = VIEWER_TEMPLATE.read_text()

    # Build previous_feedback and previous_outputs maps for the template
    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    # Fallback for the baseline label when no benchmark.json is given.
    baseline_label = next((r["baseline_label"] for r in runs if r.get("baseline_label")), None)

    embedded = {
        "skill_name": skill_name,
        "baseline_label": baseline_label,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark

    # "</" is escaped so output text containing "</script>" cannot end the script block.
    data_json = json.dumps(embedded).replace("</", "<\\/")

    return template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {data_json};", 1)


# ---------------------------------------------------------------------------
# HTTP server (stdlib only, zero dependencies)
# ---------------------------------------------------------------------------

class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves.

    Regenerates the HTML on each page load so that refreshing the browser
    picks up new eval outputs without restarting the server.
    """

    def __init__(
        self,
        workspace: Path,
        skill_name: str,
        feedback_path: Path,
        previous: dict[str, dict],
        benchmark_path: Path | None,
        *args,
        **kwargs,
    ):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            # Regenerate HTML on each request (re-scans workspace for new outputs)
            runs = find_runs(self.workspace)
            benchmark = None
            if self.benchmark_path and self.benchmark_path.exists():
                try:
                    benchmark = json.loads(self.benchmark_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            html = generate_html(runs, self.skill_name, self.previous, benchmark)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/feedback":
            data = b"{}"
            if self.feedback_path.exists():
                data = self.feedback_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or "reviews" not in data:
                    raise ValueError("Expected JSON object with 'reviews' key")
                self.feedback_path.write_text(json.dumps(data, indent=2) + "\n")
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress request logging to keep terminal clean
        pass


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("workspace", type=click.Path(path_type=Path))
@click.option("--port", "-p", type=int, default=3117, show_default=True,
              help="Server port. If busy, a free port is used instead.")
@click.option("--skill-name", "-n", default=None, help="Skill name for the header.")
@click.option("--previous-workspace", type=click.Path(path_type=Path), default=None,
              help="Previous iteration directory (shows its outputs and feedback as context).")
@click.option("--benchmark", type=click.Path(path_type=Path), default=None,
              help="Path to benchmark.json, shown in the Benchmark tab.")
@click.option("--static", "-s", "static_out", type=click.Path(path_type=Path), default=None,
              help="Write a standalone HTML file here instead of starting a server.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON instead of text.")
def main(workspace, port, skill_name, previous_workspace, benchmark, static_out, as_json):
    """Review eval outputs for an iteration directory (Outputs + Benchmark tabs, feedback.json)."""
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise click.ClickException(f"{workspace} is not a directory")

    runs = find_runs(workspace)
    if not runs:
        raise click.ClickException(f"No runs found in {workspace}")

    skill_name = skill_name or workspace.name.replace("-workspace", "")
    feedback_path = workspace / "feedback.json"

    previous: dict[str, dict] = {}
    if previous_workspace:
        previous = load_previous_iteration(previous_workspace.resolve())

    benchmark_path = benchmark.resolve() if benchmark else None
    benchmark_data = None
    if benchmark_path and benchmark_path.exists():
        try:
            benchmark_data = json.loads(benchmark_path.read_text())
        except (json.JSONDecodeError, OSError):
            click.echo(f"Warning: could not read {benchmark_path}; Benchmark tab disabled", err=True)

    if static_out:
        html = generate_html(runs, skill_name, previous, benchmark_data)
        static_out.parent.mkdir(parents=True, exist_ok=True)
        static_out.write_text(html)
        if as_json:
            click.echo(json.dumps({"mode": "static", "path": str(static_out.resolve()), "runs": len(runs)}))
        else:
            click.echo(f"\n  Static viewer written to: {static_out}\n")
        return

    handler = partial(ReviewHandler, workspace, skill_name, feedback_path, previous, benchmark_path)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Port busy (possibly another viewer): take any free port. Never touch the other listener.
        server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    url = f"http://localhost:{port}"
    if as_json:
        click.echo(json.dumps({
            "mode": "server", "url": url, "port": port, "workspace": str(workspace),
            "feedback": str(feedback_path), "runs": len(runs),
        }))
    else:
        click.echo("\n  Eval Viewer")
        click.echo("  " + "-" * 33)
        click.echo(f"  URL:       {url}")
        click.echo(f"  Workspace: {workspace}")
        click.echo(f"  Feedback:  {feedback_path}")
        if previous:
            click.echo(f"  Previous:  {previous_workspace} ({len(previous)} runs)")
        if benchmark_path:
            click.echo(f"  Benchmark: {benchmark_path}")
        click.echo("\n  Press Ctrl+C to stop.\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopped.", err=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
