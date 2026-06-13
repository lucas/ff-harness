"""Domain bundle for the website-builder agent.

Layer 3 — declarative configuration + the two factories the API layer calls
to materialize an OrchestratorConfig + stage→Worker map. Reads env at factory
time so callers don't have to thread MODEL_* args.

This module may import stdlib + pydantic + every `harness.services.*` and
`harness.models.*`. It MUST NOT import from `harness.api`.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from harness.models.enums import Stage
from harness.services.llm import ChatResponse, OpenRouterClient
from harness.services.llm_worker import LLMWorker
from harness.services.orchestrator import OrchestratorConfig
from harness.services.worker import Worker


# ---------------------------------------------------------------------------
# Declared knobs
# ---------------------------------------------------------------------------


ALLOW_LIST: list[str] = [
    "ask_user",
    "request_approval",
    "save_business_brief",
    "render_mockup",
    "read_file",
    "write_file",
    "list_files",
]

CHECKPOINT_SET: list[str] = [
    "business_brief_confirmed",
    "mockup_renders",
    "mockup_approved",
    "site_valid",
    "seo_artifacts_present",
]

CHAT_STAGES: set[str] = {Stage.BOOTSTRAP.value, Stage.MOCKUP.value}
CODE_STAGES: set[str] = {Stage.BUILD.value}

DEFAULT_TURN_CAP = 10
DEFAULT_SPEND_CAP_USD = 1.0

# Default OpenRouter models — overridden by env at factory call time.
_DEFAULT_MODEL_CHAT = "deepseek/deepseek-v4-flash:free"
_DEFAULT_MODEL_CHAT_FALLBACK = "deepseek/deepseek-v4-flash"
_DEFAULT_MODEL_CODE = "qwen/qwen3-coder:free"
_DEFAULT_MODEL_CODE_FALLBACK = ""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


# Verbatim content from skills/bootstrap.md, used inside build_system_prompt.
_BOOTSTRAP_SKILL = """\
Guide a non-technical small-business owner from a vague idea to a confirmed
Business Brief. The user may be unsure about everything, so every question
carries a sensible default — present it, let them accept ("sounds good") or
change it, and move on. Ask in small batches (2-4 questions), never all at
once. Use ask_user to ask and request_approval to confirm the final brief.

How to run it
1. Greet briefly; ask what the business is. Infer the industry if you can.
2. Load the matching industry default profile (below) to pre-fill aesthetic,
   colors, pages, and CTA.
3. Walk the question groups, always showing the default. Capture answers
   into the Business Brief.
4. When complete, present a one-screen summary of the brief and get sign-off
   with request_approval(subject='business_brief', details=<the brief>).
   Do NOT start building until approved.
5. The approved brief becomes part of the session context (persisted).

Industry default profiles
- Restaurant: warm/inviting; palette deep red + cream; pages Home/Menu/About/
  Hours & Location/Contact; CTA Reserve a table.
- Coffee shop: cozy/rustic; warm browns + cream; pages Home/Menu/About/
  Location/Instagram; CTA Visit us / Order.
- Photographer: minimal/gallery-first; monochrome + one accent; pages Home/
  Portfolio/About/Contact; CTA Book a shoot.
- Home service (HVAC, lawn care, plumbing): clean/trustworthy; blue or green
  + white; pages Home/Services/Service Areas/Reviews/Contact; CTA Call for a
  free quote.
- Influencer: bold/modern; high-contrast accent; pages Home/About/Media Kit/
  Contact/Socials; CTA Follow / Work with me.
- Other: clean/professional; neutral blue-gray; pages Home/About/Services/
  Contact; CTA Contact us.
