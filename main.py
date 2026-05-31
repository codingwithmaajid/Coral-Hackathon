import json
import os
import re
import subprocess
import time
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


VAULT_PARENT_PAGE_ID = "333f1a0a-27c9-81c8-9ca5-f2823b3f99fc"
VAULT_CACHE_SECONDS = 60

VAULT_CHILDREN_QUERY = f"""
SELECT id, type, raw
FROM notion.block_children
WHERE block_id = '{VAULT_PARENT_PAGE_ID}'
LIMIT 100
""".strip()

REPOS_QUERY = """
SELECT name, updated_at, description, full_name
FROM github.user_repos
LIMIT 50
""".strip()

GROQ_MODEL = "llama-3.3-70b-versatile"
INDEX_HTML = Path(__file__).with_name("index.html")
vault_cache: dict[str, Any] = {"expires_at": 0.0, "pages": []}

app = FastAPI(title="Reef: Notion MCP Writer powered by Coral SQL")

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
    page_id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=1200)


class AgentAction(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    action: str
    status: str
    summary: str
    page_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AskResponse(BaseModel):
    message: str
    actions: list[AgentAction]
    results: list[AgentResult] = Field(default_factory=list)


class FollowupRequest(BaseModel):
    page_id: str | None = Field(default=None, max_length=80)
    event_page_id: str | None = Field(default=None, max_length=80)
    question: str = Field(min_length=1, max_length=1200)


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def parse_json_value(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def plain_text_from_rich_text(value: Any) -> str:
    rich_text = parse_json_value(value, [])
    if not isinstance(rich_text, list):
        return ""
    return "".join(
        str(part.get("plain_text") or part.get("text", {}).get("content") or "")
        for part in rich_text
        if isinstance(part, dict)
    ).strip()


def title_from_url(url: str | None, fallback: str = "Untitled Event") -> str:
    if not url:
        return fallback
    slug = url.rstrip("/").split("/")[-1]
    title = slug
    if "-" in slug:
        title = "-".join(slug.split("-")[:-1]) or slug
    return title.replace("-", " ").strip() or fallback


def notion_url_for_page(page_id: str, title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "Untitled"
    return f"https://www.notion.so/{slug}-{page_id.replace('-', '')}"


def page_metadata_query(page_id: str) -> str:
    return f"""
SELECT id, url, last_edited_time, properties
FROM notion.pages
WHERE page_id = '{sql_literal(page_id)}'
LIMIT 1
""".strip()


def event_content_query(page_id: str) -> str:
    return f"""
SELECT id, type, rich_text, raw
FROM notion.block_children
WHERE block_id = '{sql_literal(page_id)}'
LIMIT 100
""".strip()


def notion_text(text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": text[:1800]},
    }


def build_vault_page(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("type") != "child_page":
        return None

    raw = parse_json_value(row.get("raw"), {})
    if not isinstance(raw, dict):
        return None

    page_id = str(row.get("id") or raw.get("id") or "").strip()
    if not page_id:
        return None

    title = str(raw.get("child_page", {}).get("title") or "Untitled Event").strip()
    last_edited_time = raw.get("last_edited_time")

    return {
        "id": page_id,
        "title": title,
        "url": notion_url_for_page(page_id, title),
        "last_edited_time": last_edited_time,
    }


def discover_vault_pages(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    if not force and vault_cache["pages"] and now < float(vault_cache["expires_at"]):
        return list(vault_cache["pages"])

    rows = run_coral_query(VAULT_CHILDREN_QUERY)
    pages = [
        page
        for row in rows
        if (page := build_vault_page(row)) is not None
    ]

    vault_cache["pages"] = pages
    vault_cache["expires_at"] = now + VAULT_CACHE_SECONDS
    return list(pages)


def vault_page_by_id(page_id: str) -> dict[str, Any] | None:
    for page in discover_vault_pages():
        if page["id"] == page_id:
            return page
    return None


def structure_event_blocks(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    checklist_items: list[dict[str, Any]] = []
    text_blocks: list[str] = []

    for row in rows:
        raw = parse_json_value(row.get("raw"), {})
        block_type = str(row.get("type") or "")
        text = plain_text_from_rich_text(row.get("rich_text"))

        if not text and isinstance(raw, dict) and block_type in raw:
            block_payload = raw.get(block_type) or {}
            if isinstance(block_payload, dict):
                text = plain_text_from_rich_text(block_payload.get("rich_text"))

        block = {
            "id": row.get("id"),
            "type": block_type,
            "text": text,
        }

        if block_type == "to_do":
            checked = False
            if isinstance(raw, dict):
                checked = bool(raw.get("to_do", {}).get("checked"))
            block["checked"] = checked
            checklist_items.append(block)
        elif text:
            text_blocks.append(text)

        blocks.append(block)

    return blocks, checklist_items, text_blocks


def get_event_detail(page_id: str) -> dict[str, Any]:
    metadata_rows = run_coral_query(page_metadata_query(page_id))
    metadata = metadata_rows[0] if metadata_rows else {}
    vault_page = vault_page_by_id(page_id) or {}
    block_rows = run_coral_query(event_content_query(page_id))
    blocks, checklist_items, text_blocks = structure_event_blocks(block_rows)
    url = str(metadata.get("url") or vault_page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}")

    return {
        "id": page_id,
        "title": str(vault_page.get("title") or title_from_url(url)),
        "url": url,
        "last_edited_time": metadata.get("last_edited_time") or vault_page.get("last_edited_time"),
        "properties": parse_json_value(metadata.get("properties"), {}),
        "blocks": blocks,
        "text_blocks": text_blocks,
        "checklist_items": checklist_items,
        "queries": {
            "metadata": page_metadata_query(page_id),
            "blocks": event_content_query(page_id),
        },
    }


async def append_blocks_to_notion(page_id: str, children: list[dict[str, Any]]) -> None:
    notion_key = os.getenv("NOTION_API_KEY")
    if not notion_key:
        raise HTTPException(status_code=500, detail="NOTION_API_KEY is not set")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
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
                "error": "Failed to write to Notion",
                "status": response.status_code,
                "details": response.text,
            },
        )


async def create_notion_page(title: str, content: str, parent_page_id: str | None = None) -> dict[str, str]:
    notion_key = os.getenv("NOTION_API_KEY")
    if not notion_key:
        raise HTTPException(status_code=500, detail="NOTION_API_KEY is not set")

    parent_id = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID") or VAULT_PARENT_PAGE_ID
    children = content_to_notion_blocks(content)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {notion_key}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": {
                        "title": [
                            {
                                "type": "text",
                                "text": {"content": title[:200] or "Untitled Reef Page"},
                            }
                        ]
                    }
                },
                "children": children,
            },
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Failed to create Notion page",
                "status": response.status_code,
                "details": response.text,
            },
        )

    payload = response.json()
    return {
        "page_id": payload.get("id", ""),
        "page_url": payload.get("url", ""),
    }


