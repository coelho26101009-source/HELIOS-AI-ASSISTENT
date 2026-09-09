"""The model a cloud provider uses when the user has not chosen one.

WHY THIS EXISTS
---------------
A credential can reach Nano two ways: pasted into Settings, or present in the
environment (see ``core.secret_store._ENV_FALLBACK``, which exists so an
existing ``.env`` keeps working). Both are supported and documented.

Choosing a model, however, only ever happened on ONE of those paths. The
adoption rule lived inside ``main.set_cloud_api_key`` -- the Settings flow --
so a provider credentialed through the environment got a key and never a
model. ``describe_*`` then reported SETUP_REQUIRED, ``resolve_route`` dropped
it from the candidates, and the provider was configured and permanently
unusable. That is exactly the state Mistral was found in on the development
machine: a valid key, twenty-seven discovered chat models, and no route.

So the rule moves here, to a pure function the DESCRIBE path calls. Description
happens for every credential source, which is what makes the fix architectural
rather than one more branch in the Settings handler.

WHAT IT REFUSES TO DO
---------------------
It never names a model. Nano pinning a literal vendor id is the mistake that
left the project calling a decommissioned Groq model and 404-ing on every
single message, and the ban on that is recorded in config/settings.yaml. The
input here is the account's OWN discovered catalogue, already ranked by the
provider's declared family preference; this function only decides which
survivor of that list to adopt.

FOUR PROPERTIES, AND WHY EACH ONE IS A RULE
-------------------------------------------
EXPLICIT      one function, one order of preference, no hidden state.

TESTABLE      a pure function from a catalogue to an id. No network, no clock,
              no configuration, so a test states the catalogue and asserts the
              answer.

RESPECTS THE USER   it is only ever consulted when nothing is configured. A
              model the user picked is never re-examined here.

STABLE        the same catalogue always yields the same id, and a catalogue
              that GROWS still yields it. Callers sort by (rank, id) ascending,
              so a newer sibling in the same family -- ``ministral-14b-2601``
              beside ``ministral-14b-2512`` -- sorts after the incumbent and
              does not displace it. A default that moved when a vendor shipped
              a model would be a different assistant every morning.

FLOATING ALIASES ARE THE ONE THING RANKING CANNOT SEE
-----------------------------------------------------
``gemini-flash-latest`` and ``ministral-14b-latest`` are stable IDS pointing at
a MOVING model. Adopting one satisfies every check above and still changes the
model under the user without warning, which is the precise failure the
stability rule exists to prevent. A pinned sibling is therefore preferred over
a floating alias even when the alias ranks higher. An alias is still adopted
when the account offers nothing else -- a working provider beats a correct
abstention -- and the payload says the model was defaulted, so the UI never
implies the user chose it.

TOOL CALLING IS NOT OPTIONAL FOR NANO
-------------------------------------
A model that cannot call tools does not fail loudly here: it reports the action
it was asked to perform as though it had performed it. That was measured on
Gemma. A model whose account metadata says ``function_calling: false`` is
therefore never adopted as a default, even when it ranks first. Metadata that
says nothing is not a refusal (see ``mistral_provider._model_record``), so
``None`` stays eligible.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: Suffixes that mark an id as an alias onto whatever the vendor currently
#: considers newest, rather than onto one fixed model.
_FLOATING_SUFFIXES = ("-latest", "-preview", "-exp")

#: How a caller may describe the source of the model in a provider payload.
#: ``providers.describe_*`` reports one of these so the UI can say "the model
#: that was configured" and "the model Nano adopted because nothing was"
#: differently -- claiming the second is the first would be Nano stating
#: something it has not measured. "configured" rather than "user" deliberately:
#: Groq's tiers ship as defaults in config/settings.yaml, so a payload claiming
#: the USER chose them would be false on a fresh install.
SOURCE_CONFIGURED = "configured"   # a stored/shipped setting named this id
SOURCE_DEFAULT = "default"  # resolved here, from the account's catalogue
SOURCE_NONE = "none"        # nothing configured and nothing adoptable


def is_floating_alias(model_id: Any) -> bool:
    """True for an id that points at whatever the vendor ships next."""
    lowered = str(model_id or "").strip().lower()
    if not lowered:
        return False
    return lowered.endswith(_FLOATING_SUFFIXES) or lowered.endswith("/latest")


def _entry(candidate: Any) -> tuple[str, bool, Any] | None:
    """(id, deprecated, tool_calling) for one catalogue entry, or None.

    Accepts both shapes the providers produce: a bare id string (Groq) and the
    record mapping Google and Mistral build from the account's own metadata.
    """
    if isinstance(candidate, Mapping):
        model_id = str(candidate.get("id") or "").strip()
        return (model_id, bool(candidate.get("deprecated")),
                candidate.get("tool_calling")) if model_id else None
    model_id = str(candidate or "").strip()
    return (model_id, False, None) if model_id else None


def eligible_models(catalogue: Iterable[Any]) -> list[str]:
    """The ids that may be adopted, in the caller's order.

    The order is the caller's on purpose: ``list_google_models`` and
    ``list_mistral_models`` already sort by ``(rank, id)``, which IS the
    provider's declared preference plus a total tie-break. Re-sorting here
    would put a second, competing ranking in the codebase.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for candidate in catalogue or ():
        entry = _entry(candidate)
        if entry is None:
            continue
        model_id, deprecated, tool_calling = entry
        if model_id in seen:
            continue
        if deprecated:
            continue
        if tool_calling is False:
            continue
        seen.add(model_id)
        ids.append(model_id)
    return ids


def resolve_default(catalogue: Iterable[Any]) -> str:
    """The id to adopt when the user has chosen nothing. "" when there is none.

    Returning "" is a real answer and not a failure: an account whose every
    model is deprecated or cannot call tools has nothing Nano can honestly
    adopt, and the caller reports SETUP_REQUIRED with what it found rather than
    routing to a model that will misbehave.
    """
    ids = eligible_models(catalogue)
    if not ids:
        return ""
    for model_id in ids:
        if not is_floating_alias(model_id):
            return model_id
    # Every eligible id floats. Adopting one beats leaving a credentialed
    # provider unusable, and ``SOURCE_DEFAULT`` tells the user it was not their
    # choice.
    return ids[0]


def resolve_tiers(configured_fast: Any, configured_complex: Any,
                  catalogue: Iterable[Any]) -> tuple[str, str, str]:
    """``(fast, complex, source)`` for one provider.

    THE DEFAULT FILLS THE FAST TIER ONLY, and the complex tier mirrors it.
    Nano's routing already refuses to promote an unrecognised tier to STRONG
    (see ``providers.cloud_model_for``) because ordinary conversation must
    never pay for the big model by accident. A default that reached for a
    larger, costlier sibling nobody asked for would break the same rule from
    the other end, so it does not.

    A configured value always wins and is never validated here; whether it
    still exists on the account is the caller's question, and the caller
    answers it against the same catalogue.
    """
    fast = str(configured_fast or "").strip()
    strong = str(configured_complex or "").strip()
    if fast:
        return fast, (strong or fast), SOURCE_CONFIGURED
    adopted = resolve_default(catalogue)
    if not adopted:
        return "", (strong or ""), SOURCE_NONE
    return adopted, (strong or adopted), SOURCE_DEFAULT


__all__ = [
    "SOURCE_CONFIGURED",
    "SOURCE_DEFAULT",
    "SOURCE_NONE",
    "eligible_models",
    "is_floating_alias",
    "resolve_default",
    "resolve_tiers",
]
