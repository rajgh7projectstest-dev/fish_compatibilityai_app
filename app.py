#!/usr/bin/env python3
import os
import io
import csv
import json
import math
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for, session,
    send_file, Response, flash, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from authlib.integrations.flask_client import OAuth
import requests

# Optional: PDF generation via reportlab
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------- App & Config ----------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "index"

# ---------- User model ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False)
    name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

# ---------- OAuth (Google) ----------
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    api_base_url="https://www.googleapis.com/",
    client_kwargs={"scope": "openid email profile"},
)

# ---------- Fish data helpers ----------
def fish_data_path():
    return os.path.join(app.root_path, "static", "fish_data.json")

def get_num(v, fallback=None):
    try:
        if v is None:
            return fallback
        return float(v)
    except Exception:
        return fallback

def get_range(item, key, default):
    rng = item.get(key) or item.get(key.lower())
    if isinstance(rng, list) and len(rng) >= 2:
        try:
            return [float(rng[0]), float(rng[1])]
        except:
            return default
    low = get_num(item.get(f"{key}_min")) or get_num(item.get(f"{key.lower()}_min"))
    high = get_num(item.get(f"{key}_max")) or get_num(item.get(f"{key.lower()}_max"))
    if low is not None and high is not None:
        return [low, high]
    return default

def load_fish_data():
    """
    Loads and normalizes fish_data.json.
    Returns list of fish dicts with stable keys.
    """
    path = fish_data_path()
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except Exception:
            return []

    normalized = []
    for item in raw:
        fid = str(item.get("id") or item.get("ID") or "").strip()
        name = (item.get("name") or item.get("Name") or "").strip()
        if not fid or not name:
            continue

        compat_raw = item.get("compatibility") or item.get("compat") or []
        compatibility = [str(x) for x in compat_raw]

        min_tank_size = get_num(item.get("min_tank_size"), None) or get_num(item.get("minTankSize"), None)
        adult_size = get_num(item.get("adult_size"), get_num(item.get("avg_size"), None)) or get_num(item.get("avg_size"), None)

        temperature = get_range(item, "temperature", [22.0, 26.0])
        ph = get_range(item, "ph", [6.5, 7.5])
        hardness = get_range(item, "hardness", [1.0, 12.0])

        temperament = item.get("temperament") or item.get("behavior") or "Unknown"
        diet = item.get("diet") or "Omnivore"
        schooling = bool(item.get("schooling", False))
        # Accept multiple keys for min group
        try:
            min_group_size = int(item.get("min_group_size", item.get("min_group", item.get("minGroup", 1))))
        except Exception:
            min_group_size = 6 if schooling else 1

        image = item.get("image") or item.get("img") or "/static/fish/placeholder.jpg"

        normalized.append({
            "id": fid,
            "name": name,
            "compatibility": compatibility,
            "min_tank_size": float(min_tank_size) if min_tank_size is not None else None,
            "adult_size": float(adult_size) if adult_size is not None else None,
            "temperature": [float(temperature[0]), float(temperature[1])],
            "ph": [float(ph[0]), float(ph[1])],
            "hardness": [float(hardness[0]), float(hardness[1])],
            "temperament": temperament,
            "diet": diet,
            "schooling": schooling,
            "min_group_size": int(min_group_size),
            "image": image
        })

    return normalized

def build_fish_map():
    fishes = load_fish_data()
    return {f["id"]: f for f in fishes}

# ---------- Select2 API (search + pagination + id prefetch) ----------
@app.route("/fish_data")
@login_required
def fish_data_api():
    """
    Supports:
      - ?id=123  -> returns item with that id (for Select2 prepopulation)
      - ?q=term&page=1 -> paginated search for Select2
    """
    fishes = load_fish_data()

    fid = request.args.get("id")
    if fid:
        it = next((f for f in fishes if f["id"] == str(fid)), None)
        if not it:
            return jsonify({"items": []})
        return jsonify({"items": [{"id": it["id"], "text": it["name"]}], "more": False})

    q = (request.args.get("q") or "").strip().lower()
    page = int(request.args.get("page") or 1)
    per_page = 20

    if q:
        fishes = [f for f in fishes if q in f["name"].lower()]

    total = len(fishes)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = fishes[start:end]
    more = end < total

    items = [{"id": f["id"], "text": f["name"]} for f in page_items]
    return jsonify({"items": items, "more": more})

