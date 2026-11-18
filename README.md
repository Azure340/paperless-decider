# paperless-decider

A tiny sidecar that adds **decision models** (Ollama `/v1/systemone`, a.k.a. "System One" /
Jev-style models: `tev1`, `nimble`, `clef-flash`, `autotrust-jev`) to
[paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

paperless-ngx 3.5.1 has no decision-model support: one generative LLM produces a JSON blob
with title, dates, tags, correspondents, document types and storage paths in a single chat
call, and it has to guess which existing taxonomy entries match. That is why small local
models invent near-duplicate tags or leave `matched_*` / `*_ids` empty. A decision model does
the opposite: it takes your document plus a shortlist of *existing* tags and returns a
calibrated probability per option, in one forward pass, with no text generation.

This service sits beside paperless and is driven by a normal **Workflow → Send webhook**
action. paperless itself is never patched or forked.

```
Workflow 1  Document added   -> Apply AI suggestions (title)      [paperless' own LLM]
                     |  saves the document -> fires Document updated
                     v
Workflow 2  Document updated -> webhook -> paperless-decider
                 reads the document + /api/tags/ (shortlist <= 26 options)
                 asks Ollama /v1/systemone one yes/no question per tag
                 PATCHes back only tags above the confidence threshold
                 adds `auto-tagged`, or `needs-review` when nothing fitted
```

## Why this design (verified against paperless-ngx v3.5.1 source)

| Constraint | Consequence |
|---|---|
| `send_webhook` uses `httpx.Client(timeout=5.0)` | `/decide` enqueues and answers `202` immediately; decisions run in a background worker |
| the webhook **response body is discarded** | metadata must be written back through the REST API (`PATCH`) |
| `build_workflow_action_context()` sets `"id": ""` during Consumption | trigger must be *Document added* / *Document updated*, never *Consumption* |
| `/v1/systemone` allows 2–26 options per `choice` question | big taxonomies are shortlisted before asking |
| multiple questions in one request skew each other's probabilities | one question per request, `keep_alive` keeps the model warm |
| `PAPERLESS_WEBHOOKS_ALLOWED_PORTS` is empty by default | no paperless env changes needed for a non-standard port |

## Requirements

- Ollama **>= 0.35.0** (0.35.1+ for vision decision models) with a pulled decision model.
- paperless-ngx with AI enabled (for the title step) and an API token.
- Both reachable from this container's docker network.

Check your model really is a decision model:
```bash
curl -s localhost:11434/api/show -d '{"model":"tev1"}' | python3 -c "import json,sys;print(json.load(sys.stdin).get('capabilities'))"
# must include "decision". Imported GGUFs need `capabilities: ["decision"]` set at /api/create.
```

## Configuration

Everything is environment variables. **Never commit `.env`** (it is git-ignored); copy
`.env.example`, or use your orchestrator's secret/variable store.

| Variable | Default | Meaning |
|---|---|---|
| `PAPERLESS_URL` | `http://paperless:8000` | paperless base URL *as this container sees it* |
| `PAPERLESS_API_TOKEN` | — (required) | API + Per-User token; decides which documents/tags are visible |
| `DECIDER_SHARED_SECRET` | — (required) | compared against the `X-Decider-Token` header |
| `OLLAMA_URL` | `http://ollama:11434` | inference server |
| `DECIDER_MODEL` | `tev1` | decision model name |
| `DECIDER_FIELDS` | `correspondent,document_type,tag` | pick any of `tag,correspondent,document_type,storage_path` |
| `DECIDER_THRESH_TAG` | `0.80` | probability needed to add a tag |
| `DECIDER_THRESH_SINGLE` | `0.85` | probability needed to set correspondent / type / path |
| `DECIDER_MAX_TAG_OPTIONS` | `8` | candidate tags offered = forward passes per document |
| `DECIDER_MAX_SINGLE_OPTIONS` | `20` | cap for `choice` options (hard API limit 26) |
| `DECIDER_WAIT_FOR_TITLE` | `false` | wait for paperless' AI title before deciding (the handoff) |
| `DECIDER_TITLE_POLL_SECONDS` / `_TRIES` | `5` / `12` | wait budget |
| `DECIDER_DRY_RUN` | `false` | decide and report, write nothing |
| `DECIDER_OVERWRITE` | `false` | never replace a value a human already set |
| `DECIDER_REVIEW_TAG` | empty | tag applied when a field could not be decided confidently |
| `DECIDER_DONE_TAG` | empty | tag applied when everything was decided; also the loop guard |
| `DECIDER_REVIEW_ON_NO_TAGS` | `false` | send documents that matched no tag to review instead of filing blank |
| `DECIDER_MIN_CONTENT_CHARS` | `80` | skip blank scans (they used to inherit a neighbour's metadata) |
| `DECIDER_MAX_STATE_CHARS` | `6000` | how much document text the model reads |
| `DECIDER_KEEP_ALIVE` | `10m` | passed to Ollama so the model stays loaded |
| `DECIDER_WORKERS` | `1` | parallel documents |
| `DECIDER_PORT` | `8090` | listen port |
| `DECIDER_LOG_LEVEL` | `INFO` | |

## HTTP surface

| Endpoint | Writes? | Purpose |
|---|---|---|
| `POST /decide` | later, async | what the workflow calls; returns `202` in milliseconds |
| `POST /decide/sync` | yes | one document, decision applied inline (for manual runs; may take seconds) |
| `GET /preview/<id>` | **never** | full report with probabilities — the safe way to inspect any document |
| `GET /status` | never | config + last 100 decisions |
| `GET /healthz` | never | liveness |

Requests need `X-Decider-Token: <DECIDER_SHARED_SECRET>` (or `Authorization: Bearer ...`)
whenever a secret is configured.

## Run it

```bash
docker compose up -d      # uses the published image; see docker-compose.yml
curl -s localhost:8090/healthz
```

Tested without any network or model (Python 3.11+, standard library only):
```bash
python3 tests/test_title_wait.py
python3 tests/selftest.py        # fake paperless + fake Ollama, 14 assertions
```

## paperless setup (two workflows)

1. Create tags `auto-tagged` and `needs-review` (matching rule **None**, not inbox tags).
2. **Workflow 1** — trigger *Document added*, action *Apply AI suggestions*, field **Title**.
3. **Workflow 2** — trigger *Document updated*, conditions **Has not any of the tags**
   `auto-tagged` **and** `needs-review`; action **Send webhook**:
   - URL `http://paperless-decider:8090/decide`
   - Send as JSON **on**, parameter `id` = `{id}`
   - header `X-Decider-Token` = your secret

The two "has not" conditions are the loop guard: this service adds one of those tags, so
Workflow 2 stops matching. Removing the tag from a document is the "decide it again" gesture.

Start with `DECIDER_DRY_RUN=true`, read `GET /status`, then switch it off. Nothing here ever
removes data from a document.

## CI / image

`.github/workflows/build.yml` runs the two offline test suites on every push and PR, then
builds and pushes a multi-arch image (amd64 + arm64) to GHCR:

```
ghcr.io/azure340/paperless-decider:latest   # also :main and :sha-<commit>
```

After the **first successful run**, set the package to public so any host can pull it without
credentials: GitHub → your profile → **Packages** → `paperless-decider` → **Package settings**
→ Change visibility → **Public**. Until you do, pulls need a `docker login ghcr.io`.

If the build job fails with a permission error, enable it under
Settings → Actions → General → Workflow permissions ("Read repository contents and packages
permissions" is enough).

## Limits worth knowing

- Tags are chosen from your existing taxonomy only; this service never invents a new tag name
  (that stays the LLM's job).
- Two near-duplicate labels (`Rechnung` next to `Invoice`) cannot be separated by probability.
  Merge them.
- `choice`/`noul` questions are English-shaped. For non-English archives, set the
  `DECIDER_Q_*` variables to questions in your language.
- Confidence is calibration against the model's own distribution, not correctness. Tune the
  thresholds against `GET /preview/<id>` on documents you already know the answer to.

## Layout

```
app/decider.py                 the whole service, standard library only
tests/selftest.py              offline end-to-end (fake paperless + fake Ollama)
tests/test_title_wait.py       the title -> tags handoff
Dockerfile  .dockerignore  docker-compose.yml  .env.example
.github/workflows/build.yml    tests + multi-arch GHCR publish
```

## License

MIT — see `LICENSE`.
