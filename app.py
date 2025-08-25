import os
import json
from math import ceil
from datetime import datetime

from flask import Flask, redirect, url_for, render_template, request, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, current_user, logout_user
from authlib.integrations.flask_client import OAuth
import requests

app = Flask(__name__)

# ==== Core config (adjust SECRET_KEY for prod) ====
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Keeps OAuth state cookie stable locally
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "index"

# ==== User model ====
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False)
    name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==== OAuth: Google WITHOUT 'openid' ====
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    api_base_url="https://www.googleapis.com/",
    client_kwargs={"scope": "email profile"},  # ❌ no 'openid' -> avoids jwks_uri issues
)

# ==== Helpers ====
def fish_data_path():
    # Always read from /static/fish_data.json
    return os.path.join(app.root_path, "static", "fish_data.json")

def load_fish_data():
    path = fish_data_path()
    if not os.path.exists(path):
        # Empty fallback to avoid 500s if file missing
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Normalize shape
    normalized = []
    for item in data:
        normalized.append({
            "id": str(item.get("id") or item.get("Id") or item.get("ID")),
            "name": item.get("name") or item.get("Name") or "",
            "compatibility": [str(x) for x in (item.get("compatibility") or [])],
        })
    return [f for f in normalized if f["id"] and f["name"]]

# ==== Routes ====
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login")
def login():
    redirect_uri = url_for("authorize", _external=True)
    # prompt=consent for first-time testing; remove later if you want silent SSO
    return google.authorize_redirect(redirect_uri, prompt="consent", access_type="offline")

@app.route("/authorize")
def authorize():
    # Exchange code for tokens (no ID-token parsing)
    token = google.authorize_access_token()
    # Always fetch userinfo explicitly (no jwks needed)
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
    return render_template("dashboard.html", user=current_user)

# ---- Fish data API for Select2 (search + pagination) ----
@app.route("/fish_data")
@login_required
def fish_data_api():
    q = (request.args.get("q") or "").strip().lower()
    page = int(request.args.get("page") or 1)
    per_page = 20

    fishes = load_fish_data()
    if q:
        fishes = [f for f in fishes if q in f["name"].lower()]

    total = len(fishes)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = fishes[start:end]
    more = end < total

    # Select2 expects {id, text}
    items = [{"id": f["id"], "text": f["name"]} for f in page_items]
    return jsonify({"items": items, "more": more})

# ---- Compute compatibility ----
@app.route("/compute", methods=["GET", "POST"])
@login_required
def compute():
    fishes = load_fish_data()

    if request.method == "POST":
        selected_ids = request.form.getlist("fish_ids[]")
        selected_map = {f["id"]: f for f in fishes}
        selected = [selected_map[i] for i in selected_ids if i in selected_map]

        # Attach readable compatibility names for display
        for f in selected:
            comp_names = []
            for cid in f.get("compatibility", []):
                other = selected_map.get(cid)
                if other:
                    comp_names.append(other["name"])
            f["compatibility_names"] = comp_names

        return render_template("result.html", selected_fish=selected)

    # GET -> render page (Select2 will call /fish_data)
    return render_template("compute.html")

# ---- Ask AI (GET) ----
@app.route("/ask")
@login_required
def ask():
    question = (request.args.get("question") or "").strip()
    if not question:
        return jsonify({"answer": "Please type a question."})

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        # Fallback: tiny rule-based answer so UI works without a key
        fallback = "I'm ready! Add GEMINI_API_KEY to get AI answers, but meanwhile: "
        if "betta" in question.lower() and "gold" in question.lower():
            return jsonify({"answer": fallback + "Generally, bettas and goldfish are not ideal tankmates due to temp and behavior differences."})
        return jsonify({"answer": fallback + "Ask about tank mates, water params, diet, or care tips."})

    # Call Gemini (simple text)
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


# ---- Simple health route ----
@app.route("/healthz")
def healthz():
    return "ok"
