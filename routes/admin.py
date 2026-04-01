from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from functools import wraps
from extensions import db
from flask import abort
from extensions import socketio
from models import (Train, TrainStatus, TrainRoute, Station, StationMaster,
                    Platform, PlatformAllocation, Notification, Booking, User, Payment,
                    PassengerChangeRequest, ChatMessage, ChatRecipient, MasterNotification)
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import anthropic
import json
import os

admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'super_admin']:
            flash('Admin access required.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'super_admin':
            flash('Super Admin access required.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def get_ai_client():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise Exception("Missing ANTHROPIC_API_KEY environment variable")
    return anthropic.Anthropic(api_key=api_key)

def ai_reallocate_platform(delayed_train_id, delay_minutes, station_id, eta_fixed):
    """
    Ask Claude to suggest the best platform reallocation strategy.
    Returns a dict with suggested platform and reasoning.
    """
    client = get_ai_client()

    delayed_train = Train.query.get(delayed_train_id)
    station = Station.query.get(station_id)
    platforms = Platform.query.filter_by(station_id=station_id).all()
    current_alloc = PlatformAllocation.query.filter_by(
        train_id=delayed_train_id, station_id=station_id
    ).first()

    # Get all current allocations at this station
    all_allocs = PlatformAllocation.query.filter_by(station_id=station_id).all()
    alloc_data = []
    for a in all_allocs:
        t = Train.query.get(a.train_id)
        alloc_data.append({
            'train_id': a.train_id,
            'train_name': t.train_name if t else 'Unknown',
            'platform_id': a.platform_id,
            'arrival': a.arrival_time.strftime('%H:%M'),
            'departure': a.departure_time.strftime('%H:%M')
        })

    platform_data = [{'id': p.platform_id, 'number': p.platform_number, 'available': p.is_available} for p in platforms]

    prompt = f"""You are an intelligent railway platform allocation assistant.

Station: {station.station_name}, {station.city}
Delayed Train: {delayed_train.train_number} - {delayed_train.train_name}
Current Platform: {current_alloc.platform.platform_number if current_alloc else 'Not allocated'}
Delay: {delay_minutes} minutes
Estimated time to fix: {eta_fixed} minutes

Available Platforms at station:
{json.dumps(platform_data, indent=2)}

Current platform allocations today:
{json.dumps(alloc_data, indent=2)}

Please analyze the situation and suggest:
1. Which platform the delayed train should be temporarily moved to (if any)
2. Which other trains (if any) need platform changes to avoid conflicts
3. Clear passenger announcement text

Respond in JSON format:
{{
  "action": "reassign" or "keep",
  "suggested_platform_id": <platform_id or null>,
  "affected_trains": [<train_ids that need to move>],
  "reasoning": "<brief explanation>",
  "announcement": "<passenger-facing announcement text>"
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text
        # Strip markdown fences if present
        raw = raw.strip().lstrip('```json').rstrip('```').strip()
        try:
            return json.loads(raw)
        except Exception:
            return {
                "action": "keep",
                "suggested_platform_id": None,
                "affected_trains": [],
                "reasoning": "AI returned invalid response",
                "announcement": f"Train delayed by {delay_minutes} minutes."
            }
    except Exception as e:
        return {
            "action": "keep",
            "suggested_platform_id": None,
            "affected_trains": [],
            "reasoning": f"AI unavailable: {str(e)}",
            "announcement": f"Train {delayed_train.train_number} is delayed by {delay_minutes} minutes. We apologize for the inconvenience."
        }


def notify_affected_passengers(train_id, message):
    """Create notifications for all passengers booked on this train."""
    bookings = Booking.query.filter_by(train_id=train_id)\
        .filter(Booking.status != 'CANCELLED').all()
    for b in bookings:
        n = Notification(user_id=b.user_id, train_id=train_id, message=message)
        db.session.add(n)
    db.session.commit()
    # Emit socket event for real-time notifications
    socketio.emit('new_notification', {
        'train_id': train_id,
        'message': message
    }, room=f'train_{train_id}')


@admin_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    station = current_user.station
    trains_at_station = PlatformAllocation.query.filter_by(station_id=station.station_id).all()
    platforms = Platform.query.filter_by(station_id=station.station_id).all()
    delayed = TrainStatus.query.filter(TrainStatus.delay_minutes > 0).all()
    return render_template(
            'admin/dashboard.html',
            station=station,
            trains_at_station=trains_at_station,
            platforms=platforms,
            delayed=delayed,
            now=datetime.utcnow())


@admin_bp.route('/trains')
@login_required
@admin_required
def trains():
    station = current_user.station

    # SUPER ADMIN → see all
    if current_user.role == 'super_admin':
        all_trains = Train.query.all()
    else:
        # NORMAL ADMIN → only their station trains
        all_trains = Train.query.join(TrainRoute)\
            .filter(TrainRoute.station_id == station.station_id)\
            .distinct().all()

    return render_template('admin/trains.html', trains=all_trains)


@admin_bp.route('/trains/add', methods=['GET', 'POST'])
@login_required
@super_admin_required
def add_train():
    stations_obj = Station.query.order_by(Station.station_name).all()
    # Serialize to plain dicts so tojson works in the template
    stations = [{'station_id': s.station_id, 'station_name': s.station_name, 'city': s.city} for s in stations_obj]
    if request.method == 'POST':
        train_number = request.form['train_number'].strip()
        train_name = request.form['train_name'].strip()

        # ── Duplicate validation ──────────────────────────────────────────
        if Train.query.filter_by(train_number=train_number).first():
            flash(f'Train number "{train_number}" already exists. Please use a unique train number.', 'danger')
            return render_template('admin/add_train.html', stations=stations)
        if Train.query.filter(db.func.lower(Train.train_name) == train_name.lower()).first():
            flash(f'Train name "{train_name}" already exists. Please use a unique train name.', 'danger')
            return render_template('admin/add_train.html', stations=stations)
        # ─────────────────────────────────────────────────────────────────

        train = Train(
            train_number=train_number,
            train_name=train_name,
            total_seats=int(request.form['total_seats'])
        )
        db.session.add(train)
        db.session.flush()

        # Add route stops
        stop_stations = request.form.getlist('stop_station_id')
        arrivals = request.form.getlist('arrival_time')
        departures = request.form.getlist('departure_time')
        for i, (st, arr, dep) in enumerate(zip(stop_stations, arrivals, departures)):
            route = TrainRoute(
                train_id=train.train_id,
                station_id=int(st),
                arrival_time=datetime.strptime(arr, '%H:%M').time() if arr else None,
                departure_time=datetime.strptime(dep, '%H:%M').time() if dep else None,
                stop_number=i + 1
            )
            db.session.add(route)

        db.session.commit()
        flash(f'Train {train.train_number} added successfully.', 'success')
        return redirect(url_for('admin.trains'))

    return render_template('admin/add_train.html', stations=stations)


@admin_bp.route('/trains/remove/<int:train_id>', methods=['POST'])
@login_required
@super_admin_required
def remove_train(train_id):
    train = Train.query.get_or_404(train_id)

    if current_user.role != 'super_admin':
        station = current_user.station
    
        allowed = TrainRoute.query.filter_by(
            train_id=train_id,
            station_id=station.station_id
        ).first()
    
        if not allowed:
            flash('You are not allowed to delete this train.', 'danger')
            return redirect(url_for('admin.trains'))

    Train.query.filter_by(train_id=train_id).delete()
    db.session.commit()

    flash('Train removed successfully.', 'success')
    return redirect(url_for('admin.trains'))


@admin_bp.route('/delay', methods=['GET', 'POST'])
@login_required
@admin_required
def report_delay():
    station = current_user.station

    if current_user.role == 'super_admin':
        trains = Train.query.all()
    else:
        trains = Train.query.join(TrainRoute)\
        .filter(TrainRoute.station_id == station.station_id)\
        .distinct().all()
    ai_result = None

    if request.method == 'POST':
        train_id = request.form.get('train_id', type=int)
        delay_minutes = request.form.get('delay_minutes', type=int)
        eta_fixed = request.form.get('eta_fixed', type=int) or 60
        problem_description = request.form.get('problem_description', '').strip()

        delayed_train = Train.query.get(train_id)

        # Update train status
        status = TrainStatus.query.filter_by(train_id=train_id).first()
        if not status:
            status = TrainStatus(
                train_id=train_id,
                current_station_id=station.station_id,
                delay_minutes=delay_minutes
            )
            db.session.add(status)
        else:
            status.delay_minutes = delay_minutes
            status.last_updated = datetime.utcnow()
        db.session.commit()

        # Ask AI for platform reallocation
        ai_result = ai_reallocate_platform(train_id, delay_minutes, station.station_id, eta_fixed)

        # Apply AI suggestion
        if ai_result.get('action') == 'reassign' and ai_result.get('suggested_platform_id'):
            alloc = PlatformAllocation.query.filter_by(
                train_id=train_id, station_id=station.station_id
            ).first()
            if alloc:
                alloc.platform_id = ai_result['suggested_platform_id']
                db.session.commit()

        # Notify all passengers
        announce = ai_result.get('announcement', f'Train delayed by {delay_minutes} minutes.')
        notify_affected_passengers(train_id, announce)

        # ── Notify all Super Admins about the delay ──────────────────
        super_admins = StationMaster.query.filter_by(is_super_admin=True).all()
        train_info = f"{delayed_train.train_number} {delayed_train.train_name}" if delayed_train else f"#{train_id}"
        notif_subject = f"Delay Alert: {train_info} at {station.station_name}"
        notif_body = (
            f"Station: {station.station_name}, {station.city}\n"
            f"Train: {train_info}\n"
            f"Delay: {delay_minutes} minutes\n"
            f"ETA to fix: {eta_fixed} minutes\n"
            f"Problem: {problem_description or 'Not specified'}\n"
            f"Reported by: {current_user.name} at {datetime.utcnow().strftime('%H:%M UTC')}"
        )
        for sa in super_admins:
            mn = MasterNotification(
                recipient_id=sa.master_id,
                sender_id=current_user.master_id,
                subject=notif_subject,
                body=notif_body,
                notif_type='delay_alert'
            )
            db.session.add(mn)
        db.session.commit()
        # Real-time push to super admin socket room
        socketio.emit('master_notification', {
            'subject': notif_subject,
            'body': notif_body,
            'type': 'delay_alert'
        }, room='super_admins')

        flash('Delay reported. AI has suggested platform changes, passengers notified, and Super Admin alerted.', 'success')

    return render_template('admin/delay.html', trains=trains, ai_result=ai_result, station=station)


@admin_bp.route('/platforms')
@login_required
@admin_required
def platforms():
    station = current_user.station
    platforms = Platform.query.filter_by(station_id=station.station_id).all()
    allocations = PlatformAllocation.query.filter_by(station_id=station.station_id).all()
    return render_template(
            'admin/platforms.html',
            platforms=platforms,
            allocations=allocations,
            trains=Train.query.all())


@admin_bp.route('/platforms/add', methods=['POST'])
@login_required
@admin_required
def add_platform():
    station = current_user.station
    number = request.form.get('platform_number', '').strip()
    p = Platform(station_id=station.station_id, platform_number=number)
    db.session.add(p)
    db.session.commit()
    flash(f'Platform {number} added.', 'success')
    return redirect(url_for('admin.platforms'))


@admin_bp.route('/allocate', methods=['POST'])
@login_required
@admin_required
def allocate_platform():
    station = current_user.station

    train_id = request.form.get('train_id', type=int)
    platform_id = request.form.get('platform_id', type=int)

    arrival = datetime.strptime(request.form['arrival_time'], '%Y-%m-%dT%H:%M')
    departure = datetime.strptime(request.form['departure_time'], '%Y-%m-%dT%H:%M')

    # 🚨 CHECK FOR CONFLICTS
    conflict = PlatformAllocation.query.filter(
        PlatformAllocation.platform_id == platform_id,
        PlatformAllocation.station_id == station.station_id,
        PlatformAllocation.arrival_time < departure,
        PlatformAllocation.departure_time > arrival
        ).first()

    if conflict:
        # 🔍 Find another free platform
        free_platforms = Platform.query.filter(
            Platform.station_id == station.station_id,
            Platform.platform_id != platform_id
        ).all()

        best_platform = None
        min_gap = None

        for p in free_platforms:
            allocations = PlatformAllocation.query.filter(
                PlatformAllocation.platform_id == p.platform_id,
                PlatformAllocation.station_id == station.station_id
                ).all()

            conflict_found = False
            for a in allocations:
                if arrival < a.departure_time and departure > a.arrival_time:
                    conflict_found = True
                    break

            if not conflict_found:
                gap = 0
                for a in allocations:
                    gap += abs((arrival - a.departure_time).total_seconds())

                if min_gap is None or gap < min_gap:
                    min_gap = gap
                    best_platform = p

        if best_platform:
            alloc = PlatformAllocation(
                train_id=train_id,
                station_id=station.station_id,
                platform_id=best_platform.platform_id,
                arrival_time=arrival,
                departure_time=departure
            )

            db.session.add(alloc)
            db.session.commit()

            flash(
                f'⚠️ Conflict detected! Auto-assigned to Platform {best_platform.platform_number}',
                'warning'
            )
            return redirect(url_for('admin.platforms'))
        else:
            flash('❌ No free platforms available at this time!', 'danger')
            return redirect(url_for('admin.platforms'))

    # ✅ NO CONFLICT → NORMAL ASSIGN
    alloc = PlatformAllocation(
        train_id=train_id,
        station_id=station.station_id,
        platform_id=platform_id,
        arrival_time=arrival,
        departure_time=departure
    )

    db.session.add(alloc)
    db.session.commit()

    flash('Platform allocated successfully.', 'success')
    return redirect(url_for('admin.platforms'))


# ──────────────────────────────────────────────────────────────────
# API: Check train number / name duplicates (real-time)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/api/check-train-duplicate')
@login_required
@admin_required
def check_train_duplicate():
    type_ = request.args.get('type', '')
    value = request.args.get('value', '').strip()
    if not value:
        return jsonify({'exists': False, 'message': ''})
    if type_ == 'num':
        exists = Train.query.filter_by(train_number=value).first() is not None
        return jsonify({'exists': exists, 'message': f'Train number "{value}" is already taken.' if exists else ''})
    elif type_ == 'name':
        exists = Train.query.filter(db.func.lower(Train.train_name) == value.lower()).first() is not None
        return jsonify({'exists': exists, 'message': f'Train name "{value}" is already taken.' if exists else ''})
    return jsonify({'exists': False, 'message': ''})


# ──────────────────────────────────────────────────────────────────
# API: Validate a stop name against the station database
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/api/validate-stop')
@login_required
@admin_required
def validate_stop():
    """Check if a station name or city matches any station in the DB.
    Returns JSON: {valid: bool, station: {id, name, city} or None}"""
    query = request.args.get('q', '').strip().lower()
    if not query:
        return jsonify({'valid': False, 'station': None, 'message': 'Empty query'})
    # Try exact match on station_name first, then city
    match = Station.query.filter(
        db.or_(
            db.func.lower(Station.station_name) == query,
            db.func.lower(Station.city) == query
        )
    ).first()
    if not match:
        # Try partial / LIKE match
        match = Station.query.filter(
            db.or_(
                db.func.lower(Station.station_name).contains(query),
                db.func.lower(Station.city).contains(query)
            )
        ).first()
    if match:
        return jsonify({
            'valid': True,
            'station': {
                'id': match.station_id,
                'name': match.station_name,
                'city': match.city,
                'state': match.state
            },
            'message': 'Station found'
        })
    return jsonify({'valid': False, 'station': None, 'message': 'No station found matching that name or city'})


# ──────────────────────────────────────────────────────────────────
# Edit Train Stops (Super Admin)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/super/trains/edit-stops/<int:train_id>', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit_train_stops(train_id):
    train = Train.query.get_or_404(train_id)
    stations_obj = Station.query.order_by(Station.station_name).all()
    stations = [{'station_id': s.station_id, 'station_name': s.station_name, 'city': s.city, 'state': s.state} for s in stations_obj]

    if request.method == 'POST':
        stop_station_ids = request.form.getlist('stop_station_id')
        arrivals = request.form.getlist('arrival_time')
        departures = request.form.getlist('departure_time')

        if not stop_station_ids:
            flash('At least one stop is required.', 'danger')
            return redirect(url_for('admin.edit_train_stops', train_id=train_id))

        # Delete old routes and re-create
        TrainRoute.query.filter_by(train_id=train_id).delete()
        db.session.flush()

        for i, (st, arr, dep) in enumerate(zip(stop_station_ids, arrivals, departures)):
            route = TrainRoute(
                train_id=train_id,
                station_id=int(st),
                arrival_time=datetime.strptime(arr, '%H:%M').time() if arr else None,
                departure_time=datetime.strptime(dep, '%H:%M').time() if dep else None,
                stop_number=i + 1
            )
            db.session.add(route)

        db.session.commit()
        flash(f'Stops for train {train.train_number} updated successfully.', 'success')
        return redirect(url_for('admin.trains'))

    # Load existing stops in order
    existing_stops = TrainRoute.query.filter_by(train_id=train_id).order_by(TrainRoute.stop_number).all()
    return render_template('admin/super/edit_train_stops.html',
                           train=train,
                           stations=stations,
                           existing_stops=existing_stops)


# ──────────────────────────────────────────────────────────────────
# Add Temp Station Master (Super Admin shortcut)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/super/masters/add-temp', methods=['GET', 'POST'])
@login_required
@super_admin_required
def add_temp_master():
    pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
    from extensions import bcrypt

    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')

        if StationMaster.query.filter_by(email=email).first() or User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('admin.add_temp_master'))

        if not pool:
            flash('Temporary Pool station not found. Please create a station named "Unassigned / Temporary Pool" first.', 'danger')
            return redirect(url_for('admin.add_temp_master'))

        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        master = StationMaster(
            name=name,
            email=email,
            phone=phone,
            password_hash=hashed,
            station_id=pool.station_id,
            is_super_admin=False
        )
        db.session.add(master)
        db.session.commit()
        flash(f'Temporary Station Master "{name}" added to the Temporary Pool successfully.', 'success')
        return redirect(url_for('admin.super_masters'))

    return render_template('admin/super/add_temp_master.html', pool=pool)


@admin_bp.route('/super/dashboard')
@login_required
@super_admin_required
def super_dashboard():
    total_trains = Train.query.count()
    pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
    total_stations = Station.query.filter(Station.station_id != pool.station_id).count() if pool else Station.query.count()
    total_passengers = User.query.count()
    
    total_masters = StationMaster.query.count()
    # Active = not on leave AND not in temp pool AND not super admin
    pool_id = pool.station_id if pool else None
    active_masters = StationMaster.query.filter(
        StationMaster.is_on_leave == False,
        StationMaster.is_super_admin == False,
        StationMaster.station_id != pool_id
    ).count() if pool_id else StationMaster.query.filter_by(is_on_leave=False, is_super_admin=False).count()
    temp_masters = StationMaster.query.filter(
        StationMaster.station_id == pool_id,
        StationMaster.is_super_admin == False
    ).count() if pool_id else 0
    leave_masters = StationMaster.query.filter_by(is_on_leave=True).count()
    unassigned_masters = temp_masters
    
    total_bookings = Booking.query.count()
    delayed_trains = TrainStatus.query.filter(TrainStatus.delay_minutes > 0).all()
    
    payments = Payment.query.filter_by(payment_status='SUCCESS').all()
    total_revenue = sum(p.amount for p in payments) if payments else 0
    
    recent_bookings = Booking.query.order_by(Booking.booking_date.desc()).limit(10).all()
    
    return render_template('admin/super/dashboard.html', 
                           total_trains=total_trains, 
                           total_stations=total_stations, 
                           total_passengers=total_passengers,
                           total_bookings=total_bookings,
                           delayed_trains=delayed_trains,
                           total_revenue=total_revenue,
                           recent_bookings=recent_bookings,
                           total_masters=total_masters,
                           active_masters=active_masters,
                           temp_masters=temp_masters,
                           leave_masters=leave_masters,
                           unassigned_masters=unassigned_masters,
                           now=datetime.utcnow())

@admin_bp.route('/super/masters')
@login_required
@super_admin_required
def super_masters():
    pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
    pool_id = pool.station_id if pool else None
    # Regular station masters (assigned to a real station, not temp pool, not super admin)
    regular_masters = StationMaster.query.filter(
        StationMaster.is_super_admin == False,
        StationMaster.station_id != pool_id
    ).order_by(StationMaster.name).all() if pool_id else StationMaster.query.filter_by(is_super_admin=False).order_by(StationMaster.name).all()
    # Temporary (temp pool) masters
    temp_masters = StationMaster.query.filter(
        StationMaster.station_id == pool_id,
        StationMaster.is_super_admin == False
    ).order_by(StationMaster.name).all() if pool_id else []
    # Super admins
    super_admins = StationMaster.query.filter_by(is_super_admin=True).order_by(StationMaster.name).all()
    return render_template('admin/super/masters.html',
                           masters=StationMaster.query.all(),
                           regular_masters=regular_masters,
                           temp_masters=temp_masters,
                           super_admins=super_admins)

@admin_bp.route('/super/masters/add', methods=['GET', 'POST'])
@login_required
@super_admin_required
def add_master():
    stations = Station.query.order_by(Station.station_name).all()
    from extensions import bcrypt
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        station_id = request.form.get('station_id', type=int)
        is_super = request.form.get('is_super_admin') == 'on'
        
        if StationMaster.query.filter_by(email=email).first() or User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('admin.add_master'))
            
        if not is_super:
            pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
            if pool and station_id != pool.station_id:
                active_existing = StationMaster.query.filter_by(
                    station_id=station_id,
                    is_on_leave=False,
                    is_super_admin=False
                ).first()
                if active_existing:
                    flash(f'Station already has an active master ({active_existing.name}). Assign this new master to the Temporary Pool or put the current master on leave first.', 'danger')
                    return redirect(url_for('admin.add_master'))
            
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        master = StationMaster(
            name=name,
            email=email,
            phone=phone,
            password_hash=hashed,
            station_id=station_id,
            is_super_admin=is_super
        )
        db.session.add(master)
        db.session.commit()
        flash('Station Master added successfully.', 'success')
        return redirect(url_for('admin.super_masters'))
        
    return render_template('admin/super/add_master.html', stations=stations)

@admin_bp.route('/super/masters/edit/<int:master_id>', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit_master(master_id):
    master = StationMaster.query.get_or_404(master_id)
    stations = Station.query.order_by(Station.station_name).all()
    
    if request.method == 'POST':
        master.name = request.form.get('name')
        master.phone = request.form.get('phone')
        
        new_station_id = request.form.get('station_id', type=int)
        master.is_super_admin = request.form.get('is_super_admin') == 'on'
        
        if not master.is_super_admin and new_station_id != master.station_id:
            pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
            if pool and new_station_id != pool.station_id:
                active_existing = StationMaster.query.filter_by(
                    station_id=new_station_id,
                    is_on_leave=False,
                    is_super_admin=False
                ).first()
                if active_existing and active_existing.master_id != master.master_id:
                    flash(f'Cannot assign: This station already has an active master ({active_existing.name}).', 'danger')
                    return redirect(url_for('admin.edit_master', master_id=master.master_id))
                    
        master.station_id = new_station_id
        
        db.session.commit()
        flash('Station Master updated successfully.', 'success')
        return redirect(url_for('admin.super_masters'))
        
    return render_template('admin/super/edit_master.html', master=master, stations=stations)

@admin_bp.route('/super/masters/remove/<int:master_id>', methods=['POST'])
@login_required
@super_admin_required
def remove_master(master_id):
    if current_user.master_id == master_id:
        flash('You cannot remove yourself.', 'danger')
        return redirect(url_for('admin.super_masters'))
        
    StationMaster.query.filter_by(master_id=master_id).delete()
    db.session.commit()
    flash('Station Master removed successfully.', 'success')
    return redirect(url_for('admin.super_masters'))

@admin_bp.route('/super/masters/toggle_leave/<int:master_id>', methods=['POST'])
@login_required
@super_admin_required
def toggle_leave(master_id):
    master = StationMaster.query.get_or_404(master_id)
    pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
    
    if master.is_on_leave:
        # Returning from leave. Check if someone temporarily took their spot.
        if pool and master.station_id != pool.station_id and not master.is_super_admin:
            replacement = StationMaster.query.filter_by(
                station_id=master.station_id, 
                is_on_leave=False,
                is_super_admin=False
            ).first()
            if replacement and replacement.master_id != master.master_id:
                replacement.station_id = pool.station_id
                flash(f'Master {master.name} returned! The temporary replacement ({replacement.name}) was automatically re-assigned to the Temporary Pool.', 'info')
        
        master.is_on_leave = False
        flash(f'{master.name} is now marked Active.', 'success')
    else:
        # Going on leave
        master.is_on_leave = True
        flash(f'{master.name} is now On Leave. Their station is now available for a temporary replacement.', 'warning')
        
    db.session.commit()
    return redirect(url_for('admin.super_masters'))

@admin_bp.route('/super/passengers', methods=['GET'])
@login_required
@super_admin_required
def passengers():
    trains = Train.query.all()
    train_id = request.args.get('train_id', type=int)
    journey_date_str = request.args.get('journey_date')
    
    bookings = []
    selected_train = None
    if train_id and journey_date_str:
        try:
            journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()
            selected_train = Train.query.get(train_id)
            bookings = Booking.query.filter_by(
                train_id=train_id, 
                journey_date=journey_date
            ).filter(Booking.status != 'CANCELLED').all()
        except ValueError:
            flash('Invalid date format.', 'danger')
            
    return render_template('admin/super/passengers.html', trains=trains, bookings=bookings, selected_train=selected_train, journey_date=journey_date_str)

@admin_bp.route('/super/stations', methods=['GET'])
@login_required
@super_admin_required
def stations():
    all_stations = Station.query.order_by(Station.station_name).all()
    return render_template('admin/super/stations.html', stations=all_stations)

@admin_bp.route('/super/stations/add', methods=['POST'])
@login_required
@super_admin_required
def add_station():
    name = request.form.get('station_name')
    city = request.form.get('city')
    state = request.form.get('state')
    
    if name and city and state:
        station = Station(station_name=name, city=city, state=state)
        db.session.add(station)
        db.session.commit()
        flash(f'Station {name} added successfully.', 'success')
    else:
        flash('All fields are required.', 'danger')
        
    return redirect(url_for('admin.stations'))

@admin_bp.route('/super/stations/remove/<int:station_id>', methods=['POST'])
@login_required
@super_admin_required
def remove_station(station_id):
    station = Station.query.get_or_404(station_id)
    try:
        db.session.delete(station)
        db.session.commit()
        flash(f'Station deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Cannot delete station because there are bookings or other records linked to it.', 'danger')
        
    return redirect(url_for('admin.stations'))

@admin_bp.route('/super/analytics')
@login_required
@super_admin_required
def analytics():
    from sqlalchemy import func
    
    revenue_by_method = db.session.query(
        Payment.payment_method, func.sum(Payment.amount)
    ).filter_by(payment_status='SUCCESS').group_by(Payment.payment_method).all()
    
    top_trains = db.session.query(
        Train.train_name, func.count(Booking.booking_id).label('num_bookings')
    ).join(Booking).filter(Booking.status != 'CANCELLED').group_by(Train.train_id).order_by(db.text('num_bookings DESC')).limit(5).all()
    
    return render_template('admin/super/analytics.html', 
                          revenue_by_method=revenue_by_method,
                          top_trains=top_trains)


# ══════════════════════════════════════════════════════════════════
# JUNCTION STATION TOGGLE
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/super/stations/toggle-junction/<int:station_id>', methods=['POST'])
@login_required
@super_admin_required
def toggle_junction(station_id):
    station = Station.query.get_or_404(station_id)
    station.is_junction = not station.is_junction
    db.session.commit()
    state = "Junction" if station.is_junction else "Normal"
    flash(f'{station.station_name} marked as {state} station.', 'success')
    return redirect(url_for('admin.stations'))


# ══════════════════════════════════════════════════════════════════
# ROUTE CALCULATION API (Nominatim + OSRM, no API key needed)
# ══════════════════════════════════════════════════════════════════
def _geocode(station_name, city):
    """Return (lat, lon) or None using OpenStreetMap Nominatim."""
    try:
        q = urllib.parse.quote(f"{station_name} railway station {city} India")
        url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'RailSync/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception:
        pass
    return None


def _road_distance_time(lat1, lon1, lat2, lon2):
    """Return (distance_km, travel_minutes) using OSRM public API."""
    try:
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{lon1},{lat1};{lon2},{lat2}?overview=false")
        req = urllib.request.Request(url, headers={'User-Agent': 'RailSync/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        route = data['routes'][0]
        dist_km = round(route['distance'] / 1000, 1)
        # Train avg speed ~80 km/h → time = dist / 80 * 60 minutes
        travel_min = max(5, round(dist_km / 80 * 60))
        return dist_km, travel_min
    except Exception:
        return None, None


@admin_bp.route('/api/calculate-route')
@login_required
@admin_required
def calculate_route():
    """
    GET /admin/api/calculate-route?station_ids=1,2,3&departure=08:00
    Returns JSON array of stops with calculated arrival/departure times.
    departure = HH:MM of first station departure.
    """
    ids_raw = request.args.get('station_ids', '')
    departure_str = request.args.get('departure', '06:00')

    try:
        station_ids = [int(x) for x in ids_raw.split(',') if x.strip()]
    except ValueError:
        return jsonify({'error': 'Invalid station_ids'}), 400

    if len(station_ids) < 2:
        return jsonify({'error': 'Need at least 2 stations'}), 400

    try:
        dep_h, dep_m = [int(x) for x in departure_str.split(':')]
        current_min = dep_h * 60 + dep_m  # minutes since midnight
    except Exception:
        current_min = 6 * 60

    stations_db = {s.station_id: s for s in Station.query.all()}
    results = []
    errors = []

    for i, sid in enumerate(station_ids):
        st = stations_db.get(sid)
        if not st:
            return jsonify({'error': f'Station {sid} not found'}), 404

        if i == 0:
            # First stop: no arrival, just departure
            dep_min = current_min
            arr_min = None
            dist_km = 0
            travel_min = 0
        else:
            prev = stations_db.get(station_ids[i - 1])
            coords_prev = _geocode(prev.station_name, prev.city)
            coords_cur = _geocode(st.station_name, st.city)

            if coords_prev and coords_cur:
                dist_km, travel_min = _road_distance_time(
                    coords_prev[0], coords_prev[1],
                    coords_cur[0], coords_cur[1]
                )
                if dist_km is None:
                    dist_km, travel_min = None, None
                    errors.append(f"OSRM failed for {prev.station_name}→{st.station_name}")
            else:
                dist_km, travel_min = None, None
                errors.append(f"Geocoding failed for {prev.station_name if not coords_prev else st.station_name}")

            if travel_min:
                current_min += travel_min
            else:
                current_min += 60  # fallback 1hr

            arr_min = current_min
            # Wait time: junction = 60 min, normal = 3 min
            wait = 60 if st.is_junction else 3
            dep_min = arr_min + wait
            current_min = dep_min

        def fmt(m):
            if m is None:
                return None
            m = m % (24 * 60)
            return f"{m // 60:02d}:{m % 60:02d}"

        results.append({
            'station_id': sid,
            'station_name': st.station_name,
            'city': st.city,
            'is_junction': st.is_junction,
            'arrival_time': fmt(arr_min),
            'departure_time': fmt(dep_min),
            'distance_km': dist_km,
            'travel_min': travel_min,
        })

    return jsonify({'stops': results, 'warnings': errors})


# ══════════════════════════════════════════════════════════════════
# TRAIN DETAIL API (for normal admin side panel)
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/api/train-detail/<int:train_id>')
@login_required
@admin_required
def train_detail_api(train_id):
    train = Train.query.get_or_404(train_id)
    routes = TrainRoute.query.filter_by(train_id=train_id)\
        .order_by(TrainRoute.stop_number).all()
    stops = []
    for r in routes:
        stops.append({
            'stop_number': r.stop_number,
            'station_name': r.station.station_name,
            'city': r.station.city,
            'is_junction': r.station.is_junction,
            'arrival': r.arrival_time.strftime('%H:%M') if r.arrival_time else '—',
            'departure': r.departure_time.strftime('%H:%M') if r.departure_time else '—',
            'distance_km': r.distance_km,
        })
    source = stops[0]['station_name'] if stops else '—'
    dest = stops[-1]['station_name'] if stops else '—'
    return jsonify({
        'train_number': train.train_number,
        'train_name': train.train_name,
        'total_seats': train.total_seats,
        'source': source,
        'destination': dest,
        'stops': stops,
    })


# ══════════════════════════════════════════════════════════════════
# NORMAL ADMIN — MY PASSENGERS (origin station only)
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/my-passengers')
@login_required
@admin_required
def my_passengers():
    if current_user.role == 'super_admin':
        return redirect(url_for('admin.passengers'))
    station_id = current_user.station_id
    bookings = Booking.query.filter_by(source_station_id=station_id)\
        .filter(Booking.status != 'CANCELLED')\
        .order_by(Booking.journey_date.desc()).all()
    return render_template('admin/my_passengers.html', bookings=bookings,
                           station=current_user.station)


@admin_bp.route('/my-passengers/request-change/<int:booking_id>', methods=['POST'])
@login_required
@admin_required
def request_passenger_change(booking_id):
    if current_user.role == 'super_admin':
        return jsonify({'error': 'Not applicable'}), 403

    booking = Booking.query.get_or_404(booking_id)
    # Ensure booking starts at admin's station
    if booking.source_station_id != current_user.station_id:
        flash('You can only manage passengers boarding at your station.', 'danger')
        return redirect(url_for('admin.my_passengers'))

    field = request.form.get('field_changed')
    new_val = request.form.get('new_value', '').strip()
    reason = request.form.get('reason', '').strip()

    if field not in ('journey_date', 'seat_number', 'status'):
        flash('Invalid field to change.', 'danger')
        return redirect(url_for('admin.my_passengers'))

    old_val = str(getattr(booking, field) or '')

    # Create change request
    cr = PassengerChangeRequest(
        booking_id=booking_id,
        admin_master_id=current_user.master_id,
        admin_station_id=current_user.station_id,
        field_changed=field,
        old_value=old_val,
        new_value=new_val,
        reason=reason
    )
    db.session.add(cr)
    db.session.flush()

    # Notify the passenger
    field_label = {'journey_date': 'Journey Date', 'seat_number': 'Seat Number', 'status': 'Booking Status'}.get(field, field)
    msg = (f"Station Master at {current_user.station.station_name} has requested to change your "
           f"booking #{booking_id}: {field_label} from '{old_val}' to '{new_val}'. "
           f"Reason: {reason or 'Not specified'}. Please approve or reject.")
    notif = Notification(
        user_id=booking.user_id,
        train_id=booking.train_id,
        message=msg,
        notif_type='change_request',
        extra_id=cr.request_id
    )
    db.session.add(notif)
    db.session.flush()
    cr.notification_id = notif.notification_id
    db.session.commit()

    # Real-time push to the passenger
    socketio.emit('change_request', {
        'booking_id': booking_id,
        'request_id': cr.request_id,
        'field': field_label,
        'old_value': old_val,
        'new_value': new_val,
        'reason': reason,
        'message': msg
    }, room=f'user_{booking.user_id}')

    flash(f'Change request sent to passenger. They will be notified to approve or reject.', 'success')
    return redirect(url_for('admin.my_passengers'))


# ══════════════════════════════════════════════════════════════════
# CHAT — Admin ↔ Super Admin
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/chat')
@login_required
@admin_required
def chat():
    if current_user.role == 'super_admin':
        # Super admin sees all station masters to chat with
        pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
        pool_id = pool.station_id if pool else None
        contacts = StationMaster.query.filter(
            StationMaster.is_super_admin == False,
            StationMaster.station_id != pool_id
        ).order_by(StationMaster.name).all() if pool_id else \
            StationMaster.query.filter_by(is_super_admin=False).order_by(StationMaster.name).all()
        # Also include temp pool masters
        temp = StationMaster.query.filter(
            StationMaster.station_id == pool_id,
            StationMaster.is_super_admin == False
        ).order_by(StationMaster.name).all() if pool_id else []
        contacts = contacts + temp
    else:
        # Normal admin sees only super admins
        contacts = StationMaster.query.filter_by(is_super_admin=True).order_by(StationMaster.name).all()

    # Unread count for badge
    unread = ChatRecipient.query.filter_by(
        recipient_id=current_user.master_id, is_read=False
    ).count()

    contacts_data = [{
        'master_id': c.master_id,
        'name': c.name,
        'station': c.station.station_name if c.station else '—',
        'role': c.role,
        'unread': ChatRecipient.query.join(ChatMessage).filter(
            ChatRecipient.recipient_id == current_user.master_id,
            ChatMessage.sender_id == c.master_id,
            ChatRecipient.is_read == False
        ).count()
    } for c in contacts]

    all_masters_json = [{
        'master_id': c.master_id,
        'name': c.name,
        'station': c.station.station_name if c.station else '—'
    } for c in contacts]

    return render_template('admin/chat.html',
                           contacts=contacts,
                           contacts_data=contacts_data,
                           all_masters_json=all_masters_json,
                           unread_total=unread)


@admin_bp.route('/api/chat/messages')
@login_required
@admin_required
def chat_messages():
    """GET /admin/api/chat/messages?with=<master_id> — fetch thread."""
    other_id = request.args.get('with', type=int)
    if not other_id:
        return jsonify([])

    me = current_user.master_id

    # Messages I sent to other, or other sent to me
    sent_ids = db.session.query(ChatMessage.message_id)\
        .join(ChatRecipient)\
        .filter(ChatMessage.sender_id == me,
                ChatRecipient.recipient_id == other_id).subquery()

    received_ids = db.session.query(ChatMessage.message_id)\
        .join(ChatRecipient)\
        .filter(ChatMessage.sender_id == other_id,
                ChatRecipient.recipient_id == me).subquery()

    msgs = ChatMessage.query.filter(
        db.or_(
            ChatMessage.message_id.in_(sent_ids),
            ChatMessage.message_id.in_(received_ids)
        )
    ).order_by(ChatMessage.created_at.asc()).limit(100).all()

    # Mark received messages as read
    ChatRecipient.query.filter(
        ChatRecipient.message_id.in_(received_ids),
        ChatRecipient.recipient_id == me
    ).update({'is_read': True}, synchronize_session=False)
    db.session.commit()

    result = []
    for m in msgs:
        result.append({
            'message_id': m.message_id,
            'sender_id': m.sender_id,
            'sender_name': m.sender.name,
            'body': m.body,
            'is_broadcast': m.is_broadcast,
            'created_at': m.created_at.strftime('%d %b %H:%M'),
            'is_mine': m.sender_id == me
        })
    return jsonify(result)


@admin_bp.route('/api/chat/send', methods=['POST'])
@login_required
@admin_required
def chat_send():
    """Send a message. JSON body: {body, recipient_ids: [int,...], is_broadcast: bool}"""
    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    recipient_ids = data.get('recipient_ids', [])
    is_broadcast = bool(data.get('is_broadcast', False))

    if not body:
        return jsonify({'error': 'Message cannot be empty'}), 400
    if not recipient_ids:
        return jsonify({'error': 'No recipients'}), 400

    msg = ChatMessage(
        sender_id=current_user.master_id,
        body=body,
        is_broadcast=is_broadcast
    )
    db.session.add(msg)
    db.session.flush()

    for rid in recipient_ids:
        rcpt = ChatRecipient(message_id=msg.message_id, recipient_id=int(rid))
        db.session.add(rcpt)

    db.session.commit()

    payload = {
        'message_id': msg.message_id,
        'sender_id': current_user.master_id,
        'sender_name': current_user.name,
        'body': body,
        'is_broadcast': is_broadcast,
        'created_at': msg.created_at.strftime('%d %b %H:%M'),
        'is_mine': False
    }
    for rid in recipient_ids:
        socketio.emit('chat_message', payload, room=f'master_{rid}')

    return jsonify({'ok': True, 'message_id': msg.message_id})


@admin_bp.route('/api/chat/unread-count')
@login_required
@admin_required
def chat_unread_count():
    count = ChatRecipient.query.filter_by(
        recipient_id=current_user.master_id, is_read=False
    ).count()
    return jsonify({'count': count})


# ══════════════════════════════════════════════════════════════════
# MASTER NOTIFICATIONS (Super Admin alert inbox)
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/api/master-notifications')
@login_required
@admin_required
def master_notifications_api():
    notifs = MasterNotification.query.filter_by(
        recipient_id=current_user.master_id
    ).order_by(MasterNotification.created_at.desc()).limit(30).all()
    return jsonify([{
        'notif_id': n.notif_id,
        'subject': n.subject,
        'body': n.body,
        'type': n.notif_type,
        'is_read': n.is_read,
        'created_at': n.created_at.strftime('%d %b %H:%M'),
        'sender': n.sender.name if n.sender else 'System'
    } for n in notifs])


@admin_bp.route('/api/master-notifications/unread-count')
@login_required
@admin_required
def master_notif_unread():
    count = MasterNotification.query.filter_by(
        recipient_id=current_user.master_id, is_read=False
    ).count()
    return jsonify({'count': count})


@admin_bp.route('/api/master-notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
@admin_required
def master_notif_mark_read(notif_id):
    n = MasterNotification.query.filter_by(
        notif_id=notif_id, recipient_id=current_user.master_id
    ).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({'ok': True})