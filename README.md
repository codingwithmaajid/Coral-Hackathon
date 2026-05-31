# Reef — Chennai Tech Community Tracker

A Coral-powered agent that reads your Notion Event Vault and GitHub repos, then writes actionable follow-ups back to Notion.

## What it does

Reef connects your Notion Event Vault with your GitHub repositories using Coral SQL, making it easy to reason across community events and project work. It uses Groq for fast reasoning and content generation, then writes actionable follow-up checklists back into Notion through the Notion API.

The heart of the project is Coral SQL: Reef can pull Notion event data and GitHub repo context into one agent workflow, then turn that context into useful next steps.

## Demo

<!-- add demo gif/video link here -->

## The Coral SQL queries

```sql
SELECT id, url, last_edited_time
FROM notion.pages
WHERE page_id = '333f1a0a-27c9-8157-9dfa-f95cc19a51aa'
UNION ALL
SELECT id, url, last_edited_time
FROM notion.pages
WHERE page_id = '337f1a0a-27c9-81b5-b68d-f242e62d1882'
UNION ALL
SELECT id, url, last_edited_time
FROM notion.pages
WHERE page_id = '337f1a0a-27c9-81ec-ad79-fd657dd10e16';
```

```sql
SELECT name, updated_at, description, full_name
FROM github.user_repos
LIMIT 50;
```

## Setup

1. Install Coral:

```bash
curl -fsSL https://withcoral.com/install.sh | sh
```

2. Add sources:

```bash
coral source add github --interactive
coral source add notion --interactive
```

3. Clone repo, cd into it:

```bash
git clone <your-repo-url>
cd coral-hackathon
```

4. Create `.env` with `GROQ_API_KEY` and `NOTION_API_KEY`:

```env
GROQ_API_KEY="your_groq_key"
NOTION_API_KEY="your_notion_integration_secret"
```

5. Install dependencies:

```bash
pip install -r requirements.txt --break-system-packages
```

6. Run the app:

```bash
uvicorn main:app --reload --port 8000
```

7. Open:

```text
http://127.0.0.1:8000
```

## API endpoints

- `GET /events` — returns Notion event pages.
- `GET /repos` — returns GitHub repositories.
- `GET /overview` — returns events, repos, counts, and Coral SQL queries.
- `POST /ask` — asks Reef to reason over event and repo context.
- `POST /followup` — generates follow-up tasks and writes them back to Notion.

## Stack

Coral SQL · FastAPI · Groq (`llama-3.3-70b-versatile`) · Notion API · Vanilla JS

## Hackathon

Built for Pirates of the Coral-bean hackathon, Track 2: Personal Agent.