def content_to_notion_blocks(content: str) -> list[dict[str, Any]]:
    paragraphs = [line.strip() for line in content.splitlines() if line.strip()]
    if not paragraphs:
        paragraphs = ["Created by Reef from Coral SQL context."]

    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [notion_text(paragraph)]},
        }
        for paragraph in paragraphs[:20]
    ]


def get_notion_page_url(page_id: str) -> str:
    rows = run_coral_query(
        f"SELECT url FROM notion.pages WHERE page_id = '{sql_literal(page_id)}'"
    )
    if rows and rows[0].get("url"):
        return str(rows[0]["url"])
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def build_system_prompt(event_detail: dict[str, Any], repo_rows: list[dict[str, Any]]) -> str:
    context = {
        "coral_sql_queries": {
            "vault": VAULT_CHILDREN_QUERY,
            "active_event_metadata": event_detail.get("queries", {}).get("metadata"),
            "active_event_blocks": event_detail.get("queries", {}).get("blocks"),
            "repos": REPOS_QUERY,
        },
        "coral_data": {
            "active_event": event_detail,
            "repos": repo_rows,
        },
    }

    return (
        "You are Reef: the Notion MCP Writer powered by Coral SQL. Reef reads "
        "the user's connected workspace with Coral SQL, reasons with Groq, then "
        "returns structured actions for the backend to execute. The Notion Event "
        "Vault is the source of truth. Do not use public event discovery, Google "
        "data, or website scraping.\n\n"
        "You are an agent planner, not a chatbot. When the user requests a Notion "
        "write, return actions. Do not merely suggest actions.\n\n"
        f"Active Notion page ID: {event_detail.get('id')}.\n"
        "Supported actions:\n"
        "1. create_notion_page: payload {\"title\": string, \"content\": string, "
        "\"parent_page_id\": optional string}\n"
        "2. write_followup_checklist: payload {\"page_id\": string, "
        "\"question\": string}\n"
        "3. no_op: payload {\"reason\": string}\n\n"
        "Core commands: Create a page for this event; Write a follow-up checklist; "
        "Turn this repo into a Notion project page; Summarize my Event Vault; Link "
        "this GitHub repo to this Notion event.\n\n"
        "For write_followup_checklist, always use the active Notion page ID as page_id. "
        "Return only valid JSON with this shape: "
        '{"message":"brief status","actions":[{"action":"create_notion_page",'
        '"payload":{"title":"...","content":"..."}}]}. '
        "Use no_op only when the user is asking a question and no write is needed.\n\n"
        f"Coral context:\n{json.dumps(context, indent=2)}"
    )