# ---------- Core compute utilities ----------
def pairwise_compatibility_matrix(fishes):
    """
    fishes: list of species dicts (one entry per species, counts ignored)
    returns n x n matrix with 'compatible'|'semi-compatible'|'incompatible'|'self'
    """
    n = len(fishes)
    matrix = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = "self"
                continue
            id_i = fishes[i]["id"]
            id_j = fishes[j]["id"]
            i_list = fishes[i].get("compatibility", []) or []
            j_list = fishes[j].get("compatibility", []) or []
            if id_j in i_list and id_i in j_list:
                matrix[i][j] = "compatible"
            elif id_j in i_list or id_i in j_list:
                matrix[i][j] = "semi-compatible"
            else:
                matrix[i][j] = "incompatible"
    return matrix

def compute_range_overlap(ranges):
    """
    ranges: list of [low, high]
    returns (low, high, ok_bool)
    """
    low = max(r[0] for r in ranges)
    high = min(r[1] for r in ranges)
    return (low, high, low <= high)

def estimate_tank_size_litres(fishes_expanded):
    """
    fishes_expanded: list of fish dicts repeated by count
    Heuristic:
      - base: max(min_tank_size) present among species (or 10 L)
      - extra: sum(0.5 * adult_size(cm)) per fish * waste factors
      - schooling accounted because expansion lists each fish individually
      - final: rounded up to nearest 5 L
    """
    # compute base from unique species if present
    unique_min = [f.get("min_tank_size") for f in fishes_expanded if f.get("min_tank_size")]
    base = float(max(unique_min)) if unique_min else 10.0

    extra = 0.0
    for f in fishes_expanded:
        adult_cm = f.get("adult_size") or 5.0
        per_fish = 0.5 * float(adult_cm)
        waste_factor = 1.0
        temp_str = f.get("temperament", "").lower()
        if "aggressive" in temp_str:
            waste_factor += 0.25
        name_l = f.get("name", "").lower()
        if any(x in name_l for x in ["goldfish", "oscar", "koi", "pleco"]):
            waste_factor += 0.6
        extra += per_fish * waste_factor

    # buffer
    recommended = max(base, extra + base * 0.15)
    recommended = math.ceil(recommended / 5.0) * 5
    return int(recommended)

def collect_warnings(selected_species, matrix, overlaps):
    """
    selected_species: list of species dicts (one per species) with 'count'
    matrix: species-level pairwise matrix
    overlaps: dict with temperature/ph/hardness tuples
    Returns a summarized list of warning strings.
    """
    warnings = []

    # Schooling / min group size check
    for fish in selected_species:
        if fish.get("schooling"):
            min_group = int(fish.get("min_group_size", 1))
            count = int(fish.get("count", 1))
            if count < min_group:
                warnings.append(f"{fish['name']} typically needs a group of {min_group}. You selected {count}.")

    # Parameter overlaps
    t = overlaps.get("temperature")
    p = overlaps.get("ph")
    h = overlaps.get("hardness")
    if not t[2]:
        warnings.append("Selected fishes do not share a common temperature range.")
    if not p[2]:
        warnings.append("Selected fishes do not share a common pH range.")
    if not h[2]:
        warnings.append("Selected fishes do not share a common hardness (dGH) range.")

    # Incompatible pairs at species-level (no repeats)
    incompatible_pairs = set()
    n = len(selected_species)
    for i in range(n):
        for j in range(i + 1, n):
            if matrix[i][j] == "incompatible":
                pair = tuple(sorted([selected_species[i]["name"], selected_species[j]["name"]]))
                incompatible_pairs.add(pair)
    if incompatible_pairs:
        formatted = "; ".join([f"{a} × {b}" for a, b in sorted(incompatible_pairs)])
        warnings.append(f"Incompatible pairs: {formatted}")

    # Multiple aggressive species
    aggressive = [f["name"] for f in selected_species if "aggressive" in (f.get("temperament","").lower())]
    if len(aggressive) > 1:
        warnings.append("Multiple aggressive/territorial species selected: " + ", ".join(aggressive))

    return warnings

