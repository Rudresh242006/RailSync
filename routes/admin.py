from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from functools import wraps
from extensions import db
from flask import abort
from extensions import socketio
from models import (Train, TrainStatus, TrainRoute, Station, StationMaster,
                    Platform, PlatformAllocation, Notification, Booking, User, Payment,
                    PassengerChangeRequest, ChatMessage, ChatRecipient, MasterNotification,
                    TrainDriver)
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import anthropic
import json
import os
import logging

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
    if not api_key or api_key == 'YOUR_ANTHROPIC_KEY_HERE':
        raise Exception("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic(api_key=api_key)

def ai_reallocate_platform(delayed_train_id, delay_minutes, station_id, eta_fixed):
    """
    Ask Claude to suggest the best platform reallocation strategy.
    Returns a dict with suggested platform and reasoning.
    """
    try:
        client = get_ai_client()
    except Exception as e:
        logging.exception("Anthropic AI platform reallocation failed: %s", e)
        return {
            "action": "keep",
            "suggested_platform_id": None,
            "affected_trains": [],
            "reasoning": "AI service unavailable. Keeping current platform.",
            "announcement": f"Train delayed by {delay_minutes} minutes. We apologize for the inconvenience."
        }


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
        claude_model = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-20250514')
        response = client.messages.create(
            model=claude_model,
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
    stopped_trains = TrainStatus.query.filter_by(current_station_id=station.station_id, state='stopped').all()
    return render_template(
            'admin/dashboard.html',
            station=station,
            trains_at_station=trains_at_station,
            platforms=platforms,
            delayed=delayed,
            stopped_trains=stopped_trains,
            now=datetime.utcnow())
@admin_bp.route('/delay/report/<int:train_id>', methods=['POST'])
@login_required
@admin_required
def report_train_delay(train_id):
    delay_minutes = request.form.get('delay_minutes', type=int)
    if not delay_minutes or delay_minutes <= 0:
        flash('Invalid delay minutes.', 'danger')
        return redirect(request.referrer or url_for('admin.dashboard'))
        
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    if not status:
        flash('Train status not found.', 'danger')
        return redirect(request.referrer or url_for('admin.dashboard'))
        
    if current_user.role != 'super_admin' and status.current_station_id != current_user.station_id:
        flash('You can only report delay for trains currently at your station.', 'danger')
        return redirect(request.referrer or url_for('admin.dashboard'))
        
    if status.state != 'stopped':
        flash('You can only report delay for trains currently stopped at your station.', 'danger')
        return redirect(request.referrer or url_for('admin.dashboard'))
        
    status.delay_minutes += delay_minutes
    status.last_updated = datetime.utcnow()
    db.session.commit()
    
    # Dynamically shift platform allocations to avoid collisions due to delay
    try:
        from services.train_service import recalculate_platform_allocations
        recalculate_platform_allocations()
    except Exception as e:
        print(f"Error triggering reallocation: {e}")
    
    flash(f'Reported {delay_minutes} minutes delay for Train {status.train.train_number}. Platform allocations dynamically shifted if necessary.', 'success')
    return redirect(request.referrer or url_for('admin.dashboard'))


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
            dist_km = None
            travel_min = None
            if i > 0:
                prev_st = int(stop_stations[i-1])
                curr_st = int(st)
                st1 = Station.query.get(prev_st)
                st2 = Station.query.get(curr_st)
                if st1 and st2:
                    lat1, lon1 = _lookup_station_coords(st1.station_id, st1.station_name, st1.city)
                    lat2, lon2 = _lookup_station_coords(st2.station_id, st2.station_name, st2.city)
                    if lat1 and lat2:
                        dist_km = _haversine_rail_km(lat1, lon1, lat2, lon2)
                        travel_min = max(1, round(dist_km / 60.0 * 60))

            route = TrainRoute(
                train_id=train.train_id,
                station_id=int(st),
                arrival_time=datetime.strptime(arr, '%H:%M').time() if arr else None,
                departure_time=datetime.strptime(dep, '%H:%M').time() if dep else None,
                stop_number=i + 1,
                distance_km=dist_km,
                estimated_travel_min=travel_min
            )
            db.session.add(route)

        db.session.commit()
        
        # Auto-create driver
        from extensions import bcrypt
        driver_email = f"driver{train.train_id}@gmail.com"
        driver_password = '12345678@'
        hashed_pw = bcrypt.generate_password_hash(driver_password).decode('utf-8')
        
        driver = TrainDriver(
            name=f"Driver {train.train_id}",
            email=driver_email,
            password_hash=hashed_pw,
            train_id=train.train_id
        )
        db.session.add(driver)
        db.session.commit()

        flash(f'Train {train.train_number} added successfully. Auto-assigned driver: {driver_email} (Password: {driver_password})', 'success')
        return redirect(url_for('admin.trains'))

    return render_template('admin/add_train.html', stations=stations)


@admin_bp.route('/trains/remove/<int:train_id>', methods=['POST'])
@login_required
@super_admin_required
def remove_train(train_id):
    train = Train.query.get_or_404(train_id)
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
        
        # Restriction: Station Master can only report delay if train is stopped at their station
        if current_user.role != 'super_admin':
            if not status or status.current_station_id != station.station_id or status.state != 'stopped':
                flash('You can only report a delay when the train is physically stopped at your station.', 'danger')
                return redirect(url_for('admin.report_delay'))

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
            trains=Train.query.all(),
            now=datetime.utcnow())


@admin_bp.route('/platforms/add', methods=['POST'])
@login_required
@admin_required
def add_platform():
    station = current_user.station
    number = request.form.get('platform_number', '').strip()

    if not number:
        flash('Platform number cannot be empty.', 'danger')
        return redirect(url_for('admin.platforms'))

    # Check for duplicate platform number at this station
    existing = Platform.query.filter_by(
        station_id=station.station_id,
        platform_number=number
    ).first()
    if existing:
        flash(f'Platform {number} already exists at this station.', 'danger')
        return redirect(url_for('admin.platforms'))

    p = Platform(station_id=station.station_id, platform_number=number)
    db.session.add(p)
    db.session.commit()
    flash(f'Platform {number} added successfully.', 'success')
    return redirect(url_for('admin.platforms'))


@admin_bp.route('/allocate', methods=['POST'])
@login_required
@admin_required
def allocate_platform():
    station = current_user.station
    train_id   = request.form.get('train_id',   type=int)
    platform_id = request.form.get('platform_id', type=int)

    if not train_id or not platform_id:
        flash('Please select both a train and a platform.', 'danger')
        return redirect(url_for('admin.platforms'))

    # Auto-fetch arrival/departure from the schedule set by Super Admin
    route_stop = TrainRoute.query.filter_by(
        train_id=train_id,
        station_id=station.station_id
    ).first()

    if not route_stop:
        flash('This train does not stop at your station. Cannot allocate.', 'danger')
        return redirect(url_for('admin.platforms'))

    # Use today's date + the scheduled time from TrainRoute
    today = datetime.utcnow().date()
    arrival   = datetime.combine(today, route_stop.arrival_time)   if route_stop.arrival_time   else datetime.utcnow()
    departure = datetime.combine(today, route_stop.departure_time) if route_stop.departure_time else datetime.utcnow()

    # Check for conflicts on the chosen platform
    conflict = PlatformAllocation.query.filter(
        PlatformAllocation.platform_id == platform_id,
        PlatformAllocation.station_id  == station.station_id,
        PlatformAllocation.arrival_time   < departure,
        PlatformAllocation.departure_time > arrival
    ).first()

    if conflict:
        # Auto-find a free platform
        free_platforms = Platform.query.filter(
            Platform.station_id  == station.station_id,
            Platform.platform_id != platform_id
        ).all()

        best_platform = None
        for p in free_platforms:
            has_conflict = PlatformAllocation.query.filter(
                PlatformAllocation.platform_id    == p.platform_id,
                PlatformAllocation.station_id     == station.station_id,
                PlatformAllocation.arrival_time   < departure,
                PlatformAllocation.departure_time > arrival
            ).first()
            if not has_conflict:
                best_platform = p
                break

        if best_platform:
            alloc = PlatformAllocation(
                train_id=train_id, station_id=station.station_id,
                platform_id=best_platform.platform_id,
                arrival_time=arrival, departure_time=departure
            )
            db.session.add(alloc)
            db.session.commit()
            flash(f'Conflict detected! Auto-assigned to Platform {best_platform.platform_number}.', 'warning')
        else:
            flash('No free platforms available at this time!', 'danger')
        return redirect(url_for('admin.platforms'))

    # No conflict — assign directly
    # Remove any existing allocation for this train at this station first
    PlatformAllocation.query.filter_by(
        train_id=train_id, station_id=station.station_id
    ).delete()
    alloc = PlatformAllocation(
        train_id=train_id, station_id=station.station_id,
        platform_id=platform_id,
        arrival_time=arrival, departure_time=departure
    )
    db.session.add(alloc)
    db.session.commit()
    flash('Platform allocated successfully.', 'success')
    return redirect(url_for('admin.platforms'))


@admin_bp.route('/platforms/delete/<int:platform_id>', methods=['POST'])
@login_required
@admin_required
def delete_platform(platform_id):
    station = current_user.station
    p = Platform.query.filter_by(
        platform_id=platform_id, station_id=station.station_id
    ).first_or_404()
    # Remove allocations tied to this platform first
    PlatformAllocation.query.filter_by(platform_id=platform_id).delete()
    db.session.delete(p)
    db.session.commit()
    flash(f'Platform {p.platform_number} deleted.', 'success')
    return redirect(url_for('admin.platforms'))


# ──────────────────────────────────────────────────────────────────
# Clear Delay (Station Master — resets delay back to 0 for a train)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/delay/clear/<int:train_id>', methods=['POST'])
@login_required
@admin_required
def clear_delay(train_id):
    station = current_user.station
    status = TrainStatus.query.filter_by(train_id=train_id).first()

    if not status:
        flash('No delay record found for this train.', 'danger')
        return redirect(url_for('admin.report_delay'))

    if current_user.role != 'super_admin' and status.current_station_id != current_user.station_id:
        flash('You can only clear delays for trains currently at your station.', 'danger')
        return redirect(url_for('admin.report_delay'))

    old_delay = status.delay_minutes
    status.delay_minutes = 0
    status.last_updated = datetime.utcnow()
    db.session.commit()


    # Notify passengers that the train is back on time
    train = Train.query.get(train_id)
    if train:
        msg = (f"Good news! Train {train.train_number} ({train.train_name}) "
               f"which was delayed by {old_delay} minutes is now back ON TIME. "
               f"We apologize for the earlier inconvenience.")
        notify_affected_passengers(train_id, msg)

    flash(f'Delay cleared for train. Passengers have been notified.', 'success')
    return redirect(url_for('admin.report_delay'))


# ──────────────────────────────────────────────────────────────────
# Super Admin: Platform management per station (JSON API)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/super/stations/<int:station_id>/platforms')
@login_required
@super_admin_required
def station_platforms_api(station_id):
    """GET — returns JSON list of platforms for a station."""
    station = Station.query.get_or_404(station_id)
    platforms = Platform.query.filter_by(station_id=station_id).order_by(Platform.platform_number).all()
    
    now = datetime.utcnow()
    platform_data = []
    for p in platforms:
        is_occupied = False
        allocs = PlatformAllocation.query.filter_by(platform_id=p.platform_id).all()
        for a in allocs:
            if a.arrival_time <= now and a.departure_time >= now:
                is_occupied = True
                break
                
        platform_data.append({
            'platform_id': p.platform_id, 
            'platform_number': p.platform_number, 
            'is_available': not is_occupied
        })

    return jsonify({
        'station_id': station_id,
        'station_name': station.station_name,
        'platforms': platform_data
    })


@admin_bp.route('/super/stations/<int:station_id>/platforms/add', methods=['POST'])
@login_required
@super_admin_required
def super_add_platform(station_id):
    """POST — add a platform to any station (Super Admin)."""
    station = Station.query.get_or_404(station_id)
    number = (request.form.get('platform_number') or request.get_json(silent=True, force=True) or {}).get('platform_number', '')
    if isinstance(number, dict):
        number = ''
    number = str(number).strip()

    # Support JSON body too
    if not number:
        body = request.get_json(silent=True, force=True) or {}
        number = str(body.get('platform_number', '')).strip()

    if not number:
        return jsonify({'ok': False, 'error': 'Platform number cannot be empty'}), 400

    existing = Platform.query.filter_by(station_id=station_id, platform_number=number).first()
    if existing:
        return jsonify({'ok': False, 'error': f'Platform {number} already exists at this station'}), 409

    p = Platform(station_id=station_id, platform_number=number)
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'platform_id': p.platform_id, 'platform_number': p.platform_number})


