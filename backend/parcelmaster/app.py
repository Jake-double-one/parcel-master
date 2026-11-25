"""The HTTP API and the wiring of everything behind it."""

# No "from __future__ import annotations" here: FastAPI has to resolve the
# dependency aliases defined inside create_app() when it reads the handlers.

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, build, companion, discovery, importer, together
from .carriers.base import CarrierError, CarrierModule, switched_on
from .carriers.registry import Registry, discover
from .config import Config
from .db import Store, merge, now_iso
from .i18n import Translations
from .journal import JOURNAL
from .journal import setup as setup_logging
from .notify import Notifier, vapid_keys
from .scheduler import Scheduler
from .sessions import COOKIE, DEMO_TTL, LOGIN_TTL, DemoSessions, LoginThrottle, Signer
from .status import FINAL, Status, aggregate
from .tracking import (
    daily_budget,
    parse_iso,
    pickup_deadline,
    record_live,
    record_result,
    refresh_parcel,
    reopen_mistaken,
    take_auto_archived,
    timezone,
)

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
    #: When each carrier's parcels were last refreshed by hand, by store and carrier.
    asked_by_hand: dict[tuple[int, str], float]
    #: Password checks take turns while many logins fail (see LoginThrottle).
    login_gate: asyncio.Lock


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

    def on(self, module: CarrierModule) -> bool:
        """Whether the user has this carrier switched on."""
        return switched_on(self.store.get_settings()["carriers"].get(module.id, {}))


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


journal = logging.getLogger(__name__)

#: Sec-Fetch-Site values of requests the app's own pages make.
OWN_FETCH = frozenset({"same-origin", "none"})


