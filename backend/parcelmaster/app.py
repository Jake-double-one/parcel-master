"""The HTTP API and the wiring of everything behind it."""

# No "from __future__ import annotations" here: FastAPI has to resolve the
# dependency aliases defined inside create_app() when it reads the handlers.

import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, importer
from .carriers.base import CarrierModule
from .carriers.registry import Registry, discover
from .config import Config
from .db import Store, merge, now_iso
from .i18n import Translations
from .notify import Notifier, vapid_keys
from .scheduler import Scheduler
from .sessions import COOKIE, DEMO_TTL, LOGIN_TTL, DemoSessions, LoginThrottle, Signer
from .status import FINAL, Status, aggregate
from .tracking import daily_budget, parse_iso, pickup_deadline, refresh_parcel

SECRET_MASK = "********"
DEMO_REFRESH_SECONDS = 15
MAX_EVENTS = 40
DEMO_CARRIER = "demo"


@dataclass
class State:
    config: Config
    store: Store
    registry: Registry
    translations: Translations
    signer: Signer
    demos: DemoSessions
    throttle: LoginThrottle
    http: httpx.AsyncClient
    notifier: Notifier
    scheduler: Scheduler
    background: set[asyncio.Task[Any]]


@dataclass
class Ctx:
    """What a request may see: the real database or one demo visitor's."""

    state: State
    store: Store
    demo: bool

    def modules(self) -> list[CarrierModule]:
        if self.demo:
            return [m for m in self.state.registry.all() if m.id == DEMO_CARRIER]
        show_demo = self.state.config.demo_carrier
        return [m for m in self.state.registry.all() if m.id != DEMO_CARRIER or show_demo]

    def module(self, module_id: str) -> CarrierModule | None:
        return next((m for m in self.modules() if m.id == module_id), None)


class ParcelIn(BaseModel):
    id: int | None = None
    carrier: str
    tracking_number: str = Field(min_length=3, max_length=60)
    extra: dict[str, str] = {}


class ShipmentIn(BaseModel):
    title: str = Field(default="", max_length=120)
    direction: Literal["incoming", "outgoing", "return"] = "incoming"
    note: str = Field(default="", max_length=2000)
    parcels: list[ParcelIn] = Field(min_length=1, max_length=20)


