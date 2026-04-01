from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from extensions import db, bcrypt
from models import User, StationMaster

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'super_admin':
            return redirect(url_for('admin.super_dashboard'))
        if current_user.role == 'admin':
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    return render_template('index.html')

@auth_bp.route('/create_admin')
def create_admin():
    name = "Admin"
    email = "admin@gmail.com"
    phone = "9999999999"
    password = "admin123"

    hashed = bcrypt.generate_password_hash(password).decode('utf-8')

    admin = StationMaster(
        name=name,
        email=email,
        phone=phone,
        password_hash=hashed,
        station_id=1
    )

    db.session.add(admin)
    db.session.commit()

    return "Admin created"


# existing login function continues below


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    if request.method == 'POST':
        role = request.form.get('role', 'user')
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if role == 'admin':
            master = StationMaster.query.filter_by(email=email).first()
            try:
                pw_ok = master and bcrypt.check_password_hash(master.password_hash, password)
            except ValueError:
                pw_ok = False
                flash('Admin account has a corrupted password hash. Please contact the system administrator.', 'danger')
            if pw_ok:
                login_user(master)
                flash('Welcome back, Station Master!', 'success')
                if master.role == 'super_admin':
                    return redirect(url_for('admin.super_dashboard'))
                return redirect(url_for('admin.dashboard'))
            elif not pw_ok and master:
                pass  # flash already handled
            else:
                flash('Invalid admin credentials.', 'danger')
        else:
            user = User.query.filter_by(email=email).first()
            if user and bcrypt.check_password_hash(user.password_hash, password):
                login_user(user)
                flash(f'Welcome back, {user.name}!', 'success')
                return redirect(url_for('user.dashboard'))
            flash('Invalid email or password.', 'danger')

    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please login.', 'warning')
            return redirect(url_for('auth.login'))

        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(name=name, email=email, phone=phone, password_hash=hashed)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash('Account created! Welcome to RailSync.', 'success')
        return redirect(url_for('user.dashboard'))

    return render_template('register.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
