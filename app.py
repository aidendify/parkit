"""ParKit: tomorrow's jobs × truck par → missing-SKU restock checklist."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "parkit-self-hosted-change-me")

app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "sms_configured": H.sms_configured(),
        "llm_configured": H.llm_configured(),
        "business_name": H.business_name(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "runner_channel": bool(H.runner_email() or H.runner_phone()),
        "tomorrow": H.tomorrow_date_str(),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "sms_configured": H.sms_configured(),
            "llm_configured": H.llm_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    db = H.get_db()
    runs = db.execute(
        """
        SELECT r.*,
          (SELECT COUNT(*) FROM restock_lines rl WHERE rl.run_id = r.id) AS line_count,
          (SELECT COUNT(DISTINCT rl.truck_id) FROM restock_lines rl WHERE rl.run_id = r.id) AS truck_count
        FROM restock_runs r
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT 30
        """
    ).fetchall()
    trucks = db.execute("SELECT * FROM trucks ORDER BY name").fetchall()
    pending_jobs = db.execute(
        """
        SELECT COUNT(*) AS c FROM jobs_import
        WHERE scheduled_date = ? AND (run_id IS NULL)
        """,
        (H.tomorrow_date_str(),),
    ).fetchone()["c"]
    return render_template(
        "index.html",
        runs=runs,
        trucks=trucks,
        pending_jobs=pending_jobs,
        for_date=H.tomorrow_date_str(),
    )


@app.route("/trucks", methods=["GET", "POST"])
def trucks():
    db = H.get_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        if not name:
            flash("Truck name is required.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO trucks (name, notes, created_at) VALUES (?, ?, ?)",
                    (name, notes or None, H.utc_now_iso()),
                )
                db.commit()
                flash(f"Created truck {name}. Tip: start with one truck's par sheet.", "ok")
                truck = db.execute(
                    "SELECT id FROM trucks WHERE name = ?", (name,)
                ).fetchone()
                return redirect(url_for("truck_par", truck_id=truck["id"]))
            except Exception:
                flash("That truck name already exists.", "error")
    rows = db.execute(
        """
        SELECT t.*,
          (SELECT COUNT(*) FROM par_items p WHERE p.truck_id = t.id) AS par_count
        FROM trucks t
        ORDER BY t.name
        """
    ).fetchall()
    return render_template("trucks.html", trucks=rows)


@app.route("/trucks/<int:truck_id>/par", methods=["GET", "POST"])
def truck_par(truck_id: int):
    db = H.get_db()
    truck = db.execute("SELECT * FROM trucks WHERE id = ?", (truck_id,)).fetchone()
    if not truck:
        flash("Truck not found.", "error")
        return redirect(url_for("trucks"))

    if request.method == "POST":
        action = (request.form.get("action") or "import").strip()
        if action == "update_on_hand":
            for key, val in request.form.items():
                if key.startswith("on_hand_"):
                    try:
                        item_id = int(key.split("_", 2)[2])
                        on_hand = int(val)
                    except (ValueError, IndexError):
                        continue
                    db.execute(
                        "UPDATE par_items SET on_hand = ? WHERE id = ? AND truck_id = ?",
                        (on_hand, item_id, truck_id),
                    )
            db.commit()
            flash("On-hand counts updated.", "ok")
            return redirect(url_for("truck_par", truck_id=truck_id))

        if action == "edit_truck":
            name = (request.form.get("name") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            if name:
                try:
                    db.execute(
                        "UPDATE trucks SET name = ?, notes = ? WHERE id = ?",
                        (name, notes or None, truck_id),
                    )
                    db.commit()
                    flash("Truck updated.", "ok")
                except Exception:
                    flash("Could not rename (name may be taken).", "error")
            return redirect(url_for("truck_par", truck_id=truck_id))

        text = ""
        filename = "paste.csv"
        upload = request.files.get("par_file")
        if upload and upload.filename:
            filename = upload.filename
            text = upload.read().decode("utf-8", errors="replace")
        else:
            text = request.form.get("par_text") or ""
        if not text.strip():
            flash("Provide a YAML/CSV file or paste.", "error")
        else:
            try:
                items = H.parse_par_file(filename, text)
                if not items:
                    flash("No SKUs found in import.", "error")
                else:
                    n = H.upsert_par_items(db, truck_id, items)
                    flash(f"Imported {n} par SKU(s).", "ok")
            except Exception as exc:
                flash(f"Import failed: {exc}", "error")
        return redirect(url_for("truck_par", truck_id=truck_id))

    items = db.execute(
        "SELECT * FROM par_items WHERE truck_id = ? ORDER BY sku",
        (truck_id,),
    ).fetchall()
    parsed = []
    for it in items:
        d = dict(it)
        d["tags_list"] = H.tags_from_row(it)
        parsed.append(d)
    return render_template("truck_par.html", truck=truck, items=parsed)


@app.route("/jobs/import", methods=["GET", "POST"])
def jobs_import():
    db = H.get_db()
    trucks = db.execute("SELECT * FROM trucks ORDER BY name").fetchall()
    default_date = H.tomorrow_date_str()
    if request.method == "POST":
        date = (request.form.get("date") or default_date).strip() or default_date
        text = request.form.get("jobs_text") or ""
        upload = request.files.get("jobs_file")
        if upload and upload.filename:
            text = upload.read().decode("utf-8", errors="replace")
        if not text.strip():
            flash("Paste jobs or upload a CSV.", "error")
            return render_template(
                "jobs_import.html",
                trucks=trucks,
                default_date=date,
                form_text="",
            ), 400
        first = text.strip().splitlines()[0]
        if "," in first and H._norm_header(first.split(",")[0]) in {
            "truck",
            "title",
            "jobtitle",
        }:
            jobs = H.parse_jobs_csv(text, date)
        else:
            jobs = H.parse_jobs_paste(text, date)
        for j in jobs:
            if not j.get("date"):
                j["date"] = date
        count, warnings = H.insert_jobs(db, jobs)
        for w in warnings:
            flash(w, "error" if "skipped" in w.lower() else "ok")
        flash(f"Imported {count} job(s) for {date}.", "ok")
        return redirect(url_for("index"))
    return render_template(
        "jobs_import.html",
        trucks=trucks,
        default_date=default_date,
        form_text="",
    )


@app.post("/runs/build")
def runs_build():
    db = H.get_db()
    for_date = (request.form.get("for_date") or H.tomorrow_date_str()).strip()
    notify = request.form.get("notify") == "1"
    job_count = db.execute(
        "SELECT COUNT(*) AS c FROM jobs_import WHERE scheduled_date = ?",
        (for_date,),
    ).fetchone()["c"]
    if job_count == 0:
        flash(f"No jobs for {for_date}. Import jobs first.", "error")
        return redirect(url_for("jobs_import"))
    run_id = H.build_restock(db, for_date)
    flash(f"Built restock run #{run_id} for {for_date}.", "ok")
    if notify and (H.runner_email() or H.runner_phone()):
        for msg in H.notify_runner(db, run_id):
            flash(msg, "ok")
    return redirect(url_for("run_detail", run_id=run_id))


@app.get("/runs/<int:run_id>")
def run_detail(run_id: int):
    db = H.get_db()
    run = db.execute("SELECT * FROM restock_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        flash("Run not found.", "error")
        return redirect(url_for("index"))
    by_truck = H.run_lines_by_truck(db, run_id)
    trucks = H.trucks_for_run(db, run_id)
    truck_ids = {t["id"] for t in trucks}
    for tid in by_truck:
        if tid not in truck_ids:
            t = db.execute("SELECT * FROM trucks WHERE id = ?", (tid,)).fetchone()
            if t:
                trucks = list(trucks) + [t]
    checklists = []
    for t in trucks:
        lines = by_truck.get(t["id"], [])
        text = H.format_checklist_text(t["name"], lines, run["for_date"])
        checklists.append({"truck": t, "lines": lines, "text": text})
    jobs = db.execute(
        """
        SELECT j.*, t.name AS truck_name
        FROM jobs_import j
        LEFT JOIN trucks t ON t.id = j.truck_id
        WHERE j.run_id = ?
        ORDER BY t.name, j.id
        """,
        (run_id,),
    ).fetchall()
    return render_template(
        "run_detail.html",
        run=run,
        checklists=checklists,
        jobs=jobs,
    )


@app.get("/runs/<int:run_id>.csv")
def run_csv(run_id: int):
    db = H.get_db()
    run = db.execute("SELECT * FROM restock_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        flash("Run not found.", "error")
        return redirect(url_for("index"))
    body = H.run_csv(db, run_id)
    return Response(
        body,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="parkit-run-{run_id}.csv"'
        },
    )


@app.post("/runs/<int:run_id>/notify")
def run_notify(run_id: int):
    db = H.get_db()
    run = db.execute("SELECT * FROM restock_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        flash("Run not found.", "error")
        return redirect(url_for("index"))
    for msg in H.notify_runner(db, run_id):
        flash(msg, "ok")
    return redirect(url_for("run_detail", run_id=run_id))


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


try:
    init_db()
except Exception:
    pass


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT") or "8080")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
