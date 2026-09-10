"""Disposable paired GAIA Skill experiment; no MiniAgent core changes.

Selects five tasks from each GAIA level by a documented SHA-256 rank after
excluding multimodal, image, and audio tasks. Runs the same 15 tasks with the
English Skill absent and present. Each task gets a fresh Docker container and
at most five model responses. Gold answers never enter task containers.

This is a local, fixed-sample GAIA experiment, not an official leaderboard run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import uuid

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "GAIA" / "all.json"
FILES = ROOT / "data" / "GAIA" / "files"
OUTPUT_ROOT = Path("/tmp/miniagent-gaia")
IMAGE = "miniagent-gaia-temp:local"
SELECTION_SALT = "miniagent-gaia-v1:"
SUPPORTED_EXTENSIONS = {"", ".txt", ".csv", ".json", ".jsonld", ".xlsx", ".docx", ".pptx", ".pdf", ".zip", ".pdb"}
CONFIG_KEYS = (
    "APIKEY", "API", "MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MINI_CLAUDE_API_KEY", "MINI_CLAUDE_API_BASE",
)

SKILL = """---
name: gaia_research_workflow
description: Solve GAIA research and file-analysis questions with evidence, calculation, verification, and an exact final answer.
user-invocable: false
when-to-use: Use for a GAIA question that requires web research, attachment inspection, computation, or multiple factual steps.
---

# GAIA Research Workflow

