# ParKit

Free, self-hosted truck par restock for local service shops. Paste tomorrow's jobs, match them to each truck's par sheet, and get a missing-SKU checklist before first dispatch.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create trucks and import **YAML or CSV** par sheets (`sku`, `name`, `min`, `max`, `on_hand`, `job_tags`)
- Edit `on_hand` after a physical count
- Paste tomorrow's jobs (pipe format `truck|title|tag`) or upload CSV
- **Build restock list** — keyword tag rules (optional LLM with ≤3s timeout + fallback) → union matching par SKUs → gap lines where `on_hand < min`
- Per-truck print-friendly checklist with **Copy** and **CSV** export
- Optional BYO SMTP and/or Twilio to ping the parts runner
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"sms_configured":false,"llm_configured":false}` even when those are unset

Works fully with zero SMTP, Twilio, or LLM.

## Privacy

Self-hosted. You run the box; the owner is the data controller. No bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file on the Compose volume.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/parkit.git
cd parkit
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Leave `SMTP_*`, Twilio, LLM, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `parkit-data` volume at `/data/parkit.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
BUSINESS_NAME=Harbor HVAC
PUBLIC_BASE_URL=http://localhost:8080
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*`, Twilio, and LLM vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"sms_configured":false`, `"llm_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create a truck (e.g. **Truck 1**). On the par page, import `sample-hvac-par.csv` (or YAML). Confirm at least one SKU has `on_hand < min` with an `ac_no_cool` tag (sample includes TXV/capacitor gaps).

3. **Jobs** → paste `Truck 1|Carrier no-cool — Smith` (or upload `sample-jobs.csv`). On Home, **Build restock list**. Gap checklist should include TXV / capacitor / etc. with `qty_to_pull ≥ 1`. Use **Copy** and **Export CSV**.

4. Confirm empty `MARKETING_URL` shows no "Powered by" footer. Confirm the product works with no LLM and no Twilio/SMTP.

## Job paste format

One job per line. Optional pipe format:

```
truck|title|tag
truck|title|tag|job_ref|date
```

Examples:

```
Truck 1|Carrier no-cool — Smith|ac_no_cool
Truck 1|Water heater not heating
Carrier no-cool — Smith
```

If the truck field is missing or unknown, the job is assigned to your first (default) truck and flagged in the flash message.

CSV columns: `truck`, `title`, `tag`, `job_ref`, `date` (date optional; defaults to tomorrow).

## Keyword tag rules

When `tag` is omitted, titles are mapped with in-repo rules (no LLM required), including:

| Title contains | Tag |
| --- | --- |
| no cool / won't cool / not cooling | `ac_no_cool` |
| water heater / hot water | `water_heater` |
| GFCI / GFI outlet | `electrical_gfci` |
| furnace / no heat | `furnace_no_heat` |
| … | … |

Unknown → `general_service`. Optional `OLLAMA_BASE_URL` or `LLM_API_KEY`+`LLM_BASE_URL` may map titles with a ≤3s timeout; on failure the keyword rules still apply.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/parkit.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | UI copy. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in optional notify links. |
| `RUNNER_EMAIL` / `RUNNER_PHONE` | Optional parts-runner destinations on Build / Notify. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email sign-off. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email notify. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional SMS notify. |
| `OLLAMA_BASE_URL` or `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | Optional title→tag. |
| `MARKETING_URL` | If set, footer link **Powered by ParKit** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP / Twilio / LLM secrets and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false,"sms_configured":false,"llm_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `sms_configured` is `true` only when all three Twilio vars are set. `llm_configured` is `true` when Ollama or LLM API env is set. Health succeeds even when all are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./data/parkit.db
export OWNER_PASSWORD=testpass
export PUBLIC_BASE_URL=http://localhost:8080
export BUSINESS_NAME="Harbor HVAC"
export MARKETING_URL=
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

```bash
python -m unittest test_app.py -v
```

## What this is not

ParKit is **not** PartPing (no customer `/p/{token}` parts ETA timeline). It is **not** a full inventory ERP, barcode scanner product, supplier EDI, GPS truck tracker, or Jobber/Housecall live API. It is **not** BillCatch invoicing. CSV job import only in this free wedge.

No Redis, no Celery, no required cloud LLM, no second Compose service.