def plan_actions_with_groq(
    question: str,
    event_detail: dict[str, Any],
    repo_rows: list[dict[str, Any]],
) -> AskResponse:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not set")

    client = Groq(api_key=api_key)

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt(event_detail, repo_rows)},
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
        raise HTTPException(status_code=502, detail="Groq returned invalid action JSON") from exc


async def execute_agent_action(action: AgentAction) -> AgentResult:
    if action.action == "create_notion_page":
        title = str(action.payload.get("title") or "Untitled Reef Page").strip()
        content = str(action.payload.get("content") or "").strip()
        parent_page_id = action.payload.get("parent_page_id")
        if parent_page_id is not None:
            parent_page_id = str(parent_page_id).strip()

        if not action.payload.get("title") or not content:
            return AgentResult(
                action=action.action,
                status="failed",
                summary="Missing title or content for create_notion_page action.",
                payload=action.payload,
            )

        page = await create_notion_page(title, content, parent_page_id or None)
        return AgentResult(
            action=action.action,
            status="success",
            summary=f"Created Notion page: {title}",
            page_url=page.get("page_url") or None,
            payload={"page_id": page.get("page_id"), "title": title},
        )

    if action.action == "write_followup_checklist":
        event_page_id = str(action.payload.get("page_id") or action.payload.get("event_page_id") or "").strip()
        question = str(action.payload.get("question") or "Write a follow-up checklist").strip()
        if not event_page_id:
            return AgentResult(
                action=action.action,
                status="failed",
                summary="Missing page_id for checklist action.",
                payload=action.payload,
            )

        page_blocks = run_coral_query(event_content_query(event_page_id))
        tasks = generate_followup_tasks(question, page_blocks)
        await write_tasks_to_notion(event_page_id, tasks)
        return AgentResult(
            action=action.action,
            status="success",
            summary=f"Wrote {len(tasks)} checklist tasks to the Notion event page.",
            page_url=get_notion_page_url(event_page_id),
            payload={"tasks_written": tasks, "page_id": event_page_id},
        )

    if action.action == "no_op":
        return AgentResult(
            action=action.action,
            status="skipped",
            summary=str(action.payload.get("reason") or "No tool execution was needed."),
            payload=action.payload,
        )

    return AgentResult(
        action=action.action,
        status="unsupported",
        summary=f"Unsupported action: {action.action}",
        payload=action.payload,
    )


