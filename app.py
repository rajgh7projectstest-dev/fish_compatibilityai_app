import os, json
from urllib.parse import urljoin, urlparse
from flask import Flask, render_template, redirect, url_for, request, abort, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

# Load env vars
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login_google"

# OAuth (Google)
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sub = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    avatar = db.Column(db.String(500))

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Safety check
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

# Load fish data
def load_fish():
    path = os.path.join(app.root_path, 'static', 'fish_data.json')
    with open(path, 'r') as f:
        return json.load(f)

@app.route('/tool')
@login_required
def tool():
    fish = load_fish()
    return render_template('tool.html', fish=fish)

# DB init
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)), debug=True)
