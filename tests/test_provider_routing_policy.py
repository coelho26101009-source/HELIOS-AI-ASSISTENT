"""The final provider policy: who may answer, with which model, in what order.

Three questions this suite exists to keep answered, all of which had a wrong
answer on the development machine at the time it was written.

WHICH MODEL DOES A CREDENTIALED PROVIDER USE?
    Mistral held a valid key, published twenty-seven chat models and could not
    serve a single request, because the rule that adopts a model lived inside
    the Settings key-entry handler and a key from the environment never passes
    through it. The provider was configured and permanently unroutable.

IN WHAT ORDER DOES AUTO TRY THEM?
    The order has to be the same on two consecutive turns, and it has to be the
    one the project declared rather than one that emerges from whichever
    dictionary happened to be iterated.

WHAT HAPPENS WHEN THE CHOSEN MODEL STOPS EXISTING?
    A model id that has been decommissioned 404s on every message. Nano must
    say so, and must never quietly substitute another model.

Nothing here reaches the network. The catalogue is stated by the test, which is
the whole point of :mod:`core.model_defaults` being a pure function.
"""
from __future__ import annotations

import pytest

from core import model_defaults, providers
from core.providers import ProviderId, ProviderMode, ProviderState


# --------------------------------------------------------------------------
#  Catalogues, in the record shape list_google_models/list_mistral_models
#  really return. Ids are invented on purpose: a test that used a real vendor
#  id would start failing the day that model is retired, which is the exact
#  coupling this module exists to remove.
# --------------------------------------------------------------------------

def record(model_id: str, **overrides) -> dict:
    base = {"id": model_id, "display_name": model_id, "description": "",
            "input_tokens": 128000, "output_tokens": None, "streaming": True,
            "tool_calling": True, "vision": False, "deprecated": False,
            "aliases": []}
    base.update(overrides)
    return base


#: Ranked best-first, the way the providers hand it over: a floating alias in
#: front of the pinned model it currently points at.
CATALOGUE = [record("acme-small-latest"), record("acme-small-0001"),
             record("acme-large-0001")]


# --------------------------------------------------------------------------
#  The default resolver
# --------------------------------------------------------------------------


def test_a_configured_model_is_never_replaced_by_a_default():
    fast, strong, source = model_defaults.resolve_tiers("chosen-by-the-user", "",
                                                        CATALOGUE)
    assert fast == "chosen-by-the-user"
    assert source == model_defaults.SOURCE_CONFIGURED
    assert fast not in [r["id"] for r in CATALOGUE], (
        "the resolver reached into the catalogue although a model was configured")


def test_the_default_always_comes_from_the_account_and_is_never_a_literal():
    """Two disjoint catalogues, two answers, each from its own catalogue.

    A hardcoded vendor id would survive one of these and fail the other, which
    is what makes this stronger than reading the module for a forbidden string.
    """
    first = model_defaults.resolve_default(CATALOGUE)
    second = model_defaults.resolve_default([record("other-vendor-a"),
                                             record("other-vendor-b")])
    assert first in [r["id"] for r in CATALOGUE]
    assert second == "other-vendor-a"
    assert first != second


def test_the_same_catalogue_always_resolves_to_the_same_model():
    answers = {model_defaults.resolve_default(CATALOGUE) for _ in range(20)}
    assert len(answers) == 1, f"the default is not deterministic: {answers}"


def test_a_catalogue_that_grows_keeps_the_same_default():
    """The vendor shipping a newer sibling must not move a user's model.

    Callers sort by (rank, id) ascending, so a later-dated id in the same
    family arrives AFTER the incumbent. This is the property that makes
    "the default does not change every startup" true rather than hoped for.
    """
    before = model_defaults.resolve_default(CATALOGUE)
    after = model_defaults.resolve_default(
        CATALOGUE + [record("acme-small-0002"), record("acme-small-0003")])
    assert before == after == "acme-small-0001"


def test_a_floating_alias_loses_to_a_pinned_model():
    """`-latest` is a stable id for a moving model, which is the one thing the
    ranking cannot see."""
    assert model_defaults.resolve_default(CATALOGUE) == "acme-small-0001"
    assert model_defaults.is_floating_alias("acme-small-latest")
    assert not model_defaults.is_floating_alias("acme-small-0001")


def test_an_alias_is_still_adopted_when_the_account_offers_nothing_else():
    """A working provider beats a principled abstention; the payload says the
    model was defaulted, so nobody is misled about who chose it."""
    assert model_defaults.resolve_default([record("acme-small-latest")]) \
        == "acme-small-latest"