def same_origin(request: Request) -> bool:
    """Whether a request comes from the app's own pages, or from no browser page at all.

    Browsers name the requesting site in Sec-Fetch-Site and, for changes, in
    Origin. Without either (curl, scripts, old browsers) the request is let
    through; the session or proxy still decides who may act.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in OWN_FETCH
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return urlparse(origin).netloc.lower() == host.split(",")[0].strip().lower()


def create_app(config: Config) -> FastAPI:
    log_level = setup_logging()
    registry = discover()
    store = Store(config.data_dir / "parcelmaster.db")
    translations = Translations(registry.locales, config.data_dir / "locales")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Which build runs, for the log: the first thing to check when something is off.
        journal.info(
            "Parcel Master %s, build %s, companion script %s",
            __version__,
            build(),
            companion.version(registry) or "none",
        )
        reopened = reopen_mistaken(store)
        if reopened:
            journal.info("took up %d parcel(s) again that were closed as delivered", reopened)
        http = httpx.AsyncClient(timeout=20, headers={"User-Agent": f"ParcelMaster/{__version__}"})
        notifier = Notifier(store, registry, translations, config.push_contact)
        scheduler = Scheduler(store, registry, http, notifier, config.data_dir)
        app.state.pm = State(
            config=config,
            store=store,
            registry=registry,
            translations=translations,
            signer=Signer(store, config.password),
            demos=DemoSessions(),
            throttle=LoginThrottle(),
            http=http,
            notifier=notifier,
            scheduler=scheduler,
            background=set(),
            asked_by_hand={},
            login_gate=asyncio.Lock(),
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

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Turn away changes that another website's page asks the browser for.

        The session cookie goes along with same-site requests, so SameSite
        alone does not keep a neighbour on the same domain out, nor anyone
        when an authenticating proxy is the only wall. The companion scripts
        are exempt: they talk from the user script manager, with JSON bodies
        no page can send cross-origin anyway.
        """
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/companion/")
            and not same_origin(request)
        ):
            return JSONResponse({"detail": {"error": "cross_origin"}}, status_code=403)
        return await call_next(request)

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

    def contents_of(parcel: dict[str, Any]) -> list[str]:
        """What an importer noted as being inside the parcel."""
        try:
            items = json.loads(parcel["extra"].get("items") or "[]")
        except ValueError:
            return []
        return [str(item) for item in items] if isinstance(items, list) else []

    def live_map(extra: dict[str, str]) -> dict[str, Any] | None:
        def point(text: str | None) -> list[float] | None:
            try:
                latitude, longitude = (float(part) for part in (text or "").split(","))
            except ValueError:
                return None
            return [latitude, longitude]

        van = point(extra.get("van"))
        if van is None:
            return None
        return {"van": van, "home": point(extra.get("home")), "at": extra.get("live_at")}

    def present_parcel(ctx: Ctx, parcel: dict[str, Any], language: str) -> dict[str, Any]:
        module = ctx.state.registry.get(parcel["carrier"])
        deadline = pickup_deadline(parcel, module)

        def localized(text: str) -> str:
            return ctx.state.translations.localize(module, text, language) if module else text

        hidden = (
            "position",
            "error_count",
            "stale_notified_at",
            "pickup_reminded_at",
            "reminded_for",
        )
        return {
            **{key: value for key, value in parcel.items() if key not in hidden},
            # The bell: a reminder shortly before the delivery window opens.
            "remind": bool(parcel.get("remind")),
            "description": localized(parcel["description"]),
            "events": [
                {**event, "description": localized(event["description"])}
                for event in parcel["events"][:MAX_EVENTS]
            ],
            # An imported parcel names the carrier that delivers it, once known.
            "carrier_name": parcel["extra"].get("carrier_name")
            or (module.name if module else parcel["carrier"]),
            "contents": contents_of(parcel),
            # Filled in when orders share this parcel: whose items are whose.
            "contents_by_order": None,
            # The live view of the round vanished while the parcel is still on the van.
            "live_lost": bool(parcel["extra"].get("live_lost")),
            # Stops left on the delivery round, where the carrier tells.
            "stops": (
                {
                    "left": int(parcel["extra"]["stops_left"]),
                    "about": bool(parcel["extra"].get("stops_about")),
                }
                if parcel["extra"].get("stops_left", "").isdigit()
                else None
            ),
            # The van stands within a few steps of the destination.
            "near": bool(parcel["extra"].get("near")),
            # Where the van is and where it is headed, while the carrier's live view tells.
            "map": live_map(parcel["extra"]),
            # Whose website the link below opens.
            "site_name": module.name if module else parcel["carrier"],
            "site_colour": (module.colour or None) if module else None,
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
        parts = [p["extra"].get("part") for p in shipment["parcels"] if p["extra"].get("part")]
        return {
            **shipment,
            # A parcel with a card of its own shows the order it came from.
            "reference": importer.order_of(shipment["reference"]),
            # "2/3": the second of the order's three parcels.
            "part": parts[0] if len(parts) == 1 else None,
            "parcels": parcels,
            "status": str(aggregate(statuses)),
            "parcel_count": len(parcels),
            "finished_count": len(parcels) - len(open_parcels),
            "eta_from": etas[0] if etas else None,
            "eta_to": eta_ends[0] if eta_ends else None,
            "pickup_deadline": deadlines[0] if deadlines else None,
            # The oldest lookup among the open parcels: how fresh the whole row is.
            "last_checked_at": min(checked) if checked and all(checked) else None,
            "suggested": bool(shipment.get("suggested_at")),
            "purge_at": discovery.purge_at(shipment),
            # Other orders that came in the same box and share this card.
            "together": [],
        }

    def combine(
        ctx: Ctx, shipments: list[dict[str, Any]], cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Show orders packed into one box as one card, led by the tracked one."""
        joined = together.groups(shipments, ctx.state.registry)
        if not joined:
            return cards
        by_id = {card["id"]: card for card in cards}
        for leader, ids in joined.items():
            card, others = by_id[leader], [by_id[i] for i in ids]
            card["together"] = [
                {"id": o["id"], "reference": o["reference"], "title": o["title"]} for o in others
            ]
            for parcel in card["parcels"]:
                orders = [{"reference": card["reference"], "items": parcel["contents"]}]
                for other in others:
                    orders += [
                        {"reference": other["reference"], "items": p["contents"]}
                        for p in other["parcels"]
                        if p["tracking_number"] == parcel["tracking_number"]
                    ]
                orders = [o for o in orders if o["items"]]
                if len(orders) > 1:
                    parcel["contents_by_order"] = orders
                    parcel["contents"] = [item for o in orders for item in o["items"]]
        hidden = {i for ids in joined.values() for i in ids}
        return [card for card in cards if card["id"] not in hidden]

    def companions_of(ctx: Ctx, shipment: dict[str, Any]) -> list[int]:
        """The shipment and the orders shown on its card."""
        if shipment["deleted_at"]:
            return [shipment["id"]]
        listed = ctx.store.list_shipments(shipment["archived_at"] is not None)
        return together.companions(listed, ctx.state.registry, shipment["id"])

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
            "build": build(),
            "companion_version": companion.version(state.registry),
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
    async def login(
        request: Request, response: Response, password: Annotated[str, Body(embed=True)]
    ) -> dict[str, bool]:
        state: State = request.app.state.pm
        client = request.client.host if request.client else "unknown"
        if state.config.password is None:
            return {"ok": True}
        if state.throttle.blocked(client):
            raise fail(429, "too_many_attempts")
        async with state.login_gate:
            await asyncio.sleep(state.throttle.pause())
            right = hmac.compare_digest(password.encode(), state.config.password.encode())
            if not right:
                state.throttle.failed(client)
        if not right:
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
    def logout(request: Request, response: Response) -> dict[str, bool]:
        state: State = request.app.state.pm
        state.signer.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # -- carriers ---------------------------------------------------------

    def sync_state(ctx: Ctx, module: CarrierModule) -> dict[str, Any] | None:
        if module.kind != "importer":
            return None
        state = importer.get_state(ctx.store, module.id)
        own = ctx.store.get_settings()["carriers"].get(module.id, {})
        return {
            "interval_minutes": importer.interval_minutes(module, own),
            "default_interval_minutes": module.sync_interval_minutes,
            "next_sync_at": importer.next_sync_at(ctx.store, module, own),
            "last_sync_at": state.get("last_sync_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_error": state.get("last_error"),
            "found": state.get("found"),
            # The account whose sign-in was checked, or None while not connected.
            "account": state.get("account") if state.get("connected") else None,
        }

    def health(ctx: Ctx, module: CarrierModule, parcels: list[dict[str, Any]]) -> dict[str, Any]:
        """Whether the module works right now, judged by what it did last.

        "off" is not set up, "untested" has not asked yet, "ok" and "failing"
        say how the latest lookup or sync went.
        """
        own = ctx.store.get_settings()["carriers"].get(module.id, {})
        source = module.source(own)
        if module.kind == "importer":
            sync = sync_state(ctx, module) or {}
            if not sync.get("account"):
                state = "off"
            elif sync.get("last_error"):
                state = "failing"
            else:
                state = "ok" if sync.get("last_sync_at") else "untested"
            return {"state": state, "source": source}
        if not module.is_configured(own):
            return {"state": "off", "source": source}
        asked = sorted(
            (p for p in parcels if p["carrier"] == module.id and p["last_checked_at"]),
            key=lambda p: p["last_checked_at"],
        )
        if not asked:
            return {"state": "untested", "source": source}
        return {"state": "failing" if asked[-1]["last_error"] else "ok", "source": source}

    def importer_module(ctx: Ctx, module_id: str) -> importer.ImporterModule:
        module = ctx.module(module_id)
        if not isinstance(module, importer.ImporterModule):
            raise fail(404, "not_found")
        return module

    @app.post("/api/importers/{module_id}/connect")
    async def connect_importer(
        ctx: RealContext, module_id: str, credentials: Annotated[dict[str, str], Body()]
    ) -> dict[str, Any]:
        """Check a sign-in and keep the credentials only if it works."""
        module = importer_module(ctx, module_id)
        if importer.too_soon(ctx.store, module, timedelta(seconds=15)):
            raise fail(429, "too_soon")
        allowed = {f.key for f in module.settings_fields}
        cleaned = {k: v.strip() for k, v in credentials.items() if k in allowed and v.strip()}
        if not module.is_configured(cleaned):
            raise fail(422, "missing_credentials")
        error = await importer.connect(
            ctx.store, ctx.state.registry, module, ctx.state.config.data_dir, cleaned
        )
        if error:
            raise fail(400, "sign_in", reason=error.code, message=error.detail)
        return sync_state(ctx, module)

    @app.post("/api/importers/{module_id}/import", status_code=201)
    async def import_by_reference(
        ctx: RealContext,
        module_id: str,
        reference: Annotated[str, Body(embed=True, max_length=600)],
    ) -> dict[str, Any]:
        """File one order the user names, whatever its state."""
        module = importer_module(ctx, module_id)
        reference = reference.strip()
        if not module.reference_pattern or not module.reference_pattern.fullmatch(reference):
            raise fail(422, "invalid_reference")
        try:
            shipment_id = await importer.fetch_one(
                ctx.store, ctx.state.registry, module, ctx.state.config.data_dir, reference
            )
        except CarrierError as error:
            raise fail(400, "import_failed", reason=error.code, message=error.detail) from error
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    @app.post("/api/importers/{module_id}/disconnect")
    def disconnect_importer(ctx: RealContext, module_id: str) -> dict[str, Any]:
        module = importer_module(ctx, module_id)
        importer.disconnect(ctx.store, module, ctx.state.config.data_dir)
        return sync_state(ctx, module)

    @app.post("/api/importers/{module_id}/sync")
    async def sync_importer(ctx: RealContext, module_id: str) -> dict[str, Any]:
        module = importer_module(ctx, module_id)
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
        parcels = ctx.store.open_parcels()
        result = []
        for module in ctx.modules():
            own = settings["carriers"].get(module.id, {})
            result.append(
                {
                    "id": module.id,
                    "name": module.name,
                    "country": module.country,
                    "health": health(ctx, module, parcels),
                    # Off: nothing is asked, suggested or handed over to this carrier.
                    "enabled": switched_on(own),
                    "open_parcels": sum(1 for p in parcels if p["carrier"] == module.id),
                    "last_checked_at": max(
                        (
                            p["last_checked_at"]
                            for p in parcels
                            if p["carrier"] == module.id and p["last_checked_at"]
                        ),
                        default=None,
                    ),
                    "kind": module.kind,
                    "sync": sync_state(ctx, module),
                    # Whether a single order can be added by its number.
                    "by_reference": bool(getattr(module, "reference_pattern", None)),
                    # A userscript that passes on what the carrier's page shows the user.
                    "companion": (
                        f"/api/companion/{module.id}.user.js"
                        if module.companion_script() and not ctx.demo and switched_on(own)
                        else None
                    ),
                    "configured": module.is_configured(own),
                    "settings_fields": [vars(f) for f in module.settings_fields],
                    "parcel_fields": [vars(f) for f in module.parcel_fields],
                    "parcel_defaults": module.parcel_defaults(own),
                    "default_daily_budget": module.budget(own),
                    "daily_budget": daily_budget(module, own),
                    "used_today": ctx.store.usage_today(module.id),
                }
            )
        return result

    def server_address(request: Request) -> str:
        """This server as the browser sees it, fit to be written into a script."""
        server = companion.safe_server(str(request.base_url))
        if server is None:
            raise fail(400, "bad_host")
        return server

    # The first address stays for scripts installed from it; some caches in
    # front of a server keep ".js" answers, and a copy kept there is why the
    # script moved to an address that never allowed one.
    @app.get("/api/companion/all.user.js")
    @app.get(companion.PATH)
    def companion_scripts(request: Request) -> Response:
        """The scripts of all modules as one. Open to the script manager's update check,
        which may come without a session; it holds program text only, no data."""
        state: State = request.app.state.pm
        script = companion.combined(state.registry)
        if not script:
            raise fail(404, "not_found")
        return Response(
            companion.made_out(script, server_address(request)),
            media_type="text/javascript; charset=utf-8",
            # Nothing between here and the browser may keep a copy: a proxy or CDN
            # that caches ".js" would hand out an old script after an update.
            headers={
                "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/companion/{module_id}.user.js")
    def companion_script(ctx: RealContext, module_id: str, request: Request) -> Response:
        """One module's userscript on its own, made out to this server."""
        module = ctx.module(module_id)
        script = module.companion_script() if module else None
        if not script:
            raise fail(404, "not_found")
        return Response(
            companion.made_out(script, server_address(request)),
            media_type="text/javascript; charset=utf-8",
        )

    @app.post("/api/carriers/{module_id}/enabled")
    async def switch_carrier(
        ctx: Context, module_id: str, enabled: Annotated[bool, Body(embed=True)]
    ) -> dict[str, bool]:
        """Turn a carrier on or off. Off: its parcels stay, but nothing is asked any more."""
        module = ctx.module(module_id)
        if module is None:
            raise fail(404, "not_found")
        settings = ctx.store.get_settings()
        own = dict(settings["carriers"].get(module.id, {}))
        was = switched_on(own)
        own["enabled"] = enabled
        settings["carriers"][module.id] = own
        ctx.store.save_settings(settings)
        if enabled and not was:
            # Catch up at once on what was missed while it was off.
            refresh_soon(
                ctx, [p["id"] for p in ctx.store.open_parcels() if p["carrier"] == module.id]
            )
        return {"enabled": enabled}

    @app.post("/api/carriers/{module_id}/sync")
    async def sync_carrier(ctx: Context, module_id: str) -> dict[str, int]:
        """Ask the carrier about all of its open parcels now, not when they are due."""
        module = ctx.module(module_id)
        if module is None or module.kind != "tracker":
            raise fail(404, "not_found")
        if not ctx.on(module):
            raise fail(409, "carrier_off")
        key = (id(ctx.store), module.id)
        last = ctx.state.asked_by_hand.get(key)
        if last is not None and monotonic() - last < 60:
            raise fail(429, "too_soon")
        ctx.state.asked_by_hand[key] = monotonic()
        ids = [p["id"] for p in ctx.store.open_parcels() if p["carrier"] == module.id]
        refresh_soon(ctx, ids)
        return {"parcels": len(ids)}

    @app.post("/api/companion/{module_id}")
    async def companion_news(
        ctx: RealContext,
        module_id: str,
        payload: Annotated[Any, Body(embed=True)],
        take: Annotated[list[str] | None, Body(embed=True, max_length=50)] = None,
    ) -> dict[str, Any]:
        """News a companion script saw on the carrier's page: applied to the parcels we
        know, and parcels we do not know are filed as the settings say. ``take`` names
        the ones the user clicked on in the page."""
        module = ctx.module(module_id)
        if module is None or not module.companion_script() or not ctx.on(module):
            raise fail(404, "not_found")
        settings = ctx.store.get_settings()
        zone = timezone(settings)
        now = datetime.now(UTC)
        base = int(settings["polling"]["interval_minutes"])
        taken = 0
        known = {p["tracking_number"] for p in ctx.store.open_parcels()}
        for line in module.describe_companion_news(payload, known):
            journal.debug("%s companion saw %s", module.id, line)
        found = module.discover(payload)
        wanted = frozenset(module.normalize(str(number)) for number in take or [])
        mode = discovery.mode_of(settings)
        await ctx.state.notifier.handle(discovery.take(ctx.store, module, found, mode, wanted))
        mine = ctx.store.open_parcels(suggested=True)
        keep_map = settings["carriers"].get(module.id, {}).get("live_map") != "off"
        for number, result in module.take_from_companion(payload)[:30]:
            if not keep_map:
                result.van = result.home = None  # the user wants no positions kept
            for parcel in mine:
                if (parcel["carrier"], parcel["tracking_number"]) != (module.id, number):
                    continue
                if Status(parcel["status"]) in FINAL:
                    continue
                full = ctx.store.get_parcel(parcel["id"])
                if result.live_only:
                    notices = record_live(ctx.store, full, result, zone, now)
                else:
                    notices = record_result(ctx.store, full, result, base, zone, now)
                if not parcel["suggested_at"]:  # nothing to tell about a mere suggestion
                    await ctx.state.notifier.handle(notices)
                taken += 1
        if taken:
            journal.info("%s: companion script brought news for %d parcel(s)", module.id, taken)
        seen = {item.number for item in found}
        tracked = {p["tracking_number"] for p in ctx.store.open_parcels()}
        waiting = {p["tracking_number"] for p in mine if p["suggested_at"]}
        return {
            "parcels": taken,
            "mode": mode,
            # For the script's buttons: what of the page is tracked, what awaits a decision.
            "tracked": sorted(seen & tracked),
            "suggested": sorted(seen & waiting - tracked),
        }

    @app.post("/api/detect")
    def detect(ctx: Context, text: Annotated[str, Body(embed=True, max_length=2000)]) -> dict:
        """Find carrier and tracking number in pasted text: a link or a bare number."""
        text = text.strip()
        matches: list[dict[str, str]] = []
        usable = [module for module in ctx.modules() if ctx.on(module)]
        for candidate in re.findall(r"https?://\S+", text):
            if not urlparse(candidate).hostname:
                continue
            for module in usable:
                number = module.extract_from_url(candidate)
                if number:
                    matches.append({"carrier": module.id, "tracking_number": number})
        if not matches:
            # The whole text, single words, and digit groups typed with spaces.
            tokens = [
                text,
                *re.findall(r"[A-Za-z0-9]{8,40}", text),
                *re.findall(r"\d[\d\-]{8,30}\d", text),
                *re.findall(r"\d[\d ]{6,40}\d", text),
            ]
            for token in tokens:
                for module in usable:
                    pattern = getattr(module, "reference_pattern", None)
                    if pattern and pattern.fullmatch(token.strip()):
                        match = {"carrier": module.id, "tracking_number": token.strip()}
                        if match not in matches:
                            matches.append(match)
                        continue
                    if module.matches(token):
                        match = {"carrier": module.id, "tracking_number": module.normalize(token)}
                        if match not in matches:
                            matches.append(match)
        return {"matches": matches}

    # -- shipments --------------------------------------------------------

    @app.get("/api/shipments")
    async def list_shipments(
        ctx: Context, archived: bool = False, trash: bool = False
    ) -> list[dict[str, Any]]:
        if trash:
            language = language_of(ctx)
            return [present(ctx, shipment, language) for shipment in ctx.store.list_trash()]
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
        return combine(ctx, shipments, [present(ctx, shipment, language) for shipment in shipments])

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

    @app.post("/api/notices/take")
    def take_notices(ctx: Context) -> dict[str, Any]:
        """What happened in the background since the user last looked. Reported once."""
        return {"auto_archived": len(take_auto_archived(ctx.store))}

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
    def delete_shipment(ctx: Context, shipment_id: int, permanent: bool = False) -> None:
        """Into the bin; from the bin, or when asked to, gone for good."""
        shipment = ctx.store.get_shipment(shipment_id)
        if shipment is None:
            raise fail(404, "not_found")
        if permanent or shipment["deleted_at"]:
            # Also notes that nothing shall bring it back by itself.
            discovery.remove_for_good(ctx.store, shipment)
        else:
            for shipment_id in companions_of(ctx, shipment):  # one box, one card
                ctx.store.set_deleted(shipment_id, True)

    @app.delete("/api/trash", status_code=204)
    def empty_trash(ctx: Context) -> None:
        discovery.empty_trash(ctx.store, everything=True)

    @app.post("/api/shipments/{shipment_id}/restore")
    def restore(ctx: Context, shipment_id: int) -> dict[str, Any]:
        shipment = ctx.store.get_shipment(shipment_id)
        if shipment is None or not shipment["deleted_at"]:
            raise fail(404, "not_found")
        ctx.store.set_deleted(shipment_id, False)
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    @app.post("/api/shipments/{shipment_id}/accept")
    async def accept(ctx: Context, shipment_id: int) -> dict[str, Any]:
        """A shipment a companion script found becomes one the user tracks."""
        if not ctx.store.accept_suggestion(shipment_id):
            raise fail(404, "not_found")
        return await after_save(ctx, shipment_id)

    @app.post("/api/shipments/{shipment_id}/archive")
    def archive(
        ctx: Context, shipment_id: int, archived: Annotated[bool, Body(embed=True)] = True
    ) -> dict[str, Any]:
        shipment = ctx.store.get_shipment(shipment_id)
        if shipment is None:
            raise fail(404, "not_found")
        for other in companions_of(ctx, shipment):  # one box, one card
            ctx.store.set_archived(other, archived)
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    @app.post("/api/parcels/{parcel_id}/remind")
    def remind_parcel(
        ctx: Context, parcel_id: int, on: Annotated[bool, Body(embed=True)]
    ) -> dict[str, Any]:
        """Switch a parcel's bell: a reminder shortly before its delivery window opens."""
        parcel = ctx.store.get_parcel(parcel_id)
        if parcel is None:
            raise fail(404, "not_found")
        # Switched on anew it may ring again, even if it rang before.
        ctx.store.update_parcel(parcel_id, remind=int(on), reminded_for=None)
        return present(ctx, ctx.store.get_shipment(parcel["shipment_id"]), language_of(ctx))

    @app.post("/api/shipments/{shipment_id}/refresh")
    async def refresh_shipment(ctx: Context, shipment_id: int) -> dict[str, Any]:
        shipment = ctx.store.get_shipment(shipment_id)
        if shipment is None:
            raise fail(404, "not_found")
        open_parcels = [p for p in shipment["parcels"] if Status(p["status"]) not in FINAL]
        # Parcels that an importer still tracks get their news from a sync of that service.
        carriers_to_ask = {p["carrier"] for p in open_parcels}
        if not shipment["parcels"]:
            # An imported order that lost its parcels is repaired by asking its service again.
            carriers_to_ask |= {m.id for m in ctx.modules() if m.name == shipment["source"]}
        for module_id in carriers_to_ask:
            module = ctx.module(module_id)
            if (
                isinstance(module, importer.ImporterModule)
                and not ctx.demo
                and importer.get_state(ctx.store, module.id).get("connected")
                and not importer.too_soon(ctx.store, module)
            ):
                notices = await importer.run(
                    ctx.store, ctx.state.registry, module, ctx.state.config.data_dir
                )
                await ctx.state.notifier.handle(notices)
        await refresh(ctx, [p["id"] for p in open_parcels])
        return present(ctx, ctx.store.get_shipment(shipment_id), language_of(ctx))

    # -- settings ---------------------------------------------------------

    def secret_keys(module: CarrierModule) -> set[str]:
        return {f.key for f in module.settings_fields if f.type == "secret"}

    def masked_settings(ctx: Ctx) -> dict[str, Any]:
        settings = ctx.store.get_settings()
        carriers_out: dict[str, Any] = {}
        for module in ctx.modules():
            own = dict(settings["carriers"].get(module.id, {}))
            if module.kind == "importer":
                # The account itself never leaves the server; only the pace does.
                own = {"sync_minutes": own.get("sync_minutes")}
            for key in secret_keys(module):
                if module.kind != "importer":
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

    def daily_window(window: dict[str, Any], error: str) -> dict[str, Any]:
        for key in ("from", "to"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(window.get(key, ""))):
                raise fail(422, error)
        return {"enabled": bool(window.get("enabled")), "from": window["from"], "to": window["to"]}

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
            "saver": daily_window(polling.get("saver") or defaults["saver"], "invalid_saver_hours")
            | {"last_call": bool((polling.get("saver") or {}).get("last_call", True))},
        }
        merged["discovery"] = {"mode": discovery.mode_of(merged)}
        notifications = merged["notifications"]
        urls = notifications.get("apprise_urls")
        notifications["apprise_urls"] = (
            [str(u).strip()[:500] for u in urls if str(u).strip()][:10]
            if isinstance(urls, list)
            else []
        )
        notifications["push_enabled"] = bool(notifications.get("push_enabled"))
        notifications["reminder_minutes"] = bounded(
            notifications.get("reminder_minutes"),
            5,
            240,
            current["notifications"].get("reminder_minutes") or 30,
        )
        notifications["events"] = {
            key: bool(notifications.get("events", {}).get(key))
            for key in current["notifications"]["events"]
        }
        notifications["quiet_hours"] = daily_window(
            notifications.get("quiet_hours") or {}, "invalid_quiet_hours"
        )

        carriers_in = incoming.get("carriers")
        if isinstance(carriers_in, dict):
            for module in ctx.modules():
                given = carriers_in.get(module.id)
                if not isinstance(given, dict):
                    continue
                own = dict(current["carriers"].get(module.id, {}))
                if isinstance(module, importer.ImporterModule):
                    # The account is set by connecting, never by saving the form;
                    # only the pace of the sync is a setting.
                    if "sync_minutes" in given:
                        minutes = given["sync_minutes"]
                        own["sync_minutes"] = (
                            None
                            if minutes in (None, "")
                            else bounded(
                                minutes,
                                importer.MIN_SYNC_MINUTES,
                                importer.MAX_SYNC_MINUTES,
                                module.sync_interval_minutes,
                            )
                        )
                        merged["carriers"][module.id] = own
                    continue
                for field in module.settings_fields:
                    if field.key not in given:
                        continue
                    value = str(given[field.key] or "").strip()
                    if field.type == "secret" and value == SECRET_MASK:
                        continue  # unchanged: the browser never saw the real value
                    if field.type == "choice" and value not in field.options:
                        value = ""  # means the default
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
    async def test_notification(
        ctx: RealContext, endpoint: Annotated[str | None, Body(embed=True)] = None
    ) -> dict[str, Any]:
        settings = ctx.store.get_settings()
        t = ctx.state.translations.t
        language = language_of(ctx)
        message = {
            "kind": "test",
            "title": t(language, "notify.test.title"),
            "body": t(language, "notify.test.body"),
            "shipment_id": None,
        }
        if endpoint:
            return await ctx.state.notifier.send_to_device(message, endpoint)
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

    @app.get("/api/log")
    def log_entries(ctx: RealContext) -> dict[str, Any]:
        return {
            "level": logging.getLevelName(log_level).lower(),
            "entries": list(reversed(JOURNAL.entries)),
        }

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
                    "health": health(ctx, module, parcels),
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
