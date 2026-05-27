import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel, Field


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


EVENT_PAGE_IDS = (
    "333f1a0a-27c9-8157-9dfa-f95cc19a51aa",
    "337f1a0a-27c9-81b5-b68d-f242e62d1882",
    "337f1a0a-27c9-81ec-ad79-fd657dd10e16",
)

EVENTS_QUERY = " UNION ALL ".join(
    f"SELECT id, url, last_edited_time FROM notion.pages WHERE page_id = '{page_id}'"
    for page_id in EVENT_PAGE_IDS
)

REPOS_QUERY = """
SELECT name, updated_at, description, full_name
FROM github.user_repos
LIMIT 50
""".strip()

GROQ_MODEL = "llama-3.3-70b-versatile"
INDEX_HTML = Path(__file__).with_name("index.html")

app = FastAPI(title="Reef API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_coral_query(sql: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["coral", "sql", "--format", "json", sql],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Coral query timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or "Coral query failed"
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="Coral returned invalid JSON",
        ) from exc

    if not isinstance(payload, list):
        raise HTTPException(status_code=502, detail="Coral returned an unexpected payload")

    return payload


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1200)


class AskResponse(BaseModel):
    answer: str
    suggested_actions: list[str]


class FollowupRequest(BaseModel):
    event_page_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=1200)


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def event_content_query(event_page_id: str) -> str:
    return f"""
SELECT type, rich_text, raw
FROM notion.block_children
WHERE block_id = '{sql_literal(event_page_id)}'
""".strip()


def build_system_prompt(event_rows: list[dict[str, Any]], repo_rows: list[dict[str, Any]]) -> str:
    context = {
        "coral_sql_queries": {
            "events": EVENTS_QUERY,
            "repos": REPOS_QUERY,
        },
        "coral_data": {
            "events": event_rows,
            "repos": repo_rows,
        },
    }

    return (
        "You are Reef, a personal Chennai tech community tracker for the Pirates "
        "of the Coral-bean hackathon. You help connect the user's Notion Event "
        "Vault with their GitHub repositories. Use only the provided Coral context "
        "for event and repository facts. The raw Coral SQL queries are included "
        "alongside the data so you understand how the context was fetched.\n\n"
        "Return only valid JSON with this shape: "
        '{"answer":"concise answer","suggested_actions":["action 1","action 2"]}. '
        "Keep suggested_actions practical and demo-friendly.\n\n"
        f"Coral context:\n{json.dumps(context, indent=2)}"
    )


def ask_groq(question: str, event_rows: list[dict[str, Any]], repo_rows: list[dict[str, Any]]) -> AskResponse:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not set")

    client = Groq(api_key=api_key)

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt(event_rows, repo_rows)},
                {"role": "user", "content": question},
            ],
            temperature=0.25,
            max_completion_tokens=700,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Groq request failed: {exc}") from exc

    content = completion.choices[0].message.content
    if not content:
        raise HTTPException(status_code=502, detail="Groq returned an empty response")

    try:
        payload = json.loads(content)
        return AskResponse.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Groq returned invalid structured JSON") from exc


def generate_followup_tasks(question: str, page_blocks: list[dict[str, Any]]) -> list[str]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not set")

    client = Groq(api_key=api_key)
    page_text = json.dumps(page_blocks, indent=2)

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You generate concise, actionable follow-up checklist tasks from "
                        "event notes. Return only a JSON array of strings. Do not include "
                        "markdown, prose, or keys."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Event page content:\n{page_text}\n\n"
                        f"User question:\n{question}\n\n"
                        "Generate 3-7 follow-up tasks."
                    ),
                },
            ],
            temperature=0.3,
            max_completion_tokens=500,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Groq request failed: {exc}") from exc

    content = completion.choices[0].message.content or ""
    try:
        raw_tasks = json.loads(content)
    except json.JSONDecodeError:
        raw_tasks = [
            line.strip("-*• 0123456789.").strip()
            for line in content.splitlines()
            if line.strip()
        ]

    if not isinstance(raw_tasks, list):
        raise HTTPException(status_code=502, detail="Groq did not return a task list")

    tasks = [
        str(task).strip()
        for task in raw_tasks
        if isinstance(task, str) and task.strip()
    ][:7]

    if not tasks:
        raise HTTPException(status_code=502, detail="Groq returned no follow-up tasks")

    return tasks


async def write_tasks_to_notion(event_page_id: str, tasks: list[str]) -> None:
    notion_key = os.getenv("NOTION_API_KEY")
    if not notion_key:
        raise HTTPException(status_code=500, detail="NOTION_API_KEY is not set")

    children = [
        {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": task[:1800]},
                    }
                ],
                "checked": False,
            },
        }
        for task in tasks
    ]

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.patch(
            f"https://api.notion.com/v1/blocks/{event_page_id}/children",
            headers={
                "Authorization": f"Bearer {notion_key}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"children": children},
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Failed to write tasks to Notion",
                "status": response.status_code,
                "details": response.text,
            },
        )


@app.get("/events")
def events() -> dict[str, Any]:
    return {
        "query": EVENTS_QUERY,
        "events": run_coral_query(EVENTS_QUERY),
    }


@app.get("/repos")
def repos() -> dict[str, Any]:
    return {
        "query": REPOS_QUERY,
        "repos": run_coral_query(REPOS_QUERY),
    }


@app.get("/overview")
def overview() -> dict[str, Any]:
    event_rows = run_coral_query(EVENTS_QUERY)
    repo_rows = run_coral_query(REPOS_QUERY)

    return {
        "events": event_rows,
        "repos": repo_rows,
        "summary": {
            "event_count": len(event_rows),
            "repo_count": len(repo_rows),
        },
        "queries": {
            "events": EVENTS_QUERY,
            "repos": REPOS_QUERY,
        },
    }


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    event_rows = run_coral_query(EVENTS_QUERY)
    repo_rows = run_coral_query(REPOS_QUERY)

    return ask_groq(request.question.strip(), event_rows, repo_rows)


@app.post("/followup")
async def followup(request: FollowupRequest) -> dict[str, list[str]]:
    page_blocks = run_coral_query(event_content_query(request.event_page_id))
    tasks = generate_followup_tasks(request.question.strip(), page_blocks)
    await write_tasks_to_notion(request.event_page_id, tasks)

    return {"tasks_written": tasks}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="index.html not found")

    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/index.html", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return home()


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