def write_execution_blocked(action_name: str, question: str) -> bool:
    normalized = question.lower()
    global_blockers = ("do not write", "don't write", "just explain", "only explain")
    if any(blocker in normalized for blocker in global_blockers):
        return True

    if action_name == "create_notion_page":
        return any(
            blocker in normalized
            for blocker in ("do not create", "don't create", "do not make a page", "don't make a page")
        )

    if action_name == "write_followup_checklist":
        return any(
            blocker in normalized
            for blocker in ("do not update", "don't update", "do not append", "don't append")
        )

    return False


async def execute_or_skip_agent_action(action: AgentAction, question: str) -> AgentResult:
    write_actions = {"create_notion_page", "write_followup_checklist"}
    if action.action in write_actions and write_execution_blocked(action.action, question):
        return AgentResult(
            action=action.action,
            status="skipped",
            summary="Skipped this write action because the user explicitly blocked it.",
            payload=action.payload,
        )

    return await execute_agent_action(action)


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

    await append_blocks_to_notion(event_page_id, children)


@app.on_event("startup")
def load_vault_on_startup() -> None:
    try:
        discover_vault_pages(force=True)
    except HTTPException as exc:
        print(f"Reef startup vault discovery failed: {exc.detail}")


@app.get("/vault")
def vault() -> dict[str, Any]:
    pages = discover_vault_pages()
    return {
        "query": VAULT_CHILDREN_QUERY,
        "cache_seconds": VAULT_CACHE_SECONDS,
        "cached_until": vault_cache["expires_at"],
        "pages": pages,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "groq_key_loaded": bool(os.getenv("GROQ_API_KEY")),
        "notion_key_loaded": bool(os.getenv("NOTION_API_KEY")),
        "vault_cache_size": len(vault_cache["pages"]),
    }


@app.get("/event/{page_id}")
def event_detail(page_id: str) -> dict[str, Any]:
    return get_event_detail(page_id)


@app.get("/events")
def events() -> dict[str, Any]:
    pages = discover_vault_pages()
    return {
        "query": VAULT_CHILDREN_QUERY,
        "events": pages,
    }


@app.get("/repos")
def repos() -> dict[str, Any]:
    return {
        "query": REPOS_QUERY,
        "repos": run_coral_query(REPOS_QUERY),
    }


@app.get("/overview")
def overview() -> dict[str, Any]:
    event_rows = discover_vault_pages()
    repo_rows = run_coral_query(REPOS_QUERY)

    return {
        "events": event_rows,
        "repos": repo_rows,
        "summary": {
            "event_count": len(event_rows),
            "repo_count": len(repo_rows),
        },
        "queries": {
            "events": VAULT_CHILDREN_QUERY,
            "repos": REPOS_QUERY,
        },
    }


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    event_detail_payload = get_event_detail(request.page_id.strip())
    repo_rows = run_coral_query(REPOS_QUERY)
    response = plan_actions_with_groq(request.question.strip(), event_detail_payload, repo_rows)
    response.results = [
        await execute_or_skip_agent_action(action, request.question)
        for action in response.actions
    ]

    return response


@app.post("/followup")
@app.post("/notion/follow-up-checklist")
async def followup(request: FollowupRequest) -> dict[str, Any]:
    page_id = (request.page_id or request.event_page_id or "").strip()
    if not page_id:
        raise HTTPException(status_code=422, detail="page_id is required")

    page_blocks = run_coral_query(event_content_query(page_id))
    tasks = generate_followup_tasks(request.question.strip(), page_blocks)
    await write_tasks_to_notion(page_id, tasks)
    page_url = get_notion_page_url(page_id)

    return {
        "page_url": page_url,
        "summary": f"Wrote {len(tasks)} follow-up checklist tasks to the Notion event page.",
        "tasks_written": tasks,
    }


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
