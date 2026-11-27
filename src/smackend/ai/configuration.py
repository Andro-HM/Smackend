from importlib.util import find_spec
import json
import re

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string


def configure(settings, environment, *, production):
    if find_spec("jsonschema") is None:
        raise ImproperlyConfigured("Install the Smackend ai extra before enabling AI.")
    # The models and service import the app registry. Load only transport config here.
    from .adapters import AnthropicAdapter, CompatibleAdapter, GeminiAdapter, OpenAIAdapter
    from .mock import MockAdapter

    adapters = {"openai": OpenAIAdapter, "compatible": CompatibleAdapter, "local": CompatibleAdapter,
                "gemini": GeminiAdapter, "anthropic": AnthropicAdapter}
    try:
        entries = json.loads(environment.get("SMACKEND_AI_MODELS", "{}"))
        quotas = json.loads(environment.get("SMACKEND_AI_QUOTAS", "{}"))
        if (not isinstance(entries, dict) or len(entries) > 50 or not isinstance(quotas, dict)
                or set(quotas) - {"user", "app"}):
            raise ValueError
        for quota in quotas.values():
            if not isinstance(quota, dict) or set(quota) - {"requests", "tokens", "cost_micros", "concurrent"}:
                raise ValueError
            if any((type(v) is not int or not 1 <= v <= 10**15) for v in quota.values()):
                raise ValueError
    except (TypeError, ValueError):
        raise ImproperlyConfigured("Supply valid SMACKEND_AI_MODELS and SMACKEND_AI_QUOTAS JSON objects.") from None
    models = {"offline": (MockAdapter(), "offline", MockAdapter.supported, None)}
    http_config = {}
    for alias, entry in entries.items():
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,49}", alias) or alias == "offline" or not isinstance(entry, dict)
                or set(entry) - {"adapter", "provider", "model", "capabilities", "prefix", "price", "replay_429"}):
            raise ImproperlyConfigured("Use unique lowercase AI model aliases and recognized configuration fields.")
        try:
            definition = entry["adapter"]
            provider = settings["SMACKEND_OUTBOUND_PROVIDERS"][entry["provider"]]
            if definition == "anthropic" and not any(key.lower() == "anthropic-version" for key in provider.headers):
                # API version is a transport protocol, independent of selected models.
                from dataclasses import replace

                provider = replace(provider, headers={**provider.headers, "anthropic-version": "2023-06-01"})
            adapter_type = adapters.get(definition) or import_string(definition)
            capabilities = frozenset(entry["capabilities"])
            if not isinstance(entry["model"], str) or not 1 <= len(entry["model"]) <= 180:
                raise ValueError
            options = {}
            if entry.get("prefix") is not None:
                prefix = entry["prefix"]
                if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", prefix):
                    raise ValueError
                options["prefix"] = prefix
            replay = entry.get("replay_429", False)
            if type(replay) is not bool:
                raise ValueError
            adapter = adapter_type(provider, replay_429=replay, **options)
            if not capabilities or not capabilities <= adapter.supported:
                raise ValueError
            price = entry.get("price")
            if price is not None and (not isinstance(price, list) or len(price) != 2 or
                                      any(type(rate) is not int or not 0 <= rate <= 10**12 for rate in price)):
                raise ValueError
            models[alias] = (adapter, entry["model"], capabilities, price)
            if definition in adapters and adapter.transport is None:
                http_config[alias] = (entry["provider"], adapter.provider)
        except (KeyError, ValueError, TypeError, AttributeError, ImportError):
            raise ImproperlyConfigured(
                f"AI model {alias} needs a valid configured HTTP provider, adapter, model and capability set.",
            ) from None
    demo = environment.get("SMACKEND_AI_DEMO_MODEL", "offline")
    if demo and (demo not in models or "text" not in models[demo][2]):
        raise ImproperlyConfigured("SMACKEND_AI_DEMO_MODEL must name a configured text model or be empty.")
    settings.update(SMACKEND_AI_CONFIG=models, SMACKEND_AI_QUOTA_CONFIG=quotas, SMACKEND_AI_DEMO_MODEL=demo)
    settings["SMACKEND_AI_HTTP_CONFIG"] = http_config
    settings["INSTALLED_APPS"] += ["smackend.ai"]


def configured_service():
    from django.conf import settings

    from smackend.outbound.asgi import ai_clients

    from .ledger import Quota
    from .service import Model, Service, Tariff

    models = {alias: Model(name, adapter, caps, Tariff(*price) if price is not None else None)
              for alias, (adapter, name, caps, price) in settings.SMACKEND_AI_CONFIG.items()}
    quotas = settings.SMACKEND_AI_QUOTA_CONFIG
    return Service(models, clients=ai_clients(), user_quota=Quota(**quotas.get("user", {})),
                   app_quota=Quota(**({"requests": 1000, "concurrent": 20, "tokens": 2_500_000}
                                     | quotas.get("app", {}))))
