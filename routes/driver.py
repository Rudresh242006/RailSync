from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from models import TrainDriver, TrainStatus, TrainRoute, PlatformAllocation, Train
from extensions import db, bcrypt
from datetime import datetime, timedelta
from functools import wraps

driver_bp = Blueprint('driver', __name__)

def driver_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if getattr(current_user, 'role', None) != 'driver':
            flash('Access denied. Drivers only.', 'danger')
            return redirect(url_for('auth.admin_login', role='driver'))
        return f(*args, **kwargs)
    return decorated_function

@driver_bp.route('/login', methods=['GET', 'POST'])
def login():
    return redirect(url_for('auth.admin_login', role='driver'))

@driver_bp.route('/dashboard')
@login_required
@driver_required
def dashboard():
    train = current_user.train
    status = TrainStatus.query.filter_by(train_id=train.train_id).first()
    
    if not status:
        return render_template('driver/dashboard.html', train=train, error="No active status for this train.")
        
    routes = TrainRoute.query.filter_by(train_id=train.train_id).order_by(TrainRoute.stop_number).all()
    allocs = PlatformAllocation.query.filter_by(train_id=train.train_id).all()
    allocations = {a.station_id: a.platform for a in allocs}
    
    direction = status.journey_direction
    now = datetime.now()
    
    route_details = []
    current_detail = None
    next_detail = None
    
    if status.journey_start_datetime and direction in ['forward', 'reverse']:
        ordered_routes = list(routes)
        if direction == 'reverse':
            ordered_routes.reverse()
            
        cursor_dt = status.journey_start_datetime
        
        # For forward journeys, ensure the start time exactly matches the scheduled departure of the first station
        if direction == 'forward' and ordered_routes and ordered_routes[0].departure_time:
            cursor_dt = cursor_dt.replace(
                hour=ordered_routes[0].departure_time.hour,
                minute=ordered_routes[0].departure_time.minute,
                second=0,
                microsecond=0
            )
        
        for i, r in enumerate(ordered_routes):
            travel_m = 0
            if i > 0:
                prev_r = ordered_routes[i-1]
                if direction == 'forward':
                    if r.arrival_time and prev_r.departure_time:
                        arr_m = r.arrival_time.hour * 60 + r.arrival_time.minute
                        dep_m = prev_r.departure_time.hour * 60 + prev_r.departure_time.minute
                        time_diff = arr_m - dep_m
                        if time_diff < 0: time_diff += 24*60
                        
                        est_min = r.estimated_travel_min or time_diff
                        days = round((est_min - time_diff) / (24 * 60))
                        travel_m = time_diff + days * 24 * 60
                    else:
                        travel_m = r.estimated_travel_min or 60
                else: # Reverse
                    if prev_r.arrival_time and r.departure_time:
                        arr_m = prev_r.arrival_time.hour * 60 + prev_r.arrival_time.minute
                        dep_m = r.departure_time.hour * 60 + r.departure_time.minute
                        time_diff = arr_m - dep_m
                        if time_diff < 0: time_diff += 24*60
                        
                        est_min = r.estimated_travel_min or time_diff
                        days = round((est_min - time_diff) / (24 * 60))
                        travel_m = time_diff + days * 24 * 60
                    else:
                        travel_m = r.estimated_travel_min or 60
                    
            cursor_dt += timedelta(minutes=travel_m)
            arrival_dt = cursor_dt
            
            wait_m = 5
            if r.arrival_time and r.departure_time:
                arr_m = r.arrival_time.hour * 60 + r.arrival_time.minute
                dep_m = r.departure_time.hour * 60 + r.departure_time.minute
                wait_m = dep_m - arr_m
                if wait_m < 0: wait_m += 24*60
                
            if i == 0:
                wait_m = 0 # Origin departs immediately
                
            departure_dt = cursor_dt + timedelta(minutes=wait_m)
            
            actual_arr = arrival_dt + timedelta(minutes=status.delay_minutes)
            actual_dep = departure_dt + timedelta(minutes=status.delay_minutes)
            
            platform = allocations.get(r.station_id)
            
            route_details.append({
                'route': r,
                'station': r.station,
                'platform': platform.platform_number if platform else 'TBD',
                'actual_arrival': actual_arr,
                'actual_departure': actual_dep
            })
            
            cursor_dt = departure_dt
            
        # Find current station in the ordered list
        current_idx = next((i for i, d in enumerate(route_details) if d['route'].station_id == status.current_station_id), -1)
        
        if current_idx != -1:
            current_detail = route_details[current_idx]
            
            # Auto-Start / Auto-Depart Logic
            # If train is stopped, and we have passed the departure time for this station, it automatically departs
            if status.state == 'stopped' and now >= current_detail['actual_departure']:
                if current_idx < len(route_details) - 1:
                    status.state = 'en_route'
                    db.session.commit()
            
            next_detail = route_details[current_idx + 1] if current_idx + 1 < len(route_details) else None
            
    return render_template('driver/dashboard.html', 
                           train=train, 
                           status=status,
                           route_details=route_details,
                           current_detail=current_detail,
                           next_detail=next_detail,
                           now=now)


@driver_bp.route('/api/arrive', methods=['POST'])
@login_required
@driver_required
def arrive_station():
    status = TrainStatus.query.filter_by(train_id=current_user.train_id).first()
    if not status:
        return jsonify({'ok': False, 'error': 'Status not found'}), 404
        
    if status.state == 'stopped':
        return jsonify({'ok': False, 'error': 'Already stopped'}), 400
        
    # We need to know the next route
    routes = TrainRoute.query.filter_by(train_id=current_user.train_id).order_by(TrainRoute.stop_number).all()
    direction = status.journey_direction
    
    ordered_routes = list(routes)
    if direction == 'reverse':
        ordered_routes.reverse()
        
    current_idx = next((i for i, r in enumerate(ordered_routes) if r.station_id == status.current_station_id), -1)
    
    if current_idx == -1 or current_idx + 1 >= len(ordered_routes):
        return jsonify({'ok': False, 'error': 'No next station'}), 400
        
    next_route = ordered_routes[current_idx + 1]
    
    status.current_station_id = next_route.station_id
    status.state = 'stopped'
    status.current_departure_time = None  # clear stale departure time
    
    # If this is the final destination, we trigger turnaround / cycle logic
    if current_idx + 1 == len(ordered_routes) - 1:
        train = current_user.train
        if train.cycle_enabled:
            # 1 Day Rest Logic: calculate return wait minus delays
            wait_td = timedelta(days=train.return_wait_days, hours=train.return_wait_hours)
            delay_td = timedelta(minutes=status.delay_minutes)
            
            # The wait absorbs the delay. If delay > wait, it just departs immediately (or we could enforce min wait)
            actual_wait = wait_td - delay_td
            if actual_wait.total_seconds() < 0:
                actual_wait = timedelta(seconds=0)
                
            status.journey_direction = 'reverse' if direction == 'forward' else 'forward'
            status.journey_start_datetime = datetime.now() + actual_wait
            status.delay_minutes = 0  # Delay is absorbed, reset to 0
        else:
            status.journey_direction = 'idle'
            status.journey_start_datetime = None
            status.delay_minutes = 0
            
    db.session.commit()
    
    return jsonify({'ok': True})