def test_a_model_that_cannot_call_tools_is_never_adopted():
    catalogue = [record("acme-chatty-0001", tool_calling=False),
                 record("acme-capable-0001")]
    assert model_defaults.resolve_default(catalogue) == "acme-capable-0001"


def test_a_model_the_account_says_nothing_about_stays_eligible():
    """None is "the account did not say", which is not a refusal."""
    assert model_defaults.resolve_default(
        [record("acme-unknown-0001", tool_calling=None)]) == "acme-unknown-0001"


def test_a_deprecated_model_is_never_adopted():
    catalogue = [record("acme-old-0001", deprecated=True), record("acme-new-0001")]
    assert model_defaults.resolve_default(catalogue) == "acme-new-0001"


def test_an_account_with_nothing_usable_resolves_to_no_model():
    catalogue = [record("acme-a", deprecated=True),
                 record("acme-b", tool_calling=False)]
    assert model_defaults.resolve_default(catalogue) == ""
    fast, _strong, source = model_defaults.resolve_tiers("", "", catalogue)
    assert fast == ""
    assert source == model_defaults.SOURCE_NONE


def test_the_default_fills_the_fast_tier_only_and_never_escalates():
    """Ordinary conversation must not pay for the big model by accident.

    ``cloud_model_for`` already refuses to promote an unrecognised tier to
    STRONG; a default that reached for a larger sibling would break the same
    rule from the other end.
    """
    fast, strong, source = model_defaults.resolve_tiers("", "", CATALOGUE)
    assert source == model_defaults.SOURCE_DEFAULT
    assert fast == strong == "acme-small-0001"
    assert strong != "acme-large-0001"


def test_a_configured_complex_tier_survives_a_defaulted_fast_tier():
    fast, strong, _source = model_defaults.resolve_tiers("", "acme-large-0001",
                                                         CATALOGUE)
    assert (fast, strong) == ("acme-small-0001", "acme-large-0001")


# --------------------------------------------------------------------------
#  A credentialed provider is a usable provider
# --------------------------------------------------------------------------


@pytest.fixture
def configured_secret(monkeypatch):
    """Report every provider's key as present, from the environment.

    The environment is the point: it is the credential source that never
    reaches the Settings adoption rule, and therefore the one that produced a
    configured-but-unusable provider.
    """
    from core import secret_store

    monkeypatch.setattr(secret_store, "describe", lambda _name: {
        "configured": True, "masked": "ab••••yz", "source": "environment",
        "encrypted": False})


@pytest.fixture
def no_secret(monkeypatch):
    from core import secret_store

    monkeypatch.setattr(secret_store, "describe", lambda _name: {
        "configured": False, "masked": "", "source": "none", "encrypted": False})


def stub_catalogue(monkeypatch, provider_module, catalogue, error=None):
    lister = ("list_mistral_models" if provider_module.__name__.endswith("mistral_provider")
              else "list_google_models")
    monkeypatch.setattr(provider_module, lister,
                        lambda *_a, **_k: ([] if error else list(catalogue), error))


def describers():
    from core import google_provider, mistral_provider

    return [(mistral_provider, mistral_provider.describe_mistral),
            (google_provider, google_provider.describe_google)]


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_a_key_alone_makes_the_provider_usable(monkeypatch, configured_secret,
                                               module_and_describe):
    """THE MISTRAL BUG, stated as a rule for every cloud provider.

    Before the fix this returned SETUP_REQUIRED -- which routing reads as
    "cannot serve a request" -- for an account with a valid key and a full
    catalogue, because no model had been picked in Settings.
    """
    module, describe = module_and_describe
    stub_catalogue(monkeypatch, module, CATALOGUE)
    payload = describe("", "")
    assert payload["state"] == ProviderState.READY.value, payload["detail"]
    assert payload["model"] == "acme-small-0001"
    assert payload["tiers"]["fast"] == "acme-small-0001"


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_an_adopted_model_is_reported_as_a_default_not_as_a_choice(
        monkeypatch, configured_secret, module_and_describe):
    module, describe = module_and_describe
    stub_catalogue(monkeypatch, module, CATALOGUE)
    payload = describe("", "")
    assert payload["model_source"] == model_defaults.SOURCE_DEFAULT
    assert "predefinido" in payload["detail"], (
        "the user is not told the model was chosen for them")


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_a_chosen_model_is_reported_as_configured(monkeypatch, configured_secret,
                                                  module_and_describe):
    module, describe = module_and_describe
    stub_catalogue(monkeypatch, module, CATALOGUE)
    payload = describe("acme-large-0001", "")
    assert payload["state"] == ProviderState.READY.value
    assert payload["model"] == "acme-large-0001"
    assert payload["model_source"] == model_defaults.SOURCE_CONFIGURED


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_a_stale_model_is_flagged_and_never_silently_replaced(
        monkeypatch, configured_secret, module_and_describe):
    """A decommissioned id 404s on every message. Substituting another model
    is what hid that failure for weeks."""
    module, describe = module_and_describe
    stub_catalogue(monkeypatch, module, CATALOGUE)
    payload = describe("acme-retired-9999", "")
    assert payload["state"] == ProviderState.MODEL_UNAVAILABLE.value
    assert payload["model"] == "acme-retired-9999", (
        "a model that does not exist was quietly swapped for one that does")
    assert "acme-small-0001" in payload["detail"], (
        "the account's real models are not offered as the fix")


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_no_key_still_asks_for_a_key(monkeypatch, no_secret, module_and_describe):
    """The default must not paper over an unconfigured provider: with no
    credential there is no catalogue to adopt from and nothing to probe."""
    module, describe = module_and_describe

    def _never_called(*_a, **_k):
        raise AssertionError("an unconfigured provider was probed over the network")

    stub_catalogue(monkeypatch, module, [])
    monkeypatch.setattr(module, "list_mistral_models" if "mistral" in module.__name__
                        else "list_google_models", _never_called)
    payload = describe("", "")
    assert payload["state"] == ProviderState.SETUP_REQUIRED.value
    assert payload["model_source"] == model_defaults.SOURCE_NONE
    assert "chave" in payload["detail"].lower()


