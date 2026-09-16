"""ParKit helpers: DB, par/jobs import, tag rules, restock build, notify."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import g

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = str(APP_ROOT / "data" / "parkit.db")

OPEN_ENDPOINTS = {"health", "login", "static"}

TITLE_TAG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"no[\s\-]?cool|won'?t\s*cool|will\s*not\s*cool|not\s*cooling", re.I), "ac_no_cool"),
    (re.compile(r"water\s*heater|hot\s*water", re.I), "water_heater"),
    (re.compile(r"\bgfci\b|gfi\s*outlet|ground\s*fault", re.I), "electrical_gfci"),
    (re.compile(r"capacitor|\bcap\b", re.I), "capacitor"),
    (re.compile(r"furnace|no\s*heat|won'?t\s*heat", re.I), "furnace_no_heat"),
    (re.compile(r"drain|clog|sewer", re.I), "plumbing_drain"),
    (re.compile(r"thermostat", re.I), "thermostat"),
    (re.compile(r"refrigerant|freon|r[\-\s]?410", re.I), "refrigerant"),
]

KNOWN_TAGS = {
    "ac_no_cool",
    "residential_cool",
    "water_heater",
    "electrical_gfci",
    "capacitor",
    "furnace_no_heat",
    "plumbing_drain",
    "thermostat",
    "refrigerant",
    "general_service",
    "always",
    "truck_base",
}

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def database_path() -> str:
    return _env("DATABASE_PATH") or DEFAULT_DB

def owner_password() -> str:
    return _env("OWNER_PASSWORD")

def business_name() -> str:
    return _env("BUSINESS_NAME") or "ParKit"

def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")

def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))

def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )

def llm_configured() -> bool:
    if _env("OLLAMA_BASE_URL"):
        return True
    return bool(_env("LLM_API_KEY") and _env("LLM_BASE_URL"))

def runner_email() -> str:
    return _env("RUNNER_EMAIL")

def runner_phone() -> str:
    return _env("RUNNER_PHONE")

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

def tomorrow_date_str() -> str:
    return (utc_now().date() + timedelta(days=1)).isoformat()

def connect_db(path: str | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db

def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS trucks (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          notes TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS par_items (
          id INTEGER PRIMARY KEY,
          truck_id INTEGER NOT NULL REFERENCES trucks(id) ON DELETE CASCADE,
          sku TEXT NOT NULL,
          name TEXT NOT NULL,
          min_qty INTEGER NOT NULL,
          max_qty INTEGER,
          on_hand INTEGER NOT NULL DEFAULT 0,
          job_tags TEXT NOT NULL,
          UNIQUE(truck_id, sku)
        );

        CREATE TABLE IF NOT EXISTS jobs_import (
          id INTEGER PRIMARY KEY,
          run_id INTEGER,
          truck_id INTEGER REFERENCES trucks(id),
          title TEXT NOT NULL,
          job_tag TEXT,
          job_ref TEXT,
          scheduled_date TEXT NOT NULL,
          raw_line TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS restock_runs (
          id INTEGER PRIMARY KEY,
          for_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          notes TEXT
        );

        CREATE TABLE IF NOT EXISTS restock_lines (
          id INTEGER PRIMARY KEY,
          run_id INTEGER NOT NULL REFERENCES restock_runs(id) ON DELETE CASCADE,
          truck_id INTEGER NOT NULL REFERENCES trucks(id),
          sku TEXT NOT NULL,
          name TEXT NOT NULL,
          on_hand INTEGER NOT NULL,
          min_qty INTEGER NOT NULL,
          qty_to_pull INTEGER NOT NULL,
          reason_tags TEXT
        );
        """
    )
    db.commit()

def parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(t).strip() for t in data if str(t).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[|;,]", text)
    return [p.strip() for p in parts if p.strip()]

def tags_to_storage(tags: list[str]) -> str:
    return json.dumps(tags)

def tags_from_row(row: sqlite3.Row | dict) -> list[str]:
    raw = row["job_tags"] if not isinstance(row, dict) else row.get("job_tags", "")
    return parse_tags(raw)

# --- Par import ---

def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower())

