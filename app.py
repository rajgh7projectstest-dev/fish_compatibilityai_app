import os, json, math
from urllib.parse import urljoin, urlparse

from flask import Flask, render_template, redirect, url_for, session, request, abort, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, current_user, logout_user
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
import google.generativeai as genai   # ✅ Gemini AI SDK

# Load environment variables
load_dotenv()

# Flask setup
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['APP_NAME'] = os.getenv('APP_NAME', 'Fish Compatibility System')

# Database
db = SQLAlchemy(app)

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    flash("⚠️ Please login with Google to use this tool.")
    return redirect(url_for("login_google", next=request.endpoint))

# OAuth (Google)
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Gemini API config
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# User Model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sub = db.Column(db.String(255), unique=True, nullable=False)  # Google sub
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    avatar = db.Column(db.String(500))

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Helpers
def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login/google')
def login_google():
    redirect_uri = url_for('auth_google', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/auth/google')
def auth_google():
    token = oauth.google.authorize_access_token()
    userinfo = token.get('userinfo') or oauth.google.parse_id_token(token)
    if not userinfo:
        abort(400, 'Google auth failed')
    sub = userinfo['sub']
    email = userinfo.get('email', '')
    name = userinfo.get('name', 'User')
    picture = userinfo.get('picture')

    user = User.query.filter_by(sub=sub).first()
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.sub = sub
            user.name = name
            user.avatar = picture
        else:
            user = User(sub=sub, email=email, name=name, avatar=picture)
            db.session.add(user)
        db.session.commit()

    login_user(user, remember=True)
    next_url = request.args.get('next')
    if next_url and is_safe_url(next_url):
        return redirect(next_url)
    return redirect(url_for('tool'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

def load_fish():
    path = os.path.join(app.root_path, 'static', 'fish_data.json')
    with open(path, 'r') as f:
        return json.load(f)

@app.route('/tool')
@login_required
def tool():
    fish = load_fish()
    return render_template('tool.html', fish=fish)

def compatibility_score(picks, tank_liters, temp_c, ph, tank_type, fish_data):
    issues = []
    suggestions = []
    score = 100

    # Tank size rule
    total_size = sum(f['size_cm'] for f in picks)
    if total_size > tank_liters:
        over = total_size - tank_liters
        penalty = min(40, over * 0.5)
        score -= penalty
        issues.append(f"Stocking too high: {total_size:.1f} cm fish size > {tank_liters} L tank.")
        suggestions.append("Reduce stocking or increase tank size.")

    # Parameter mismatches
    for f in picks:
        if not (f['temp_min'] <= temp_c <= f['temp_max']):
            score -= 8
            issues.append(f"{f['common_name']} prefers {f['temp_min']}–{f['temp_max']}°C; your tank is {temp_c}°C.")
        if not (f['ph_min'] <= ph <= f['ph_max']):
            score -= 8
            issues.append(f"{f['common_name']} prefers pH {f['ph_min']}-{f['ph_max']}; your tank is {ph}.")
        if tank_liters < f['min_tank_l']:
            score -= 6
            issues.append(f"{f['common_name']} needs ≥ {f['min_tank_l']} L; your tank is {tank_liters} L.")

    # Temperament conflicts
    temperaments = [f['temperament'] for f in picks]
    if 'aggressive' in temperaments and any(t == 'peaceful' for t in temperaments):
        score -= 25
        issues.append("Aggressive and peaceful species mixed—risk of bullying.")
        suggestions.append("Avoid combining aggressive with peaceful species.")

    if 'semi-aggressive' in temperaments and any(t == 'peaceful' for t in temperaments):
        score -= 10
        issues.append("Semi-aggressive with peaceful species may cause stress.")
        suggestions.append("Add hiding spaces or separate groups.")

    # Special cases
    ids = {f['id'] for f in picks}
    if 'betta' in ids and any(f['shoaling'] for f in picks if f['id'] != 'betta'):
        score -= 10
        issues.append("Betta may harass small shoaling fish.")
    if 'angelfish' in ids and 'neon_tetra' in ids:
        score -= 10
        issues.append("Angelfish may prey on small tetras.")

    # Tank type
    if tank_type == 'species-only' and len(picks) > 1:
        score -= 10
        issues.append("Species-only tank should contain only one species.")
    if tank_type == 'community' and any(t in ('aggressive',) for t in temperaments):
        score -= 10
        issues.append("Aggressive species not recommended in a community tank.")

    # Shoaling
    for f in picks:
        if f.get('shoaling'):
            suggestions.append(f"Keep {f['common_name']} in groups of 6+ for comfort.")

    score = int(max(0, min(100, score)))
    suggestions = list(dict.fromkeys(suggestions))
    return score, issues, suggestions

@app.route('/compute', methods=['POST'])
@login_required
def compute():
    fish_all = load_fish()
    fish_map = {f['id']: f for f in fish_all}
    selected_ids = request.form.getlist('species')[:8]
    picks = [fish_map[i] for i in selected_ids if i in fish_map]

    try:
        tank_liters = float(request.form.get('tank_liters', '60'))
        temp_c = float(request.form.get('temp_c', '26'))
        ph = float(request.form.get('ph', '7.0'))
    except ValueError:
        tank_liters, temp_c, ph = 60, 26, 7.0

    tank_type = request.form.get('tank_type', 'community')
    score, issues, suggestions = compatibility_score(picks, tank_liters, temp_c, ph, tank_type, fish_all)

    return render_template('_results.html', picks=picks, score=score, issues=issues, suggestions=suggestions)

# ✅ New Gemini AI endpoint
@app.route('/ask', methods=['POST'])
@login_required
def ask_ai():
    data = request.json
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"answer": "Please ask a valid question."}), 400

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(question)
        ai_answer = response.text if response else "No response from AI."
        return jsonify({"answer": ai_answer})
    except Exception as e:
        return jsonify({"answer": f"⚠️ Error: {str(e)}"}), 500

# Init DB
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', 8000)),
        debug=True
    )
