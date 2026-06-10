from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from functools import wraps
from extensions import db
from models import (User, Train, TrainRoute, TrainStatus, Booking,
                    Payment, Notification, Station, Platform, PlatformAllocation,
                    PassengerChangeRequest)
from datetime import date, datetime
import uuid
import random
import string
import math

def haversine(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    straight_km = R * c
    return round(straight_km * 1.30, 1)

user_bp = Blueprint('user', __name__)


def user_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'user':
            flash('Access denied.', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def generate_seat(train_id, journey_date):
    """Generate a random seat number (1-9 rows, A-F seats)."""
    booked = set()
    for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all():
        if b.seat_number:
            booked.update([s.strip() for s in b.seat_number.split(',')])
            
    all_seats = [f"{r}{c}" for r in range(1, 10) for c in 'ABCDEF']
    available = [s for s in all_seats if s not in booked]
    return random.choice(available) if available else None


@user_bp.route('/dashboard')
@login_required
@user_required
def dashboard():
    bookings = Booking.query.filter_by(user_id=current_user.user_id)\
        .order_by(Booking.booking_date.desc()).limit(5).all()
    unread = Notification.query.filter_by(
        user_id=current_user.user_id, is_read=False
    ).count()
    return render_template('user/dashboard.html', bookings=bookings, unread_count=unread)


@user_bp.route('/search', methods=['GET', 'POST'])
@login_required
@user_required
def search():
    trains = []
    stations = Station.query.order_by(Station.station_name).all()
    src_id = None
    dst_id = None
    journey_date_str = None
    
    if request.method == 'GET' and request.args.get('source_station_id'):
        src_id = request.args.get('source_station_id', type=int)
        dst_id = request.args.get('destination_station_id', type=int)
        journey_date_str = request.args.get('journey_date')
        
        try:
            journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            flash('Invalid date selected.', 'warning')
            return redirect(url_for('user.search'))

        src_routes = TrainRoute.query.filter_by(station_id=src_id).all()
        src_train_ids = {r.train_id: r.stop_number for r in src_routes}

        dst_routes = TrainRoute.query.filter_by(station_id=dst_id).all()
        for r in dst_routes:
            if r.train_id in src_train_ids and src_train_ids[r.train_id] < r.stop_number:
                train = Train.query.get(r.train_id)
                status = TrainStatus.query.filter_by(train_id=train.train_id).first()
                
                # Calculate distance between src and dst
                all_routes = TrainRoute.query.filter_by(train_id=train.train_id).order_by(TrainRoute.stop_number).all()
                total_distance = 0.0
                in_segment = False
                prev_station = None
                for route in all_routes:
                    if route.station_id == src_id:
                        in_segment = True
                        prev_station = route.station
                    elif in_segment:
                        dist = route.distance_km
                        if not dist:
                            if prev_station and route.station:
                                dist = haversine(
                                    prev_station.latitude, prev_station.longitude,
                                    route.station.latitude, route.station.longitude
                                )
                        total_distance += dist or 0.0
                        prev_station = route.station
                        if route.station_id == dst_id:
                            break
                            
                # Fallback to 100 if distance is 0 or missing, else 1 INR per km
                ticket_price = total_distance if total_distance > 0 else 100.0
                ticket_price = round(ticket_price, 2)
                
                booked_seats = set()
                for b in Booking.query.filter_by(
                    train_id=train.train_id, journey_date=journey_date
                ).filter(Booking.status != 'CANCELLED').all():
                    if b.seat_number:
                        booked_seats.update([s.strip() for s in b.seat_number.split(',')])
                        
                available_count = train.total_seats - len(booked_seats)

                trains.append({
                    'train': train,
                    'status': status,
                    'src_id': src_id,
                    'dst_id': dst_id,
                    'journey_date': journey_date_str,
                    'available_seats': available_count,
                    'booked_seats': booked_seats,
                    'total_distance': total_distance,
                    'ticket_price': ticket_price
                })
    return render_template('user/search.html', trains=trains, stations=stations, src_id=src_id, dst_id=dst_id, journey_date=journey_date_str)


@user_bp.route('/book', methods=['POST'])
@login_required
@user_required
def book():
    train_id = request.form.get('train_id', type=int)
    src_id = request.form.get('source_station_id', type=int)
    dst_id = request.form.get('destination_station_id', type=int)
    journey_date_str = request.form.get('journey_date')
    selected_seats_raw = request.form.get('selected_seats', '').strip()
    payment_method = request.form.get('payment_method', 'UPI')
    
    # Calculate distance for pricing dynamically
    all_routes = TrainRoute.query.filter_by(train_id=train_id).order_by(TrainRoute.stop_number).all()
    total_distance = 0.0
    in_segment = False
    prev_station = None
    for route in all_routes:
        if route.station_id == src_id:
            in_segment = True
            prev_station = route.station
        elif in_segment:
            dist = route.distance_km
            if not dist:
                if prev_station and route.station:
                    dist = haversine(
                        prev_station.latitude, prev_station.longitude,
                        route.station.latitude, route.station.longitude
                    )
            total_distance += dist or 0.0
            prev_station = route.station
            if route.station_id == dst_id:
                break
    amount = float(total_distance) if total_distance > 0 else 100.0

    journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()

    booked = set()
    for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all():
        if b.seat_number:
            booked.update([s.strip() for s in b.seat_number.split(',')])

    selected_seats = [s.strip() for s in selected_seats_raw.split(',') if s.strip()]
    
    # Verify availability
    for s in selected_seats:
        if s in booked:
            flash(f'Sorry, seat {s} was just taken. Please try again.', 'danger')
            return redirect(url_for('user.search'))
            
    if not selected_seats:
        seat = generate_seat(train_id, journey_date)
        if not seat:
            flash('No seats available.', 'danger')
            return redirect(url_for('user.search'))
        selected_seats = [seat]

    final_seat_string = ", ".join(selected_seats)
    total_amount = amount * len(selected_seats)

    from sqlalchemy.exc import IntegrityError
    for attempt in range(3):
        try:
            booking = Booking(
                user_id=current_user.user_id,
                train_id=train_id,
                source_station_id=src_id,
                destination_station_id=dst_id,
                journey_date=journey_date,
                seat_number=final_seat_string,
                status='CONFIRMED'
            )
            db.session.add(booking)
            db.session.flush()

            txn_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            payment = Payment(
                booking_id=booking.booking_id,
                user_id=current_user.user_id,
                amount=total_amount,
                payment_method=payment_method,
                transaction_id=txn_id,
                payment_status='PENDING'
            )
            db.session.add(payment)
            db.session.commit()

            return redirect(url_for('user.payment_page', payment_id=payment.payment_id))

        except IntegrityError:
            db.session.rollback()
            seat = generate_seat(train_id, journey_date)

    flash('Sorry, the seat you requested was just taken by another user. Please try again.', 'danger')
    return redirect(url_for('user.search'))


@user_bp.route('/payment/<int:payment_id>')
@login_required
@user_required
def payment_page(payment_id):
    payment = Payment.query.filter_by(
        payment_id=payment_id, user_id=current_user.user_id
    ).first_or_404()
    
    if payment.payment_status != 'PENDING':
        flash('Payment already processed.', 'info')
        return redirect(url_for('user.my_bookings'))
        
    return render_template('user/payment.html', payment=payment)


@user_bp.route('/payment/process/<int:payment_id>', methods=['POST'])
@login_required
@user_required
def process_payment(payment_id):
    payment = Payment.query.filter_by(
        payment_id=payment_id, user_id=current_user.user_id
    ).first_or_404()
    
    if payment.payment_status == 'PENDING':
        action = request.form.get('action', 'success')
        if action == 'success':
            payment.payment_status = 'SUCCESS'
            payment.booking.status = 'CONFIRMED' if payment.booking.seat_number else 'WAITLISTED'
            db.session.commit()
            flash(f'Payment successful! Transaction ID: {payment.transaction_id}', 'success')
        else:
            payment.payment_status = 'FAILED'
            payment.booking.status = 'CANCELLED'
            db.session.commit()
            flash('Payment failed. Booking cancelled.', 'danger')
            
    return redirect(url_for('user.my_bookings'))


@user_bp.route('/bookings')
@login_required
@user_required
def my_bookings():
    bookings = Booking.query.filter_by(user_id=current_user.user_id)\
        .order_by(Booking.booking_date.desc()).all()
    return render_template('user/bookings.html', bookings=bookings)


@user_bp.route('/cancel/<int:booking_id>', methods=['POST'])
@login_required
@user_required
def cancel_booking(booking_id):
    booking = Booking.query.filter_by(
        booking_id=booking_id, user_id=current_user.user_id
    ).first_or_404()
    booking.status = 'CANCELLED'
    if booking.payment:
        booking.payment.payment_status = 'REFUNDED'
    db.session.commit()
    flash('Booking cancelled and refund initiated.', 'info')
    return redirect(url_for('user.my_bookings'))


@user_bp.route('/track/<int:train_id>')
@login_required
@user_required
def track(train_id):
    train = Train.query.get_or_404(train_id)
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    routes = (TrainRoute.query
              .filter_by(train_id=train_id)
              .order_by(TrainRoute.stop_number)
              .all())
    allocation = PlatformAllocation.query.filter_by(train_id=train_id).first()
    return render_template('user/track.html', train=train, status=status,
                           routes=routes, allocation=allocation)


@user_bp.route('/api/track-status/<int:train_id>')
@login_required
@user_required
def track_status_api(train_id):
    """Lightweight JSON endpoint for AJAX polling — no full page reload needed."""
    train = Train.query.get(train_id)
    if not train:
        return jsonify({'error': 'Not found'}), 404
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    allocation = PlatformAllocation.query.filter_by(train_id=train_id).first()
    return jsonify({
        'train_id':       train_id,
        'state':          status.state if status else 'stopped',
        'delay_minutes':  status.delay_minutes if status else 0,
        'current_station_id': status.current_station_id if status else None,
        'current_station_name': status.current_station.station_name if status else None,
        'current_station_city': status.current_station.city if status else None,
        'expected_arrival': status.expected_arrival.strftime('%d %b, %H:%M') if (status and status.expected_arrival) else None,
        'last_updated':   status.last_updated.strftime('%H:%M:%S') if status else None,
        'platform_number': allocation.platform.platform_number if allocation else None,
        'platform_arrival': allocation.arrival_time.strftime('%H:%M') if allocation else None,
        'platform_departure': allocation.departure_time.strftime('%H:%M') if allocation else None,
    })


@user_bp.route('/notifications')
@login_required
@user_required
def notifications():
    notes = Notification.query.filter_by(user_id=current_user.user_id)\
        .order_by(Notification.created_at.desc()).all()
    # mark all as read
    for n in notes:
        n.is_read = True
    db.session.commit()
    return render_template('user/notifications.html', notifications=notes)


@user_bp.route('/seats/<int:train_id>/<journey_date>')
@login_required
@user_required
def seat_map(train_id, journey_date):
    train = Train.query.get_or_404(train_id)
    booked = set()
    for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all():
        if b.seat_number:
            booked.update([s.strip() for s in b.seat_number.split(',')])
            
    rows = range(1, 10)
    cols = ['A', 'B', 'C', 'D', 'E', 'F']
    return render_template('user/seat_map.html', train=train, booked=booked,
                           rows=rows, cols=cols, journey_date=journey_date)


# ══════════════════════════════════════════════════════════════════
# PASSENGER CHANGE REQUEST — Approve / Reject
# ══════════════════════════════════════════════════════════════════
@user_bp.route('/api/pending-changes')
@login_required
@user_required
def pending_changes():
    """Return pending change requests for the current user (for popup)."""
    pending = PassengerChangeRequest.query.join(Booking).filter(
        Booking.user_id == current_user.user_id,
        PassengerChangeRequest.status == 'PENDING'
    ).order_by(PassengerChangeRequest.created_at.desc()).all()

    result = []
    for cr in pending:
        b = cr.booking
        field_label = {
            'journey_date': 'Journey Date',
            'seat_number': 'Seat Number',
            'status': 'Booking Status'
        }.get(cr.field_changed, cr.field_changed)
        result.append({
            'request_id': cr.request_id,
            'booking_id': cr.booking_id,
            'train': b.train.train_name if b.train else '—',
            'train_number': b.train.train_number if b.train else '—',
            'field': field_label,
            'old_value': cr.old_value,
            'new_value': cr.new_value,
            'reason': cr.reason or 'Not specified',
            'station': cr.admin_station.station_name if cr.admin_station else '—',
            'created_at': cr.created_at.strftime('%d %b %H:%M')
        })
    return jsonify(result)


@user_bp.route('/approve-change/<int:request_id>', methods=['POST'])
@login_required
@user_required
def approve_change(request_id):
    cr = PassengerChangeRequest.query.get_or_404(request_id)
    booking = Booking.query.filter_by(
        booking_id=cr.booking_id, user_id=current_user.user_id
    ).first_or_404()

    if cr.status != 'PENDING':
        flash('This request has already been responded to.', 'info')
        return redirect(url_for('user.notifications'))

    # Apply the change
    if cr.field_changed == 'journey_date':
        try:
            booking.journey_date = datetime.strptime(cr.new_value, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid date in change request.', 'danger')
            return redirect(url_for('user.notifications'))
    elif cr.field_changed == 'seat_number':
        booking.seat_number = cr.new_value
    elif cr.field_changed == 'status':
        if cr.new_value in ('CONFIRMED', 'WAITLISTED', 'CANCELLED'):
            booking.status = cr.new_value

    cr.status = 'APPROVED'
    cr.responded_at = datetime.utcnow()

    # Mark the notification as read
    if cr.notification_id:
        notif = Notification.query.get(cr.notification_id)
        if notif:
            notif.is_read = True

    db.session.commit()
    flash('Change approved and applied to your booking.', 'success')
    return redirect(url_for('user.my_bookings'))


@user_bp.route('/reject-change/<int:request_id>', methods=['POST'])
@login_required
@user_required
def reject_change(request_id):
    cr = PassengerChangeRequest.query.get_or_404(request_id)
    Booking.query.filter_by(
        booking_id=cr.booking_id, user_id=current_user.user_id
    ).first_or_404()  # security: ensure it's this user's booking

    if cr.status != 'PENDING':
        flash('This request has already been responded to.', 'info')
        return redirect(url_for('user.notifications'))

    cr.status = 'REJECTED'
    cr.responded_at = datetime.utcnow()

    if cr.notification_id:
        notif = Notification.query.get(cr.notification_id)
        if notif:
            notif.is_read = True

    db.session.commit()
    flash('Change request rejected. Your booking remains unchanged.', 'info')
    return redirect(url_for('user.notifications'))