def parse_par_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    keymap = {_norm_header(h): h for h in reader.fieldnames}
    required = {"sku", "name", "min"}
    if not required.issubset(keymap.keys()) and not {"sku", "name", "minqty"}.issubset(keymap.keys()):
        if not {"sku", "name"}.issubset(keymap.keys()):
            raise ValueError("CSV needs sku, name, min (or min_qty) columns")
    items: list[dict[str, Any]] = []
    for row in reader:
        def get(*names: str, default: str = "") -> str:
            for n in names:
                key = keymap.get(_norm_header(n))
                if key is not None and row.get(key) is not None:
                    return str(row.get(key, "")).strip()
            return default

        sku = get("sku")
        name = get("name")
        if not sku or not name:
            continue
        min_raw = get("min", "min_qty", "minqty", default="1")
        max_raw = get("max", "max_qty", "maxqty", default="")
        on_hand_raw = get("on_hand", "onhand", default="0")
        tags_raw = get("job_tags", "jobtags", "tags", default="")
        try:
            min_qty = int(float(min_raw or "1"))
        except ValueError:
            min_qty = 1
        try:
            max_qty = int(float(max_raw)) if max_raw else None
        except ValueError:
            max_qty = None
        try:
            on_hand = int(float(on_hand_raw or "0"))
        except ValueError:
            on_hand = 0
        items.append(
            {
                "sku": sku,
                "name": name,
                "min": min_qty,
                "max": max_qty,
                "on_hand": on_hand,
                "job_tags": parse_tags(tags_raw),
            }
        )
    return items

