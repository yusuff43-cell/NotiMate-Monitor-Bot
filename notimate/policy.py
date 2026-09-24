"""Confirmation policy per event type (Этап 5 of docs/21).

Three modes decide whether a parsed event is written straight away or goes through the
draft → confirm/edit/cancel flow first:

* ``auto`` — record immediately (JSC's LINE pipeline: unchanged, it never consults this).
* ``confirm`` — always show a draft with buttons.
* ``confirm_if_low_confidence`` — record immediately when the extraction passes the
  pack's own sanity checks, otherwise fall back to a draft.

Defaults live here per vertical pack; a tenant can override any event type through
``tenants.modules['confirmation']``, e.g. ``{"confirmation": {"report": "auto"}}``.
The repeat-tap guarantee (a draft settles exactly once) is enforced by each pack's
draft store, not here — this module only chooses the path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AUTO = 'auto'
CONFIRM = 'confirm'
CONFIRM_IF_LOW_CONFIDENCE = 'confirm_if_low_confidence'
POLICIES = (AUTO, CONFIRM, CONFIRM_IF_LOW_CONFIDENCE)

DEFAULT_POLICIES: dict[str, dict[str, str]] = {
    'location_reports': {'report': CONFIRM},
    'accountant': {'document': CONFIRM_IF_LOW_CONFIDENCE},
}


def resolve_policy(tenant: Mapping[str, Any], pack: str, event_type: str) -> str:
    """Effective policy for one (pack, event type). Unknown or invalid values fall back
    to the safest mode, ``confirm``, so a typo in tenant settings can never turn a
    confirmed flow into silent auto-saving."""
    modules = tenant.get('modules') or {}
    overrides = modules.get('confirmation') if isinstance(modules, Mapping) else None
    if isinstance(overrides, Mapping) and overrides.get(event_type) in POLICIES:
        return overrides[event_type]
    default = DEFAULT_POLICIES.get(pack, {}).get(event_type, CONFIRM)
    return default if default in POLICIES else CONFIRM


def needs_confirmation(policy: str, confident: bool) -> bool:
    if policy == AUTO:
        return False
    if policy == CONFIRM_IF_LOW_CONFIDENCE:
        return not confident
    return True
