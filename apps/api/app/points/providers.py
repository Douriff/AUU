"""Top-up channels behind one interface (payment provider abstraction).

Shipped today:
  * ``manual_admin``  an admin books a top-up the user paid outside the platform. OFF unless
                      AUU_POINTS_MANUAL=on; every booking needs the admin's password + 2FA and a reason.
  * ``mock``          test double that "pays" instantly. Only works inside the test runner
                      (AUU_TEST=1); it can never be switched on in production.
Reserved slots (NOT implemented, never enabled, no keys anywhere): ``alipay``, ``wechat_pay``, ``stripe``.
Wiring a real one later = implement ``create_order`` + ``parse_webhook`` (signature check with the
merchant's own keys from the environment), add it to AUU_POINTS_PROVIDERS, and credit on the
verified webhook only (never on the browser redirect).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from app.data_paths import tests_active


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "on", "yes"}


def _listed(pid: str) -> bool:
    raw = os.getenv("AUU_POINTS_PROVIDERS", "")
    return pid in {x.strip().lower() for x in raw.split(",") if x.strip()}


@dataclass
class OrderResult:
    status: str  # "paid" (credit now) | "pending" (wait for the verified webhook) | "failed"
    external_ref: Optional[str] = None
    redirect_url: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class PaymentProvider:
    id = ""
    user_initiated = True  # the user starts it from the top-up page
    test_only = False
    implemented = True

    def enabled(self) -> bool:
        return False

    def create_order(self, *, order_id: str, user_id: str, points: int) -> OrderResult:
        raise NotImplementedError

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> Optional[dict[str, Any]]:
        """Return {"order_id", "external_ref", "paid": bool} after verifying the signature, else None."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "enabled": self.enabled(), "userInitiated": self.user_initiated, "testOnly": self.test_only,
                "implemented": self.implemented}


class ManualAdminProvider(PaymentProvider):
    id = "manual_admin"
    user_initiated = False

    def enabled(self) -> bool:
        return _on("AUU_POINTS_MANUAL")

    def create_order(self, *, order_id: str, user_id: str, points: int) -> OrderResult:
        return OrderResult(status="paid", external_ref=None)


class MockProvider(PaymentProvider):
    id = "mock"
    test_only = True

    def enabled(self) -> bool:
        return tests_active() and _on("AUU_POINTS_MOCK")

    def create_order(self, *, order_id: str, user_id: str, points: int) -> OrderResult:
        if not self.enabled():
            raise RuntimeError("mock provider is test-only")
        if points == 13:  # deterministic failure path for tests
            return OrderResult(status="failed", external_ref=f"mock-{order_id}")
        return OrderResult(status="paid", external_ref=f"mock-{order_id}")


class _Reserved(PaymentProvider):
    """Placeholder: no implementation, no keys, cannot be enabled."""
    implemented = False

    def enabled(self) -> bool:
        return False

    def create_order(self, *, order_id: str, user_id: str, points: int) -> OrderResult:
        raise NotImplementedError(f"{self.id} is not implemented")

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> Optional[dict[str, Any]]:
        raise NotImplementedError(f"{self.id} is not implemented")


class AlipayProvider(_Reserved):
    id = "alipay"


class WechatPayProvider(_Reserved):
    id = "wechat_pay"


class StripeProvider(_Reserved):
    id = "stripe"


REGISTRY: dict[str, PaymentProvider] = {p.id: p for p in (ManualAdminProvider(), MockProvider(), AlipayProvider(), WechatPayProvider(), StripeProvider())}


def get_provider(pid: str) -> Optional[PaymentProvider]:
    return REGISTRY.get((pid or "").strip().lower())


def user_channels() -> list[PaymentProvider]:
    """Channels a user can start a top-up with right now (none in production today)."""
    out = []
    for p in REGISTRY.values():
        if not (p.user_initiated and p.implemented and p.enabled()):
            continue
        if not p.test_only and not _listed(p.id):
            continue
        out.append(p)
    return out


def topup_status() -> dict[str, Any]:
    chans = user_channels()
    manual = REGISTRY["manual_admin"].enabled()
    # The mock never shows in a production listing (it is only enabled under the test runner).
    shown = [p.describe() for p in REGISTRY.values() if not p.test_only or p.enabled()]
    from app.points import trc20

    ids = [p.id for p in chans]
    if trc20.enabled():  # watch-only USDT-TRC20 (own order flow, see app.points.trc20)
        ids.append(trc20.PROVIDER_ID)
    shown.append({"id": trc20.PROVIDER_ID, "enabled": trc20.enabled(), "userInitiated": True, "testOnly": False, "implemented": True})
    return {"open": bool(ids), "channels": ids, "manual": manual, "providers": shown}
