from flask import Blueprint, render_template, redirect, url_for, flash, request, session, make_response
from flask_login import login_user, logout_user, login_required, current_user
from extensions import db, bcrypt
from models import User, StationMaster, TrainDriver

auth_bp = Blueprint('auth', __name__)


@auth_bp.after_request
def no_cache(response):
    """Prevent ALL auth pages from being cached by the browser.
    This stops the back-button from showing stale login/OTP pages."""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'super_admin':
            return redirect(url_for('admin.super_dashboard'))
        if current_user.role == 'admin':
            return redirect(url_for('admin.dashboard'))
        if getattr(current_user, 'role', None) == 'driver':
            return redirect(url_for('driver.dashboard'))
        return redirect(url_for('user.dashboard'))
    return render_template('index.html')




@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user)
            flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('user.dashboard'))
        flash('Invalid email or password.', 'danger')

    return render_template('login.html')


@auth_bp.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        selected_role = request.form.get('role', 'admin')

        if selected_role == 'driver':
            driver = TrainDriver.query.filter_by(email=email).first()
            try:
                pw_ok = driver and bcrypt.check_password_hash(driver.password_hash, password)
            except ValueError:
                pw_ok = False
                flash('Driver account has a corrupted password hash. Please contact the system administrator.', 'danger')
            
            if pw_ok:
                login_user(driver)
                flash(f'Welcome back, {driver.name}!', 'success')
                return redirect(url_for('driver.dashboard'))
            else:
                flash('Invalid driver credentials.', 'danger')
        else:
            master = StationMaster.query.filter_by(email=email).first()

            try:
                pw_ok = master and bcrypt.check_password_hash(master.password_hash, password)
            except ValueError:
                pw_ok = False
                flash('Admin account has a corrupted password hash. Please contact the system administrator.', 'danger')

            if pw_ok:
                if selected_role == 'super_admin' and not master.is_super_admin:
                    flash('You do not have Super Admin privileges.', 'danger')
                    return redirect(url_for('auth.admin_login', role='super_admin'))

                login_user(master)
                flash('Welcome back, Station Master!', 'success')
                if master.role == 'super_admin':
                    return redirect(url_for('admin.super_dashboard'))
                return redirect(url_for('admin.dashboard'))
            else:
                flash('Invalid admin credentials.', 'danger')

    return render_template('master_login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        phone    = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if not name or not email or not phone or not password:
            flash('All fields are required.', 'danger')
            return render_template('register.html')

        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please sign in.', 'warning')
            return redirect(url_for('auth.login'))

        from services.email_service import generate_otp, send_otp_email
        import time

        otp = generate_otp()
        session['pending_registration'] = {
            'name':          name,
            'email':         email,
            'phone':         phone,
            'password_hash': bcrypt.generate_password_hash(password).decode('utf-8'),
            'otp':           otp,
            'otp_expires':   time.time() + 600,   # 10 minutes
            'attempts':      0,
        }

        sent = send_otp_email(email, name, otp)
        if not sent:
            flash('Could not send verification email. Please try again.', 'danger')
            return render_template('register.html')

        flash(f'A 6-digit verification code has been sent to {email}.', 'info')
        return redirect(url_for('auth.verify_otp'))

    return render_template('register.html')


@auth_bp.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    """OTP confirmation step — activated after registration form submission."""
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    pending = session.get('pending_registration')
    if not pending:
        flash('Session expired. Please register again.', 'warning')
        return redirect(url_for('auth.register'))

    import time

    if request.method == 'POST':
        action = request.form.get('action', 'verify')

        # --- Resend OTP ---
        if action == 'resend':
            from services.email_service import generate_otp, send_otp_email
            new_otp = generate_otp()
            pending['otp']         = new_otp
            pending['otp_expires'] = time.time() + 600
            pending['attempts']    = 0
            session['pending_registration'] = pending
            send_otp_email(pending['email'], pending['name'], new_otp)
            flash('A new verification code has been sent.', 'info')
            return redirect(url_for('auth.verify_otp'))

        # --- Verify OTP ---
        entered = request.form.get('otp', '').strip()

        if time.time() > pending.get('otp_expires', 0):
            session.pop('pending_registration', None)
            flash('Your OTP has expired. Please register again.', 'danger')
            return redirect(url_for('auth.register'))

        if pending.get('attempts', 0) >= 5:
            session.pop('pending_registration', None)
            flash('Too many incorrect attempts. Please register again.', 'danger')
            return redirect(url_for('auth.register'))

        if entered == pending['otp']:
            user = User(
                name=pending['name'],
                email=pending['email'],
                phone=pending['phone'],
                password_hash=pending['password_hash']
            )
            db.session.add(user)
            db.session.commit()
            session.pop('pending_registration', None)
            login_user(user)
            flash(f'Welcome aboard, {user.name}! Your account is verified. 🎉', 'success')
            return redirect(url_for('user.dashboard'))
        else:
            pending['attempts'] = pending.get('attempts', 0) + 1
            session['pending_registration'] = pending
            remaining = 5 - pending['attempts']
            flash(f'Incorrect code. {remaining} attempt(s) remaining.', 'danger')

    # Mask email for display  e.g.  rud***@gmail.com
    parts = pending['email'].split('@')
    if len(parts[0]) > 3:
        masked_email = parts[0][:3] + '•••@' + parts[1]
    else:
        masked_email = pending['email']

    return render_template('verify_otp.html', masked_email=masked_email)


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Step 1 — user enters email, OTP is sent."""
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    if request.method == 'POST':
        action = request.form.get('action', 'send')

        # ── Step 1: send OTP ──────────────────────────────────────
        if action == 'send':
            email = request.form.get('email', '').strip().lower()
            user = User.query.filter_by(email=email).first()
            if not user:
                flash('No account found with that email address.', 'danger')
                return render_template('forgot_password.html', step='email')

            from services.email_service import generate_otp, send_otp_email
            import time
            otp = generate_otp()
            session['forgot_pw'] = {
                'email':       email,
                'otp':         otp,
                'otp_expires': time.time() + 600,
                'attempts':    0,
                'verified':    False,
            }
            send_otp_email(email, user.name, otp)
            flash(f'A verification code was sent to {email}.', 'info')
            return render_template('forgot_password.html', step='otp',
                                   masked_email=email[:3] + '•••@' + email.split('@')[1])

        # ── Step 2: verify OTP ────────────────────────────────────
        if action == 'verify':
            import time
            pending = session.get('forgot_pw')
            if not pending:
                flash('Session expired. Please start again.', 'warning')
                return redirect(url_for('auth.forgot_password'))

            entered = request.form.get('otp', '').strip()

            if time.time() > pending['otp_expires']:
                session.pop('forgot_pw', None)
                flash('OTP expired. Please try again.', 'danger')
                return redirect(url_for('auth.forgot_password'))

            if pending.get('attempts', 0) >= 5:
                session.pop('forgot_pw', None)
                flash('Too many incorrect attempts. Please start again.', 'danger')
                return redirect(url_for('auth.forgot_password'))

            if entered != pending['otp']:
                pending['attempts'] = pending.get('attempts', 0) + 1
                session['forgot_pw'] = pending
                remaining = 5 - pending['attempts']
                flash(f'Incorrect code. {remaining} attempt(s) remaining.', 'danger')
                masked = pending['email'][:3] + '•••@' + pending['email'].split('@')[1]
                return render_template('forgot_password.html', step='otp', masked_email=masked)

            pending['verified'] = True
            session['forgot_pw'] = pending
            return render_template('forgot_password.html', step='reset')

        # ── Step 3: set new password ──────────────────────────────
        if action == 'reset':
            pending = session.get('forgot_pw')
            if not pending or not pending.get('verified'):
                flash('Session expired. Please start again.', 'warning')
                return redirect(url_for('auth.forgot_password'))

            new_pw  = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')

            if len(new_pw) < 8:
                flash('Password must be at least 8 characters.', 'danger')
                return render_template('forgot_password.html', step='reset')
            if new_pw != confirm:
                flash('Passwords do not match.', 'danger')
                return render_template('forgot_password.html', step='reset')

            user = User.query.filter_by(email=pending['email']).first()
            if user:
                user.password_hash = bcrypt.generate_password_hash(new_pw).decode('utf-8')
                db.session.commit()

            session.pop('forgot_pw', None)
            flash('Password reset successfully. Please sign in.', 'success')
            return redirect(url_for('auth.login'))

    return render_template('forgot_password.html', step='email')


@auth_bp.route('/logout')
@login_required
def logout():
    role = getattr(current_user, 'role', 'user')
    logout_user()
    flash('You have been logged out.', 'info')
    
    if role == 'driver':
        return redirect(url_for('auth.admin_login', role='driver'))
    elif role in ['admin', 'super_admin']:
        return redirect(url_for('auth.admin_login'))
        
    return redirect(url_for('auth.login'))
