# Parcel Master

Self-hosted parcel tracking. One overview for everything that is on its way,
with one small module per carrier.

- **Carrier modules.** Each carrier is a folder of its own, sorted by country
  (`carriers/de/dhl`). A new carrier is a new folder; nothing else changes.
- **Shipments with parcels.** An order that arrives in two parcels stays one
  entry, with both parcels listed under it.
- **Overview by arrival.** Needs you, today, tomorrow, this week, later. Each
  entry shows when it was last checked.
- **Notifications.** Browser push (installable as an app) and
  [Apprise](https://github.com/caronc/apprise) for Gotify, ntfy, Telegram,
  e-mail and many more. Quiet hours included.
- **Careful with limits.** A daily lookup budget per carrier, and polling that
  slows down for parcels that are not moving.
- **Languages.** English and German. A new language is one JSON file; anything
  missing falls back to English.
- **Demo mode.** Optional, with made-up parcels and a private sandbox per
  visitor.

## Carriers

| Country | Carrier | How | Needs |
| --- | --- | --- | --- |
| DE | DHL | Official "Shipment Tracking - Unified" API | Free API key from [developer.dhl.com](https://developer.dhl.com) |
| DE | Amazon | Reads the order history of your account with [amazon-orders](https://github.com/alexdlaird/amazon-orders) | Account e-mail and password; the two-step secret key if two-step verification is on |

A demo carrier (`DEMO` followed by digits) exists for the demo mode and for
development.

### Amazon

Amazon offers customers no tracking API. The Amazon module therefore signs in
to amazon.de like a browser and reads the orders of the last 30 days, every
three hours. Each order with something on its way becomes a shipment. As soon
as Amazon names carrier and tracking number and that carrier's module is set
up, the parcel is handed over to it ("Amazon via DHL"); until then it shows
the status Amazon reports, in Amazon's English wording.

Things to know:

- Amazon sometimes answers the sign-in with a JavaScript check. The image
  ships a headless Chromium that the amazon-orders library uses to pass it.
  This works on a best-effort basis.
- Amazon may ask for a captcha or an approval that only a person can give.
  The module then stops and says so; it does not keep trying, so the account
  is not locked. Sign in once in a browser and sync again.
- With two-step verification the module needs the secret key of an
  authenticator app, because nobody can type a code for the server.
- The password is stored unencrypted in the data directory, like the API keys.
- Amazon changes its pages from time to time. If orders stop showing up,
  update the image; the parsing lives in the amazon-orders library.

## Run it

```bash
docker run -d --name parcelmaster -p 8000:8000 -v parcelmaster-data:/data ghcr.io/jake-double-one/parcel-master:latest
```

Or use the [docker-compose.yml](docker-compose.yml) in this repository. Then
open <http://localhost:8000>, go to **Settings → Carriers** and enter your
keys.

Image tags: `latest` is the newest release, `main` follows the main branch.

### Configuration

Everything you change while the app runs (keys, polling, notifications,
language) lives in the settings dialog and is stored in `/data`. The
environment only holds what belongs to the deployment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PM_PASSWORD` | empty | Password for the built-in login. Empty means no login: put the app behind a reverse proxy that authenticates (Authentik, Authelia, ...). |
| `PM_DEMO` | `false` | Offer the demo mode. |
| `PM_PUSH_CONTACT` | `mailto:parcelmaster@localhost` | Contact address sent to browser push services. Use a real `mailto:` or `https:` address of yours. |
| `PM_DATA_DIR` | `/data` | Where the database and extra language files live. |
| `PM_SCHEDULER` | `true` | Run the background checks. |

Without `PM_PASSWORD` anyone who can reach the port can read and change
everything, including stored API keys. Do not expose it directly.

Push notifications need HTTPS and a server that can reach the internet. On
iPhone, add the app to the home screen first.

## Develop

Backend (Python 3.13+, [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv sync
PM_DEMO=1 PM_DEMO_CARRIER=1 uv run uvicorn parcelmaster.asgi:app --reload
```

Frontend (Node 22+), in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Checks:

```bash
cd backend && uv run ruff check . && uv run pytest
```

```bash
cd frontend && npm run check && npm test && npm run build
```

## Add a carrier

Create `backend/parcelmaster/carriers/<country>/<name>/__init__.py` and expose
a `MODULE`:

```python
import re

from parcelmaster.carriers.base import (
    CarrierError, CarrierModule, Field, TrackingEvent, TrackingResult, TrackRequest,
)
from parcelmaster.status import Status


class Example(CarrierModule):
    id = "de.example"            # "<country>.<name>"
    country = "de"
    name = "Example Post"
    patterns = (re.compile(r"EX\d{10}"),)          # shapes of its tracking numbers
    settings_fields = (Field("api_key", type="secret", required=True),)
    parcel_fields = ()                              # per-parcel extras, e.g. postal code
    default_daily_budget = 500                      # or None for no limit

    def tracking_url(self, tracking_number, extra, language):
        return f"https://example.org/track/{tracking_number}"

    async def track(self, request: TrackRequest, http) -> TrackingResult:
        response = await http.get(
            "https://api.example.org/track",
            params={"id": request.tracking_number},
            headers={"Authorization": request.settings["api_key"]},
        )
        if response.status_code == 404:
            raise CarrierError("not_found")
        data = response.json()
        return TrackingResult(status=Status.IN_TRANSIT, description=data["text"])


MODULE = Example()
```

A service that knows the shipments itself, like a shop account, subclasses
`ImporterModule` instead and implements `sync()`; see `carriers/de/amazon`.

The module is picked up on the next start. The settings dialog builds its
fields from `settings_fields`. Labels go into `locales/en.json` next to the
module:

```json
{ "settings": { "api_key": { "label": "API key", "help": "Where to get it." } } }
```

Map whatever the carrier reports onto the shared statuses in
[`status.py`](backend/parcelmaster/status.py), and add a test with a recorded
answer like the one for DHL in `backend/tests`.

## Add a language

Copy `backend/parcelmaster/locales/en.json` to `<code>.json`, set `meta.name`
and translate what you like. Keys you leave out are shown in English.

To add or override texts without rebuilding the image, put the file into
`/data/locales/`.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): use, change and share it for any
noncommercial purpose. Commercial use is not permitted.