@pytest.mark.parametrize("module_and_describe", describers(),
                         ids=["mistral", "google"])
def test_a_key_whose_account_offers_nothing_usable_does_not_ask_for_a_key(
        monkeypatch, configured_secret, module_and_describe):
    """Two different problems must not share one sentence.

    SETUP_REQUIRED now means "no key" OR "a key whose account has nothing Nano
    can use". Telling the second user to paste a key they already pasted is
    Nano reporting a state it did not measure.
    """
    module, describe = module_and_describe
    stub_catalogue(monkeypatch, module, [record("acme-a", tool_calling=False)])
    payload = describe("", "")
    assert payload["state"] == ProviderState.SETUP_REQUIRED.value
    assert payload["model_source"] == model_defaults.SOURCE_NONE
    assert "chave" not in payload["detail"].lower(), payload["detail"]
    assert payload["secret"]["configured"] is True


# --------------------------------------------------------------------------
#  AUTO order: declared, preference-first, and the same every time
# --------------------------------------------------------------------------


def payloads(**states) -> dict[str, dict]:
    """One payload per cloud provider, defaulting to READY."""
    out = {}
    for pid in providers.CLOUD_PROVIDER_IDS:
        state = states.get(pid, ProviderState.READY)
        out[pid] = {"id": pid, "state": state.value, "model": f"{pid}-model",
                    "tiers": {"fast": f"{pid}-model", "complex": f"{pid}-model"},
                    "detail": f"{pid} detail"}
    return out


LOCAL_READY = {"id": "ollama", "state": ProviderState.READY.value,
               "model": "qwen3:8b", "detail": "local pronto"}


def test_the_fallback_order_after_the_preferred_one_is_the_declared_one():
    """The order is a source constant, not an accident of iteration.

    ``CLOUD_PROVIDER_IDS`` is the single authority; this asserts the router
    reproduces it rather than restating the literal, so changing the constant
    changes the behaviour and this test keeps passing for the right reason.
    """
    for preferred in providers.CLOUD_PROVIDER_IDS:
        order = [pid for pid, _ in providers.cloud_candidates(preferred, payloads())]
        expected = [preferred] + [pid for pid in providers.CLOUD_PROVIDER_IDS
                                  if pid != preferred]
        assert order == expected, f"preferred={preferred}"


def test_the_auto_order_is_identical_on_repeated_resolutions():
    """Two consecutive turns must route the same way.

    An order derived from measured latency would reorder itself between two
    identical questions, for reasons the user cannot see.
    """
    seen = {tuple(pid for pid, _ in providers.cloud_candidates("groq", payloads()))
            for _ in range(25)}
    assert len(seen) == 1, seen


def test_auto_puts_the_preferred_provider_first_and_keeps_the_rest_as_alternatives():
    for preferred in providers.CLOUD_PROVIDER_IDS:
        route = providers.resolve_route(
            ProviderMode.AUTO, payloads()[ProviderId.GROQ.value], LOCAL_READY,
            google=payloads()[ProviderId.GOOGLE.value],
            mistral=payloads()[ProviderId.MISTRAL.value], preferred=preferred)
        assert route["provider"] == preferred
        assert route["alternatives"] == [pid for pid in providers.CLOUD_PROVIDER_IDS
                                         if pid != preferred]


