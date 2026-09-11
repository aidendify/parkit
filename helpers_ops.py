"""ParKit restock build and runner notify."""
from __future__ import annotations

import base64
import csv
import io
import json
import smtplib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

import helpers as H

# --- Restock build ---

def qty_to_pull(on_hand: int, min_qty: int) -> int:
    if on_hand < min_qty:
        gap = min_qty - on_hand
        if on_hand == 0 and min_qty >= 1:
            return max(gap, 1)
        return max(gap, 1) if gap > 0 else 0
    return 0

def build_restock(db: sqlite3.Connection, for_date: str) -> int:
    """Build restock run for date. Attach matching jobs. Return run id."""
    jobs = db.execute(
        """
        SELECT * FROM jobs_import
        WHERE scheduled_date = ? AND truck_id IS NOT NULL
        ORDER BY id ASC
        """,
        (for_date,),
    ).fetchall()

    truck_tags: dict[int, set[str]] = {}
    job_resolved: list[tuple[int, str]] = []  # job id, resolved tag
    for job in jobs:
        tag = H.resolve_job_tag(job["title"], job["job_tag"])
        job_resolved.append((job["id"], tag))
        tid = job["truck_id"]
        truck_tags.setdefault(tid, set()).add(tag)

    now = H.utc_now_iso()
    cur = db.execute(
        "INSERT INTO restock_runs (for_date, created_at, notes) VALUES (?, ?, ?)",
        (for_date, now, None),
    )
    run_id = int(cur.lastrowid)

    for job_id, tag in job_resolved:
        db.execute(
            "UPDATE jobs_import SET run_id = ?, job_tag = COALESCE(NULLIF(job_tag,''), ?) WHERE id = ?",
            (run_id, tag, job_id),
        )

    for tid, tags in list(truck_tags.items()):
        tags.update({"always", "truck_base"})  # intersect filter handles absence

    all_truck_ids = {j["truck_id"] for j in jobs if j["truck_id"]}

    for tid in all_truck_ids:
        job_needed = truck_tags.get(tid, set())
        items = db.execute(
            "SELECT * FROM par_items WHERE truck_id = ?",
            (tid,),
        ).fetchall()
        for item in items:
            item_tags = set(H.tags_from_row(item))
            match_job = item_tags & job_needed
            match_base = item_tags & {"always", "truck_base"}
            if not match_job and not match_base:
                continue
            pull = qty_to_pull(int(item["on_hand"]), int(item["min_qty"]))
            if pull < 1:
                continue
            reason = sorted(match_job or match_base)
            db.execute(
                """
                INSERT INTO restock_lines
                  (run_id, truck_id, sku, name, on_hand, min_qty, qty_to_pull, reason_tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tid,
                    item["sku"],
                    item["name"],
                    int(item["on_hand"]),
                    int(item["min_qty"]),
                    pull,
                    H.tags_to_storage(reason),
                ),
            )

    db.commit()
    return run_id

def run_lines_by_truck(db: sqlite3.Connection, run_id: int) -> dict[int, list[sqlite3.Row]]:
    rows = db.execute(
        """
        SELECT rl.*, t.name AS truck_name
        FROM restock_lines rl
        JOIN trucks t ON t.id = rl.truck_id
        WHERE rl.run_id = ?
        ORDER BY t.name, rl.sku
        """,
        (run_id,),
    ).fetchall()
    out: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        out.setdefault(r["truck_id"], []).append(r)
    return out

def trucks_for_run(db: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT DISTINCT t.*
        FROM trucks t
        JOIN jobs_import j ON j.truck_id = t.id
        WHERE j.run_id = ?
        ORDER BY t.name
        """,
        (run_id,),
    ).fetchall()

def format_checklist_text(truck_name: str, lines: list[sqlite3.Row], for_date: str) -> str:
    if not lines:
        return f"Truck {truck_name} is covered for {for_date}."
    out = [f"ParKit restock — {truck_name} — {for_date}", ""]
    for ln in lines:
        out.append(
            f"- {ln['sku']}  {ln['name']}  pull {ln['qty_to_pull']}  "
            f"(on hand {ln['on_hand']}, min {ln['min_qty']})"
        )
    return "\n".join(out)

def run_csv(db: sqlite3.Connection, run_id: int) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["truck", "sku", "name", "on_hand", "min_qty", "qty_to_pull", "reason_tags"])
    rows = db.execute(
        """
        SELECT t.name AS truck_name, rl.*
        FROM restock_lines rl
        JOIN trucks t ON t.id = rl.truck_id
        WHERE rl.run_id = ?
        ORDER BY t.name, rl.sku
        """,
        (run_id,),
    ).fetchall()
    for r in rows:
        tags = H.parse_tags(r["reason_tags"])
        w.writerow(
            [
                r["truck_name"],
                r["sku"],
                r["name"],
                r["on_hand"],
                r["min_qty"],
                r["qty_to_pull"],
                "|".join(tags),
            ]
        )
    return buf.getvalue()

# --- Notify ---

def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    if not H.smtp_configured():
        return False, "SMTP not configured"
    host = H._env("SMTP_HOST")
    port = int(H._env("SMTP_PORT") or "587")
    user = H._env("SMTP_USER")
    password = H._env("SMTP_PASSWORD")
    use_tls = (H._env("SMTP_TLS") or "true").lower() in {"1", "true", "yes"}
    from_email = H._env("FROM_EMAIL") or user or "parkit@localhost"
    from_name = H._env("FROM_NAME") or H.business_name()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, str(exc)

def send_sms(to_phone: str, body: str) -> tuple[bool, str]:
    if not H.sms_configured():
        return False, "SMS not configured"
    sid = H._env("TWILIO_ACCOUNT_SID")
    token = H._env("TWILIO_AUTH_TOKEN")
    from_num = H._env("TWILIO_FROM_NUMBER")
    data = urllib.parse.urlencode(
        {"To": to_phone, "From": from_num, "Body": body}
    ).encode("utf-8")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Basic {auth}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, "sent"
    except urllib.error.HTTPError as exc:
        return False, f"Twilio HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)

def notify_runner(db: sqlite3.Connection, run_id: int) -> list[str]:
    """Email/SMS runner if configured. Returns status messages."""
    run = db.execute("SELECT * FROM restock_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return ["Run not found"]
    lines = db.execute(
        "SELECT COUNT(*) AS c FROM restock_lines WHERE run_id = ?", (run_id,)
    ).fetchone()["c"]
    base = H.public_base_url() or "http://localhost:8080"
    link = f"{base}/runs/{run_id}"
    summary = (
        f"{H.business_name()} restock for {run['for_date']}: "
        f"{lines} gap line(s). Open: {link}"
    )
    msgs: list[str] = []
    email = H.runner_email()
    phone = H.runner_phone()
    if email and H.smtp_configured():
        ok, detail = send_email(email, f"ParKit restock {run['for_date']}", summary)
        msgs.append(f"Email {'sent' if ok else 'failed'}: {detail}")
    elif email:
        msgs.append("RUNNER_EMAIL set but SMTP not configured — skipped email.")
    if phone and H.sms_configured():
        ok, detail = send_sms(phone, summary[:300])
        msgs.append(f"SMS {'sent' if ok else 'failed'}: {detail}")
    elif phone:
        msgs.append("RUNNER_PHONE set but Twilio not configured — skipped SMS.")
    if not email and not phone:
        msgs.append("No RUNNER_EMAIL / RUNNER_PHONE set.")
    return msgs