# ---------- Routes ----------
@app.route("/")
def index():
    # Simple landing / login call-to-action
    return render_template("index.html")

@app.route("/login")
def login():
    redirect_uri = url_for("authorize", _external=True)
    return google.authorize_redirect(redirect_uri, prompt="consent", access_type="offline")

@app.route("/authorize")
def authorize():
    token = google.authorize_access_token()
    resp = google.get("https://openidconnect.googleapis.com/v1/userinfo", token=token)
    info = resp.json() if resp else {}
    email = info.get("email")
    name = info.get("name") or email
    if not email:
        return "Google login failed (no email)", 400

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name)
        db.session.add(user)
        db.session.commit()

    login_user(user)
    return redirect(url_for("dashboard"))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    last_report = session.get("last_report")
    return render_template("dashboard.html", user=current_user, last_report=last_report)

# Compute route (full)
@app.route("/compute", methods=["GET", "POST"])
@login_required
def compute():
    fishes_all = load_fish_data()

    if request.method == "POST":
        selected_ids = request.form.getlist("fish_ids[]")
        selected_counts = request.form.getlist("fish_counts[]")

        id_map = {f["id"]: f for f in fishes_all}
        selected_species = []   # one entry per species with .count
        expanded = []           # repeated entries by count for tank calc

        for fid, count_str in zip(selected_ids, selected_counts):
            if fid in id_map:
                base = id_map[fid]
                try:
                    count = max(1, int(count_str or "1"))
                except Exception:
                    count = 1
                fish_copy = base.copy()
                fish_copy["count"] = count
                selected_species.append(fish_copy)
                # expanded uses base (without count) repeated
                for _ in range(count):
                    expanded.append(base)

        if not selected_species:
            return render_template("compute.html",
                                   error="Selected fishes not found. Please choose from the list.",
                                   selected_ids=selected_ids,
                                   selected_counts=selected_counts)

        # species-level matrix
        matrix = pairwise_compatibility_matrix(selected_species)

        # parameter overlaps (species-level)
        temp_ranges = [f.get("temperature", [22, 26]) for f in selected_species]
        ph_ranges = [f.get("ph", [6.5, 7.5]) for f in selected_species]
        hard_ranges = [f.get("hardness", [1, 12]) for f in selected_species]
        overlaps = {
            "temperature": compute_range_overlap(temp_ranges),
            "ph": compute_range_overlap(ph_ranges),
            "hardness": compute_range_overlap(hard_ranges)
        }

        # tank size using expanded list (counts matter)
        tank_l = estimate_tank_size_litres(expanded)
        tank_gal = round(tank_l * 0.264172, 1)

        # warnings (species-level)
        warnings = collect_warnings(selected_species, matrix, overlaps)

        # compatibility score (species-level)
        n = len(selected_species)
        total_pairs = n * (n - 1) / 2 if n > 1 else 1
        compatible_pairs = 0
        semi_pairs = 0
        for i in range(n):
            for j in range(i + 1, n):
                if matrix[i][j] == "compatible":
                    compatible_pairs += 1
                elif matrix[i][j] == "semi-compatible":
                    semi_pairs += 1
        score = int(100 * (compatible_pairs + 0.5 * semi_pairs) / total_pairs) if total_pairs > 0 else 100

        # persist last report in session for dashboard download
        session["last_report"] = {
            "selected_ids": selected_ids,
            "selected_counts": selected_counts,
            "fishes": [{"id": f["id"], "name": f["name"], "count": f["count"]} for f in selected_species],
            "tank_l": tank_l,
            "tank_gal": tank_gal,
            "score": score,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        return render_template(
            "result.html",
            fishes=selected_species,
            matrix=matrix,
            tank_l=tank_l,
            tank_gal=tank_gal,
            overlaps=overlaps,
            warnings=warnings,
            score=score,
            selected_ids=selected_ids,
            selected_counts=selected_counts
        )

    # GET
    # prepopulate form with last selection if present
    last = session.get("last_report")
    if last:
        selected_ids = last.get("selected_ids", [])
        selected_counts = last.get("selected_counts", [])
    else:
        selected_ids = []
        selected_counts = []
    return render_template("compute.html", selected_ids=selected_ids, selected_counts=selected_counts)

# Download report
@app.route("/download_report", methods=["POST"])
@login_required
def download_report():
    fmt = (request.form.get("format") or "csv").lower()
    selected_ids = request.form.getlist("fish_ids[]")
    selected_counts = request.form.getlist("fish_counts[]")

    fishes_all = load_fish_data()
    id_map = {f["id"]: f for f in fishes_all}

    selected_species = []
    expanded = []
    for fid, count_str in zip(selected_ids, selected_counts):
        if fid in id_map:
            base = id_map[fid]
            try:
                count = max(1, int(count_str or "1"))
            except Exception:
                count = 1
            fcopy = base.copy()
            fcopy["count"] = count
            selected_species.append(fcopy)
            for _ in range(count):
                expanded.append(base)

    if not selected_species:
        flash("No fishes selected for download.", "warning")
        return redirect(url_for("compute"))

    # species-level data for report
    matrix = pairwise_compatibility_matrix(selected_species)
    temp_ranges = [f.get("temperature", [22, 26]) for f in selected_species]
    ph_ranges = [f.get("ph", [6.5, 7.5]) for f in selected_species]
    hard_ranges = [f.get("hardness", [1, 12]) for f in selected_species]
    overlaps = {
        "temperature": compute_range_overlap(temp_ranges),
        "ph": compute_range_overlap(ph_ranges),
        "hardness": compute_range_overlap(hard_ranges)
    }
    tank_l = estimate_tank_size_litres(expanded)
    tank_gal = round(tank_l * 0.264172, 1)
    warnings = collect_warnings(selected_species, matrix, overlaps)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Fish Compatibility Report"])
        writer.writerow([f"Generated: {datetime.utcnow().isoformat()} UTC"])
        writer.writerow([])
        writer.writerow(["id", "name", "count", "adult_size_cm", "min_tank_size_L", "temp_min", "temp_max", "ph_min", "ph_max", "hardness_min", "hardness_max", "temperament", "diet", "schooling", "min_group_size"])
        for f in selected_species:
            writer.writerow([
                f.get("id", ""),
                f.get("name", ""),
                f.get("count", 1),
                f.get("adult_size", ""),
                f.get("min_tank_size", ""),
                f.get("temperature", [None, None])[0], f.get("temperature", [None, None])[1],
                f.get("ph", [None, None])[0], f.get("ph", [None, None])[1],
                f.get("hardness", [None, None])[0], f.get("hardness", [None, None])[1],
                f.get("temperament", ""), f.get("diet", ""), f.get("schooling", False), f.get("min_group_size", 1)
            ])
        writer.writerow([])
        writer.writerow(["Tank recommendation (L)", tank_l])
        writer.writerow(["Tank recommendation (gal)", tank_gal])
        writer.writerow([])
        t = overlaps["temperature"]; p = overlaps["ph"]; h = overlaps["hardness"]
        writer.writerow(["Temperature overlap", t[0], t[1], t[2]])
        writer.writerow(["pH overlap", p[0], p[1], p[2]])
        writer.writerow(["Hardness overlap", h[0], h[1], h[2]])
        writer.writerow([])
        writer.writerow(["Warnings"])
        for w in warnings:
            writer.writerow([w])

        csv_data = output.getvalue().encode("utf-8")
        output.close()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename=fish_report_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv"}
        )

    elif fmt == "pdf":
        if not REPORTLAB_AVAILABLE:
            flash("PDF library not available on server; downloaded CSV instead.", "warning")
            return redirect(url_for("compute"))

        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        w, h = letter
        margin = 40
        y = h - margin
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, y, "🐟 Fish Compatibility Report")
        c.setFont("Helvetica", 9)
        y -= 18
        c.drawString(margin, y, f"Generated: {datetime.utcnow().isoformat()} UTC")
        y -= 18

        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, y, f"Recommended tank: {tank_l} L ({tank_gal} gal)")
        y -= 16

        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "Selected fishes:")
        y -= 14
        c.setFont("Helvetica", 10)
        for f in selected_species:
            line = f"{f.get('name','?')} × {f.get('count',1)} — {f.get('adult_size','?')} cm"
            c.drawString(margin, y, line)
            # try draw image (small)
            try:
                img_path = os.path.join(app.root_path, f["image"].lstrip("/"))
                if os.path.exists(img_path):
                    c.drawImage(img_path, margin + 380, y - 6, width=60, height=30, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
            y -= 14
            if y < margin + 80:
                c.showPage()
                y = h - margin

        y -= 8
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "Overlaps:")
        y -= 12
        c.setFont("Helvetica", 10)
        t = overlaps["temperature"]; p = overlaps["ph"]; hard = overlaps["hardness"]
        c.drawString(margin, y, f"Temperature: {t[0]} - {t[1]} (ok={t[2]})"); y -= 12
        c.drawString(margin, y, f"pH: {p[0]} - {p[1]} (ok={p[2]})"); y -= 12
        c.drawString(margin, y, f"Hardness: {hard[0]} - {hard[1]} (ok={hard[2]})"); y -= 14

        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "Warnings:")
        y -= 12
        c.setFont("Helvetica", 10)
        for w in warnings:
            # wrap naive
            if len(w) <= 90:
                c.drawString(margin, y, f"- {w}"); y -= 12
            else:
                # break into chunks
                parts = [w[i:i+90] for i in range(0, len(w), 90)]
                for ptxt in parts:
                    c.drawString(margin, y, ptxt); y -= 12
            if y < margin + 60:
                c.showPage(); y = h - margin

        c.save()
        buffer.seek(0)
        return send_file(buffer, mimetype="application/pdf", as_attachment=True,
                         download_name=f"fish_report_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf")
    else:
        return "Unknown format", 400

# ---------- Ask AI route (preserve fallback) ----------
@app.route("/ask")
@login_required
def ask():
    question = (request.args.get("question") or "").strip()
    if not question:
        return jsonify({"answer": "Please type a question."})

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        fallback = "I'm ready! Add GEMINI_API_KEY to get AI answers. Meanwhile: "
        if "betta" in question.lower() and "gold" in question.lower():
            return jsonify({"answer": fallback + "Bettas and goldfish are not ideal tankmates due to temp and behavior differences."})
        return jsonify({"answer": fallback + "Ask about tank mates, water params, diet, or care tips."})

    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
        payload = {
            "contents": [{
                "parts": [{"text": f"You are an aquarium assistant. Answer briefly: {question}"}]
            }]
        }
        r = requests.post(f"{url}?key={api_key}", json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        txt = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        if not txt:
            txt = "I couldn't get a response right now."
        return jsonify({"answer": txt})
    except Exception as e:
        return jsonify({"answer": f"AI error: {e}"})

# ---------- Health check ----------
@app.route("/healthz")
def healthz():
    return "ok"

# ---------- Run ----------
if __name__ == "__main__":
    app.run(debug=True)