def test_an_unusable_preferred_provider_is_skipped_in_auto_but_not_in_cloud():
    """The two modes disagree here on purpose, and that IS the contract."""
    unusable = payloads(google=ProviderState.SETUP_REQUIRED)
    auto = providers.resolve_route(
        ProviderMode.AUTO, unusable["groq"], LOCAL_READY, google=unusable["google"],
        mistral=unusable["mistral"], preferred="google")
    assert auto["provider"] == "groq", "AUTO refused to move past a dead preference"

    cloud = providers.resolve_route(
        ProviderMode.CLOUD, unusable["groq"], LOCAL_READY, google=unusable["google"],
        mistral=unusable["mistral"], preferred="google")
    assert cloud["provider"] == "google"
    assert cloud["usable"] is False
    assert cloud["alternatives"] == [], "CLOUD offered a vendor the user did not pick"


def test_cloud_mode_never_carries_an_alternative_whatever_the_preference():
    for preferred in providers.CLOUD_PROVIDER_IDS:
        route = providers.resolve_route(
            ProviderMode.CLOUD, payloads()[ProviderId.GROQ.value], LOCAL_READY,
            google=payloads()[ProviderId.GOOGLE.value],
            mistral=payloads()[ProviderId.MISTRAL.value], preferred=preferred)
        assert route["provider"] == preferred
        assert route["alternatives"] == []
        assert route["fallback"] is False


def test_local_mode_resolves_no_hostname_off_this_machine(monkeypatch):
    """LOCAL's guarantee, asserted at the socket layer rather than in prose.

    ``resolve_route`` returning "ollama" only proves where the ANSWER comes
    from. The promise is stronger: in LOCAL mode nothing at all leaves the
    machine, not even a status probe -- so a describe of every provider must
    not resolve a single name that is not loopback. Asserting on state values
    would keep passing the day a probe is added back, because the payload it
    produced would still say DISABLED.
    """
    import socket

    from core import provider_status

    resolved: list[str] = []
    real = socket.getaddrinfo

    def _watch(host, port, *args, **kwargs):
        name = str(host or "")
        if name not in {"localhost", "127.0.0.1", "::1", ""}:
            resolved.append(name)
            raise AssertionError(f"LOCAL mode resolved {name!r}")
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _watch)

    clouds, ollama = provider_status.describe_all(
        ProviderMode.LOCAL,
        cloud_tiers={pid: (f"{pid}-model", f"{pid}-model")
                     for pid in providers.CLOUD_PROVIDER_IDS},
        ollama_model="qwen3:8b",
        # A port nothing listens on: the local probe is allowed to try and to
        # fail. What must not happen is a lookup for a cloud host.
        ollama_base_url="http://127.0.0.1:1",
        local_enabled=True)

    assert resolved == []
    for pid in providers.CLOUD_PROVIDER_IDS:
        assert clouds[pid]["state"] == ProviderState.DISABLED.value
        # A mode that does not read the key must not imply it did.
        assert clouds[pid]["secret"]["configured"] is False
        assert clouds[pid]["models"] == []
    assert ollama["id"] == ProviderId.OLLAMA.value


def test_local_mode_never_yields_a_cloud_provider_however_healthy_they_are():
    for preferred in providers.CLOUD_PROVIDER_IDS:
        route = providers.resolve_route(
            ProviderMode.LOCAL, payloads()[ProviderId.GROQ.value], LOCAL_READY,
            google=payloads()[ProviderId.GOOGLE.value],
            mistral=payloads()[ProviderId.MISTRAL.value], preferred=preferred)
        assert route["provider"] == ProviderId.OLLAMA.value
        assert route["alternatives"] == []


def test_every_cloud_provider_can_be_preferred_and_none_is_unreachable():
    """A provider that cannot be reached from any preference is dead code.

    Mistral was in that position: present in CLOUD_PROVIDER_IDS, selectable in
    Settings, and never routable because it could not reach READY.
    """
    reachable = set()
    for preferred in providers.CLOUD_PROVIDER_IDS:
        route = providers.resolve_route(
            ProviderMode.AUTO, payloads()[ProviderId.GROQ.value], LOCAL_READY,
            google=payloads()[ProviderId.GOOGLE.value],
            mistral=payloads()[ProviderId.MISTRAL.value], preferred=preferred)
        reachable.add(route["provider"])
    assert reachable == set(providers.CLOUD_PROVIDER_IDS)


