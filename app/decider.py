#!/usr/bin/env python3
"""paperless-decider - decision-model sidecar for paperless-ngx workflows.

paperless-ngx 3.5.1 sends every AI field (title, dates, tags, correspondents,
document types, storage paths) through one generative LLM chat call. This service
adds the other kind of model: a System One / Jev-style decision model (tev1,
nimble, clef-flash, autotrust-jev, ...) which returns calibrated probabilities for
typed questions in a single forward pass, with no text generation.

Intended chain (two paperless workflows):
  Document added    -> paperless' own AI action writes the title
  Document updated  -> webhook here -> tags back to paperless

Inside this service:
  POST /decide (202 immediately) -> worker optionally waits for the AI title ->
  GET document + taxonomy -> POST /v1/systemone (one question per call) ->
  threshold policy -> PATCH the document with only what changed.

Standard library only, Python 3.11+. Configuration is entirely environment variables.
"""

from __future__ import annotations

import collections
import dataclasses
import http.client
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("decider")
MAX_BODY_BYTES = 1000000
RESULTS = collections.deque(maxlen=100)

SINGLE_FIELDS = ("correspondent", "document_type", "storage_path")
TAXONOMY_ENDPOINTS = {
    "correspondent": "/api/correspondents/",
    "document_type": "/api/document_types/",
    "storage_path": "/api/storage_paths/",
    "tag": "/api/tags/",
}
NONE_KEY = "cnone"
MIN_TOKEN_LENGTH = 3
# paperless' DocumentSerializer.tags is a list of primary keys ([3, 24]); older and
# hand-built payloads use objects ([{"id": 3}]). Both are accepted, because guessing
# wrong here means the PATCH silently deletes the tags of a real document.
TAG_LIST_FIELDS = ("tags",)
WARNED_MISSING_SKIP = set()


class ConfigError(RuntimeError):
    pass


class HTTPProblem(RuntimeError):
    def __init__(self, status, url, detail=""):
        super().__init__("HTTP " + str(status) + " from " + url + ": " + detail[:400])
        self.status = status
        self.url = url
        self.detail = detail


class DecisionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# environment helpers
# --------------------------------------------------------------------------
def _s(name, default=""):
    return (os.environ.get(name) or default).strip()


