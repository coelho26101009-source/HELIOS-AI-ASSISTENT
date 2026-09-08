"""The execution ledger, and the rule that one EFFECT gets one identity.

THE DEFECT THESE TESTS EXIST FOR.

The per-turn ledger in ``core.brain`` stops a non-idempotent Windows action from
running twice when a turn crosses providers mid-flight. It does that by keying
on ``(tool name, arguments)``. Until this suite existed, "arguments" meant the
JSON the provider happened to emit -- and providers do not agree on how to
spell a number. Gemini's ``{"delta": 10}`` and Groq's ``{"delta": "10.0"}`` are
the same call: ``core.pc_control.audio.parse_delta`` coerces both to the integer
``10`` and moves the volume by ten points. They were two ledger keys, so the
volume moved twice.

The fix is that arguments are normalised against the tool's REGISTERED SCHEMA
before the identity is taken, and the normalised object is what the executor
then runs -- so identity and effect are the same thing by construction.

WHAT IS AND IS NOT REAL HERE.

The Brain, its ledger, the real ``resolve_route`` and the real failover are all
production code. The transports are the scripted fakes the provider suites
already use. The side effect is a COUNTER, never Windows: no test in this file
changes a volume, a file, an application or a setting. Where the point is that
an argument must not reach a handler at all, the real ``ToolExecutor`` runs
against the real ``PermissionManager`` and the assertion is that nothing was
counted.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import plugin_loader, provider_failures
from core.permission_manager import PermissionManager
from core.pc_control import audio
from core.schema_validation import SchemaValidationError, normalize_arguments
from core.tool_execution import ToolExecutor

from tests.test_multi_provider import (          # the production-shaped fakes
    build_brain, collect, google_call, google_text, rate_limited, run,
)
from tests.test_provider_fallback import _Chunk, local_text, local_tool_call


# --------------------------------------------------------------------------
#  A counting executor that carries the REAL registered schemas
# --------------------------------------------------------------------------


def registered_schemas(*names: str) -> dict:
    """The schemas the production registry really holds for these tools.

    Taking them from the real ``ToolExecutor`` rather than retyping them is the
    difference between testing Nano and testing a copy of Nano: a schema that
    changes in ``plugins/pc_control.py`` changes here too, and a test written
    against a schema that no longer exists fails instead of passing hollowly.
    """
    plugin_loader.load_all_plugins()
    executor = ToolExecutor(PermissionManager(confirmation_callback=lambda *_: True))
    executor.register_plugin_tools()
    return {name: {"input_schema": executor.registry[name]["input_schema"]}
            for name in names}


class CountingExecutor:
    """Counts EFFECTIVE executions -- what the narrow handler would have done.

    ``pc_volume_change`` is counted through ``audio.parse_delta``, the same pure
    coercion the real handler performs, and then stops. Nothing reaches Win32.
    Counting the coerced value rather than the raw argument is the whole point:
    a duplicate is two counts of the SAME effect, which a raw-argument counter
    could not see.

    IT VALIDATES FIRST, exactly as the real ``ToolExecutor`` does, and for the
    reason ``FakeOllamaClient`` validates: a fake more permissive than the thing
    it stands in for cannot fail where the real one fails, so it would quietly
    stop testing the fail-closed half of the contract. ``Brain`` deliberately
    hands a schema-invalid call ON to the executor instead of answering it
    itself -- the executor is the authority and logs the denial to the audit
    trail -- so a fake that accepted anything would report that call as an
    execution when production refuses it.
    """

    def __init__(self, tools: dict):
        self.registry = dict(tools)
        self.executions: list[tuple[str, dict]] = []
        self.effects: list = []

    async def execute_tool_async(self, name, args=None, **_kw):
        schema = (self.registry.get(name) or {}).get("input_schema")
        try:
            args = normalize_arguments(schema, args)
        except SchemaValidationError as exc:
            return {"success": False, "status": "invalid_input",
                    "output": {"ok": False, "status": "invalid_input",
                               "error": "invalid_input", "message": str(exc)},
                    "error": str(exc), "metadata": {}}
        self.executions.append((name, args))
        if name == "pc_volume_change":
            self.effects.append(audio.parse_delta(args.get("delta")))
        elif name == "pc_volume_set":
            self.effects.append(audio.parse_level(args.get("level")))
        else:
            self.effects.append(args)
        return {"success": True, "status": "completed",
                "output": {"ok": True}, "metadata": {}}


@pytest.fixture(autouse=True)
def clean_cooldowns():
    provider_failures.reset_all_cooldowns()
    yield
    provider_failures.reset_all_cooldowns()


@pytest.fixture
def volume_executor():
    return CountingExecutor(registered_schemas("pc_volume_change", "pc_volume_set"))


def call(name: str, arguments: dict) -> dict:
    """A tool call shaped exactly as a provider hands one to ``_run_tool``."""
    return {"function": {"name": name, "arguments": json.dumps(arguments)}}


def run_together(brain, *calls) -> list:
    """Run several tool calls the way one round really runs them.

    ``_tool_rounds`` gathers the whole batch, so a duplicate inside one response
    is CONCURRENT, not sequential. Running them one after another would test a
    different thing entirely and would pass against the broken code.
    """
    async def _round():
        return await asyncio.gather(*(brain._run_tool(c) for c in calls))

    return run(_round())


# --------------------------------------------------------------------------
#  1. Semantic identity: the pre-fix defect, stated as a test
# --------------------------------------------------------------------------


EQUIVALENT_SPELLINGS = [
    pytest.param(10.0, id="float"),
    pytest.param("10", id="numeric-string"),
    pytest.param("10.0", id="numeric-string-with-fraction"),
    pytest.param(" 10 ", id="padded-numeric-string"),
]


@pytest.mark.parametrize("second", EQUIVALENT_SPELLINGS)
def test_one_effect_has_one_identity_however_the_provider_spelled_it(
        monkeypatch, volume_executor, second):
    """THE REGRESSION. Each of these executed as +10 before the fix AND after;
    what changed is that they now collapse onto ONE ledger entry.

    Reintroduce the defect by fingerprinting the raw arguments instead of the
    normalised ones and every parameter here fails, which is what makes the
    guard non-vacuous.
    """
    brain = build_brain(monkeypatch, executor=volume_executor)
    run(brain._run_tool(call("pc_volume_change", {"delta": 10})))
    run(brain._run_tool(call("pc_volume_change", {"delta": second})))

    assert volume_executor.effects == [10], (
        f"{second!r} executed a second time as the same +10 change")


def test_a_genuinely_different_value_is_still_a_different_call(
        monkeypatch, volume_executor):
    """Normalisation must MERGE spellings, never merge meanings. +10 followed
    by +20 is two requests and must remain two executions."""
    brain = build_brain(monkeypatch, executor=volume_executor)
    run(brain._run_tool(call("pc_volume_change", {"delta": 10})))
    run(brain._run_tool(call("pc_volume_change", {"delta": 20})))

    assert volume_executor.effects == [10, 20]


def test_an_identical_typed_repeat_is_still_deduplicated(
        monkeypatch, volume_executor):
    """The protection that already existed has to survive the change."""
    brain = build_brain(monkeypatch, executor=volume_executor)
    first = run(brain._run_tool(call("pc_volume_change", {"delta": 10})))
    second = run(brain._run_tool(call("pc_volume_change", {"delta": 10})))

    assert volume_executor.effects == [10]
    assert first.get("metadata", {}).get("replayed") is not True
    assert second["metadata"]["replayed"] is True


def test_an_omitted_argument_and_its_declared_default_are_one_call(
        monkeypatch, volume_executor):
    """"Aumenta o volume" and "aumenta o volume 10 pontos" are the same action.

    The handler's default lives in ``audio.DEFAULT_STEP``; the schema declares
    the same number, so the normaliser can see it. Without that declaration the
    two spellings were two ledger keys and the volume moved twice -- the same
    defect as the numeric spellings above, reached by omission instead.
    """
    brain = build_brain(monkeypatch, executor=volume_executor)
    run(brain._run_tool(call("pc_volume_change", {})))
    run(brain._run_tool(call("pc_volume_change", {"delta": audio.DEFAULT_STEP})))

    assert volume_executor.effects == [audio.DEFAULT_STEP]


def test_the_executed_arguments_are_the_arguments_that_were_fingerprinted(
        monkeypatch, volume_executor):
    """Requirement that keeps the fix honest: there is no normalised-for-the-
    ledger object that differs from the one the handler receives."""
    brain = build_brain(monkeypatch, executor=volume_executor)
    run(brain._run_tool(call("pc_volume_change", {"delta": "10.0"})))

    name, executed = volume_executor.executions[0]
    assert executed == {"delta": 10}, "the executor received the raw spelling"
    assert brain._call_fingerprint(name, executed) in brain._turn_tool_results


# --------------------------------------------------------------------------
#  2. Key order and structure
# --------------------------------------------------------------------------


def test_object_key_order_cannot_change_the_identity():
    from core.brain import Brain

    a = Brain._call_fingerprint("pc_file_move", {"source": "a", "destination": "b"})
    b = Brain._call_fingerprint("pc_file_move", {"destination": "b", "source": "a"})
    assert a == b


def test_array_order_is_preserved_because_it_carries_meaning():
    """`pc_file_search.roots` is a search ORDER. Sorting it to make identities
    match would change the result the user gets, so two orders stay two calls."""
    schema = registered_schemas("pc_file_search")["pc_file_search"]["input_schema"]
    first = normalize_arguments(schema, {"query": "x", "roots": ["a", "b"]})
    second = normalize_arguments(schema, {"query": "x", "roots": ["b", "a"]})
    assert first["roots"] == ["a", "b"]
    assert first != second


def test_nested_structures_normalise_deterministically():
    """No registered schema nests today. The normaliser supports it anyway, and
    this pins the behaviour so a schema that starts nesting is already covered
    rather than silently unvalidated."""
    schema = {"type": "object", "properties": {
        "window": {"type": "object", "properties": {
            "id": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "integer"}}}}}}
    normalised = normalize_arguments(
        schema, {"window": {"id": "7", "tags": ["1", 2.0, 3]}})
    assert normalised == {"window": {"id": 7, "tags": [1, 2, 3]}}
    assert normalize_arguments(schema, normalised) == normalised, "not idempotent"


def test_normalisation_is_idempotent_for_every_registered_tool():
    """Brain normalises to build the key and ToolExecutor normalises again as
    the authority. If those two passes disagreed, the executed arguments would
    drift away from the identity on every call."""
    plugin_loader.load_all_plugins()
    executor = ToolExecutor(PermissionManager(confirmation_callback=lambda *_: True))
    executor.register_plugin_tools()
    samples = {"integer": 5, "number": 1.5, "string": "x", "boolean": True,
               "array": []}
    for name, tool in executor.registry.items():
        schema = tool.get("input_schema") or {}
        args = {}
        for key, declared in (schema.get("properties") or {}).items():
            if isinstance(declared, dict) and "enum" in declared:
                args[key] = declared["enum"][0]
            elif isinstance(declared, dict) and declared.get("type") in samples:
                args[key] = samples[declared["type"]]
        once = normalize_arguments(schema, args)
        assert normalize_arguments(schema, once) == once, name


# --------------------------------------------------------------------------
#  3. Booleans are not numbers, and numbers are not booleans
# --------------------------------------------------------------------------


def test_a_boolean_never_passes_as_a_number():
    """``isinstance(True, int)`` is True in Python. Without an explicit check a
    ``true`` would be executed as ``1`` -- a real, unrequested volume change."""
    schema = {"type": "object", "properties": {"delta": {"type": "integer"}}}
    for value in (True, False):
        with pytest.raises(SchemaValidationError):
            normalize_arguments(schema, {"delta": value})


def test_a_number_never_passes_as_a_boolean():
    schema = {"type": "object", "properties": {"topmost": {"type": "boolean"}}}
    for value in (1, 0, 1.0, "true", "false", "yes"):
        with pytest.raises(SchemaValidationError):
            normalize_arguments(schema, {"topmost": value})
    assert normalize_arguments(schema, {"topmost": True}) == {"topmost": True}


def test_the_string_false_is_refused_rather_than_read_as_true():
    """``bool("false")`` is ``True``. A handler that coerced it would have
    turned a request to DISABLE into a request to ENABLE, so the central
    boundary refuses it instead."""
    schema = {"type": "object", "properties": {"dry_run": {"type": "boolean"}}}
    with pytest.raises(SchemaValidationError):
        normalize_arguments(schema, {"dry_run": "false"})


# --------------------------------------------------------------------------
#  4. Malformed arguments fail closed, before the handler
# --------------------------------------------------------------------------


MALFORMED = [
    pytest.param({"level": "muito alto"}, id="not-a-number"),
    pytest.param({"level": float("nan")}, id="nan"),
    pytest.param({"level": float("inf")}, id="infinity"),
    pytest.param({"level": True}, id="boolean"),
    pytest.param({"level": []}, id="list"),
    pytest.param({"level": {}}, id="dict"),
    pytest.param({"level": 50.5}, id="fractional-for-an-integer-field"),
    pytest.param({}, id="required-argument-missing"),
    pytest.param({"level": None}, id="required-argument-null"),
]


@pytest.mark.parametrize("args", MALFORMED)
def test_a_malformed_argument_produces_no_effect_through_the_chat_loop(
        monkeypatch, volume_executor, args):
    """From the Brain's side of the boundary: the call is refused and the
    machine is untouched, whichever layer said no."""
    brain = build_brain(monkeypatch, executor=volume_executor)
    result = run(brain._run_tool(call("pc_volume_set", args)))

    assert volume_executor.effects == [], f"{args} produced an effect"
    assert volume_executor.executions == [], f"{args} was executed"
    assert result.get("success") is not True


@pytest.mark.parametrize("args", MALFORMED)
def test_a_malformed_argument_never_reaches_the_handler(args):
    """The real ToolExecutor, the real PermissionManager, the real registry.

    A refusal here is the authority's refusal, and it is logged as one -- which
    is why Brain hands a schema-invalid call through rather than answering it
    itself.
    """
    plugin_loader.load_all_plugins()
    executor = ToolExecutor(PermissionManager(confirmation_callback=lambda *_: True))
    executor.register_plugin_tools()
    ran: list = []
    executor.registry["pc_volume_set"]["handler"] = lambda a: ran.append(a)

    result = executor.execute_tool("pc_volume_set", args)

    assert ran == [], f"{args} reached the handler"
    assert result["success"] is False
    assert result["status"] == "invalid_input"
    # The OUTPUT carries the same shape a handler's own refusal carries, so a
    # caller does not have to know which layer said no.
    assert result["output"]["status"] == "invalid_input"


def test_an_invalid_call_is_not_remembered_so_a_corrected_retry_still_runs(
        monkeypatch, volume_executor):
    """Only a real execution enters the ledger. A model that fixes its argument
    must not be answered with a cached refusal."""
    brain = build_brain(monkeypatch, executor=volume_executor)
    run(brain._run_tool(call("pc_volume_set", {"level": "muito alto"})))
    run(brain._run_tool(call("pc_volume_set", {"level": 30})))

    assert volume_executor.effects == [30]


def test_a_model_supplied_permission_target_cannot_change_the_identity():
    """`_pc_target` is written by the executor and read by PermissionManager;
    anything the MODEL puts there is discarded. It is discarded from the ledger
    key too, or two calls that execute identically would be two entries."""
    from core.brain import Brain

    schema = registered_schemas("pc_volume_change")["pc_volume_change"]["input_schema"]
    plain = normalize_arguments(schema, {"delta": 10})
    spoofed = normalize_arguments(schema, {"delta": 10, "_pc_target": "volume:get"})
    assert plain == spoofed
    assert (Brain._call_fingerprint("pc_volume_change", plain)
            == Brain._call_fingerprint("pc_volume_change", spoofed))


# --------------------------------------------------------------------------
#  5. Enums stay strict
# --------------------------------------------------------------------------


def test_an_enum_value_outside_the_schema_is_refused():
    schema = registered_schemas("pc_window_snap")["pc_window_snap"]["input_schema"]
    with pytest.raises(SchemaValidationError):
        normalize_arguments(schema, {"window_id": 1, "position": "diagonal"})


def test_an_enum_keeps_the_case_insensitivity_the_handlers_already_have():
    """Every enum consumer in core/pc_control lowercases before matching, so
    "Left" works today. It must keep working, and it must be ONE identity with
    "left" rather than a second ledger entry."""
    schema = registered_schemas("pc_window_snap")["pc_window_snap"]["input_schema"]
    for spelling in ("left", "Left", "LEFT", " left "):
        assert normalize_arguments(
            schema, {"window_id": 1, "position": spelling})["position"] == "left"


# --------------------------------------------------------------------------
#  6. Duplicate-effect proof across a real failover
# --------------------------------------------------------------------------


def test_a_differently_spelled_repeat_cannot_act_twice_across_cloud_to_cloud(
        monkeypatch, volume_executor):
    """THE HEADLINE PROOF.

    Gemini asks for +10 and the change really happens. Gemini's next round is
    rate-limited. Groq picks the turn up, sees the recorded result and re-issues
    the call spelled ``"10"`` instead of ``10``. Before the fix that was a
    different ledger key and the volume moved a second time.
    """
    brain = build_brain(
        monkeypatch, preferred="google", executor=volume_executor,
        tools=[{"type": "function", "function": {
            "name": "pc_volume_change", "description": "volume",
            "parameters": {"type": "object",
                           "properties": {"delta": {"type": "integer"}}}}}],
        google_script=[google_call("pc_volume_change", {"delta": 10}), rate_limited()],
        groq_script=[
            [_Chunk(tool_calls=[("pc_volume_change", '{"delta": "10"}')])],
            [_Chunk("Volume aumentado.")],
        ])
    answer = run(collect(brain, "aumenta o volume"))

    assert volume_executor.effects == [10], (
        f"the volume changed {len(volume_executor.effects)} times across the failover")
    assert "Volume aumentado." in answer


def test_a_differently_spelled_repeat_cannot_act_twice_across_cloud_to_local(
        monkeypatch, volume_executor):
    """The same crossing, ending at the local model.

    The local model is the one most likely to spell a number differently: it is
    a different family entirely, and it is handed a history in which the action
    has already run. Ollama sends ``10.0`` where Gemini sent ``10``.
    """
    brain = build_brain(
        monkeypatch, preferred="google", executor=volume_executor,
        groq_state=__import__("core.providers", fromlist=["providers"]).ProviderState.SETUP_REQUIRED,
        tools=[{"type": "function", "function": {
            "name": "pc_volume_change", "description": "volume",
            "parameters": {"type": "object",
                           "properties": {"delta": {"type": "integer"}}}}}],
        google_script=[google_call("pc_volume_change", {"delta": 10}), rate_limited()],
        ollama_script=[local_tool_call("pc_volume_change", {"delta": 10.0}),
                       local_text("Volume aumentado.")])
    run(collect(brain, "aumenta o volume"))

    assert volume_executor.effects == [10], (
        f"the volume changed {len(volume_executor.effects)} times reaching the local model")


def test_a_side_effect_then_a_provider_failure_is_not_repeated_by_the_fallback(
        monkeypatch, volume_executor):
    """Stated as the audit stated it: a first-provider side effect followed by
    that provider failing must not be repeatable merely because the fallback
    serialised the same request differently. Three spellings, one effect."""
    brain = build_brain(
        monkeypatch, preferred="google", executor=volume_executor,
        tools=[{"type": "function", "function": {
            "name": "pc_volume_change", "description": "volume",
            "parameters": {"type": "object",
                           "properties": {"delta": {"type": "integer"}}}}}],
        google_script=[google_call("pc_volume_change", {"delta": 10}), rate_limited()],
        groq_script=[
            [_Chunk(tool_calls=[("pc_volume_change", '{"delta": 10.0}')])],
            [_Chunk(tool_calls=[("pc_volume_change", '{"delta": " 10 "}')])],
            [_Chunk("Feito.")],
        ])
    run(collect(brain, "aumenta o volume"))

    assert volume_executor.effects == [10]


def test_the_ledger_is_still_per_turn_after_the_change(monkeypatch, volume_executor):
    """A genuine second request in a LATER turn must still act. A ledger that
    outlived its turn would be a silently broken feature, not a safer one."""
    brain = build_brain(
        monkeypatch, preferred="google", executor=volume_executor,
        tools=[{"type": "function", "function": {
            "name": "pc_volume_change", "description": "volume",
            "parameters": {"type": "object",
                           "properties": {"delta": {"type": "integer"}}}}}],
        google_script=[google_call("pc_volume_change", {"delta": 10}), google_text("feito"),
                       google_call("pc_volume_change", {"delta": "10"}), google_text("feito")])
    run(collect(brain, "aumenta o volume"))
    run(collect(brain, "aumenta outra vez"))

    assert volume_executor.effects == [10, 10]


# --------------------------------------------------------------------------
#  7. The same call twice in ONE round, which failover never touched
# --------------------------------------------------------------------------


class SlowCountingExecutor(CountingExecutor):
    """A real Win32 call is not instantaneous, and that is what opened the hole.

    Without the pause the two coroutines below would happen to finish before
    either yielded, and the test would pass against the broken code -- a guard
    that cannot fail is not a guard.
    """

    def __init__(self, tools: dict, *, succeeds: bool = True):
        super().__init__(tools)
        self.succeeds = succeeds

    async def execute_tool_async(self, name, args=None, **kw):
        await asyncio.sleep(0.01)
        result = await super().execute_tool_async(name, args, **kw)
        if not self.succeeds and result.get("success"):
            return {"success": False, "status": "failed",
                    "output": {"ok": False, "error": "failed"}, "metadata": {}}
        return result


def slow_executor(succeeds: bool = True) -> SlowCountingExecutor:
    return SlowCountingExecutor(
        registered_schemas("pc_volume_change", "pc_volume_set"), succeeds=succeeds)


def test_the_same_call_emitted_twice_in_one_round_acts_once(monkeypatch):
    """A DEFECT INDEPENDENT OF PROVIDER FAILOVER.

    Emitting the identical tool call twice in one response is a known
    parallel-tool-calling failure, and ``_tool_rounds`` hands the batch to
    ``asyncio.gather``. The completed-call ledger could not see it: both copies
    passed the lookup before either had written anything, so the volume moved
    twice. The in-flight half of the ledger is what closes it.
    """
    executor = slow_executor()
    brain = build_brain(monkeypatch, executor=executor)
    results = run_together(brain,
                           call("pc_volume_change", {"delta": 10}),
                           call("pc_volume_change", {"delta": 10}))

    assert executor.effects == [10], "the concurrent duplicate acted twice"
    assert [r.get("metadata", {}).get("replayed") for r in results].count(True) == 1


def test_two_concurrent_spellings_of_one_call_also_act_once(monkeypatch):
    """Both halves of this pass have to hold at the same time: the identity is
    semantic AND the in-flight entry is keyed on that same identity."""
    executor = slow_executor()
    brain = build_brain(monkeypatch, executor=executor)
    run_together(brain,
                 call("pc_volume_change", {"delta": 10}),
                 call("pc_volume_change", {"delta": "10.0"}))

    assert executor.effects == [10]


def test_two_concurrent_different_calls_both_act(monkeypatch):
    """The in-flight entry must not serialise unrelated work into one result."""
    executor = slow_executor()
    brain = build_brain(monkeypatch, executor=executor)
    run_together(brain,
                 call("pc_volume_change", {"delta": 10}),
                 call("pc_volume_change", {"delta": 20}))

    assert sorted(executor.effects) == [10, 20]


def test_a_failed_call_is_shared_while_running_but_stays_retryable_after(monkeypatch):
    """The in-flight entry must not become a second, longer-lived cache.

    A concurrent twin waits for the failure rather than running a second copy;
    a LATER attempt in the same turn still executes, because only a SUCCESS is
    written to the completed-call ledger and the user may approve on a retry.
    """
    executor = slow_executor(succeeds=False)
    brain = build_brain(monkeypatch, executor=executor)
    run_together(brain,
                 call("pc_volume_change", {"delta": 10}),
                 call("pc_volume_change", {"delta": 10}))
    assert executor.effects == [10], "the concurrent twin ran a second copy"

    run(brain._run_tool(call("pc_volume_change", {"delta": 10})))
    assert executor.effects == [10, 10], "a failed call stopped being retryable"


def test_the_inflight_ledger_is_emptied_when_a_call_finishes(monkeypatch):
    """A leaked in-flight entry would make every later identical call wait on a
    future that is already resolved -- silently caching failures for the turn."""
    executor = slow_executor()
    brain = build_brain(monkeypatch, executor=executor)
    run(brain._run_tool(call("pc_volume_change", {"delta": 10})))

    assert brain._turn_tool_inflight == {}


def test_the_inflight_ledger_is_cleared_between_user_turns(monkeypatch):
    executor = slow_executor()
    brain = build_brain(
        monkeypatch, preferred="google", executor=executor,
        tools=[{"type": "function", "function": {
            "name": "pc_volume_change", "description": "volume",
            "parameters": {"type": "object",
                           "properties": {"delta": {"type": "integer"}}}}}],
        google_script=[google_text("feito")])
    brain._turn_tool_inflight["stale"] = "not a future"
    run(collect(brain, "olá"))

    assert brain._turn_tool_inflight == {}


# --------------------------------------------------------------------------
#  8. Normalisation narrows; it never widens
# --------------------------------------------------------------------------


def test_normalisation_never_invents_an_argument_the_model_did_not_send():
    """Only a DECLARED default may appear, and only with the declared value.
    Anything else would be the boundary choosing an action on the model's
    behalf."""
    plugin_loader.load_all_plugins()
    executor = ToolExecutor(PermissionManager(confirmation_callback=lambda *_: True))
    executor.register_plugin_tools()
    for name, tool in executor.registry.items():
        schema = tool.get("input_schema") or {}
        properties = schema.get("properties") or {}
        if schema.get("required"):
            continue
        added = set(normalize_arguments(schema, {}))
        declared = {key for key, spec in properties.items()
                    if isinstance(spec, dict) and "default" in spec}
        assert added == declared, name


def test_every_declared_default_matches_what_the_handler_would_have_done():
    """A schema default that disagreed with the handler's own default would
    silently change behaviour on every omitted argument. These are the four
    PC-control defaults that were literals in a handler before this pass."""
    from core.pc_control import display, files, keyboard, screen

    schemas = registered_schemas(
        "pc_volume_change", "pc_display_change_brightness",
        "pc_pointer_scroll", "pc_file_search", "pc_screenshot_capture")

    def default_for(tool: str, key: str):
        return schemas[tool]["input_schema"]["properties"][key]["default"]

    assert default_for("pc_volume_change", "delta") == audio.DEFAULT_STEP
    assert default_for("pc_display_change_brightness", "delta") == display.DEFAULT_STEP
    assert default_for("pc_pointer_scroll", "clicks") == keyboard.DEFAULT_SCROLL_CLICKS
    assert default_for("pc_file_search", "max_results") == files.DEFAULT_FILE_RESULTS
    assert default_for("pc_screenshot_capture", "mode") == screen.DEFAULT_CAPTURE_MODE


def test_an_undeclared_property_is_passed_through_and_counts_for_identity():
    """No registered schema sets ``additionalProperties``, so the JSON Schema
    default applies and an extra key is allowed. It reaches the handler, so it
    must reach the identity too -- dropping it would merge two calls that do
    different things."""
    from core.brain import Brain

    schema = {"type": "object", "properties": {"delta": {"type": "integer"}}}
    normalised = normalize_arguments(schema, {"delta": 10, "note": "x"})
    assert normalised == {"delta": 10, "note": "x"}
    assert (Brain._call_fingerprint("t", normalised)
            != Brain._call_fingerprint("t", {"delta": 10}))