# --------------------------------------------------------------------------
#  Settings: UI -> backend -> disk -> the NEXT request's routing
# --------------------------------------------------------------------------


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point user_settings at a temp file.

    The real ``user_settings.json`` is the user's own configuration and a test
    must never write to it -- a suite that rewrote the preferred provider would
    change the application the user opens next.
    """
    from core import user_settings

    monkeypatch.setattr(user_settings, "_PATH", tmp_path / "user_settings.json")
    monkeypatch.setattr(user_settings, "_cache", None)
    yield tmp_path / "user_settings.json"
    monkeypatch.setattr(user_settings, "_cache", None)


def test_a_saved_preference_survives_a_restart_and_reaches_the_router(
        isolated_settings, monkeypatch):
    """The whole chain, with a real file in the middle.

    "Persisted" is asserted by dropping the in-process cache -- which is what a
    restart does -- and reading the value back off disk, not by trusting the
    setter's return value.
    """
    from core import user_settings

    for provider_id in providers.CLOUD_PROVIDER_IDS:
        assert user_settings.set_value("preferred_cloud", provider_id)["ok"]
        assert user_settings.set_value(f"{provider_id}_fast_model", "acme-small-0001")["ok"]

        monkeypatch.setattr(user_settings, "_cache", None)      # the restart
        assert isolated_settings.exists()

        merged = user_settings.apply_overlay({})
        assert merged["preferred_cloud"] == provider_id
        assert providers.parse_preferred_cloud(merged["preferred_cloud"]) == provider_id

        route = providers.resolve_route(
            ProviderMode.AUTO, payloads()[ProviderId.GROQ.value], LOCAL_READY,
            google=payloads()[ProviderId.GOOGLE.value],
            mistral=payloads()[ProviderId.MISTRAL.value],
            preferred=providers.parse_preferred_cloud(merged["preferred_cloud"]))
        assert route["provider"] == provider_id


def test_a_setting_outside_the_allow_list_is_refused_and_never_written(
        isolated_settings):
    from core import user_settings

    result = user_settings.set_value("preferred_cloud_provider_typo", "groq")
    assert result["ok"] is False
    assert result["error"] == "unknown_setting"


def test_every_routable_provider_can_have_both_tiers_saved():
    """A provider whose model cannot be persisted is configurable only until
    the next restart, which is the same class of defect as not being
    configurable at all."""
    from core import user_settings

    for provider_id in providers.CLOUD_PROVIDER_IDS:
        for tier in ("fast", "complex"):
            assert f"{provider_id}_{tier}_model" in user_settings.ALLOWED_KEYS


def test_the_declared_fallback_order_is_the_benchmarked_one():
    """The order is evidence, and the evidence is in the repository.

    ``benchmarks/provider_routing/benchmark_results.json`` records the run this
    order came from. A change to CLOUD_PROVIDER_IDS that does not come with a
    new measurement will fail here, which is the point: the previous order was
    alphabetical and nobody could tell.
    """
    import json
    from pathlib import Path

    artifact = (Path(__file__).resolve().parent.parent / "benchmarks"
                / "provider_routing" / "benchmark_results.json")
    assert artifact.exists(), "the routing order has no benchmark artifact"
    recorded = json.loads(artifact.read_text(encoding="utf-8"))
    assert recorded["recommended_order"]["cloud"] == list(providers.CLOUD_PROVIDER_IDS), (
        "CLOUD_PROVIDER_IDS no longer matches the measurement it was derived from")
    assert recorded["recommended_order"]["terminal"] == ProviderId.OLLAMA.value
    assert recorded["recommended_order"]["default_primary"] == \
        providers.DEFAULT_CLOUD_PROVIDER


def test_the_default_primary_heads_the_declared_order():
    """Two constants, one answer.

    They are stated separately because they answer different questions -- who
    goes first when nobody chose, and who follows whoever did -- but a default
    that is not the head of the chain would mean AUTO's first hop with no
    preference differs from AUTO's first hop with the preference set to the
    same provider, for no reason anybody could explain.
    """
    assert providers.DEFAULT_CLOUD_PROVIDER == providers.CLOUD_PROVIDER_IDS[0]


def test_the_local_model_is_never_a_cloud_candidate():
    """Ollama's position is architectural, not measured, and it is not in the
    ranked tuple at all -- so it cannot be promoted by a future benchmark."""
    assert ProviderId.OLLAMA.value not in providers.CLOUD_PROVIDER_IDS
    assert providers.parse_preferred_cloud("ollama") == providers.DEFAULT_CLOUD_PROVIDER
