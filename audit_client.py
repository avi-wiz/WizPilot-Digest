"""Rule-based triage for the daily failure audit.

Deliberately deterministic: a blank response is a FACT, not a judgment call, so
it is never sent to an LLM to "decide". Only genuinely ambiguous turns go to the
model (see audit_digest.classify_ambiguous), which keeps token spend down and
keeps the blank/no-widget counts exact and reproducible.

Signals were derived from real response text (sampled 2026-08-31), not guessed:
  - '[widget]' appears LITERALLY in the stored response when the assistant
    rendered a data widget/table/chart. No '[widget]' => the user got prose only.
  - Refusal wording in the wild: "That's outside what I can help with here".
  - Empty-result wording: "No sales were recorded...", "no ... records were found".
"""
from __future__ import annotations
import re

WIDGET = "[widget]"

# Ordered: the first bucket that matches wins. BLANK is checked before anything
# else because an empty string can't match any phrase pattern.
REFUSAL_PAT = re.compile(
    r"outside (of )?what i can help|i can'?t help|i cannot help|not able to help"
    r"|i'?m not able to|i cannot assist|can'?t assist with|beyond what i can"
    r"|i don'?t have (access|the ability)|not something i can", re.I)

NO_DATA_PAT = re.compile(
    r"\bno (sales|orders|records|results|data|invoices|customers|products|items)\b"
    r"|were (found|recorded)|was found|not found|could not be generated"
    r"|couldn'?t find|no matching|returned no|0 results", re.I)

CLARIFY_PAT = re.compile(
    r"would you like|could you (please )?(clarify|specify)|did you mean"
    r"|which (one|of these)|can you confirm|please specify", re.I)

ERROR_PAT = re.compile(
    r"\b(error|failed|failure|something went wrong|try again|timed out"
    r"|unable to (process|complete|generate))\b", re.I)


def strip_widgets(text: str) -> str:
    """Response text minus the widget markers, for length/phrase checks."""
    return text.replace(WIDGET, " ").strip()


def triage(question: str, response: str | None) -> tuple[str, str]:
    """Return (bucket, reason).

    Buckets:
      BLANK        no response at all — the hardest failure
      REFUSAL      explicitly declined / out of scope
      NO_DATA      ran, but came back empty
      ERROR        surfaced an error/failure to the user
      CLARIFY      answered with a question instead of an answer
      NO_WIDGET    prose-only answer where a data question expected a widget
      AMBIGUOUS    none of the above fired — hand to the LLM
      OK           has a widget and no failure signal — not audited
    """
    r = (response or "").strip()
    if not r:
        return "BLANK", "empty response stored"

    body = strip_widgets(r)
    has_widget = WIDGET in r

    if REFUSAL_PAT.search(body):
        return "REFUSAL", "refusal/out-of-scope wording"
    if ERROR_PAT.search(body):
        return "ERROR", "error/failure wording"
    if NO_DATA_PAT.search(body):
        return "NO_DATA", "empty-result wording"
    if CLARIFY_PAT.search(body):
        return "CLARIFY", "asked the user a question back"
    if not has_widget:
        # Prose-only. Often fine (how-to answers, chit-chat), so this is the
        # bucket most likely to be wrong — the LLM adjudicates these.
        return "AMBIGUOUS", "no widget rendered; intent unclear"
    return "OK", "widget present, no failure signal"