def fail(status: int, code: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": code, **extra})


def create_app(config: Config) -> FastAPI:
    registry = discover()
    store = Store(config.data_dir / "parcelmaster.db")
    translations = Translations(registry.locales, config.data_dir / "locales")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=20, headers={"User-Agent": f"ParcelMaster/{__version__}"})
        notifier = Notifier(store, registry, translations, config.push_contact)
        scheduler = Scheduler(store, registry, http, notifier, config.data_dir)
        app.state.pm = State(
            config=config,
            store=store,
            registry=registry,
            translations=translations,
            signer=Signer(store),
            demos=DemoSessions(),
            throttle=LoginThrottle(),
            http=http,
            notifier=notifier,
            scheduler=scheduler,
            background=set(),
        )
        if config.scheduler:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await http.aclose()
            store.close()

    app = FastAPI(title="Parcel Master", version=__version__, lifespan=lifespan)

    # -- who is asking ----------------------------------------------------

    def session_of(request: Request) -> tuple[State, tuple[str, str] | None]:
        state: State = request.app.state.pm
        return state, state.signer.verify(request.cookies.get(COOKIE))

    def get_ctx(request: Request) -> Ctx:
        state, session = session_of(request)
        if session and session[0] == "demo":
            demo_store = state.demos.get(session[1])
            if demo_store is not None:
                return Ctx(state, demo_store, True)
            session = None  # the demo expired
        if state.config.password is None or (session and session[0] == "user"):
            return Ctx(state, state.store, False)
        raise fail(401, "login_required")

    def real_ctx(ctx: Annotated[Ctx, Depends(get_ctx)]) -> Ctx:
        if ctx.demo:
            raise fail(403, "not_in_demo")
        return ctx

    Context = Annotated[Ctx, Depends(get_ctx)]
    RealContext = Annotated[Ctx, Depends(real_ctx)]

    def set_cookie(response: Response, request: Request, value: str, ttl: int) -> None:
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(
            COOKIE, value, max_age=ttl, httponly=True, samesite="lax", secure=secure, path="/"
        )

    # -- presentation -----------------------------------------------------

    def present_parcel(ctx: Ctx, parcel: dict[str, Any], language: str) -> dict[str, Any]:
        module = ctx.state.registry.get(parcel["carrier"])
        deadline = pickup_deadline(parcel, module)
        hidden = ("position", "error_count", "stale_notified_at", "pickup_reminded_at")
        return {
            **{key: value for key, value in parcel.items() if key not in hidden},
            "events": parcel["events"][:MAX_EVENTS],
            # An imported parcel names the carrier that delivers it, once known.
            "carrier_name": parcel["extra"].get("carrier_name")
            or (module.name if module else parcel["carrier"]),
            # Whose website the link below opens.
            "site_name": module.name if module else parcel["carrier"],
            "tracking_url": (
                module.tracking_url(parcel["tracking_number"], parcel["extra"], language) or None
                if module
                else None
            ),
            "pickup_deadline": deadline.isoformat(timespec="seconds") if deadline else None,
        }

    def present(ctx: Ctx, shipment: dict[str, Any], language: str) -> dict[str, Any]:
        parcels = [present_parcel(ctx, parcel, language) for parcel in shipment["parcels"]]
        statuses = [Status(parcel["status"]) for parcel in parcels]
        open_parcels = [p for p in parcels if Status(p["status"]) not in FINAL]
        etas = sorted(p["eta_from"] for p in open_parcels if p["eta_from"])
        eta_ends = sorted(p["eta_to"] for p in open_parcels if p["eta_from"] and p["eta_to"])
        deadlines = sorted(p["pickup_deadline"] for p in parcels if p["pickup_deadline"])
        checked = [p["last_checked_at"] for p in (open_parcels or parcels)]
        return {
            **shipment,
            "parcels": parcels,
            "status": str(aggregate(statuses)),
            "parcel_count": len(parcels),
            "finished_count": len(parcels) - len(open_parcels),
            "eta_from": etas[0] if etas else None,
            "eta_to": eta_ends[0] if eta_ends else None,
            "pickup_deadline": deadlines[0] if deadlines else None,
            # The oldest lookup among the open parcels: how fresh the whole row is.
            "last_checked_at": min(checked) if checked and all(checked) else None,
        }

    def language_of(ctx: Ctx) -> str:
        return ctx.state.translations.resolve(ctx.store.get_settings()["language"])

    async def refresh(ctx: Ctx, parcel_ids: list[int]) -> None:
        for parcel_id in parcel_ids:
            notices = await refresh_parcel(ctx.store, ctx.state.registry, ctx.state.http, parcel_id)
            if not ctx.demo:
                await ctx.state.notifier.handle(notices)

    def refresh_soon(ctx: Ctx, parcel_ids: list[int]) -> None:
        task = asyncio.create_task(refresh(ctx, parcel_ids))
        ctx.state.background.add(task)
        task.add_done_callback(ctx.state.background.discard)

    # -- meta, languages, login -------------------------------------------

    @app.get("/api/meta")
    def meta(request: Request) -> dict[str, Any]:
        state, session = session_of(request)
        demo = bool(session and session[0] == "demo" and state.demos.get(session[1]) is not None)
        logged_in = bool(session and session[0] == "user")
        password_set = state.config.password is not None
        active_store = state.demos.get(session[1]) if demo and session else None
        show_settings = demo or logged_in or not password_set
        return {
            "version": __version__,
            "login_required": password_set and not logged_in and not demo,
            "password_set": password_set,
            "demo_available": state.config.demo,
            "demo": demo,
            "languages": state.translations.languages(),
            "language": (
                state.translations.resolve((active_store or state.store).get_settings()["language"])
                if show_settings
                else None
            ),
        }

    @app.get("/api/i18n/{language}")
    def i18n(language: str, request: Request) -> dict[str, str]:
        translations: Translations = request.app.state.pm.translations
        if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language):
            raise fail(404, "unknown_language")
        return translations.catalog(language)

    @app.post("/api/auth/login")
    def login(
        request: Request, response: Response, password: Annotated[str, Body(embed=True)]
    ) -> dict[str, bool]:
        state: State = request.app.state.pm
        client = request.client.host if request.client else "unknown"
        if state.config.password is None:
            return {"ok": True}
        if state.throttle.blocked(client):
            raise fail(429, "too_many_attempts")
        if not hmac.compare_digest(password.encode(), state.config.password.encode()):
            state.throttle.failed(client)
            raise fail(401, "wrong_password")
        state.throttle.succeeded(client)
        set_cookie(response, request, state.signer.sign("user", "1", LOGIN_TTL), LOGIN_TTL)
        return {"ok": True}

    @app.post("/api/auth/demo")
    def start_demo(
        request: Request, response: Response, language: Annotated[str, Body(embed=True)] = "en"
    ) -> dict[str, bool]:
        state: State = request.app.state.pm
        if not state.config.demo:
            raise fail(404, "demo_disabled")
        session_id = state.demos.create(state.translations.resolve(language))
        set_cookie(response, request, state.signer.sign("demo", session_id, DEMO_TTL), DEMO_TTL)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # -- carriers ---------------------------------------------------------

    def sync_state(ctx: Ctx, module: CarrierModule) -> dict[str, Any] | None:
        if module.kind != "importer":
            return None
        state = importer.get_state(ctx.store, module.id)
        return {
            "last_sync_at": state.get("last_sync_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_error": state.get("last_error"),
            "found": state.get("found"),
        }

    @app.post("/api/importers/{module_id}/sync")
    async def sync_importer(ctx: RealContext, module_id: str) -> dict[str, Any]:
        module = ctx.module(module_id)
        if not isinstance(module, importer.ImporterModule):
            raise fail(404, "not_found")
        if importer.too_soon(ctx.store, module):
            raise fail(429, "too_soon")
        notices = await importer.run(
            ctx.store, ctx.state.registry, module, ctx.state.config.data_dir
        )
        await ctx.state.notifier.handle(notices)
        return sync_state(ctx, module)

    @app.get("/api/carriers")
    def carriers(ctx: Context) -> list[dict[str, Any]]:
        settings = ctx.store.get_settings()
        result = []
        for module in ctx.modules():
            own = settings["carriers"].get(module.id, {})
            result.append(
                {
                    "id": module.id,
                    "name": module.name,
                    "country": module.country,
                    "kind": module.kind,
                    "sync": sync_state(ctx, module),
                    "configured": module.is_configured(own),
                    "settings_fields": [vars(f) for f in module.settings_fields],
                    "parcel_fields": [vars(f) for f in module.parcel_fields],
                    "default_daily_budget": module.default_daily_budget,
                    "daily_budget": daily_budget(module, own),
                    "used_today": ctx.store.usage_today(module.id),
                }
            )
        return result

    @app.post("/api/detect")
    def detect(ctx: Context, text: Annotated[str, Body(embed=True, max_length=2000)]) -> dict:
        """Find carrier and tracking number in pasted text: a link or a bare number."""
        text = text.strip()
        matches: list[dict[str, str]] = []
        for candidate in re.findall(r"https?://\S+", text):
            if not urlparse(candidate).hostname:
                continue
            for module in ctx.modules():
                number = module.extract_from_url(candidate)
                if number:
                    matches.append({"carrier": module.id, "tracking_number": number})
        if not matches:
            # The whole text, single words, and digit groups typed with spaces.
            tokens = [
                text,
                *re.findall(r"[A-Za-z0-9]{8,40}", text),
                *re.findall(r"\d[\d ]{6,40}\d", text),
            ]
            for token in tokens:
                for module in ctx.modules():
                    if module.matches(token):
                        match = {"carrier": module.id, "tracking_number": module.normalize(token)}
                        if match not in matches:
                            matches.append(match)
        return {"matches": matches}

    # -- shipments --------------------------------------------------------

    @app.get("/api/shipments")
    async def list_shipments(ctx: Context, archived: bool = False) -> list[dict[str, Any]]:
        shipments = ctx.store.list_shipments(archived)
        if ctx.demo and not archived:
            # The demo has no background loop; it catches up when someone looks.
            cutoff = (datetime.now(UTC) - timedelta(seconds=DEMO_REFRESH_SECONDS)).isoformat()
            due = [
                parcel["id"]
                for shipment in shipments
                for parcel in shipment["parcels"]
                if Status(parcel["status"]) not in FINAL
                and (parcel["last_checked_at"] or "") < cutoff
            ]
            if due:
                await refresh(ctx, due)
                shipments = ctx.store.list_shipments(archived)
        language = language_of(ctx)
        return [present(ctx, shipment, language) for shipment in shipments]

    def clean_parcels(
        ctx: Ctx, payload: ShipmentIn, shipment_id: int | None
    ) -> list[dict[str, Any]]:
        parcels: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        stored = ctx.store.get_shipment(shipment_id) if shipment_id is not None else None
        stored_parcels = {p["id"]: p for p in (stored["parcels"] if stored else [])}
        for item in payload.parcels:
            module = ctx.module(item.carrier)
            if module is None:
                raise fail(422, "unknown_carrier", carrier=item.carrier)
            before = stored_parcels.get(item.id or -1)
            if module.kind == "importer":
                # Only the importing service creates these; editing may keep them as they are.
                if not before or (before["carrier"], before["tracking_number"]) != (
                    module.id,
                    item.tracking_number,
                ):
                    raise fail(422, "unknown_carrier", carrier=item.carrier)
                seen.add((module.id, item.tracking_number))
                parcels.append(
                    {
                        "id": item.id,
                        "carrier": module.id,
                        "tracking_number": item.tracking_number,
                        "extra": before["extra"],
                    }
                )
                continue
            number = module.normalize(item.tracking_number)
            if len(number) < 3:
                raise fail(422, "invalid_tracking_number")
            if (module.id, number) in seen:
                raise fail(409, "duplicate", tracking_number=number, title="")
            seen.add((module.id, number))
            duplicate = ctx.store.find_duplicate(module.id, number, shipment_id)
            if duplicate:
                raise fail(
                    409,
                    "duplicate",
                    tracking_number=number,
                    title=duplicate["title"],
                    shipment_id=duplicate["id"],
                )
            allowed = {f.key for f in module.parcel_fields}
            extra = {k: v.strip()[:80] for k, v in item.extra.items() if k in allowed and v.strip()}
            if before and (before["carrier"], before["tracking_number"]) == (module.id, number):
                # Keep what an importer noted on this parcel; the form only knows its own fields.
                extra = {**{k: v for k, v in before["extra"].items() if k not in allowed}, **extra}
            parcels.append(
                {"id": item.id, "carrier": module.id, "tracking_number": number, "extra": extra}
            )
        return parcels

    async def after_save(ctx: Ctx, shipment_id: int) -> dict[str, Any]:
        shipment = ctx.store.get_shipment(shipment_id)
        fresh = [p["id"] for p in shipment["parcels"] if p["last_checked_at"] is None]
        if ctx.demo:
            await refresh(ctx, fresh)
            shipment = ctx.store.get_shipment(shipment_id)
        elif fresh:
            refresh_soon(ctx, fresh)
        return present(ctx, shipment, language_of(ctx))

    @app.post("/api/shipments", status_code=201)
    async def create_shipment(ctx: Context, payload: ShipmentIn) -> dict[str, Any]:
        parcels = clean_parcels(ctx, payload, None)
        data = {
            "title": payload.title.strip(),
            "direction": payload.direction,
            "note": payload.note.strip(),
        }
        return await after_save(ctx, ctx.store.save_shipment(None, data, parcels))

    @app.put("/api/shipments/{shipment_id}")
    async def update_shipment(ctx: Context, shipment_id: int, payload: ShipmentIn) -> dict:
        if ctx.store.get_shipment(shipment_id) is None:
            raise fail(404, "not_found")
        parcels = clean_parcels(ctx, payload, shipment_id)
        data = {
            "title": payload.title.strip(),
            "direction": payload.direction,
            "note": payload.note.strip(),
        }
        return await after_save(ctx, ctx.store.save_shipment(shipment_id, data, parcels))

    @app.delete("/api/shipments/{shipment_id}", status_code=204)
    def delete_shipment(ctx: Context, shipment_id: int) -> None:
        if not ctx.store.delete_shipment(shipment_id):
            raise fail(404, "not_found")

    @app.post("/api/shipments/{shipment_id}/archive")
    def archive(
        ctx: Context, shipment_id: int, archived: Annotated[bool, Body(embed=True)] = True
    ) -> dict[str, Any]:
        if not ctx.store.set_archived(shipment_id, archived):
            raise fail(404, "not_found")
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    @app.post("/api/shipments/{shipment_id}/refresh")
    async def refresh_shipment(ctx: Context, shipment_id: int) -> dict[str, Any]:
        shipment = ctx.store.get_shipment(shipment_id)
        if shipment is None:
            raise fail(404, "not_found")
        await refresh(
            ctx, [p["id"] for p in shipment["parcels"] if Status(p["status"]) not in FINAL]
        )
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    # -- settings ---------------------------------------------------------

    def secret_keys(module: CarrierModule) -> set[str]:
        return {f.key for f in module.settings_fields if f.type == "secret"}

    def masked_settings(ctx: Ctx) -> dict[str, Any]:
        settings = ctx.store.get_settings()
        carriers_out: dict[str, Any] = {}
        for module in ctx.modules():
            own = dict(settings["carriers"].get(module.id, {}))
            for key in secret_keys(module):
                own[key] = SECRET_MASK if own.get(key) else ""
            carriers_out[module.id] = own
        return {**settings, "carriers": carriers_out}

    @app.get("/api/settings")
    def get_settings(ctx: Context) -> dict[str, Any]:
        return masked_settings(ctx)

    def bounded(value: Any, low: int, high: int, default: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    @app.put("/api/settings")
    def put_settings(ctx: Context, incoming: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        current = ctx.store.get_settings()
        merged = merge(current, {k: v for k, v in incoming.items() if k != "carriers"})

        merged["language"] = ctx.state.translations.resolve(str(merged.get("language", "en")))
        try:
            ZoneInfo(str(merged.get("timezone")))
        except (ZoneInfoNotFoundError, ValueError):
            raise fail(422, "invalid_timezone") from None
        polling = merged["polling"]
        defaults = current["polling"]
        merged["polling"] = {
            "interval_minutes": bounded(
                polling.get("interval_minutes"), 15, 1440, defaults["interval_minutes"]
            ),
            "stale_days": bounded(polling.get("stale_days"), 1, 30, defaults["stale_days"]),
            "auto_archive_days": bounded(
                polling.get("auto_archive_days"), 0, 90, defaults["auto_archive_days"]
            ),
        }
        notifications = merged["notifications"]
        urls = notifications.get("apprise_urls")
        notifications["apprise_urls"] = (
            [str(u).strip()[:500] for u in urls if str(u).strip()][:10]
            if isinstance(urls, list)
            else []
        )
        notifications["push_enabled"] = bool(notifications.get("push_enabled"))
        notifications["events"] = {
            key: bool(notifications.get("events", {}).get(key))
            for key in current["notifications"]["events"]
        }
        quiet = notifications.get("quiet_hours") or {}
        for key in ("from", "to"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(quiet.get(key, ""))):
                raise fail(422, "invalid_quiet_hours")
        notifications["quiet_hours"] = {
            "enabled": bool(quiet.get("enabled")),
            "from": quiet["from"],
            "to": quiet["to"],
        }

        carriers_in = incoming.get("carriers")
        if isinstance(carriers_in, dict):
            for module in ctx.modules():
                given = carriers_in.get(module.id)
                if not isinstance(given, dict):
                    continue
                own = dict(current["carriers"].get(module.id, {}))
                for field in module.settings_fields:
                    if field.key not in given:
                        continue
                    value = str(given[field.key] or "").strip()
                    if field.type == "secret" and value == SECRET_MASK:
                        continue  # unchanged: the browser never saw the real value
                    own[field.key] = value
                if "daily_budget" in given:
                    budget = given["daily_budget"]
                    own["daily_budget"] = (
                        None if budget in (None, "") else bounded(budget, 1, 1_000_000, 1)
                    )
                merged["carriers"][module.id] = own
        ctx.store.save_settings(merged)
        return masked_settings(ctx)

    # -- notifications ----------------------------------------------------

    @app.post("/api/notifications/test")
    async def test_notification(ctx: RealContext) -> dict[str, Any]:
        settings = ctx.store.get_settings()
        t = ctx.state.translations.t
        language = language_of(ctx)
        message = {
            "kind": "test",
            "title": t(language, "notify.test.title"),
            "body": t(language, "notify.test.body"),
            "shipment_id": None,
        }
        return await ctx.state.notifier.send(message, settings)

    @app.get("/api/push/key")
    def push_key(ctx: RealContext) -> dict[str, str]:
        return {"public_key": vapid_keys(ctx.store)["public"]}

    @app.post("/api/push/subscriptions", status_code=204)
    def push_subscribe(ctx: RealContext, subscription: Annotated[dict[str, Any], Body()]) -> None:
        keys = subscription.get("keys")
        endpoint = subscription.get("endpoint")
        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("https://")
            or not isinstance(keys, dict)
            or not keys.get("p256dh")
            or not keys.get("auth")
        ):
            raise fail(422, "invalid_subscription")
        ctx.store.add_push_subscription(
            {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
        )

    @app.post("/api/push/unsubscribe", status_code=204)
    def push_unsubscribe(ctx: RealContext, endpoint: Annotated[str, Body(embed=True)]) -> None:
        ctx.store.remove_push_subscription(endpoint)

    # -- status, export, import -------------------------------------------

    @app.get("/api/status")
    def status(ctx: Context) -> dict[str, Any]:
        settings = ctx.store.get_settings()
        parcels = ctx.store.open_parcels()
        modules = []
        for module in ctx.modules():
            own = settings["carriers"].get(module.id, {})
            mine = [p for p in parcels if p["carrier"] == module.id]
            open_mine = [p for p in mine if Status(p["status"]) not in FINAL]
            errors = sorted(
                (p for p in open_mine if p["last_error"]),
                key=lambda p: p["last_checked_at"] or "",
            )
            successes = sorted(p["last_success_at"] for p in mine if p["last_success_at"])
            modules.append(
                {
                    "id": module.id,
                    "name": module.name,
                    "kind": module.kind,
                    "sync": sync_state(ctx, module),
                    "configured": module.is_configured(own),
                    "open_parcels": len(open_mine),
                    "daily_budget": daily_budget(module, own),
                    "used_today": ctx.store.usage_today(module.id),
                    "last_success_at": successes[-1] if successes else None,
                    "last_error": errors[-1]["last_error"] if errors else None,
                    "failing_parcels": len(errors),
                }
            )
        scheduler = ctx.state.scheduler
        return {
            "version": __version__,
            "scheduler": {
                "running": scheduler.running and not ctx.demo,
                "last_run": (
                    scheduler.last_run.isoformat(timespec="seconds")
                    if scheduler.last_run and not ctx.demo
                    else None
                ),
            },
            "modules": modules,
            "push_subscriptions": 0 if ctx.demo else len(ctx.store.push_subscriptions()),
        }

    @app.get("/api/export")
    def export(ctx: Context) -> dict[str, Any]:
        keep = ("title", "direction", "note", "source", "reference", "archived_at", "created_at")
        shipments = []
        for archived in (False, True):
            for shipment in ctx.store.list_shipments(archived):
                shipments.append(
                    {
                        **{key: shipment[key] for key in keep},
                        "parcels": [
                            {
                                "carrier": p["carrier"],
                                "tracking_number": p["tracking_number"],
                                "extra": p["extra"],
                                "created_at": p["created_at"],
                            }
                            for p in shipment["parcels"]
                        ],
                    }
                )
        return {
            "format": "parcelmaster",
            "version": 1,
            "exported_at": now_iso(),
            "shipments": shipments,
        }

    @app.post("/api/import")
    async def import_(ctx: Context, data: Annotated[dict[str, Any], Body()]) -> dict[str, int]:
        if data.get("format") != "parcelmaster" or not isinstance(data.get("shipments"), list):
            raise fail(422, "invalid_file")
        imported = skipped = 0
        new_parcels: list[int] = []
        for raw in data["shipments"][:1000]:
            try:
                payload = ShipmentIn.model_validate(raw)
                parcels = clean_parcels(ctx, payload, None)
            except (HTTPException, ValueError):
                skipped += 1
                continue
            shipment_id = ctx.store.save_shipment(
                None,
                {
                    "title": payload.title.strip(),
                    "direction": payload.direction,
                    "note": payload.note.strip(),
                    "source": str(raw.get("source") or "import")[:40],
                    "reference": str(raw.get("reference") or "")[:120],
                    "created_at": raw.get("created_at")
                    if parse_safe(raw.get("created_at"))
                    else None,
                },
                parcels,
            )
            if raw.get("archived_at"):
                ctx.store.set_archived(shipment_id, True)
            else:
                new_parcels += [p["id"] for p in ctx.store.get_shipment(shipment_id)["parcels"]]
            imported += 1
        if new_parcels and not ctx.demo:
            refresh_soon(ctx, new_parcels)
        return {"imported": imported, "skipped": skipped}

    def parse_safe(value: Any) -> bool:
        try:
            return isinstance(value, str) and parse_iso(value) is not None
        except ValueError:
            return False

    if config.static_dir and config.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=config.static_dir, html=True), name="frontend")

    return app