def parse_par_yaml(text: str) -> list[dict[str, Any]]:
    if yaml is None:
        raise ValueError("PyYAML is not installed; use CSV import instead")
    data = yaml.safe_load(text)
    if data is None:
        return []
    if isinstance(data, dict) and "skus" in data:
        rows = data["skus"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("YAML must be a list of SKUs or {skus: [...]}")
    items: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sku = str(row.get("sku") or "").strip()
        name = str(row.get("name") or "").strip()
        if not sku or not name:
            continue
        min_qty = int(row.get("min", row.get("min_qty", 1)) or 1)
        max_val = row.get("max", row.get("max_qty"))
        max_qty = int(max_val) if max_val is not None and str(max_val).strip() != "" else None
        on_hand = int(row.get("on_hand", 0) or 0)
        items.append(
            {
                "sku": sku,
                "name": name,
                "min": min_qty,
                "max": max_qty,
                "on_hand": on_hand,
                "job_tags": parse_tags(row.get("job_tags") or row.get("tags")),
            }
        )
    return items

def parse_par_file(filename: str, text: str) -> list[dict[str, Any]]:
    lower = (filename or "").lower()
    stripped = text.lstrip()
    if lower.endswith((".yaml", ".yml")) or stripped.startswith("skus:") or stripped.startswith("- sku:"):
        return parse_par_yaml(text)
    return parse_par_csv(text)

def upsert_par_items(db: sqlite3.Connection, truck_id: int, items: list[dict[str, Any]]) -> int:
    count = 0
    for item in items:
        db.execute(
            """
            INSERT INTO par_items (truck_id, sku, name, min_qty, max_qty, on_hand, job_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(truck_id, sku) DO UPDATE SET
              name = excluded.name,
              min_qty = excluded.min_qty,
              max_qty = excluded.max_qty,
              on_hand = excluded.on_hand,
              job_tags = excluded.job_tags
            """,
            (
                truck_id,
                item["sku"],
                item["name"],
                int(item["min"]),
                item.get("max"),
                int(item.get("on_hand") or 0),
                tags_to_storage(item.get("job_tags") or []),
            ),
        )
        count += 1
    db.commit()
    return count

# --- Jobs import ---

def find_truck(db: sqlite3.Connection, name_or_id: str) -> sqlite3.Row | None:
    raw = (name_or_id or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        row = db.execute("SELECT * FROM trucks WHERE id = ?", (int(raw),)).fetchone()
        if row:
            return row
    return db.execute(
        "SELECT * FROM trucks WHERE lower(name) = lower(?)",
        (raw,),
    ).fetchone()

def default_truck(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM trucks ORDER BY id ASC LIMIT 1").fetchone()

def parse_job_line(line: str, default_date: str) -> dict[str, Any] | None:
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    truck = ""
    title = text
    tag = ""
    job_ref = ""
    date = default_date
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        if len(parts) >= 2:
            truck = parts[0]
            title = parts[1]
            if len(parts) >= 3:
                tag = parts[2]
            if len(parts) >= 4:
                job_ref = parts[3]
            if len(parts) >= 5 and parts[4]:
                date = parts[4]
    return {
        "truck": truck,
        "title": title,
        "tag": tag,
        "job_ref": job_ref,
        "date": date,
        "raw_line": text,
    }

def parse_jobs_csv(text: str, default_date: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    keymap = {_norm_header(h): h for h in reader.fieldnames}
    jobs: list[dict[str, Any]] = []
    for row in reader:
        def get(*names: str, default: str = "") -> str:
            for n in names:
                key = keymap.get(_norm_header(n))
                if key is not None and row.get(key) is not None:
                    return str(row.get(key, "")).strip()
            return default

        title = get("title", "job_title", "description")
        if not title:
            continue
        jobs.append(
            {
                "truck": get("truck", "truck_id", "truck_name"),
                "title": title,
                "tag": get("tag", "job_tag"),
                "job_ref": get("job_ref", "ref"),
                "date": get("date", "scheduled_date") or default_date,
                "raw_line": None,
            }
        )
    return jobs

def parse_jobs_paste(text: str, default_date: str) -> list[dict[str, Any]]:
    first = (text.strip().splitlines() or [""])[0]
    if "," in first and _norm_header(first.split(",")[0]) in {"truck", "title", "jobtitle"}:
        return parse_jobs_csv(text, default_date)
    jobs: list[dict[str, Any]] = []
    for line in text.splitlines():
        parsed = parse_job_line(line, default_date)
        if parsed:
            jobs.append(parsed)
    return jobs

def insert_jobs(
    db: sqlite3.Connection,
    jobs: list[dict[str, Any]],
    *,
    flag_unparsed: bool = True,
) -> tuple[int, list[str]]:
    """Insert jobs. Returns (count, warnings). Unparsed truck → default truck or flag."""
    warnings: list[str] = []
    fallback = default_truck(db)
    count = 0
    now = utc_now_iso()
    for job in jobs:
        truck_row = find_truck(db, job.get("truck") or "")
        truck_id = None
        if truck_row:
            truck_id = truck_row["id"]
        elif fallback:
            truck_id = fallback["id"]
            if flag_unparsed and (job.get("truck") or "").strip():
                warnings.append(
                    f"Truck '{job.get('truck')}' not found for “{job.get('title')}” — assigned to {fallback['name']}."
                )
            elif flag_unparsed and not (job.get("truck") or "").strip():
                warnings.append(
                    f"No truck on “{job.get('title')}” — assigned to default {fallback['name']}."
                )
        else:
            warnings.append(f"No trucks exist; skipped “{job.get('title')}”.")
            continue
        db.execute(
            """
            INSERT INTO jobs_import
              (run_id, truck_id, title, job_tag, job_ref, scheduled_date, raw_line, created_at)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                truck_id,
                job["title"],
                (job.get("tag") or "").strip() or None,
                (job.get("job_ref") or "").strip() or None,
                job.get("date") or tomorrow_date_str(),
                job.get("raw_line"),
                now,
            ),
        )
        count += 1
    db.commit()
    return count, warnings

# --- Tag resolution ---

def keyword_tag(title: str) -> str | None:
    for pattern, tag in TITLE_TAG_RULES:
        if pattern.search(title or ""):
            return tag
    return None

def llm_map_title(title: str) -> str | None:
    """Optional LLM title→tag with ≤3s timeout. Returns None on any failure."""
    if not llm_configured():
        return None
    tags_list = ", ".join(sorted(KNOWN_TAGS - {"always", "truck_base"}))
    prompt = (
        f"Map this service job title to exactly one tag from: {tags_list}. "
        f"Reply with only the tag.\nTitle: {title}"
    )
    try:
        ollama = _env("OLLAMA_BASE_URL").rstrip("/")
        if ollama:
            body = json.dumps(
                {
                    "model": _env("LLM_MODEL") or "llama3.2",
                    "prompt": prompt,
                    "stream": False,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{ollama}/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (data.get("response") or "").strip().split()[0].strip(".,\"'`")
            if text in KNOWN_TAGS:
                return text
            return None

        base = _env("LLM_BASE_URL").rstrip("/")
        key = _env("LLM_API_KEY")
        model = _env("LLM_MODEL") or "gpt-4o-mini"
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16,
                "temperature": 0,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .split()[0]
            .strip(".,\"'`")
        )
        if content in KNOWN_TAGS:
            return content
    except Exception:
        return None
    return None

def resolve_job_tag(title: str, provided_tag: str | None = None) -> str:
    tag = (provided_tag or "").strip()
    if tag:
        return tag
    llm = llm_map_title(title)
    if llm:
        return llm
    kw = keyword_tag(title)
    if kw:
        return kw
    return "general_service"


# Re-export restock/notify from helpers_ops (avoid circular import at definition time)
def __getattr__(name: str):
    if name in {
        "qty_to_pull",
        "build_restock",
        "run_lines_by_truck",
        "trucks_for_run",
        "format_checklist_text",
        "run_csv",
        "send_email",
        "send_sms",
        "notify_runner",
    }:
        import helpers_ops as _ops
        return getattr(_ops, name)
    raise AttributeError(name)