@admin_bp.route('/super/stations/<int:station_id>/platforms/remove/<int:platform_id>', methods=['POST'])
@login_required
@super_admin_required
def super_remove_platform(station_id, platform_id):
    """POST — remove a platform from any station (Super Admin)."""
    p = Platform.query.filter_by(platform_id=platform_id, station_id=station_id).first_or_404()
    PlatformAllocation.query.filter_by(platform_id=platform_id).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


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
# API: Calculate Journey (Distance & Time via AI)
# ──────────────────────────────────────────────────────────────────
def ai_estimate_journey(st1, st2):
    client = get_ai_client()
    prompt = f"""You are a railway expert system.
What is the approximate realistic railway track distance (in km) and the standard train travel time (in minutes) between these two stations in India:
Station 1: {st1.station_name}, {st1.city}
Station 2: {st2.station_name}, {st2.city}

Please provide a highly realistic estimation for a standard express train based on real-world Indian Railway data.
DO NOT provide any text, just return a raw JSON object exactly in this format:
{{
  "distance_km": <number>,
  "estimated_minutes": <number>
}}"""
    try:
        claude_model = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-20250514')
        response = client.messages.create(
            model=claude_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text
        raw = raw.strip().lstrip('```json').rstrip('```').strip()
        data = json.loads(raw)
        return int(data['distance_km']), int(data['estimated_minutes'])
    except Exception as e:
        print("AI Distance Error:", e)
        return None, None

# ── Hardcoded coordinates for major Indian railway stations ──────────────
# Format: "station name lowercase" → (lat, lon)
_KNOWN_STATIONS = {
    # Major hubs
    "new delhi":            (28.6420, 77.2201),
    "delhi":                (28.6420, 77.2201),
    "mumbai central":       (18.9706, 72.8193),
    "mumbai":               (18.9696, 72.8195),
    "cst":                  (18.9400, 72.8356),
    "chhatrapati shivaji":  (18.9400, 72.8356),
    "chennai central":      (13.0827, 80.2707),
    "chennai":              (13.0827, 80.2707),
    "kolkata":              (22.5726, 88.3639),
    "howrah":               (22.5852, 88.3426),
    "sealdah":              (22.5661, 88.3697),
    "bangalore city":       (12.9767, 77.5713),
    "krantivira sangolli rayanna": (12.9767, 77.5713),
    "bengaluru":            (12.9767, 77.5713),
    "bangalore":            (12.9767, 77.5713),
    "hyderabad":            (17.4065, 78.4772),
    "secunderabad":         (17.4344, 78.5013),
    "kacheguda":            (17.3850, 78.4867),
    "ahmedabad":            (23.0225, 72.5714),
    "pune":                 (18.5286, 73.8742),
    "jaipur":               (26.9124, 75.7873),
    "lucknow":              (26.8467, 80.9462),
    "kanpur central":       (26.4499, 80.3319),
    "kanpur":               (26.4499, 80.3319),
    "varanasi":             (25.3176, 82.9739),
    "patna":                (25.5941, 85.1376),
    "bhopal":               (23.2599, 77.4126),
    "indore":               (22.7196, 75.8577),
    "nagpur":               (21.1458, 79.0882),
    "surat":                (21.1702, 72.8311),
    "visakhapatnam":        (17.6868, 83.2185),
    "vijayawada":           (16.5062, 80.6480),
    "coimbatore":           (11.0168, 76.9558),
    "madurai":              (9.9195,  78.1194),
    "trichy":               (10.7909, 78.7047),
    "tiruchirapalli":       (10.7909, 78.7047),
    "salem":                (11.6643, 78.1460),
    "ernakulam":            (9.9816,  76.2999),
    "kochi":                (9.9816,  76.2999),
    "thiruvananthapuram":   (8.5241,  76.9366),
    "trivandrum":           (8.5241,  76.9366),
    "kozhikode":            (11.2588, 75.7804),
    "calicut":              (11.2588, 75.7804),
    "mangalore":            (12.9141, 74.8560),
    "hubli":                (15.3647, 75.1240),
    "mysore":               (12.2958, 76.6394),
    "mysuru":               (12.2958, 76.6394),
    "guwahati":             (26.1158, 91.7086),
    "bhubaneswar":          (20.2961, 85.8245),
    "cuttack":              (20.4625, 85.8830),
    "raipur":               (21.2514, 81.6296),
    "bilaspur":             (22.0797, 82.1391),
    "allahabad":            (25.4358, 81.8463),
    "prayagraj":            (25.4358, 81.8463),
    "agra":                 (27.1767, 78.0081),
    "agra cantt":           (27.1767, 78.0081),
    "mathura":              (27.4924, 77.6737),
    "meerut":               (28.9845, 77.7064),
    "amritsar":             (31.6340, 74.8723),
    "ludhiana":             (30.9010, 75.8573),
    "chandigarh":           (30.7333, 76.7794),
    "jammu":                (32.7266, 74.8570),
    "jodhpur":              (26.2389, 73.0243),
    "udaipur":              (24.5854, 73.7125),
    "ajmer":                (26.4521, 74.6446),
    "bikaner":              (28.0229, 73.3119),
    "kota":                 (25.2138, 75.8648),
    "gwalior":              (26.2183, 78.1828),
    "jabalpur":             (23.1815, 79.9864),
    "nashik":               (19.9975, 73.7898),
    "aurangabad":           (19.8762, 75.3433),
    "solapur":              (17.6805, 75.9064),
    "ranchi":               (23.3441, 85.3096),
    "jamshedpur":           (22.8046, 86.2029),
    "dhanbad":              (23.7957, 86.4304),
    "silchar":              (24.8333, 92.7789),
    "siliguri":             (26.7271, 88.3953),
    "new jalpaiguri":       (26.7139, 88.4485),
    "jammu tawi":           (32.7266, 74.8570),
    "shirdi":               (19.7675, 74.4773),
}

# Runtime OSM coordinate cache: {station_id: (lat, lon) or None for failed lookups}
_coord_cache = {}
_coord_failed = set()   # station_ids that definitively have no OSM coords


def _lookup_station_coords(station_id, station_name, city):
    """
    Multi-strategy coordinate lookup with full persistence:
    1. Runtime memory cache (instant)
    2. Database (Station.latitude / longitude) — survives server restarts
    3. Hardcoded known-stations table (instant)
    4. OSM Nominatim — only called if all above fail; result saved to DB
    Returns (lat, lon) or (None, None)
    """
    # 1. Memory cache — fastest
    if station_id in _coord_cache:
        return _coord_cache[station_id]

    # 2. DB persistent cache — survives restarts
    st = Station.query.get(station_id)
    if st and st.latitude and st.longitude:
        coords = (st.latitude, st.longitude)
        _coord_cache[station_id] = coords
        print(f"[COORDS] {station_name} -> DB cache ({coords})")
        return coords

    # 3. Hardcoded table — exact name match
    key = station_name.strip().lower()
    if key in _KNOWN_STATIONS:
        coords = _KNOWN_STATIONS[key]
        _coord_cache[station_id] = coords
        # Persist to DB so we never look it up again
        if st:
            st.latitude, st.longitude = coords
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        print(f"[COORDS] {station_name} -> hardcoded table ({coords})")
        return coords

    # 4. Partial match by city name
    city_key = city.strip().lower()
    if city_key in _KNOWN_STATIONS:
        coords = _KNOWN_STATIONS[city_key]
        _coord_cache[station_id] = coords
        if st:
            st.latitude, st.longitude = coords
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        print(f"[COORDS] {station_name} -> city match '{city_key}' ({coords})")
        return coords

    # Already confirmed unfindable this session — skip immediately
    if station_id in _coord_failed:
        return (None, None)

    # 5. OSM Nominatim — last resort, rate-limited
    # Clean up the station name: strip '_Station' suffix and underscores
    # so 'Mangalore_Station' → 'Mangalore' for a cleaner OSM search
    import re, time
    clean_name = re.sub(r'[_\s]*[Ss]tation$', '', station_name).replace('_', ' ').strip()
    queries = [
        f"{clean_name} railway station, {city}, India",
        f"{clean_name}, {city}, India",
        f"{city} railway station, India",
        f"{city}, India",
    ]

    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(1.1)   # Nominatim requires max 1 req/sec
        url = ("https://nominatim.openstreetmap.org/search?q="
               + urllib.parse.quote(q)
               + "&format=json&limit=1&countrycodes=IN")
        req = urllib.request.Request(url, headers={'User-Agent': 'RailSync/3.0'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                if data:
                    lat = float(data[0]['lat'])
                    lon = float(data[0]['lon'])
                    coords = (lat, lon)
                    _coord_cache[station_id] = coords
                    # Persist to DB — never call OSM again for this station
                    if st:
                        st.latitude, st.longitude = lat, lon
                        try:
                            db.session.commit()
                            print(f"[COORDS] {station_name} -> OSM '{q}' -> saved to DB ({lat:.4f}, {lon:.4f})")
                        except Exception:
                            db.session.rollback()
                    return coords
        except Exception as e:
            err_str = str(e)
            print(f"[COORDS] OSM failed for '{q}': {e}")
            if '429' in err_str:
                print(f"[COORDS] 429 rate-limit hit — aborting OSM for {station_name}")
                break
            continue

    # 6. Gemini API Fallback
    import os
    gemini_key = os.environ.get('GEMINI_API_KEY')
    if not gemini_key:
        # .env is already loaded by app.py; if key is still missing, skip
        pass

    if gemini_key:
        try:
            print(f"[COORDS] OSM failed. Trying Gemini API for {clean_name}, {city}...")
            prompt = (f"Return only a valid JSON object with keys 'lat' and 'lon' containing the floating point "
                      f"latitude and longitude coordinates for {clean_name} railway station in {city}, India. "
                      f"Do not include any other text or markdown.")
            gemini_model = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read())
                text = resp_data['candidates'][0]['content']['parts'][0]['text'].strip()
                # Clean markdown blocks if Gemini includes them
                if text.startswith('```'):
                    text = text.replace('```json', '').replace('```', '').strip()
                res = json.loads(text)
                lat, lon = float(res['lat']), float(res['lon'])
                coords = (lat, lon)
                _coord_cache[station_id] = coords
                # Persist to DB — never call Gemini again for this station
                if st:
                    st.latitude, st.longitude = lat, lon
                    try:
                        db.session.commit()
                        print(f"[COORDS] {station_name} -> Gemini -> saved to DB ({lat:.4f}, {lon:.4f})")
                    except Exception:
                        db.session.rollback()
                return coords
        except Exception as e:
            logging.exception("Gemini API coordinate fallback failed: %s", e)


    print(f"[COORDS] FAILED all strategies for: {station_name}, {city}")
    _coord_failed.add(station_id)
    return (None, None)


@admin_bp.route('/api/calculate-journey')
@login_required
@admin_required
def calculate_journey():
    import math

    from_id = request.args.get('from_id', type=int)
    to_id   = request.args.get('to_id',   type=int)
    if not from_id or not to_id:
        return jsonify({'error': 'Missing from_id or to_id'}), 400

    st1 = Station.query.get(from_id)
    st2 = Station.query.get(to_id)
    if not st1 or not st2:
        return jsonify({'error': 'Invalid station ID'}), 404

    # Use Station lat/lon if both already in DB (no OSM call at all)
    if st1.latitude and st1.longitude and st2.latitude and st2.longitude:
        lat1, lon1 = st1.latitude, st1.longitude
        lat2, lon2 = st2.latitude, st2.longitude
        print(f"[JOURNEY] {st1.station_name} -> {st2.station_name}: using DB coords (no OSM)")
    else:
        # Only call OSM for stations that don't have coords yet
        lat1, lon1 = _lookup_station_coords(from_id, st1.station_name, st1.city)
        lat2, lon2 = _lookup_station_coords(to_id,   st2.station_name, st2.city)

    print(f"[JOURNEY] {st1.station_name}({lat1},{lon1}) -> {st2.station_name}({lat2},{lon2})")

    if lat1 and lat2:
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat/2)**2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
        straight_km = 2 * 6371 * math.asin(math.sqrt(a))
        rail_km  = round(straight_km * 1.30, 1)
        # Indian express trains average ~60 km/h (including intermediate halts)
        est_min  = max(1, round(rail_km / 60.0 * 60))
        print(f"[JOURNEY] straight={straight_km:.1f}km -> rail={rail_km}km -> {est_min}min")
        return jsonify({'distance_km': rail_km, 'estimated_minutes': est_min, 'source': 'calculated'})

    return jsonify({'error': f'Could not find coordinates for: {st1.station_name} or {st2.station_name}'}), 422




# ──────────────────────────────────────────────────────────────────
# Edit Train Stops (Super Admin)
# ──────────────────────────────────────────────────────────────────
@admin_bp.route('/super/trains/edit-stops/<int:train_id>', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit_train_stops(train_id):
    train = Train.query.get_or_404(train_id)
    
    # Check if journey has started
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    is_locked = False
    if status and status.journey_start_datetime and datetime.now() >= status.journey_start_datetime:
        is_locked = True
        
    stations_obj = Station.query.order_by(Station.station_name).all()
    stations = [{'station_id': s.station_id, 'station_name': s.station_name, 'city': s.city, 'state': s.state} for s in stations_obj]

    if request.method == 'POST':
        if is_locked:
            flash('Journey has started. Schedule changes are locked unless a delay is reported by a Station Master.', 'danger')
            return redirect(url_for('admin.trains'))
            
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
            dist_km = None
            travel_min = None
            if i > 0:
                prev_st = int(stop_station_ids[i-1])
                curr_st = int(st)
                st1 = Station.query.get(prev_st)
                st2 = Station.query.get(curr_st)
                if st1 and st2:
                    lat1, lon1 = _lookup_station_coords(st1.station_id, st1.station_name, st1.city)
                    lat2, lon2 = _lookup_station_coords(st2.station_id, st2.station_name, st2.city)
                    if lat1 and lat2:
                        dist_km = _haversine_rail_km(lat1, lon1, lat2, lon2)
                        travel_min = max(1, round(dist_km / 60.0 * 60))

            route = TrainRoute(
                train_id=train_id,
                station_id=int(st),
                arrival_time=datetime.strptime(arr, '%H:%M').time() if arr else None,
                departure_time=datetime.strptime(dep, '%H:%M').time() if dep else None,
                stop_number=i + 1,
                distance_km=dist_km,
                estimated_travel_min=travel_min
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
                           existing_stops=existing_stops,
                           is_locked=is_locked)


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

@admin_bp.route('/super/drivers', methods=['GET'])
@login_required
@super_admin_required
def super_drivers():
    drivers = TrainDriver.query.all()
    return render_template('admin/super/drivers.html', drivers=drivers)


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
# ROUTE CALCULATION API  (pure-math, no external APIs for known stations)
# ══════════════════════════════════════════════════════════════════
def _haversine_rail_km(lat1, lon1, lat2, lon2):
    """Straight-line haversine distance × 1.30 rail-track factor → km."""
    import math
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    straight_km = 2 * 6371 * math.asin(math.sqrt(a))
    return round(straight_km * 1.30, 1)


@admin_bp.route('/api/calculate-route')
@login_required
@admin_required
def calculate_route():
    """
    GET /admin/api/calculate-route?station_ids=1,2,3&departure=08:00
    Returns JSON array of stops with calculated arrival/departure times.
    departure = HH:MM of first station departure.
    Uses _lookup_station_coords (known table -> DB -> OSM last resort).
    Distance = haversine * 1.30 rail factor. No OSRM API calls.
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
            dep_min  = current_min
            arr_min  = None
            dist_km  = 0
            travel_min = 0
        else:
            prev = stations_db.get(station_ids[i - 1])

            # Use cached/known coords — NO external API for known stations
            lat1, lon1 = _lookup_station_coords(prev.station_id, prev.station_name, prev.city)
            lat2, lon2 = _lookup_station_coords(st.station_id,   st.station_name,   st.city)

            if lat1 and lon1 and lat2 and lon2:
                dist_km   = _haversine_rail_km(lat1, lon1, lat2, lon2)
                # 60 km/h average speed
                travel_min = max(1, round(dist_km / 60 * 60))
                print(f"[ROUTE] {prev.station_name} -> {st.station_name}: "
                      f"{dist_km} km, {travel_min} min")
            else:
                dist_km    = None
                travel_min = None
                errors.append(f"No coords for "
                              f"{'prev' if not lat1 else st.station_name}")

            if travel_min:
                current_min += travel_min
            else:
                current_min += 60  # last-resort 1-hr fallback

            arr_min = current_min
            # Wait time at stop: junction = 10 min, normal = 3 min
            wait    = 10 if st.is_junction else 3
            dep_min = arr_min + wait
            current_min = dep_min

        def fmt(m):
            if m is None:
                return None
            m = m % (24 * 60)
            return f"{m // 60:02d}:{m % 60:02d}"

        results.append({
            'station_id':    sid,
            'station_name':  st.station_name,
            'city':          st.city,
            'is_junction':   st.is_junction,
            'arrival_time':  fmt(arr_min),
            'departure_time': fmt(dep_min),
            'distance_km':   dist_km,
            'travel_min':    travel_min,
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


# ══════════════════════════════════════════════════════════════════
# LIVE STATUS — India Map with animated train routes
# ══════════════════════════════════════════════════════════════════

# A visually distinct palette for up to 20 trains
_TRAIN_COLORS = [
    '#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#f9844a',
    '#a29bfe', '#fd79a8', '#00cec9', '#fdcb6e', '#e17055',
    '#74b9ff', '#55efc4', '#fab1a0', '#dfe6e9', '#81ecec',
    '#b2bec3', '#ff7675', '#00b894', '#0984e3', '#e84393',
]

def _get_station_coords_for_map(station):
    """
    Return (lat, lon) for a station.
    Uses persisted DB columns first (instant). Falls back to OSM once and saves.
    """
    if station.latitude and station.longitude:
        return station.latitude, station.longitude

    # First time: lookup and persist so we never call OSM again
    if station.station_name == 'Unassigned / Temporary Pool':
        return None, None
    lat, lon = _lookup_station_coords(station.station_id, station.station_name, station.city)
    if lat and lon:
        station.latitude  = lat
        station.longitude = lon
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return lat, lon


@admin_bp.route('/super/live-status')
@login_required
@super_admin_required
def live_status():
    return render_template('admin/super/live_status.html')



@admin_bp.route('/api/live-status')
@login_required
@super_admin_required
def live_status_api():
    """
    Returns JSON with trains + stations for the Live Status map.

    PERFORMANCE DESIGN:
    - All DB queries run BEFORE processing (no lazy loads inside loops).
    - TrainStatus is fetched in ONE bulk query → dict lookup (no N+1).
    - Train routes are sorted once per train using already-loaded data.
    - Station coords come ONLY from Station.latitude/longitude (DB columns).
      → No OSM / no network calls at all during this request.
      → Stations without persisted coordinates are simply skipped (no block).
    - _get_station_coords_for_map() is still called so that the first-ever
      request for a station triggers a one-time OSM lookup & DB persist,
      but that only happens for new stations that have never been geocoded.
      After that single save, every subsequent call is instant (DB read).
    """
    import math
    from sqlalchemy.orm import joinedload

    # ── 1. Bulk-load everything in as few queries as possible ──────────
    # Trains with routes + station eagerly loaded (avoids N+1 lazy loads)
    trains = (Train.query
              .options(
                  joinedload(Train.routes).joinedload(TrainRoute.station)
              )
              .all())

    # All stations (single query)
    all_stations = Station.query.all()

    # All TrainStatus records → dict keyed by train_id (ONE query, no loop)
    all_statuses = {s.train_id: s for s in TrainStatus.query.all()}

    # ── 2. Build station coord map (DB only — instant, no network) ──────
    station_coords = {}
    pool_id = None

    for s in all_stations:
        if s.station_name == 'Unassigned / Temporary Pool':
            pool_id = s.station_id
            continue  # skip pool station

        # Fast path: coords already in DB
        if s.latitude and s.longitude:
            station_coords[s.station_id] = {'lat': s.latitude, 'lon': s.longitude}
        else:
            # Slow path: first-ever load — lookup + persist so next call is instant
            lat, lon = _get_station_coords_for_map(s)
            if lat and lon:
                station_coords[s.station_id] = {'lat': lat, 'lon': lon}
            # else: station has no coords at all → silently skip

    # ── 3. Build stations output (background dots) ──────────────────────
    stations_out = []
    for s in all_stations:
        if s.station_id == pool_id:
            continue
        if s.station_id in station_coords:
            c = station_coords[s.station_id]
            stations_out.append({
                'station_id': s.station_id,
                'name': s.station_name,
                'city': s.city,
                'state': s.state,
                'is_junction': s.is_junction,
                'lat': c['lat'],
                'lon': c['lon'],
            })

    # ── 4. Build trains output ──────────────────────────────────────────
    trains_out = []
    for i, train in enumerate(trains):
        color = _TRAIN_COLORS[i % len(_TRAIN_COLORS)]

        # Routes are already eagerly loaded — no extra queries
        routes = sorted(train.routes, key=lambda r: r.stop_number)

        stops = []
        total_route_minutes = 0
        total_dist = 0.0
        for j, r in enumerate(routes):
            if r.station_id not in station_coords:
                continue  # skip stations without coords (instant, no network)
            c = station_coords[r.station_id]
            if j > 0 and r.distance_km:
                total_dist += r.distance_km
            if r.estimated_travel_min:
                total_route_minutes += r.estimated_travel_min
            stops.append({
                'stop_number':    r.stop_number,
                'station_id':     r.station_id,
                'name':           r.station.station_name,
                'city':           r.station.city,
                'is_junction':    r.station.is_junction,
                'lat':            c['lat'],
                'lon':            c['lon'],
                'arrival':        r.arrival_time.strftime('%H:%M')   if r.arrival_time   else None,
                'departure':      r.departure_time.strftime('%H:%M') if r.departure_time else None,
                'distance_km':    r.distance_km,
                'cumulative_km':  round(total_dist, 1),
                'travel_min':     r.estimated_travel_min,
            })

        if len(stops) < 2:
            continue  # skip trains with insufficient mapped stops

        # Status — dict lookup, no extra query (pre-fetched above)
        status            = all_statuses.get(train.train_id)
        current_station_id = status.current_station_id     if status else stops[0]['station_id']
        delay_minutes      = status.delay_minutes           if status else 0
        journey_start_dt   = status.journey_start_datetime if status else None
        journey_direction  = status.journey_direction       if status else 'idle'

        # Compute total route distance using haversine (pure math, no network)
        total_route_km = 0.0
        prev_lat = prev_lon = None
        for stop in stops:
            if prev_lat is not None:
                dlat = math.radians(stop['lat'] - prev_lat)
                dlon = math.radians(stop['lon'] - prev_lon)
                a = (math.sin(dlat / 2) ** 2
                     + math.cos(math.radians(prev_lat))
                     * math.cos(math.radians(stop['lat']))
                     * math.sin(dlon / 2) ** 2)
                total_route_km += 2 * 6371 * math.asin(math.sqrt(a)) * 1.30
            prev_lat, prev_lon = stop['lat'], stop['lon']

        trains_out.append({
            'train_id':               train.train_id,
            'train_number':           train.train_number,
            'train_name':             train.train_name,
            'total_seats':            train.total_seats,
            'color':                  color,
            'current_station_id':     current_station_id,
            'delay_minutes':          delay_minutes,
            'total_route_km':         round(total_route_km, 1),
            'total_route_minutes':    total_route_minutes,
            'turnaround_minutes':     train.turnaround_minutes or 360,
            'anim_speed_scale':       float(train.anim_speed_scale or 8.0),
            'journey_start_datetime': journey_start_dt.isoformat() if journey_start_dt else None,
            'journey_direction':      journey_direction,
            'stops':                  stops,
        })

    return jsonify({'trains': trains_out, 'stations': stations_out})

# ────────────────────────────────────────────────────────────────
# API: Schedule a train journey (Super Admin)
# ────────────────────────────────────────────────────────────────
@admin_bp.route('/super/trains/<int:train_id>/schedule', methods=['POST'])
@login_required
@super_admin_required
def schedule_train(train_id):
    """
    Set or update a train's journey start datetime and direction.
    Body JSON: {
        start_datetime: 'YYYY-MM-DDTHH:MM',   # local IST
        direction: 'forward' | 'reverse' | 'idle'
    }
    """
    from datetime import datetime as dt
    train = Train.query.get_or_404(train_id)
    
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    if status and status.journey_start_datetime and dt.utcnow() >= status.journey_start_datetime:
        return jsonify({'ok': False, 'error': 'Journey has started. Schedule changes are locked.'}), 403
        
    body  = request.get_json(force=True, silent=True) or {}

    start_str = body.get('start_datetime', '').strip()
    direction  = body.get('direction', 'forward').strip()

    if direction not in ('forward', 'reverse', 'idle'):
        return jsonify({'ok': False, 'error': 'direction must be forward | reverse | idle'}), 400

    # Parse datetime (ISO format from HTML datetime-local input)
    start_dt = None
    if start_str and direction != 'idle':
        try:
            start_dt = dt.fromisoformat(start_str)
        except ValueError:
            return jsonify({'ok': False, 'error': f'Invalid datetime: {start_str}'}), 400

    # Upsert TrainStatus
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    if not status:
        # Create one pointing to the first stop
        first_route = (TrainRoute.query
                       .filter_by(train_id=train_id)
                       .order_by(TrainRoute.stop_number)
                       .first())
        if not first_route:
            return jsonify({'ok': False, 'error': 'Train has no route stops'}), 400
        status = TrainStatus(
            train_id=train_id,
            current_station_id=first_route.station_id,
        )
        db.session.add(status)

    status.journey_start_datetime = start_dt
    status.journey_direction      = direction
    if direction == 'idle':
        status.journey_start_datetime = None

    db.session.commit()
    return jsonify({
        'ok': True,
        'train_id': train_id,
        'journey_start_datetime': status.journey_start_datetime.isoformat() if status.journey_start_datetime else None,
        'journey_direction': status.journey_direction,
    })


# ────────────────────────────────────────────────────────────────
# API: Set turnaround settings for a train (Super Admin)
# ────────────────────────────────────────────────────────────────
@admin_bp.route('/super/trains/<int:train_id>/set-turnaround', methods=['POST'])
@login_required
@super_admin_required
def set_turnaround(train_id):
    """Update turnaround_minutes and anim_speed_scale for a train."""
    train = Train.query.get_or_404(train_id)

    # Frontend always sends JSON; fall back to form data just in case
    body = request.get_json(silent=True, force=True)
    if not body:
        body = request.form.to_dict()

    turnaround = body.get('turnaround_minutes')
    scale      = body.get('anim_speed_scale')

    try:
        if turnaround is not None:
            train.turnaround_minutes = max(0, int(turnaround))
        if scale is not None:
            train.anim_speed_scale = max(0.5, float(scale))
    except (ValueError, TypeError) as e:
        return jsonify({'ok': False, 'error': f'Invalid value: {e}'}), 400

    db.session.commit()
    return jsonify({
        'ok': True,
        'train_id': train_id,
        'turnaround_minutes': train.turnaround_minutes,
        'anim_speed_scale': float(train.anim_speed_scale)
    })


# ────────────────────────────────────────────────────────────────
# Helper: compute one-way journey duration from stored stop times
# ────────────────────────────────────────────────────────────────
def _compute_one_way_minutes(routes: list, start_dt) -> int:
    """
    Compute one-way journey duration (minutes) from service_start_dt to
    the last route stop arrival, using stored arrival/departure times.
    Handles routes that span multiple days (e.g. 39+ hour journeys).
    """
    from datetime import timedelta

    if len(routes) < 2:
        return 8 * 60  # fallback

    # The true start anchor for measuring duration = first stop departure datetime
    cursor_dt = start_dt

    # The true start anchor for measuring duration = first stop departure datetime
    journey_anchor = cursor_dt

    for i in range(1, len(routes)):
        r    = routes[i]
        prev = routes[i - 1]

        # Travel time: prev departure → this arrival
        if prev.departure_time and r.arrival_time:
            dep_m = prev.departure_time.hour * 60 + prev.departure_time.minute
            arr_m = r.arrival_time.hour      * 60 + r.arrival_time.minute
            time_diff   = arr_m - dep_m
            if time_diff < 0:
                time_diff += 24 * 60   # single midnight crossing (each segment < 24 h)
            
            est_min = r.estimated_travel_min or time_diff
            days = round((est_min - time_diff) / (24 * 60))
            seg = time_diff + days * 24 * 60
        else:
            seg = 60  # fallback: 1 hr per segment

        cursor_dt += timedelta(minutes=seg)

        # Wait at intermediate stop before departing
        if i < len(routes) - 1 and r.arrival_time and r.departure_time:
            arr_m2 = r.arrival_time.hour   * 60 + r.arrival_time.minute
            dep_m2 = r.departure_time.hour * 60 + r.departure_time.minute
            wait   = dep_m2 - arr_m2
            if wait < 0:
                wait += 24 * 60
            cursor_dt += timedelta(minutes=wait)

    # Measure from first stop's departure to last stop's arrival (not from start_dt)
    # This ensures the preview shows: start_dt + one_way_min = correct last-stop arrival
    # by adding back the first-dep offset below in set_cycle_schedule
    return max(1, int((cursor_dt - journey_anchor).total_seconds() / 60))

# ────────────────────────────────────────────────────────────────
# API: Set cycle schedule for a train (Super Admin)
# ────────────────────────────────────────────────────────────────
@admin_bp.route('/super/trains/<int:train_id>/set-cycle-schedule', methods=['POST'])
@login_required
@super_admin_required
def set_cycle_schedule(train_id):
    """
    Save the train's service start datetime, return-wait period, and cycle toggle.
    Body JSON: {
        start_datetime: 'YYYY-MM-DDTHH:MM',  # datetime-local value
        return_wait_days: int,
        return_wait_hours: int,
        cycle_enabled: bool
    }
    Returns the saved values + a preview of the next 3 trip legs for the frontend.
    """
    from datetime import datetime as dt, timedelta

    train = Train.query.get_or_404(train_id)

    status = TrainStatus.query.filter_by(train_id=train_id).first()
    if status and status.journey_start_datetime and dt.now() >= status.journey_start_datetime:
        return jsonify({'ok': False, 'error': 'Journey has started. Schedule changes are locked.'}), 403

    body  = request.get_json(force=True, silent=True) or {}

    start_str         = (body.get('start_datetime') or '').strip()
    return_wait_days  = max(0, int(body.get('return_wait_days',  0) or 0))
    return_wait_hours = max(0, int(body.get('return_wait_hours', 0) or 0))
    cycle_enabled     = bool(body.get('cycle_enabled', False))

    # Parse the start datetime
    start_dt = None
    if start_str:
        try:
            start_dt = dt.fromisoformat(start_str)
        except ValueError:
            return jsonify({'ok': False, 'error': f'Invalid datetime: {start_str}'}), 400

    train.service_start_date = start_dt
    train.return_wait_days   = return_wait_days
    train.return_wait_hours  = return_wait_hours
    train.cycle_enabled      = cycle_enabled

    # Also sync the TrainStatus so the driver dashboard picks up the new start time
    if status and start_dt:
        status.journey_start_datetime = start_dt
        status.journey_direction = 'forward'

    db.session.commit()

    # Build a simple preview of the next 4 trip legs (forward/return alternating)
    preview = []
    if start_dt and cycle_enabled:
        # Estimate one-way journey duration from route stops
        routes = (TrainRoute.query
                  .filter_by(train_id=train_id)
                  .order_by(TrainRoute.stop_number)
                  .all())

        if len(routes) >= 2:
            one_way_min       = _compute_one_way_minutes(routes, start_dt)
            return_wait_total = timedelta(days=return_wait_days, hours=return_wait_hours)
            one_way_td        = timedelta(minutes=one_way_min)

            cursor = start_dt
            direction = 'forward'
            for leg in range(6):
                arrive_dt = cursor + one_way_td
                preview.append({
                    'leg':       leg + 1,
                    'direction': direction,
                    'departs':   cursor.strftime('%d %b %Y, %I:%M %p'),
                    'arrives':   arrive_dt.strftime('%d %b %Y, %I:%M %p'),
                })
                cursor    = arrive_dt + return_wait_total
                direction = 'return' if direction == 'forward' else 'forward'

    return jsonify({
        'ok': True,
        'train_id':           train_id,
        'service_start_date': start_dt.isoformat() if start_dt else None,
        'return_wait_days':   return_wait_days,
        'return_wait_hours':  return_wait_hours,
        'cycle_enabled':      cycle_enabled,
        'preview':            preview,
    })



# ══════════════════════════════════════════════════════════════════
# HISTORY SECTION
# ══════════════════════════════════════════════════════════════════
@admin_bp.route('/super/history')
@login_required
@super_admin_required
def history():
    # Users
    all_users = User.query.all()
    # "Current Users" -> Passengers with at least one CONFIRMED booking
    current_users = User.query.filter(User.bookings.any(status='CONFIRMED')).all()

    current_user_ids = {u.user_id for u in current_users}
    old_users = [u for u in all_users if u.user_id not in current_user_ids]

    # Station Masters
    all_masters = StationMaster.query.all()
    pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
    pool_id = pool.station_id if pool else None

    # Permanent: not in pool and not super_admin
    permanent_masters = [m for m in all_masters if not m.is_super_admin and m.station_id != pool_id]
    # Temporary: in pool and not super_admin
    temp_masters = [m for m in all_masters if not m.is_super_admin and m.station_id == pool_id]

    super_admins = [m for m in all_masters if m.is_super_admin]

    return render_template(
        'admin/super/history.html',
        all_users=all_users,
        current_users=current_users,
        old_users=old_users,
        all_masters=all_masters,
        permanent_masters=permanent_masters,
        temp_masters=temp_masters,
        super_admins=super_admins
    )


@admin_bp.route('/super/history/change-password', methods=['POST'])
@login_required
@super_admin_required
def change_password():
    from extensions import bcrypt

    user_type    = request.form.get('user_type')
    user_id      = request.form.get('target_id', type=int)
    new_password = request.form.get('new_password', '').strip()

    if not new_password or len(new_password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters long.'}), 400

    # Super admins may only change Station Master passwords, not passenger accounts
    if user_type == 'user':
        return jsonify({'ok': False, 'error': 'Access denied: Super Admin cannot change passenger passwords.'}), 403

    hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')

    if user_type == 'master':
        target = StationMaster.query.get(user_id)
        if not target:
            return jsonify({'ok': False, 'error': 'Station Master not found.'}), 404
        if target.is_super_admin:
            return jsonify({'ok': False, 'error': "Cannot change another Super Admin's password."}), 403

        target.password_hash = hashed
        db.session.commit()
        return jsonify({'ok': True, 'message': f"Password updated for {target.name}"})

    return jsonify({'ok': False, 'error': 'Invalid request.'}), 400
