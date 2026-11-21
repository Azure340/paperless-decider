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
Workflow 1  Document added   -> [1] Apply AI suggestions (title)   [paperless' own LLM]
                              -> [2] Send webhook -> paperless-decider
                 (the AI step is a queued celery task and does NOT re-trigger a
                  workflow, so both actions live in Workflow 1; the sidecar waits for
                  the title to land before deciding - DECIDER_WAIT_FOR_TITLE=true)
Workflow 2  Document updated -> webhook -> paperless-decider       [optional re-decide]
                 reads the document + /api/tags/ (shortlist <= 26 options)
                 asks Ollama /v1/systemone one yes/no question per tag
                 PATCHes back only tags above the confidence threshold
                 (a `needs-review`/`auto-tagged` stamp only appears if you set
                  DECIDER_REVIEW_TAG / DECIDER_DONE_TAG - both empty by default)
```

## Why this design (verified against paperless-ngx v3.5.1 source)

| Constraint | Consequence |
|---|---|
| `send_webhook` uses `httpx.Client(timeout=5.0)` | `/decide` enqueues and answers `202` immediately; decisions run in a background worker |
| the webhook **response body is discarded** | metadata must be written back through the REST API (`PATCH`) |
| `build_workflow_action_context()` sets `"id": ""` during Consumption | trigger must be *Document added* / *Document updated*, never *Consumption* |
| `apply_ai_suggestions` ends with `# No document_updated signal to avoid loop` | the AI title step never fires a *Document updated* workflow: the webhook has to be a **second action of the same workflow** |
| webhook params are Jinja2 (`{{ doc_id }}`, `{{ doc_title }}`) | `{id}` is not a variable, arrives literally, and the sidecar answers 422 |
| `send_webhook.apply_async()` runs **inside** the consumer's `transaction.atomic()` block (only `apply_ai_suggestions` uses `delay_on_commit`) | the webhook can be delivered before the new row is visible: the first `GET /api/documents/<id>/` answers 404. `read_document` retries inside the wait budget instead of believing the document is gone - measured live, the commit landed 231 ms after that read |
| `/v1/systemone` allows 2–26 options per `choice` question | big taxonomies are shortlisted first: whole name in the text, then stem-matched words (taxes~tax), then usage |
| batching questions shifts probabilities (measured: mean 0.002, worst 0.022 - the same at every chunk size) | the whole taxonomy goes out in one request, and anything within `DECIDER_GREY_ZONE` of the threshold is re-asked alone; `keep_alive` keeps the model warm |
| `choice` dilutes its winner across the field | tags are independent yes/no questions (`noul`), never a 26-option contest: `US Taxes` read 0.9876 solo but only 0.7448 as the winner of a 27-option choice |
| `PAPERLESS_WEBHOOKS_ALLOWED_PORTS` is empty by default | no paperless env changes needed for a non-standard port |

## Requirements

- Ollama **>= 0.35.0** (0.35.1+ for vision decision models) with a pulled decision model.
- paperless-ngx with AI enabled (for the title step) and an API token.
- Both reachable from this container's docker network.
- Enough RAM for the model you pick. `tev1:latest` is the **4b Q8_0** build (4.6 GB
  weights, ~5.5 GB to run) and `nimble` is ~9.5 GB; start with **`tev1:0.8b`**
  (834 MB). If the container running Ollama has a memory limit, loading a too-big
  model gets llama-server SIGKILLed and `/v1/systemone` answers 500
  `llama-server process has terminated: signal: killed` - which reaches the sidecar
  as a closed connection, visible in `GET /status` as a `DecisionError`.

Check your model really is a decision model:
```bash
curl -s localhost:11434/api/show -d '{"model":"tev1:0.8b"}' | python3 -c "import json,sys;print(json.load(sys.stdin).get('capabilities'))"
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
| `DECIDER_MODEL` | `tev1:0.8b` | decision model name (use a small tag, see Requirements) |
| `DECIDER_FIELDS` | `correspondent,document_type,tag` | pick any of `tag,correspondent,document_type,storage_path` |
| `DECIDER_THRESH_TAG` | `0.80` | probability needed to add a tag |
| `DECIDER_THRESH_SINGLE` | `0.85` | probability needed to set correspondent / type / path |
| `DECIDER_MAX_TAG_OPTIONS` | `0` (= all) | how many tags get asked about; `0` asks about every visible tag |
| `DECIDER_QUESTIONS_PER_REQUEST` | `0` (= one request) | questions per `/v1/systemone` call; `0` sends the whole taxonomy at once |
| `DECIDER_GREY_ZONE` | `0.03` | re-ask a tag alone when its batched probability is this close to the threshold |
| `DECIDER_MAX_SINGLE_OPTIONS` | `20` | cap for `choice` options (hard API limit 26) |
| `DECIDER_WAIT_FOR_TITLE` | `false` | wait for paperless' AI title before deciding (the handoff) |
| `DECIDER_TITLE_POLL_SECONDS` / `_TRIES` | `5` / `12` | wait budget |
| `DECIDER_DRY_RUN` | `false` | decide and report, write nothing |
| `DECIDER_OVERWRITE` | `false` | never replace a value a human already set |
| `DECIDER_REVIEW_TAG` | empty | tag applied when a field could not be decided confidently |
| `DECIDER_DONE_TAG` | empty | tag applied when everything was decided; also the loop guard |
| `DECIDER_REVIEW_ON_NO_TAGS` | `false` | send documents that matched no tag to review instead of filing blank |
| `DECIDER_SKIP_TAGS` | empty | comma-separated tag names never offered as candidates (workflow-state tags, e.g. `inbox`) |
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
| `GET /healthz` | never | liveness (the only endpoint that needs no token, for the container healthcheck) |

Every endpoint except `/healthz` needs `X-Decider-Token: <DECIDER_SHARED_SECRET>` (or
`Authorization: Bearer ...`), because `/status` and `/preview` expose document titles.

## Run it

```bash
docker compose up -d      # uses the published image; see docker-compose.yml
curl -s localhost:8090/healthz
```

Verified offline with no network and no model (Python 3.11+, standard library only) —
74 checks in total, run in CI on every push and PR:
```bash
python3 tests/test_title_wait.py     # 16 checks: the title -> tags handoff + the commit race
python3 tests/selftest.py            # 58 checks: fake paperless + fake Ollama end to end
```

## paperless setup (two workflows)

1. Marker tags are **opt-in and empty by default** (`DECIDER_REVIEW_TAG`, `DECIDER_DONE_TAG`).
   If you do want the stamps, create `auto-tagged` and `needs-review` first (matching rule
   **None**, so they are not inbox tags) and set both names. Creating them alone is not
   enough to keep them away either: the service *creates* a missing marker tag, so a
   non-empty default in `docker-compose.yml` would resurrect a tag you deleted.
2. **Workflow 1** (the ingest path) — trigger *Document added*, two actions in this
   order: *Apply AI suggestions* (field **Title**), then *Send webhook*:
   - URL `http://paperless-decider:8090/decide`
   - Send as JSON **on**, parameter `id` = `{{ doc_id }}` — Jinja2, and `{id}` is not a
     variable: it arrives as literal text and the sidecar rejects it with 422
   - header `X-Decider-Token` = your secret
   Keep `DECIDER_WAIT_FOR_TITLE=true`: paperless queues the title as a celery task and
   deliberately sends no `document_updated` afterwards, so the webhook arrives first.
3. **Workflow 2** (optional, re-decide after a human edit) — trigger *Document updated*,
   conditions **Has not any of the tags** `auto-tagged` **and** `needs-review`, with the
   same webhook action.

The two "has not" conditions are the loop guard: this service adds one of those tags, so
Workflow 2 stops matching. Removing the tag from a document is the "decide it again" gesture.

Start with `DECIDER_DRY_RUN=true`, read `GET /status`, then switch it off. Nothing here ever
removes data from a document - and it is worth knowing why that took a bug to guarantee: a
`tags` PATCH replaces the *whole* list, and paperless returns `tags` as a list of primary
keys. The sidecar now reads both shapes, re-reads the document immediately before writing
(the read can be minutes old, because it waited for the AI title), merges, and refuses to
write at all if it cannot read the current tags. A tag paperless added in the meantime -
typically an inbox tag - is kept, and `tags_added_by_others` in `/status` says which.

### Reading the log

Every document that arrives leaves at least one line now, because "no tags were
written" used to be indistinguishable from "nothing happened":

```
document 1335: webhook received, decision queued (queue=0)      <- proof it arrived
document 1335: patched tags=[3, 24] (21734ms)                   <- the write, add-only
document 1335: dry run, would patch tags=[3]                    <- DECIDER_DRY_RUN=true
document 1335: no tag and no field above threshold, nothing written (18990ms)
document 1335: left correspondent alone, a human had already set them
WARNING document 1335: skipped (content under 80 chars (blank scan?))
WARNING document 1335: skipped (paperless answered 404 for the whole wait budget - ...)
```

A skip is a WARNING on purpose: it is not a normal outcome, and `/status`
`last_results` was the only other place it appeared. The 404 wording covers both
causes - a document deleted mid-flight, and an API token whose user cannot see the
document - so if *every* upload skips that way while the archive is otherwise
reachable, check which user owns `PAPERLESS_API_TOKEN` before anything else.

## CI / image

`.github/workflows/build.yml` byte-compiles the code and runs both offline suites on every
push and PR (output stays in the Actions console log), then builds and pushes a multi-arch
image (amd64 + arm64) to GHCR:

```
ghcr.io/azure340/paperless-decider:latest   # also :main and :sha-<commit>
```

After the **first successful run**, set the package to public so any host can pull it without
credentials: GitHub → your profile → **Packages** → `paperless-decider` → **Package settings**
→ Change visibility → **Public**. Until you do, pulls need a `docker login ghcr.io`.

If the build job fails with a permission error, enable it under
Settings → Actions → General → Workflow permissions ("Read repository contents and packages
permissions" is enough). The workflow needs no write access to the repository itself.

## Limits worth knowing

- Tags are chosen from your existing taxonomy only; this service never invents a new tag name
  (that stays the LLM's job).
- Two near-duplicate labels (`Rechnung` next to `Invoice`) cannot be separated by probability.
  Merge them.
- Tags that are really workflow state - an `inbox` tag, `scanned`, `review` - are questions the
  model answers badly, because the *name* looks like a subject. Put them in `DECIDER_SKIP_TAGS`
  and they are never offered; `DECIDER_REVIEW_TAG`/`DECIDER_DONE_TAG` are excluded automatically.
- Your `DECIDER_REVIEW_TAG` / `DECIDER_DONE_TAG` markers are never offered to the model - that
  service bookkeeping is not a subject a document can be about. Without the exclusion the
  model answers "Is this document about needs-review?" (measured live: p=0.79 on an
  insurance policy), which wastes a candidate slot and can file a document for review on
  wording alone.
- `choice`/`noul` questions are English-shaped. For non-English archives, set the
  `DECIDER_Q_*` variables to questions in your language.
- Confidence is calibration against the model's own distribution, not correctness. Tune the
  thresholds against `GET /preview/<id>` on documents you already know the answer to.
- Asking about every tag costs one forward pass per tag. One request for the whole taxonomy is
  the default because the document is prefilled once per request either way: measured with 50
  tags on `tev1:0.8b` + Intel Arc, one request took 9.4 s (a 690-character letter) and 19.9 s
  (a 38 KB policy) versus 10.0 s / 22.3 s in five chunks of 10, and 32.2 s / 132.2 s asked one
  at a time. Chunk with a positive `DECIDER_QUESTIONS_PER_REQUEST` if the taxonomy grows past
  ~100 tags or `DECIDER_TIMEOUT` is tight - a smaller request loses less when it fails, and a
  failed batch falls back to asking those tags one at a time. paperless never waits for any of
  it (the webhook is answered in milliseconds); `DECIDER_WORKERS` is the bulk-ingest knob.
- Tags are multi-label by design. A single `choice` question over the whole taxonomy would be
  one forward pass and one winner, and would silently drop every second tag a document earns.

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
