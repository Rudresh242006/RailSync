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
    booked = {b.seat_number for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all()}
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
    if request.method == 'POST':
        src = request.form.get('source_station_id', type=int)
        dst = request.form.get('destination_station_id', type=int)
        journey_date_str = request.form.get('journey_date')
        
        try:
            journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            flash('Invalid date selected.', 'warning')
            return redirect(url_for('user.search'))

        src_routes = TrainRoute.query.filter_by(station_id=src).all()
        src_train_ids = {r.train_id: r.stop_number for r in src_routes}

        dst_routes = TrainRoute.query.filter_by(station_id=dst).all()
        for r in dst_routes:
            if r.train_id in src_train_ids and src_train_ids[r.train_id] < r.stop_number:
                train = Train.query.get(r.train_id)
                status = TrainStatus.query.filter_by(train_id=train.train_id).first()
                trains.append({
                    'train': train,
                    'status': status,
                    'src_id': src,
                    'dst_id': dst,
                    'journey_date': journey_date_str,
                    'available_seats': train.total_seats - Booking.query.filter_by(
                        train_id=train.train_id, journey_date=journey_date
                    ).filter(Booking.status != 'CANCELLED').count()
                })
    return render_template('user/search.html', trains=trains, stations=stations)


@user_bp.route('/book', methods=['POST'])
@login_required
@user_required
def book():
    train_id = request.form.get('train_id', type=int)
    src_id = request.form.get('source_station_id', type=int)
    dst_id = request.form.get('destination_station_id', type=int)
    journey_date_str = request.form.get('journey_date')
    seat_pref = request.form.get('seat_preference', 'any')  # 'window' or 'any'
    payment_method = request.form.get('payment_method', 'UPI')
    amount = request.form.get('amount', type=float, default=500.0)

    journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()

    if seat_pref == 'window':
        # Window = A or F seats
        all_window = [f"{r}{c}" for r in range(1, 10) for c in ['A', 'F']]
        booked = {b.seat_number for b in Booking.query.filter_by(
            train_id=train_id, journey_date=journey_date
        ).filter(Booking.status != 'CANCELLED').all()}
        available_window = [s for s in all_window if s not in booked]
        seat = random.choice(available_window) if available_window else generate_seat(train_id, journey_date)
    else:
        seat = generate_seat(train_id, journey_date)

    from sqlalchemy.exc import IntegrityError
    for attempt in range(3):
        try:
            booking = Booking(
                user_id=current_user.user_id,
                train_id=train_id,
                source_station_id=src_id,
                destination_station_id=dst_id,
                journey_date=journey_date,
                seat_number=seat,
                status='CONFIRMED' if seat else 'WAITLISTED'
            )
            db.session.add(booking)
            db.session.flush()

            txn_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            payment = Payment(
                booking_id=booking.booking_id,
                user_id=current_user.user_id,
                amount=amount,
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
    routes = TrainRoute.query.filter_by(train_id=train_id).order_by(TrainRoute.stop_number).all()
    allocation = PlatformAllocation.query.filter_by(train_id=train_id).first()
    return render_template('user/track.html', train=train, status=status,
                           routes=routes, allocation=allocation)


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
    booked = {b.seat_number for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all()}
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