1. Identify the exact requested quantity, entity, unit, rounding, and output format.
2. Inspect any attachment directly with suitable command-line or Python tools.
3. For web facts, search with `python /opt/miniagent/search_web.py "query"`, fetch relevant pages, and prefer primary sources when available.
4. Perform calculations in Python rather than mentally. Keep units and rounding explicit.
5. Cross-check the decisive fact or calculation when the response budget permits.
6. Finish with exactly one line in the form `FINAL ANSWER: <answer>`. Put only the requested answer after the colon, without explanation.
"""

SEARCH_HELPER = r'''#!/usr/bin/env python3
import json
import sys
from ddgs import DDGS
query = " ".join(sys.argv[1:]).strip()
if not query:
    raise SystemExit("Usage: search_web.py QUERY")
for item in DDGS().text(query, max_results=8):
    print(json.dumps({"title": item.get("title"), "url": item.get("href"), "snippet": item.get("body")}, ensure_ascii=False))
'''

RUNNER = r'''
import asyncio
import json
import os
from pathlib import Path
import traceback

config_path = Path('/tmp/miniagent-config.json')
os.environ.update(json.loads(config_path.read_text()))
config_path.unlink()
os.environ['MINIAGENT_AUTO_SKILL_EVOLUTION'] = '0'
os.environ['PYTHONUNBUFFERED'] = '1'

SKILL_ENABLED = __SKILL_ENABLED__
SKILL_TEXT = __SKILL_TEXT__
skill_path = Path('/workspace/.miniagent/skills/gaia_research_workflow/SKILL.md')
if SKILL_ENABLED:
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(SKILL_TEXT)

from agents.agent import Agent
from agents.main import _resolve_api_config
from agents.tools import tool_definitions
from agents import session
session.SESSION_DIR = Path('/logs/sessions')

class RoundLimit(Exception):
    pass

class FixedRoundAgent(Agent):
    model_rounds = 0
    def count_round(self):
        if self.model_rounds >= 5:
            raise RoundLimit('Reached five model responses')
        self.model_rounds += 1
    async def _call_openai_stream(self):
        self.count_round()
        return await super()._call_openai_stream()
    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        self.count_round()
        return await super()._call_anthropic_stream(on_tool_block_complete)

api, key, use_openai = _resolve_api_config(None)
allowed = {'read_file', 'write_file', 'edit_file', 'run_shell', 'list_files', 'grep_search', 'skill'}
agent = FixedRoundAgent(
    permission_mode='bypassPermissions', model=os.environ.get('MODEL', 'deepseek-chat'),
    api_key=key, api_base=(api or 'https://api.openai.com/v1') if use_openai else None,
    anthropic_base_url=api if not use_openai else None,
    is_sub_agent=False, custom_tools=[t for t in tool_definitions if t['name'] in allowed],
)
agent._mcp_initialized = True
agent._build_side_query = lambda **kwargs: None
agent._output_buffer = []
calls = []
original_execute = agent._execute_tool_call
async def recorded_execute(name, inp):
    calls.append({'name': name, 'input': inp})
    return await original_execute(name, inp)
agent._execute_tool_call = recorded_execute

status = 'finished'
try:
    asyncio.run(asyncio.wait_for(agent.chat(Path('/workspace/prompt.txt').read_text()), timeout=220))
except RoundLimit:
    status = 'round_limit'
except asyncio.TimeoutError:
    status = 'timeout'
except Exception:
    status = 'error'
    traceback.print_exc()
finally:
    try:
        asyncio.run(agent.drain_background_skill_tasks())
    except Exception:
        pass
    messages = agent._openai_messages if agent.use_openai else agent._anthropic_messages
    Path('/logs/messages.json').write_text(json.dumps(messages, indent=2, default=lambda x: x.model_dump()))
    answer_text = ''.join(agent._output_buffer or [])
    Path('/logs/answer.txt').write_text(answer_text)
    Path('/logs/usage.json').write_text(json.dumps({
        'status': status, 'model_rounds': agent.model_rounds, 'tool_calls': calls,
        'input_tokens': agent.total_input_tokens, 'output_tokens': agent.total_output_tokens,
        'retrieval_hits': agent._last_retrieved_skill_hits,
        'skill_invocations': [c for c in calls if c['name'] == 'skill'],
    }, indent=2))
'''


def run_command(args, *, timeout=300, check=True, log_path=None):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if log_path:
        Path(log_path).write_text(result.stdout)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args[:3])}; see {log_path}")
    return result


def docker(*args, **kwargs):
    return run_command(["docker", *args], **kwargs)


def select_tasks(rows):
    selected = []
    for level in (1, 2, 3):
        eligible = []
        for row in rows:
            filename = row.get("file_name") or ""
            extension = Path(filename).suffix.lower() if filename else ""
            if int(row["Level"]) != level or row.get("problem_type") == "mm" or extension not in SUPPORTED_EXTENSIONS:
                continue
            rank = hashlib.sha256((SELECTION_SALT + row["task_id"]).encode()).hexdigest()
            eligible.append((rank, row))
        selected.extend(row for _, row in sorted(eligible)[:5])
    return selected


def normalize_number(value):
    for char in "$%,":
        value = value.replace(char, "")
    try:
        return float(value)
    except ValueError:
        return float("inf")


def normalize_string(value, remove_punctuation=True):
    value = re.sub(r"\s", "", str(value))
    if remove_punctuation:
        value = value.translate(str.maketrans("", "", string.punctuation))
    return value.lower()


def official_score(model_answer, ground_truth):
    model_answer = "None" if model_answer is None else str(model_answer)
    ground_truth = str(ground_truth)
    try:
        numeric_ground_truth = float(ground_truth)
    except ValueError:
        numeric_ground_truth = None
    if numeric_ground_truth is not None:
        return normalize_number(model_answer) == numeric_ground_truth
    if any(char in ground_truth for char in ",;"):
        gt_parts = re.split("[,;]", ground_truth)
        answer_parts = re.split("[,;]", model_answer)
        if len(gt_parts) != len(answer_parts):
            return False
        comparisons = []
        for answer_part, gt_part in zip(answer_parts, gt_parts):
            try:
                numeric = float(gt_part)
            except ValueError:
                comparisons.append(normalize_string(answer_part, False) == normalize_string(gt_part, False))
            else:
                comparisons.append(normalize_number(answer_part) == numeric)
        return all(comparisons)
    return normalize_string(model_answer) == normalize_string(ground_truth)


def extract_answer(text):
    matches = re.findall(r"(?im)^\s*FINAL ANSWER\s*:\s*(.*?)\s*$", text or "")
    if matches:
        return matches[-1].strip()
    nonempty = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return nonempty[-1] if nonempty else ""


def make_runner(skill_enabled):
    return RUNNER.replace("__SKILL_ENABLED__", repr(skill_enabled)).replace("__SKILL_TEXT__", repr(SKILL))


def build_image(output):
    context = output / "build"
    (context / "agents").mkdir(parents=True)
    for source in (ROOT / "agents").glob("*.py"):
        shutil.copy2(source, context / "agents" / source.name)
    shutil.copy2(ROOT / "requirements.txt", context / "requirements.txt")
    (context / "search_web.py").write_text(SEARCH_HELPER)
    (context / "Dockerfile").write_text(
        "FROM python:3.11-slim\n"
        "ENV PYTHONPATH=/opt/miniagent PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends curl unzip poppler-utils ripgrep && rm -rf /var/lib/apt/lists/*\n"
        "COPY requirements.txt /opt/miniagent/requirements.txt\n"
        "RUN pip install --no-cache-dir -r /opt/miniagent/requirements.txt ddgs requests beautifulsoup4 openpyxl python-docx python-pptx pypdf biopython\n"
        "COPY agents /opt/miniagent/agents\nCOPY search_web.py /opt/miniagent/search_web.py\n"
        "RUN mkdir -p /workspace /logs && chmod +x /opt/miniagent/search_web.py\nWORKDIR /workspace\n"
    )
    docker("build", "-t", IMAGE, str(context), timeout=600, log_path=output / "build.log")


def run_task(task, arm, output, config):
    task_dir = output / arm / f"level-{task['Level']}" / task["task_id"]
    task_dir.mkdir(parents=True)
    filename = task.get("file_name") or ""
    attachment_note = f"The attachment is available at /workspace/{filename}." if filename else "There is no attachment."
    prompt = (
        "Solve this GAIA benchmark task autonomously. Use the available tools and perform the research, file inspection, and calculations yourself. "
        "Internet search is available with `python /opt/miniagent/search_web.py \"query\"`; use curl or Python requests to fetch pages. "
        "Do not ask the user questions and do not merely explain how to solve it. You have at most five model responses. "
        "End with exactly `FINAL ANSWER: <answer>` and put only the requested answer after the colon.\n\n"
        + attachment_note + "\n\n" + task["Question"]
    )
    (task_dir / "prompt.txt").write_text(prompt)
    (task_dir / "runner.py").write_text(make_runner(arm == "on"))
    container = "miniagent-gaia-" + uuid.uuid4().hex[:10]
    started = time.monotonic()
    timed_out = False
    try:
        docker("run", "-d", "--name", container, "--memory", "768m", "--cpus", "1", "--pids-limit", "192", IMAGE, "sleep", "600")
        docker("cp", str(task_dir / "prompt.txt"), container + ":/workspace/prompt.txt")
        docker("cp", str(task_dir / "runner.py"), container + ":/opt/miniagent/runner.py")
        if filename:
            source = FILES / filename
            if not source.is_file():
                raise FileNotFoundError(source)
            docker("cp", str(source), container + f":/workspace/{filename}")
        with tempfile.TemporaryDirectory(prefix="miniagent-gaia-key-") as temporary:
            secret = Path(temporary) / "config.json"
            secret.write_text(json.dumps(config))
            secret.chmod(0o600)
            docker("cp", str(secret), container + ":/tmp/miniagent-config.json")
        try:
            docker("exec", container, "python", "/opt/miniagent/runner.py", timeout=240, check=False, log_path=task_dir / "console.log")
        except subprocess.TimeoutExpired:
            timed_out = True
        docker("cp", container + ":/logs/.", str(task_dir), check=False)
    finally:
        docker("rm", "-f", container, check=False)
    usage = json.loads((task_dir / "usage.json").read_text()) if (task_dir / "usage.json").is_file() else {
        "status": "outer_timeout" if timed_out else "execution_error", "model_rounds": None, "tool_calls": [],
        "input_tokens": 0, "output_tokens": 0, "retrieval_hits": [], "skill_invocations": [],
    }
    raw_answer = (task_dir / "answer.txt").read_text() if (task_dir / "answer.txt").is_file() else ""
    answer = extract_answer(raw_answer)
    return {
        "task_id": task["task_id"], "level": int(task["Level"]), "file_name": filename,
        "problem_type": task.get("problem_type"), "question": task["Question"],
        "answer": answer, "passed": official_score(answer, task["answer"]),
        "elapsed_seconds": round(time.monotonic() - started, 1), **usage,
    }


def main():
    rows = json.loads(DATA.read_text())
    selected = select_tasks(rows)
    assert len(selected) == 15 and {int(t["Level"]) for t in selected} == {1, 2, 3}
    for enabled in (False, True):
        compile(make_runner(enabled), "gaia_runner.py", "exec")
    if sys.argv[1:] == ["--check"]:
        print(json.dumps({"task_ids": {str(level): [t["task_id"] for t in selected if int(t["Level"]) == level] for level in (1,2,3)},
                          "english_skill": SKILL.isascii()}, indent=2))
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: temp_gaia_skills.py [--check]")
    file_config = dotenv_values(ROOT / ".env")
    config = {key: os.environ.get(key, file_config.get(key)) for key in CONFIG_KEYS}
    config = {key: value for key, value in config.items() if value}
    if not any(config.get(key) for key in ("APIKEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MINI_CLAUDE_API_KEY")):
        raise ValueError("No configured model credential")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="comparison-", dir=OUTPUT_ROOT))
    (output / "SKILL.md").write_text(SKILL)
    report = {
        "protocol": "Fixed 15-task local GAIA comparison; not an official leaderboard run",
        "selection": "Five per level, ascending SHA-256(miniagent-gaia-v1: + task_id), excluding multimodal/image/audio and unsupported attachment extensions",
        "dataset_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(), "model": config.get("MODEL", "deepseek-chat"),
        "max_model_responses": 5, "selected_task_ids": [t["task_id"] for t in selected], "arms": {"off": [], "on": []},
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print("Results:", output, flush=True)
    build_image(output)
    print("Image ready.", flush=True)
    for arm in ("off", "on"):
        print("Skills", arm.upper(), flush=True)
        for index, task in enumerate(selected, 1):
            print(f"[{arm} {index}/15] Level {task['Level']} {task['task_id']}", flush=True)
            result = run_task(task, arm, output, config)
            report["arms"][arm].append(result)
            (output / "report.json").write_text(json.dumps(report, indent=2))
            print(f"  passed={result['passed']} status={result['status']} rounds={result['model_rounds']} tools={len(result['tool_calls'])}", flush=True)
    for arm, results in report["arms"].items():
        summary = {"passed": sum(r["passed"] for r in results), "tasks": len(results),
                   "by_level": {str(level): sum(r["passed"] for r in results if r["level"] == level) for level in (1,2,3)},
                   "input_tokens": sum(r["input_tokens"] for r in results), "output_tokens": sum(r["output_tokens"] for r in results),
                   "model_rounds": sum(r["model_rounds"] or 0 for r in results),
                   "retrieved_tasks": sum(bool(r["retrieval_hits"]) for r in results),
                   "invoked_tasks": sum(bool(r["skill_invocations"]) for r in results)}
        report.setdefault("summary", {})[arm] = summary
        print(arm, summary, flush=True)
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print("Report:", output / "report.json", flush=True)


if __name__ == "__main__":
    main()
