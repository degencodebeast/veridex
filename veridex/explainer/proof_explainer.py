"""Proof Explainer — an EDUCATIONAL LLM narrator that lives OUTSIDE the trust core.

This module is deliberately OUTSIDE every trust dir (``law/``, ``scoring.py``,
``leaderboard.py``, ``verifier/``, ``checks/``, ``ingest/``, ``policy/``). That placement is
the whole point: it is the ONE place an LLM is allowed, precisely because it can NEVER touch
the proof path. The deterministic verifier remains the sole source of truth.

STRICT ISOLATION (enforced by :func:`veridex.verifier.import_audit.assert_no_trust_imports`):
this package imports NOTHING from any trust dir. It receives an ALREADY-SANITIZED read-model
dict (served view-model fields only) — never a raw ``RunResult``, a DB handle, or any unsealed
live state — and it computes/certifies/scores/recomputes NOTHING. It only narrates.

Async network shell (CON-010): ``httpx`` is imported LAZILY inside :func:`explain_proof`, and
all LLM calls route through OpenRouter using the shared ``model_id`` + ``require_openrouter_key``
config, mirroring :mod:`veridex.ingest.live_client`. A missing key degrades gracefully to an
honest "unavailable" message — NEVER a fabricated explanation.
"""

from __future__ import annotations

import json
from typing import Any

from veridex.explainer.glossary import GLOSSARY_DEFINITIONS

#: OpenRouter chat-completions endpoint (matches the config note ``https://openrouter.ai/api/v1``).
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: The FENCE. Verbatim-intent system prompt that pins the narrator to EDUCATIONAL-only behavior.
_FENCE_SYSTEM_PROMPT = (
    "You are the Veridex Proof Explainer — an EDUCATIONAL narrator, NOT a verifier. "
    "Explain what the ALREADY-PRODUCED proof fields mean in plain language, grounded ONLY in "
    "the provided artifact + glossary. NEVER say 'I verified/proved/validated this run', never "
    "assert a run is valid, never compute/recompute any hash/score/CLV/edge, never certify. If "
    "asked whether a run is valid, point to the deterministic Verify result shown on the card. "
    "If something isn't in the provided artifact, say you don't have it — never guess."
)

#: Response disclaimer carried on EVERY explanation (fabricated or honest-unavailable alike).
DISCLAIMER = (
    "Explainer (LLM) · educational only · does not verify, score, certify, or control agents. "
    "The deterministic verifier is the source of truth."
)

#: Response footer pinning the source of truth.
FOOTER = "Source of truth: deterministic Verify result + Proof Card fields."

#: Honest graceful-degrade text when no OpenRouter key is configured (NEVER a fabricated answer).
_UNAVAILABLE_NO_KEY = "Explainer unavailable (no LLM key configured)."


def _build_system_prompt(read_model: dict[str, Any]) -> str:
    """Compose the fenced system prompt: FENCE + embedded glossary + the sanitized artifact JSON.

    The artifact is the caller-sanitized read-model (served fields only). It is serialized with
    ``json.dumps`` so a raw/non-serializable object (e.g. a ``RunResult`` or a DB handle) would
    raise here rather than leak into the prompt — a defense-in-depth on top of the endpoint's
    whitelist.

    Args:
        read_model: The sanitized served read-model dict (proof artifact + verify + glossary).

    Returns:
        The full system-prompt string handed to the LLM.
    """
    glossary_block = "\n".join(
        f"- {term['label']}: {term['definition']}" for term in GLOSSARY_DEFINITIONS.values()
    )
    artifact_json = json.dumps(read_model, sort_keys=True, ensure_ascii=False)
    return (
        f"{_FENCE_SYSTEM_PROMPT}\n\n"
        f"GLOSSARY (pinned doctrine — ground your narration in these):\n{glossary_block}\n\n"
        f"SANITIZED PROOF ARTIFACT (the ONLY facts you may narrate):\n{artifact_json}"
    )


def _build_user_message(question: str | None, target_field: str | None) -> str:
    """Compose the user turn from the optional question / target field.

    Args:
        question: A free-form question about the proof, or ``None``.
        target_field: A specific served field the user wants explained, or ``None``.

    Returns:
        The user-message string.
    """
    if question:
        return question
    if target_field:
        return f"Explain what the '{target_field}' field on this proof means, in plain language."
    return "Explain what this proof shows, in plain language, for a non-expert reader."


def _envelope(explanation: str) -> dict[str, str]:
    """Wrap narration text in the fixed ``{explanation, disclaimer, footer}`` response envelope."""
    return {"explanation": explanation, "disclaimer": DISCLAIMER, "footer": FOOTER}


async def explain_proof(
    read_model: dict[str, Any],
    *,
    question: str | None = None,
    target_field: str | None = None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, str]:
    """Narrate an already-produced proof in plain language — EDUCATIONAL, never verifying.

    Builds the fenced system prompt from the SANITIZED read-model + embedded glossary, calls the
    LLM via OpenRouter (shared ``model_id`` + ``require_openrouter_key``), and returns the narration
    wrapped in the ``{explanation, disclaimer, footer}`` envelope. This function certifies nothing,
    recomputes nothing, and mutates nothing — it receives a plain dict and returns text.

    Graceful degrade (NEVER fabricate): if no OpenRouter key is configured, or the LLM call fails,
    the returned ``explanation`` is an honest "unavailable" string — never an invented answer.

    Args:
        read_model: The sanitized served read-model (proof artifact + verify + glossary). MUST be
            plain served fields only — never a raw ``RunResult``, unsealed state, or DB handle.
        question: Optional free-form question about the proof.
        target_field: Optional specific served field to explain.
        settings: Optional settings override (defaults to :func:`veridex.config.get_settings`).
        client: Optional injected async HTTP client for offline / mock tests. Must support
            ``client.post(url, headers=..., json=...)`` returning a response with ``.json()`` and
            ``.raise_for_status()``. When ``None`` (production), an ``httpx.AsyncClient`` is used.

    Returns:
        ``{"explanation": ..., "disclaimer": DISCLAIMER, "footer": FOOTER}``.
    """
    from veridex.config import get_settings  # noqa: PLC0415  # shell-only config (CON-010)

    resolved_settings = settings if settings is not None else get_settings()

    # Graceful degrade BEFORE any network work: no key (missing OR empty/whitespace) ⇒ honest
    # unavailable, never a fabrication. An empty-string OPENROUTER_API_KEY="" is NOT a real key.
    api_key = getattr(resolved_settings, "openrouter_api_key", None)
    if not api_key or not str(api_key).strip():
        return _envelope(_UNAVAILABLE_NO_KEY)

    system_prompt = _build_system_prompt(read_model)
    user_message = _build_user_message(question, target_field)
    payload = {
        "model": resolved_settings.model_id,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    headers = {
        "Authorization": f"Bearer {resolved_settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    # Lazy httpx import keeps this module load network-library-free (CON-010).
    own_client = client is None
    if own_client:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient()
    try:
        resp = await client.post(_OPENROUTER_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except Exception as exc:  # network/parse failure ⇒ honest unavailable, NEVER a fabrication
        return _envelope(f"Explainer unavailable (LLM call failed: {type(exc).__name__}).")
    finally:
        if own_client:
            await client.aclose()

    return _envelope(str(text))