def _i(name, default):
    raw = _s(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        raise ConfigError(name + " is not an integer: " + repr(raw)) from None


def _f(name, default):
    raw = _s(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        raise ConfigError(name + " is not a number: " + repr(raw)) from None


def _b(name, default):
    raw = _s(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _csv(name, default):
    raw = _s(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclasses.dataclass(frozen=True)
class Config:
    host: str
    port: int
    secret: str
    allow_no_auth: bool
    paperless_url: str
    paperless_token: str
    api_timeout: float
    ollama_url: str
    model: str
    decision_timeout: float
    retries: int
    keep_alive: str
    fields: tuple
    max_state_chars: int
    min_content_chars: int
    thresh_single: float
    thresh_tag: float
    max_single_options: int
    max_tag_options: int
    questions_per_request: int
    grey_zone: float
    overwrite: bool
    review_tag: str
    review_on_no_tags: bool
    done_tag: str
    skip_tags: tuple
    dry_run: bool
    taxonomy_ttl: int
    workers: int
    queue_max: int
    dedupe_seconds: int
    wait_for_title: bool
    title_poll_seconds: float
    title_poll_tries: int
    q_correspondent: str
    q_document_type: str
    q_storage_path: str
    q_tag: str

    @classmethod
    def from_env(cls):
        allowed = tuple(SINGLE_FIELDS) + ("tag",)
        fields = tuple(f for f in _csv("DECIDER_FIELDS", "correspondent,document_type,tag") if f in allowed)
        cfg = cls(
            host=_s("DECIDER_HOST", "0.0.0.0"),
            port=_i("DECIDER_PORT", 8090),
            secret=_s("DECIDER_SHARED_SECRET"),
            allow_no_auth=_b("DECIDER_ALLOW_NO_AUTH", False),
            paperless_url=_s("PAPERLESS_URL", "http://paperless:8000").rstrip("/"),
            paperless_token=_s("PAPERLESS_API_TOKEN"),
            api_timeout=_f("DECIDER_API_TIMEOUT", 30.0),
            ollama_url=_s("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            model=_s("DECIDER_MODEL", "tev1:0.8b"),
            decision_timeout=_f("DECIDER_TIMEOUT", 60.0),
            retries=_i("DECIDER_RETRIES", 2),
            keep_alive=_s("DECIDER_KEEP_ALIVE", "10m"),
            fields=fields,
            max_state_chars=_i("DECIDER_MAX_STATE_CHARS", 6000),
            min_content_chars=_i("DECIDER_MIN_CONTENT_CHARS", 80),
            thresh_single=_f("DECIDER_THRESH_SINGLE", 0.85),
            thresh_tag=_f("DECIDER_THRESH_TAG", 0.80),
            max_single_options=min(25, _i("DECIDER_MAX_SINGLE_OPTIONS", 20)),
            # 0 (the default) = ask about EVERY tag. Ranking stays as the order to ask
            # in and as the knob that re-introduces a shortlist if you set it > 0.
            max_tag_options=max(0, min(25, _i("DECIDER_MAX_TAG_OPTIONS", 0))),
            questions_per_request=max(1, min(25, _i("DECIDER_QUESTIONS_PER_REQUEST", 10))),
            grey_zone=_f("DECIDER_GREY_ZONE", 0.03),
            overwrite=_b("DECIDER_OVERWRITE", False),
            review_tag=_s("DECIDER_REVIEW_TAG"),
            review_on_no_tags=_b("DECIDER_REVIEW_ON_NO_TAGS", False),
            done_tag=_s("DECIDER_DONE_TAG"),
            skip_tags=tuple(_csv("DECIDER_SKIP_TAGS", "")),
            dry_run=_b("DECIDER_DRY_RUN", False),
            taxonomy_ttl=_i("DECIDER_TAXONOMY_TTL", 300),
            workers=max(1, _i("DECIDER_WORKERS", 1)),
            queue_max=_i("DECIDER_QUEUE_MAX", 200),
            dedupe_seconds=_i("DECIDER_DEDUPE_SECONDS", 900),
            wait_for_title=_b("DECIDER_WAIT_FOR_TITLE", False),
            title_poll_seconds=_f("DECIDER_TITLE_POLL_SECONDS", 5.0),
            title_poll_tries=_i("DECIDER_TITLE_POLL_TRIES", 12),
            q_correspondent=_s(
                "DECIDER_Q_CORRESPONDENT",
                "Who sent this document or who is it addressed to (the correspondent)?",
            ),
            q_document_type=_s(
                "DECIDER_Q_DOCUMENT_TYPE",
                "What kind of document is this (the document type)?",
            ),
            q_storage_path=_s(
                "DECIDER_Q_STORAGE_PATH",
                "Where should this document be filed (the storage path)?",
            ),
            q_tag=_s("DECIDER_Q_TAG", "Is this document about {label}?"),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if not self.paperless_token:
            raise ConfigError("PAPERLESS_API_TOKEN is required")
        if not self.paperless_url.startswith(("http://", "https://")):
            raise ConfigError("PAPERLESS_URL must be an http(s) URL")
        if not self.fields:
            raise ConfigError("DECIDER_FIELDS selected no supported field")
        if not self.secret and not self.allow_no_auth:
            raise ConfigError(
                "DECIDER_SHARED_SECRET is unset; set it, or set "
                "DECIDER_ALLOW_NO_AUTH=true if the port is unreachable outside your "
                "docker network",
            )
        for name in ("thresh_single", "thresh_tag"):
            if not 0.0 < getattr(self, name) <= 1.0:
                raise ConfigError(name + " must be in (0, 1]")
        if not 0.0 <= self.grey_zone < 0.5:
            raise ConfigError("grey_zone must be in [0, 0.5)")
        if self.title_poll_seconds <= 0:
            raise ConfigError("DECIDER_TITLE_POLL_SECONDS must be positive")
        if self.title_poll_tries < 1:
            raise ConfigError("DECIDER_TITLE_POLL_TRIES must be at least 1")

    def as_dry_run(self):
        return dataclasses.replace(self, dry_run=True)

    def as_no_wait(self):
        return dataclasses.replace(self, wait_for_title=False)


# --------------------------------------------------------------------------
# HTTP plumbing (stdlib)
# --------------------------------------------------------------------------
def request_json(method, url, body=None, headers=None, timeout=30.0):
    data = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise HTTPProblem(exc.code, url, _read_error_body(exc)) from exc
    except urllib.error.URLError as exc:
        raise HTTPProblem(0, url, str(getattr(exc, "reason", exc))) from exc
    except (OSError, http.client.HTTPException) as exc:
        # An upstream that dies mid-request (Ollama SIGKILLs llama-server when a
        # decision model outgrows its container, which is how tev1:latest fails on
        # a small host) hangs up without a status line. urllib only wraps connect
        # failures, so without this the raw RemoteDisconnected escaped, skipped the
        # retries and aborted the whole decision with a traceback.
        raise HTTPProblem(0, url, type(exc).__name__ + ": " + str(exc)) from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise HTTPProblem(0, url, "non-JSON response: " + str(exc)) from exc


def _read_error_body(exc):
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:
        return str(exc)


# --------------------------------------------------------------------------
# paperless REST client
# --------------------------------------------------------------------------
class Paperless:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.paperless_url
        self.headers = {"Authorization": "Token " + cfg.paperless_token}
        self._tax_cache = {}
        self._lock = threading.Lock()

    def document(self, doc_id):
        url = self.base + "/api/documents/" + str(doc_id) + "/"
        result = request_json("GET", url, headers=self.headers, timeout=self.cfg.api_timeout)
        if not isinstance(result, dict):
            raise HTTPProblem(0, url, "no body")
        return result

    def taxonomy(self, kind):
        """All tags / correspondents / types / storage paths, cached.

        Listed with the sidecar token's own permissions, so it can only ever offer
        (and assign) objects that user may see.
        """
        path = TAXONOMY_ENDPOINTS[kind]
        now = time.monotonic()
        with self._lock:
            hit = self._tax_cache.get(kind)
            if hit and hit[0] > now:
                return hit[1]
        items = []
        url = self.base + path + "?page_size=50"
        pages = 0
        while url and pages < 200:
            page = request_json("GET", url, headers=self.headers, timeout=self.cfg.api_timeout)
            if not isinstance(page, dict):
                break
            items.extend(entry for entry in (page.get("results") or []) if isinstance(entry, dict))
            pages += 1
            nxt = page.get("next")
            url = nxt if isinstance(nxt, str) else None
        with self._lock:
            self._tax_cache[kind] = (now + self.cfg.taxonomy_ttl, items)
        return items

    def patch_document(self, doc_id, payload):
        result = request_json(
            "PATCH",
            self.base + "/api/documents/" + str(doc_id) + "/",
            body=payload,
            headers=self.headers,
            timeout=self.cfg.api_timeout,
        )
        return result if isinstance(result, dict) else {}

    def tag_id(self, name, create=True):
        """Find a tag by exact (accent/case-insensitive) name, optionally create it."""
        wanted = normalize(name)
        for tag in self.taxonomy("tag"):
            if normalize(str(tag.get("name", ""))) == wanted:
                try:
                    return int(tag["id"])
                except (KeyError, TypeError, ValueError):
                    return None
        if not create:
            return None
        created = request_json(
            "POST",
            self.base + "/api/tags/",
            body={"name": name[:128], "match": "", "matching": 0},
            headers=self.headers,
            timeout=self.cfg.api_timeout,
        )
        if isinstance(created, dict) and "id" in created:
            with self._lock:
                self._tax_cache.pop("tag", None)
            LOG.info("created tag %r (id %s)", name, created["id"])
            try:
                return int(created["id"])
            except (TypeError, ValueError):
                return None
        return None


# --------------------------------------------------------------------------
# Ollama System One client
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Decision:
    kind: str
    key: object = None
    label: object = None
    object_id: object = None
    probability: float = 0.0
    confidence: float = 0.0
    probabilities: dict = dataclasses.field(default_factory=dict)
    note: str = ""

    @property
    def applied(self):
        return self.object_id is not None

    def as_dict(self):
        ranked = sorted(self.probabilities.items(), key=lambda kv: -kv[1])[:6]
        return {
            "kind": self.kind,
            "choice": self.label if self.label is not None else self.key,
            "id": self.object_id,
            "probability": round(self.probability, 4),
            "confidence": round(self.confidence, 4),
            "top_probabilities": {k: round(v, 4) for k, v in ranked},
            "note": self.note,
        }


class Ollama:
    def __init__(self, cfg):
        self.cfg = cfg

    def _ask(self, state, questions):
        """One /v1/systemone request carrying one or more typed questions.

        Batching is not free of cost for accuracy: asking fifty noul questions in a
        single request shifts each probability a little, because the questions share a
        forward pass. Measured on Ollama 0.40.16 + tev1:0.8b against this archive, the
        shift is mean 0.002 / worst 0.014 - which is nothing except right next to a
        threshold, where it is decisive. So the batch is the cheap first pass and
        anything within grey_zone of the threshold is asked again on its own, solo, in
        _ask({'q': ...}) form. What is bought: the document is prefilled once per
        request instead of once per tag (50 tags batched ~10-20 s, versus ~7-21 s for
        just 8 tags asked one at a time).
        """
        body = {
            "model": self.cfg.model,
            "state": state,
            "questions": questions,
            "keep_alive": self.cfg.keep_alive,
        }
        last = None
        for attempt in range(self.cfg.retries + 1):
            try:
                response = request_json(
                    "POST",
                    self.cfg.ollama_url + "/v1/systemone",
                    body=body,
                    timeout=self.cfg.decision_timeout,
                )
                if not isinstance(response, dict):
                    raise DecisionError("empty systemone response")
                answers = response.get("answers")
                if not isinstance(answers, dict):
                    raise DecisionError("no answers in response: " + json.dumps(response)[:200])
                missing = sorted(key for key in questions if not isinstance(answers.get(key), dict))
                if missing:
                    raise DecisionError("no answer for " + ",".join(missing))
                return answers
            except (HTTPProblem, DecisionError) as exc:
                last = exc
                # A 4xx is a configuration problem; retrying only burns time.
                if isinstance(exc, HTTPProblem) and 400 <= exc.status < 500:
                    break
                LOG.warning("systemone attempt %s failed: %s", attempt + 1, exc)
                time.sleep(0.5 * (attempt + 1))
        raise DecisionError(
            "systemone failed after " + str(self.cfg.retries + 1) + " attempts: " + str(last)
        )

    def _systemone(self, state, question):
        """A single question, solo - the authoritative form near a threshold."""
        return self._ask(state, {"q": question})["q"]

    def noul_many(self, state, labels):
        """One request, one noul question per label -> {label: probability}.

        Returns {} if the batch could not be answered at all. The caller then asks each
        label solo, which is slower but never wrong: a batching optimisation must not
        be able to lose a document its tags.
        """
        if not labels:
            return {}
        questions = {
            "n%d" % index: {
                "type": "noul",
                "instructions": self.cfg.q_tag.replace("{label}", label),
                "criteria": {
                    "false": "The document is not about " + label + ".",
                    "true": "The document is about " + label + ".",
                },
            }
            for index, label in enumerate(labels)
        }
        try:
            answers = self._ask(state, questions)
        except DecisionError as exc:
            LOG.warning("batched questions failed, asking one at a time: %s", exc)
            return {}
        out = {}
        for index, label in enumerate(labels):
            try:
                out[label] = float(answers["n%d" % index].get("noul"))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out

    def choice(self, state, instructions, options):
        # Single-label by construction, and capped at 26 options by the API: this is
        # the right question for correspondent / document_type / storage_path, never
        # for tags - a contest dilutes the winner (US Taxes read 0.9876 solo as a
        # yes/no, but only 0.7448 as the winner of a 27-option choice).
        answer = self._systemone(
            state,
            {"type": "choice", "instructions": instructions, "criteria": options},
        )
        probs = {}
        for key, value in (answer.get("probabilities") or {}).items():
            try:
                probs[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        key = answer.get("choice")
        key = str(key) if key is not None else None
        return Decision(
            kind="choice",
            key=key,
            probability=float(probs.get(key, 0.0)),
            confidence=float(answer.get("confidence") or 0.0),
            probabilities=probs,
        )

    def noul(self, state, instructions, true_desc, false_desc):
        answer = self._systemone(
            state,
            {
                "type": "noul",
                "instructions": instructions,
                "criteria": {"false": false_desc, "true": true_desc},
            },
        )
        try:
            prob = float(answer.get("noul"))
        except (TypeError, ValueError):
            prob = 0.0
        return Decision(kind="noul", probability=prob, confidence=prob)

    def model_capabilities(self):
        try:
            response = request_json(
                "POST",
                self.cfg.ollama_url + "/api/show",
                body={"model": self.cfg.model},
                timeout=self.cfg.decision_timeout,
            )
        except HTTPProblem as exc:
            LOG.warning("could not probe model %s: %s", self.cfg.model, exc)
            return []
        if isinstance(response, dict):
            return [str(cap) for cap in (response.get("capabilities") or [])]
        return []


# --------------------------------------------------------------------------
# text helpers and candidate shortlisting
# --------------------------------------------------------------------------
def normalize(text):
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def tokenize(text):
    """Words of at least MIN_TOKEN_LENGTH that contain a letter.

    Hand-rolled rather than regex so non-ASCII scripts survive and so there is
    no escaping to get wrong: split on everything that is not alphanumeric.
    """
    words = []
    current = []
    for ch in normalize(text):
        if ch.isalnum() and ch != "_":
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return {
        word
        for word in words
        if len(word) >= MIN_TOKEN_LENGTH and any(ch.isalpha() for ch in word)
    }


STEM_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "es", "s")


def stem(word):
    """A cheap English lemma key so `taxes` matches `tax` and `licenses` `licensing`.

    Deliberately dumb - one suffix stripped, stem kept at 3+ characters - and applied to
    tag names and document words alike, so a wrong guess is symmetric: it stops matching
    rather than matching something unrelated.
    """
    for suffix in STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            return base if len(base) >= 3 else word
    return word


def shortlist(candidates, text, limit, exclude=frozenset()):
    """Rank taxonomy entries against the document and keep the top `limit`.

    `limit=None` keeps every candidate: asked tags are batched now, so ranking is no
    longer a budget that decides which tags get considered at all - it only decides the
    order. That is deliberate. While ranking was a budget, a lexical miss silently cost
    documents their correct tag ("US Taxes" at p=0.9876, never asked, because nothing in
    a 690-character IRS letter contains the string "us taxes").

    Signal, strongest first: the entry's full name occurs verbatim in the document, its
    stem-matched words overlap the document's words, and (weakly) how often it is used
    elsewhere in the archive.
    """
    norm_text = normalize(text)
    doc_tokens = {stem(word) for word in tokenize(text)}
    scored = []
    for cand in candidates:
        try:
            object_id = int(cand.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(cand.get("name", "")).strip()
        if not name or object_id in exclude:
            continue
        score = 0.0
        norm_name = normalize(name)
        if len(norm_name) > 2 and norm_name in norm_text:
            score += 4.0
        name_tokens = {stem(word) for word in tokenize(name)}
        if name_tokens and doc_tokens:
            score += 2.0 * (len(name_tokens & doc_tokens) / len(name_tokens))
        try:
            count = max(0.0, float(cand.get("document_count") or 0))
        except (TypeError, ValueError):
            count = 0.0
        score += 0.15 * min(3.0, (count + 1) ** 0.5 - 1.0)
        scored.append((score, count, {"id": object_id, "name": name, "score": round(score, 3)}))

    # Ties break on how much a tag is actually used, then on its name. Without the usage
    # term a document whose text matches no tag name degenerates into alphabetical order,
    # which silently drops the correct tag from DECIDER_MAX_TAG_OPTIONS (measured live).
    scored.sort(key=lambda pair: (-pair[0], -pair[1], pair[2]["name"].casefold()))
    return [item for _score, _count, item in scored[:limit]]


def tag_ids(doc, key="tags"):
    """Tag ids on a document, from either shape paperless can return.

    Returns None (not an empty set) when the field is missing entirely, so a caller
    can tell 'no tags' apart from 'I could not read the tags'.
    """
    raw = doc.get(key)
    if raw is None:
        return None
    ids = set()
    for entry in raw:
        if isinstance(entry, dict):
            entry = entry.get("id")
        try:
            ids.add(int(entry))
        except (TypeError, ValueError):
            continue
    return ids


def document_stem(doc):
    filename = str(doc.get("original_file_name") or doc.get("filename") or "")
    return normalize(os.path.splitext(os.path.basename(filename))[0])


def title_is_placeholder(doc):
    """True when the title still looks like paperless' filename-derived default.

    Only used to decide whether to wait for the paperless AI action to finish.
    Deliberately approximate: a genuinely descriptive filename reads as "already
    titled", which simply means we do not wait.
    """
    title = normalize(str(doc.get("title") or ""))
    stem = document_stem(doc)
    if not title:
        return True
    if not stem:
        return False
    if title == stem:
        return True
    title_tokens = tokenize(title)
    if not title_tokens:
        return False
    # paperless strips some date/ASN prefixes, so compare on shared words too
    return len(title_tokens & tokenize(stem)) == len(title_tokens)


def build_state(doc, max_chars):
    filename = str(doc.get("original_file_name") or doc.get("filename") or "")
    title = str(doc.get("title") or "") or os.path.splitext(filename)[0]
    content = str(doc.get("content") or "").strip()
    budget = max(200, max_chars - len(title) - len(filename) - 24)
    newline = chr(10)
    return "TITLE: " + title + newline + "FILENAME: " + filename + newline + newline + content[:budget]


# --------------------------------------------------------------------------
# decision policy
# --------------------------------------------------------------------------
def decide_single_field(cfg, pl, ollama, field, state, question, existing_id):
    if existing_id and not cfg.overwrite:
        return Decision(kind="skip", object_id=existing_id, note="already set, overwrite disabled")
    candidates = shortlist(pl.taxonomy(field), state, cfg.max_single_options)
    if len(candidates) < 2:
        return Decision(kind="skip", note="only " + str(len(candidates)) + " candidate(s) in archive")
    options = {}
    by_key = {}
    for index, cand in enumerate(candidates):
        key = "c" + str(index + 1)
        options[key] = cand["name"]
        by_key[key] = cand
    options[NONE_KEY] = "None of the listed options fits this document."
    decision = ollama.choice(state, question, options)
    if decision.key == NONE_KEY:
        decision.note = "model answered 'none of these'"
        return decision
    cand = by_key.get(decision.key or "")
    if cand is None:
        decision.note = "model returned unknown option key: " + repr(decision.key)
        decision.probability = 0.0
        return decision
    # The returned label is never trusted, only our own key -> id mapping.
    decision.label = cand["name"]
    decision.object_id = cand["id"]
    if decision.probability < cfg.thresh_single:
        decision.note = "below threshold %.2f" % cfg.thresh_single
    return decision


def marker_tag_ids(cfg, pl):
    """Ids of the bookkeeping tags this service writes for its own state machine.

    `auto-tagged` / `needs-review` are metadata, not subjects a document can be
    about. Left in the taxonomy the model is asked things like "Is this document
    about needs-review?" - measured live: p=0.79 on an insurance policy, almost
    enough to file a document as needing review because its wording sounded
    review-ish - and each one also burns a candidate slot the real tags needed.
    """
    wanted = {normalize(cfg.review_tag), normalize(cfg.done_tag)} - {""}
    if not wanted:
        return set()
    ids = set()
    for tag in pl.taxonomy("tag"):
        if normalize(str(tag.get("name", ""))) in wanted:
            try:
                ids.add(int(tag["id"]))
            except (TypeError, ValueError, KeyError):
                continue
    return ids


def skip_tag_ids(cfg, pl):
    """Ids for DECIDER_SKIP_TAGS: taxonomy entries that must never be subjects.

    The bookkeeping tags are handled by marker_tag_ids; this covers the rest -
    above all an `inbox` tag. Without it the model gets asked "Is this document
    about inbox?", and an affirmative answer re-files a document the human just
    cleared out of the review view. Names are matched accent/case-insensitively;
    one that matches nothing is warned about once, because a silent typo here
    would leave exactly the tag you meant to protect in play.
    """
    wanted = {normalize(name) for name in cfg.skip_tags} - {""}
    if not wanted:
        return set()
    ids = set()
    seen = set()
    for tag in pl.taxonomy("tag"):
        name = normalize(str(tag.get("name", "")))
        if name in wanted:
            seen.add(name)
            try:
                ids.add(int(tag["id"]))
            except (TypeError, ValueError, KeyError):
                continue
    for missing in sorted(wanted - seen):
        if missing not in WARNED_MISSING_SKIP:
            WARNED_MISSING_SKIP.add(missing)
            LOG.warning("DECIDER_SKIP_TAGS entry %r matches no visible tag name", missing)
    return ids


def ask_tag_probabilities(cfg, ollama, state, labels):
    """Probability per label, in as few requests as possible.

    Returns {label: (probability, re_asked_alone)}. A label is re-asked solo when the
    batch has no answer for it, and when its batched number sits within grey_zone of
    the threshold - that is the only band where the batching shift can flip a decision,
    and a flip there is a silent wrong answer.
    """
    def ask_alone(label):
        return ollama.noul(
            state,
            cfg.q_tag.replace("{label}", label),
            "The document is about " + label + ".",
            "The document is not about " + label + ".",
        ).probability

    probs = {}
    if cfg.questions_per_request > 1:
        for start in range(0, len(labels), cfg.questions_per_request):
            chunk = labels[start:start + cfg.questions_per_request]
            probs.update(ollama.noul_many(state, chunk))
    reasked = set()
    for label in labels:
        if label not in probs:
            probs[label] = ask_alone(label)
        elif cfg.grey_zone and abs(probs[label] - cfg.thresh_tag) <= cfg.grey_zone:
            probs[label] = ask_alone(label)
            reasked.add(label)
    return {label: (probs.get(label, 0.0), label in reasked) for label in labels}


def decide_tags(cfg, pl, ollama, state, current_tag_ids):
    blocked = set(current_tag_ids) | marker_tag_ids(cfg, pl) | skip_tag_ids(cfg, pl)
    candidates = shortlist(pl.taxonomy("tag"), state, cfg.max_tag_options or None,
                           exclude=blocked)
    labels = [cand['name'] for cand in candidates]
    scored = ask_tag_probabilities(cfg, ollama, state, labels)
    decisions = []
    for cand in candidates:
        label = cand["name"]
        probability, re_asked = scored.get(label, (0.0, False))
        decision = Decision(kind="noul", probability=probability, confidence=probability)
        decision.key = "tag:" + str(cand["id"])
        decision.label = label
        decision.object_id = cand["id"]
        notes = []
        if re_asked:
            notes.append("near threshold in batch, re-asked alone")
        if probability < cfg.thresh_tag:
            notes.append("below threshold %.2f" % cfg.thresh_tag)
        decision.note = "; ".join(notes)
        decisions.append(decision)
    return decisions


def reload_for_title(doc_id, cfg, pl, report):
    """Optionally wait for paperless' AI title before deciding.

    Returns the refreshed document, or None if it disappeared meanwhile.
    """
    doc = pl.document(doc_id)
    if not cfg.wait_for_title:
        return doc
    waited = 0.0
    for _attempt in range(cfg.title_poll_tries):
        if not title_is_placeholder(doc):
            break
        time.sleep(cfg.title_poll_seconds)
        waited += cfg.title_poll_seconds
        try:
            doc = pl.document(doc_id)
        except HTTPProblem as exc:
            if exc.status == 404:
                return None
            raise
    else:
        if title_is_placeholder(doc):
            report["title_wait"] = "no AI title within the wait budget, proceeding anyway"
    if waited:
        report["title_wait_seconds"] = round(waited, 1)
    return doc


def process_document(doc_id, cfg, pl, ollama):
    """Decide one document and (unless dry run) write back what changed."""
    started = time.monotonic()
    report = {"document_id": doc_id, "dry_run": cfg.dry_run, "decisions": {}}
    try:
        doc = reload_for_title(doc_id, cfg, pl, report)
    except HTTPProblem as exc:
        if exc.status == 404:
            report["skipped"] = "document no longer exists"
            return report
        raise
    if doc is None:
        report["skipped"] = "document deleted while waiting for its title"
        return report

    content = str(doc.get("content") or "").strip()
    report["title"] = doc.get("title")
    if len(content) < cfg.min_content_chars:
        report["skipped"] = "content under " + str(cfg.min_content_chars) + " chars (blank scan?)"
        return report

    state = build_state(doc, cfg.max_state_chars)
    existing_set = tag_ids(doc)
    if existing_set is None:
        # Without this the write below would replace the whole list with only what
        # the model picked, i.e. delete the document's existing tags.
        report["skipped"] = "document had no readable tags field - refusing to write"
        return report
    existing_tags = sorted(existing_set)

    payload = {}
    decisions = report["decisions"]
    unfiled = []

    questions = {
        "correspondent": cfg.q_correspondent,
        "document_type": cfg.q_document_type,
        "storage_path": cfg.q_storage_path,
    }
    for field in SINGLE_FIELDS:
        if field not in cfg.fields:
            continue
        existing = doc.get(field)
        existing_id = int(existing) if isinstance(existing, int) else None
        decision = decide_single_field(cfg, pl, ollama, field, state, questions[field], existing_id)
        decisions[field] = decision.as_dict()
        if decision.kind == "skip":
            continue
        if decision.applied and decision.probability >= cfg.thresh_single:
            payload[field] = decision.object_id
        else:
            # We were asked to fill this field and could not do it confidently:
            # precisely what the review queue is for.
            unfiled.append(field)

    if "tag" in cfg.fields:
        tag_decisions = decide_tags(cfg, pl, ollama, state, set(existing_tags))
        decisions["tags"] = [d.as_dict() for d in tag_decisions]
        added = [d.object_id for d in tag_decisions if d.applied and d.probability >= cfg.thresh_tag]
        if added:
            payload["tags"] = sorted(set(existing_tags) | set(added))
        elif tag_decisions and cfg.review_on_no_tags:
            # Nothing in the taxonomy fitted: let a human look rather than filing
            # the document silently untagged.
            unfiled.append("tag")

    needs_review = bool(unfiled)
    report["needs_review"] = needs_review
    report["unresolved_fields"] = unfiled

    marker_ids = set(payload.get("tags") or existing_tags)
    wanted_marker = (cfg.review_tag if needs_review else cfg.done_tag) or None
    if wanted_marker:
        marker_id = pl.tag_id(wanted_marker)
        if marker_id is not None and marker_id not in marker_ids:
            marker_ids.add(marker_id)
            payload["tags"] = sorted(marker_ids)
            report["marker_tag"] = wanted_marker

    report["would_patch"] = payload
    report["duration_ms"] = int((time.monotonic() - started) * 1000)

    if not payload:
        report["result"] = "nothing to apply"
        return report
    if cfg.dry_run:
        report["result"] = "dry run"
        return report

    # The read above can be minutes old (we waited for the AI title first), and a tags
    # PATCH replaces the whole list. Re-read and merge so this write can only ADD -
    # anything paperless did in the meantime (inbox tags, a human edit, the classifier)
    # has to survive it.
    try:
        fresh = pl.document(doc_id)
    except HTTPProblem as exc:
        if exc.status == 404:
            report["skipped"] = "document disappeared before the write"
            report["result"] = "nothing to apply"
            return report
        raise
    now_tags = tag_ids(fresh)
    if now_tags is None:
        report["skipped"] = "re-read had no readable tags field - refusing to write"
        report["result"] = "nothing to apply"
        return report
    if "tags" in payload:
        merged = sorted(now_tags | set(payload["tags"]))
        report["tags_added_by_others"] = sorted(now_tags - set(payload["tags"]))
        if merged == sorted(now_tags):
            payload.pop("tags")
        else:
            payload["tags"] = merged
    for field in SINGLE_FIELDS:
        if field in payload and fresh.get(field) not in (None, ""):
            payload.pop(field)
            report.setdefault("skipped_stale_fields", []).append(field)
    if not payload:
        report["result"] = "nothing to apply after re-read"
        return report

    pl.patch_document(doc_id, payload)
    report["applied"] = True
    report["result"] = "patched"
    LOG.info(
        "document %s: patched %s (%dms)",
        doc_id,
        ", ".join(str(key) + "=" + str(value) for key, value in payload.items()),
        report["duration_ms"],
    )
    return report


# --------------------------------------------------------------------------
# queue + HTTP server
# --------------------------------------------------------------------------
class Worker:
    def __init__(self, cfg, pl, ollama):
        self.cfg = cfg
        self.pl = pl
        self.ollama = ollama
        self.queue = queue.Queue(maxsize=cfg.queue_max)
        self.inflight = {}
        self.lock = threading.Lock()
        self.dropped = 0
        self.failed = 0

    def submit(self, doc_id):
        cutoff = time.monotonic() - self.cfg.dedupe_seconds
        with self.lock:
            self.inflight = {k: v for k, v in self.inflight.items() if v > cutoff}
            if doc_id in self.inflight:
                return "duplicate"
            self.inflight[doc_id] = time.monotonic()
        try:
            self.queue.put_nowait(doc_id)
        except queue.Full:
            self.dropped += 1
            with self.lock:
                self.inflight.pop(doc_id, None)
            LOG.error("queue full, dropped document %s", doc_id)
            return "full"
        return "queued"

    def forget(self, doc_id):
        with self.lock:
            self.inflight.pop(doc_id, None)

    def run(self):
        while True:
            doc_id = self.queue.get()
            try:
                RESULTS.appendleft(process_document(doc_id, self.cfg, self.pl, self.ollama))
            except Exception as exc:
                self.failed += 1
                LOG.exception("decision failed for document %s", doc_id)
                # /status is the only place an operator can see WHY a document was
                # never tagged, so keep one readable line. These messages carry URLs
                # and response bodies, never the request headers, so no token leaks.
                RESULTS.appendleft(
                    {
                        "document_id": doc_id,
                        "error": type(exc).__name__ + ": " + " ".join(str(exc).split())[:300],
                    },
                )
                # Let a later webhook delivery (or a manual retry) try again.
                self.forget(doc_id)
            finally:
                self.queue.task_done()


class Handler(BaseHTTPRequestHandler):
    server_version = "paperless-decider/1.0"
    protocol_version = "HTTP/1.1"

    cfg = None
    worker = None
    pl = None
    ollama = None

    def _send(self, status, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self.cfg.secret:
            return True
        supplied = self.headers.get("X-Decider-Token") or ""
        if not supplied:
            auth = self.headers.get("Authorization") or ""
            supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return supplied == self.cfg.secret

    def _reject_unless_authorized(self, path):
        """Every endpoint but /healthz leaks document titles, so it is guarded.

        The container's published port is loopback-only by default, but the
        docker network is shared with paperless and anything else on it.
        """
        if path == "/healthz" or self._authorized():
            return True
        LOG.warning("401 from %s on %s (missing or wrong X-Decider-Token)", self.client_address[0], path)
        self._send(401, {"error": "unauthorized"})
        return False

    @staticmethod
    def _extract_doc_id(payload):
        for key in ("id", "document_id", "doc_id"):
            value = payload.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                return int(str(value).strip())
            except ValueError:
                continue
        return None

    def _read_payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, MAX_BODY_BYTES)) if length else b""
        if not raw:
            return {}, True
        text = raw.decode("utf-8", "replace")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            # paperless can send a templated string body instead of JSON params
            match = re.search("(?:id|document_id)[^0-9]{0,4}([0-9]{1,9})", text)
            if match:
                return {"id": int(match.group(1))}, True
            return None, False
        if isinstance(decoded, dict):
            return decoded, True
        return {"id": decoded}, True

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path not in ("/decide", "/decide/sync", "/preview"):
            self._send(
                404,
                {
                    "error": "unknown path",
                    "endpoints": ["/decide", "/decide/sync", "/preview", "/status", "/healthz"],
                },
            )
            return
        if not self._reject_unless_authorized(path):
            return
        payload, ok = self._read_payload()
        if not ok:
            self._send(400, {"error": "body was neither JSON nor contained an id"})
            return
        doc_id = self._extract_doc_id(payload or {})
        if doc_id is None:
            self._send(422, {"error": "no document id; set the workflow webhook param id = {id}"})
            return

        sync = path in ("/preview", "/decide/sync") or query.get("sync") == ["1"]
        if sync:
            # Preview never writes and never waits, so a manual probe answers
            # immediately even while the AI title is still on its way.
            run_cfg = self.cfg.as_no_wait().as_dry_run() if path == "/preview" else self.cfg
            try:
                report = process_document(doc_id, run_cfg, self.pl, self.ollama)
            except Exception as exc:
                LOG.exception("sync decision failed for %s", doc_id)
                self._send(502, {"error": str(exc)[:300], "document_id": doc_id})
                return
            if path == "/preview":
                report["note"] = "preview never writes and ignores DECIDER_WAIT_FOR_TITLE"
            self._send(200, report)
            return

        # paperless' webhook client gives up after 5.0 s: never decide inline.
        status_text = self.worker.submit(doc_id)
        self._send(
            202 if status_text != "full" else 503,
            {"document_id": doc_id, "status": status_text, "queue": self.worker.queue.qsize()},
        )

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._reject_unless_authorized(path):
            return
        if path == "/healthz":
            self._send(
                200,
                {
                    "ok": True,
                    "model": self.cfg.model,
                    "queue": self.worker.queue.qsize(),
                    "wait_for_title": self.cfg.wait_for_title,
                },
            )
            return
        if path == "/status":
            self._send(
                200,
                {
                    "queue": self.worker.queue.qsize(),
                    "dropped": self.worker.dropped,
                    "failed": self.worker.failed,
                    "fields": list(self.cfg.fields),
                    "dry_run": self.cfg.dry_run,
                    "overwrite": self.cfg.overwrite,
                    "wait_for_title": self.cfg.wait_for_title,
                    "model": self.cfg.model,
                    "thresholds": {"single": self.cfg.thresh_single, "tag": self.cfg.thresh_tag},
                    "options": {"single": self.cfg.max_single_options, "tag": self.cfg.max_tag_options},
                    "questions_per_request": self.cfg.questions_per_request,
                    "grey_zone": self.cfg.grey_zone,
                    "skip_tags": list(self.cfg.skip_tags),
                    "last_results": list(RESULTS)[:15],
                },
            )
            return
        match = re.fullmatch("/preview/([0-9]+)", path)
        if match:
            try:
                report = process_document(
                    int(match.group(1)),
                    self.cfg.as_no_wait().as_dry_run(),
                    self.pl,
                    self.ollama,
                )
                report["note"] = "preview never writes and ignores DECIDER_WAIT_FOR_TITLE"
                self._send(200, report)
            except Exception as exc:
                self._send(502, {"error": str(exc)[:300]})
            return
        self._send(404, {"error": "unknown path"})

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.client_address[0], fmt % args)


def main():
    logging.basicConfig(
        level=getattr(logging, _s("DECIDER_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    pl = Paperless(cfg)
    ollama = Ollama(cfg)
    caps = ollama.model_capabilities()
    if caps and "decision" not in caps:
        LOG.warning(
            "model %s does not advertise the 'decision' capability (found: %s); "
            "/v1/systemone will probably fail",
            cfg.model,
            ",".join(caps),
        )
    elif caps:
        LOG.info("model %s capabilities: %s", cfg.model, ",".join(caps))
    if cfg.skip_tags:
        LOG.info("never offered as tag candidates: %s", ",".join(cfg.skip_tags))

    worker = Worker(cfg, pl, ollama)
    for index in range(cfg.workers):
        threading.Thread(target=worker.run, name="decider-" + str(index), daemon=True).start()

    Handler.cfg = cfg
    Handler.worker = worker
    Handler.pl = pl
    Handler.ollama = ollama

    try:
        httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    except OSError as exc:
        LOG.error("cannot bind %s:%s: %s", cfg.host, cfg.port, exc)
        return 2
    httpd.daemon_threads = True

    def shutdown(_signum, _frame):
        LOG.info("shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info(
        "listening on %s:%s | fields=%s thresh(single=%.2f tag=%.2f) overwrite=%s "
        "dry_run=%s wait_for_title=%s model=%s auth=%s tags_asked=%s questions_per_request=%d grey_zone=%.3f",
        cfg.host,
        cfg.port,
        ",".join(cfg.fields),
        cfg.thresh_single,
        cfg.thresh_tag,
        cfg.overwrite,
        cfg.dry_run,
        cfg.wait_for_title,
        cfg.model,
        "token" if cfg.secret else "OPEN",
        "all" if not cfg.max_tag_options else cfg.max_tag_options,
        cfg.questions_per_request,
        cfg.grey_zone,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
