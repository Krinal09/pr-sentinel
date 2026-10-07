# PR Sentinel — AI GitHub Pull Request Reviewer

PR Sentinel reviews every pull request automatically. When a PR is opened or updated it:

1. fetches the diff from the GitHub API,
2. pulls **related code from the rest of the repository** (RAG with FastEmbed + FAISS),
3. asks an LLM (Groq / Llama 3.3 by default, via LangChain) to review each changed file,
4. posts **inline review comments** on the exact lines — with one-click `suggestion` fixes —
5. and keeps one **PR summary comment** (overview, key changes, risk level, findings count) up to date.

```
GitHub PR event
      │
      ▼
┌──────────────┐   webhook    ┌─────────────────────────────────────────────┐
│ GitHub App / │ ───────────▶ │ FastAPI  /webhook   or   AWS Lambda          │
│ GitHub Action│              │  ├─ verify HMAC signature                    │
└──────────────┘              │  └─ background job                           │
                              │       ├─ GitHub API: PR, files, tarball      │
                              │       ├─ diff parser → commentable lines     │
                              │       ├─ RAG: chunk → embed → FAISS search   │
                              │       ├─ LangChain LLM → structured findings │
                              │       └─ post inline review + summary        │
                              └─────────────────────────────────────────────┘
```

## Features

- **Line-accurate inline comments** – a unified-diff parser maps findings to lines GitHub accepts; off-by-one LLM answers are snapped to the nearest valid line, and anything GitHub still rejects falls back to a single review comment.
- **Structured LLM output** – Pydantic schemas (`severity`, `category`, `message`, `suggestion`) via `with_structured_output`, so no fragile text parsing.
- **Repository-aware context** – the repo is downloaded as one tarball, chunked, embedded and searched, so the model sees the code a change touches.
- **Severity filter, ignore patterns, file limits** – skip lockfiles, build output and images; only comment at or above `MIN_SEVERITY`.
- **Three ways to deploy** – GitHub Action (zero servers), GitHub App on FastAPI/Docker, or AWS Lambda + API Gateway (SAM template).
- **Provider-agnostic** – any LangChain chat model: Groq, OpenAI, Anthropic, AWS Bedrock…
- **Tested** – unit tests for the diff parser, webhook security, review pipeline, GitHub client and Lambda handler, with fakes for GitHub and the LLM.

## Tech stack

Python · FastAPI · LangChain · Groq (Llama 3.3) · FAISS · FastEmbed · GitHub REST API & GitHub Apps · Docker · GitHub Actions · AWS Lambda, API Gateway, CloudWatch (SAM)

## Project structure

```
pr_sentinel/
  config.py          settings from environment variables
  github_client.py   GitHub REST client + GitHub App (JWT → installation token) auth
  diff_parser.py     unified diff → commentable line numbers + annotated diff for the LLM
  context.py         RAG over the repository (FastEmbed + FAISS)
  reviewer.py        prompts + LangChain structured output
  service.py         orchestrates a full review and publishes it
  webhook.py         signature check + event filtering (shared)
  main.py            FastAPI webhook server
  lambda_handler.py  AWS Lambda entry point
  cli.py             GitHub Actions / local command line
action.yml           reusable GitHub Action
deploy/aws/          SAM template + Lambda container image
tests/               unit tests
```

---

## Option 1 — GitHub Action (easiest, no server)

1. Get a free API key at [console.groq.com](https://console.groq.com).
2. In the repository you want reviewed: **Settings → Secrets and variables → Actions → New secret** named `GROQ_API_KEY`.
3. Add `.github/workflows/pr-review.yml`:

```yaml
name: PR Sentinel review
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: Krinal09/pr-sentinel@main
        with:
          groq-api-key: ${{ secrets.GROQ_API_KEY }}
          min-severity: medium   # optional
```

Open a pull request — the review appears within a minute or two.

> Pull requests from **forks** get a read-only token in Actions, so the review can't be posted there. Use the GitHub App option for public repos that take outside contributions.

## Option 2 — GitHub App + FastAPI (Docker)

1. **Create the app:** GitHub → Settings → Developer settings → GitHub Apps → *New GitHub App*
   - Webhook URL: `https://<your-host>/webhook` (for local testing use `ngrok http 8000`)
   - Webhook secret: any random string
   - Permissions: **Pull requests: Read & write**, **Contents: Read-only**, **Issues: Read & write**
   - Subscribe to events: **Pull request**
2. Generate a **private key** (.pem) and note the **App ID**, then install the app on your repositories.
3. Configure and run:

```bash
cp .env.example .env          # fill in GROQ_API_KEY, GITHUB_APP_ID, key path, webhook secret
pip install -r requirements.txt
uvicorn pr_sentinel.main:app --reload --port 8000

# or with Docker
docker build -t pr-sentinel .
docker run --env-file .env -v $PWD/pr-sentinel.private-key.pem:/app/pr-sentinel.private-key.pem -p 8000:8000 pr-sentinel
```

`GET /health` should return `{"status": "ok"}`.

## Option 3 — AWS Lambda + API Gateway

GitHub expects a webhook reply within 10 seconds, so the function verifies the request, re-invokes itself **asynchronously** with the job and returns `202` immediately. The review then runs in the background, with logs in **CloudWatch**.

```bash
cd deploy/aws
sam build
sam deploy --guided      # asks for GitHub App ID, webhook secret, private key, Groq key
```

Copy the `WebhookUrl` output into your GitHub App's webhook URL.

## Run against any PR from your terminal

```bash
export GITHUB_TOKEN=ghp_...   # a personal access token
export GROQ_API_KEY=gsk_...
python -m pr_sentinel.cli --repo owner/name --pr 12 --dry-run   # print, don't post
python -m pr_sentinel.cli --repo owner/name --pr 12             # post the review
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `groq` | Any LangChain provider (`openai`, `anthropic`, `bedrock`…) — install its package |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Model name |
| `MIN_SEVERITY` | `low` | `low`, `medium`, `high` or `critical` |
| `MAX_FILES` | `25` | Maximum files reviewed per PR (largest changes first) |
| `MAX_PATCH_CHARS` | `12000` | Diff characters sent per file |
| `IGNORE_PATTERNS` | – | Extra comma-separated globs, e.g. `docs/*,*.snap` |
| `ENABLE_RAG` | `true` | Use repository context |
| `RAG_TOP_K` | `4` | Related chunks given to the LLM per file |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | FastEmbed model |

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## License

MIT