"""


def build_system_prompt() -> str:
    """Build the base system prompt used for every worker turn.

    The orchestrator appends the per-session business brief separately, so
    this prompt is constant across sessions.
    """
    return (
        "# Role\n"
        "You are the website-builder agent. You help a small-business owner\n"
        "go from a vague idea to a published HTML+CSS website by following a\n"
        "fixed flow: bootstrap (capture a Business Brief) -> mockup (ASCII\n"
        "layout, human-approved) -> build (write_file the HTML/CSS) -> final.\n"
        "\n"
        "# Available tools\n"
        "- ask_user(question, options?) — ask the human one question. Pauses\n"
        "  the loop until they answer. When the question has discrete sensible\n"
        "  choices (yes/no, an industry, a palette, a CTA verb, etc.), pass\n"
        "  them as `options: list[str]` — the UI renders each as a clickable\n"
        "  button plus an automatic 'Other…' button for freeform input. Do NOT\n"
        "  write 'please specify' or 'or other' in the question — the UI\n"
        "  handles that. Omit `options` only when the answer is genuinely\n"
        "  open-ended (a business name, a phone number, a tagline).\n"
        "- request_approval(subject, details?) — ask the human to approve a\n"
        "  decision (brief, mockup, etc.). Pauses until they decide. Use\n"
        "  request_approval (not ask_user) when seeking sign-off on a\n"
        "  structured object like the Business Brief — the UI renders the\n"
        "  `details` dict as a clean card.\n"
        "- save_business_brief(brief) — persist the brief into session memory\n"
        "  after the user has explicitly approved it. Call this once approval\n"
        "  is granted so downstream tools (render_mockup uses it for theming)\n"
        "  see the user's actual business name, palette, and other details.\n"
        "  The brief should be the FULL collected dict (name, industry,\n"
        "  tagline, contact, palette, pages, primary_cta, etc).\n"
        "- render_mockup(layout_spec) — render the layout as an ASCII mockup.\n"
        "- read_file(path) — read a file from the site sandbox.\n"
        "- write_file(path, content) — write a file to the site sandbox.\n"
        "  Triggers automatic validation, SEO regen, and a git commit.\n"
        "- list_files(path?) — list files in the site sandbox.\n"
        "\n"
        "# Response envelope (MANDATORY)\n"
        "Respond with EXACTLY ONE JSON object matching one of these shapes.\n"
        "No prose, no markdown, no code fences — JSON only.\n"
        '  {"type":"tool_call","tool":"<name>","args":{...}}\n'
        '  {"type":"final","summary":"<one-paragraph summary>"}\n'
        '  {"type":"escalate","reason":"<why you cannot proceed>"}\n'
        "\n"
        "# Examples\n"
        "Example 1a (open-ended question — no options):\n"
        '{"type":"tool_call","tool":"ask_user","args":{"question":'
        '"What is the business name?"}}\n'
        "\n"
        "Example 1b (multiple-choice question — options become buttons + Other…):\n"
        '{"type":"tool_call","tool":"ask_user","args":{"question":'
        '"What industry is this?","options":["Restaurant","Coffee shop",'
        '"Photographer","Home service","Influencer"]}}\n'
        "\n"
        "Example 1d (persisting the brief after the user said 'Looks good!'):\n"
        '{"type":"tool_call","tool":"save_business_brief","args":{"brief":'
        '{"name":"Jim\'s HVAC","industry":"Home service","palette":'
        '{"primary":"#0055aa","secondary":"#ffffff"},'
        '"primary_cta":"Call for a free quote",'
        '"pages":["Home","Services","Contact"]}}}\n'
        "\n"
        "Example 2 (writing a tiny HTML file):\n"
        '{"type":"tool_call","tool":"write_file","args":{"path":"index.html",'
        '"content":"<!DOCTYPE html><html lang=\\"en\\"><head>'
        '<meta name=\\"viewport\\" content=\\"width=device-width\\">'
        '<title>Hi</title></head><body><h1>Hello</h1></body></html>"}}\n'
        "\n"
        "Example 3 (finishing):\n"
        '{"type":"final","summary":"Site built, validated, committed."}\n'
        "\n"
        "# Flow guidance\n"
        "1. Bootstrap stage: collect/confirm the business brief via batched\n"
        "   ask_user rounds (2-4 questions each), then seek sign-off. Once the\n"
        "   user has approved the brief (via 'Looks good!' / 'Yes' / 'Approved'\n"
        "   / etc.), IMMEDIATELY call save_business_brief(brief={...full\n"
        "   collected dict...}). Then call request_approval(subject='business_brief',\n"
        "   details=brief) to formally close out the bootstrap stage.\n"
        "2. Mockup stage: design the layout, call render_mockup with the\n"
        "   layout_spec (sections list + primary_cta), then request_approval\n"
        "   (subject='mockup').\n"
        "3. Build stage: write_file index.html and styles.css. Build the site\n"
        "   in short, focused iterations — start with a minimal HTML skeleton,\n"
        "   then refine based on validation feedback from the harness. The\n"
        "   harness auto-runs HTML5/CSS validators and regenerates sitemap.xml,\n"
        "   robots.txt, and llms.txt after every write. Read state.last_alarm\n"
        "   to see validator failures and re-write to fix them.\n"
        "4. When the site is valid and committed, emit a final envelope.\n"
        "\n"
        "## Bootstrap protocol\n"
        + _BOOTSTRAP_SKILL
    )


# ---------------------------------------------------------------------------
# Restaurant seed brief (for fast demos)
# ---------------------------------------------------------------------------


RESTAURANT_SEED_BRIEF: dict = {
    "name": "Maria's Pizzeria",
    "industry": "restaurant",
    "tagline": "Wood-fired pizza, made by hand.",
    "contact": {
        "phone": "555-0142",
        "email": "hello@marias.example",
        "address": "221 Mulberry St, Springfield",
    },
    "hours": {
        "mon_thu": "11:00-21:00",
        "fri_sat": "11:00-22:00",
        "sun": "12:00-20:00",
    },
    "service_areas": ["Springfield"],
    "aesthetic": "warm/inviting",
    "palette": {"primary": "#7B1E1E", "secondary": "#F5E9DA"},
    "audience": "local diners & families",
    "pages": ["Home", "Menu", "About", "Hours & Location", "Contact"],
    "primary_cta": "Reserve a table",
    "socials": {"instagram": "@marias.pizzeria"},
    "logo": "wordmark",
}


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def sandbox_path_for(session_id: str, sandbox_root: Path) -> Path:
    """Return `sandbox_root / session_id`. Caller is responsible for mkdir."""
    return Path(sandbox_root) / session_id


def make_worker_for_stage(
    *,
    session_id: str,
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    llm_client: OpenRouterClient,
    model_chat: str | None = None,
    model_chat_fallback: str | None = None,
    model_code: str | None = None,
    model_code_fallback: str | None = None,
) -> Callable[[str], Worker]:
    """Return a stage→Worker function bound to this session's connections.

    Constructs one LLMWorker per (primary, fallback) pair (chat + code) and
    dispatches by stage via CHAT_STAGES / CODE_STAGES. Unknown stages fall
    back to the chat worker — bootstrap/mockup/build cover all v1 stages.

    Reads env defaults when model args are None or empty.
    """
    chat_primary = model_chat or os.environ.get("MODEL_CHAT") or _DEFAULT_MODEL_CHAT
    chat_fallback_raw = (
        model_chat_fallback
        if model_chat_fallback is not None
        else os.environ.get("MODEL_CHAT_FALLBACK", _DEFAULT_MODEL_CHAT_FALLBACK)
    )
    code_primary = model_code or os.environ.get("MODEL_CODE") or _DEFAULT_MODEL_CODE
    code_fallback_raw = (
        model_code_fallback
        if model_code_fallback is not None
        else os.environ.get("MODEL_CODE_FALLBACK", _DEFAULT_MODEL_CODE_FALLBACK)
    )

    chat_fallback = chat_fallback_raw if chat_fallback_raw else None
    code_fallback = code_fallback_raw if code_fallback_raw else None

    chat_worker = LLMWorker(
        primary=chat_primary,
        fallback=chat_fallback,
        llm_client=llm_client,
        core_conn=core_conn,
        session_conn=session_conn,
        session_id=session_id,
    )
    code_worker = LLMWorker(
        primary=code_primary,
        fallback=code_fallback,
        llm_client=llm_client,
        core_conn=core_conn,
        session_conn=session_conn,
        session_id=session_id,
    )

    def worker_for_stage(stage: str) -> Worker:
        if stage in CODE_STAGES:
            return code_worker
        # CHAT_STAGES + any unknown stage routes to chat.
        return chat_worker

    return worker_for_stage


def make_orchestrator_config(
    *,
    session_id: str,
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    llm_client: OpenRouterClient,
    sandbox_root: Path,
    core_db_path: Path,
    sessions_dir: Path,
    worker_for_stage_override: Callable[[str], Worker] | None = None,
) -> OrchestratorConfig:
    """Compose an OrchestratorConfig from a session + LLM client + paths.

    `worker_for_stage_override` lets tests inject a closure that hands back
    a MockWorker for every stage without touching env vars or constructing
    an LLMWorker.

    Also constructs a ``code_chat`` closure bound to the configured
    ``MODEL_CODE`` (or ``_DEFAULT_MODEL_CODE``) so tools like
    ``render_mockup`` can invoke the code LLM directly without owning the
    OpenRouterClient (layer rule: tools don't import the LLM client class).
    """
    if worker_for_stage_override is not None:
        worker_for_stage = worker_for_stage_override
    else:
        worker_for_stage = make_worker_for_stage(
            session_id=session_id,
            core_conn=core_conn,
            session_conn=session_conn,
            llm_client=llm_client,
        )

    def sandbox_root_for(sid: str) -> Path:
        return sandbox_path_for(sid, sandbox_root)

    code_model = (
        os.environ.get("MODEL_CODE") or _DEFAULT_MODEL_CODE
    )

    def _code_chat(
        messages: list[dict],
        *,
        response_format: dict | None = None,
        temperature: float = 0.2,
    ) -> ChatResponse:
        return llm_client.chat(
            model=code_model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
        )

    return OrchestratorConfig(
        worker_for_stage=worker_for_stage,
        system_prompt=build_system_prompt(),
        allow_list=list(ALLOW_LIST),
        sandbox_root_for=sandbox_root_for,
        core_db_path=Path(core_db_path),
        sessions_dir=Path(sessions_dir),
        turn_cap=DEFAULT_TURN_CAP,
        spend_cap_usd=DEFAULT_SPEND_CAP_USD,
        code_chat=_code_chat,
        code_model=code_model,
        code_model_is_fallback=False,
    )


__all__ = [
    "ALLOW_LIST",
    "CHAT_STAGES",
    "CHECKPOINT_SET",
    "CODE_STAGES",
    "DEFAULT_SPEND_CAP_USD",
    "DEFAULT_TURN_CAP",
    "RESTAURANT_SEED_BRIEF",
    "build_system_prompt",
    "make_orchestrator_config",
    "make_worker_for_stage",
    "sandbox_path_for",
]
