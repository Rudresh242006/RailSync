from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from models import Train, TrainStatus, Notification, PlatformAllocation
from datetime import datetime
from services.train_service import allocate_platform

api_bp = Blueprint('api', __name__)



@api_bp.route('/train-status/<int:train_id>')
@login_required
def train_status(train_id):
    status = TrainStatus.query.filter_by(train_id=train_id).first()
    alloc = PlatformAllocation.query.filter_by(train_id=train_id).first()
    if not status:
        return jsonify({'error': 'No status found'}), 404
    return jsonify({
        'train_id': train_id,
        'current_station': status.current_station.station_name,
        'current_city': status.current_station.city,
        'delay_minutes': status.delay_minutes,
        'expected_arrival': status.expected_arrival.isoformat() if status.expected_arrival else None,
        'expected_departure': status.expected_departure.isoformat() if status.expected_departure else None,
        'last_updated': status.last_updated.isoformat(),
        'platform': alloc.platform.platform_number if alloc and alloc.platform else None
    })

from functools import wraps

def admin_api_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or getattr(current_user, 'role', None) not in ['admin', 'super_admin']:
            return jsonify({'error': 'Admin authorization required'}), 403
        return f(*args, **kwargs)
    return decorated

@api_bp.route('/allocate', methods=['POST'])
@login_required
@admin_api_required
def allocate():
    data = request.get_json() or {}
    result = allocate_platform(data)
    return jsonify(result)


@api_bp.route('/notifications/unread-count')
@login_required
def unread_count():
    if current_user.role != 'user':
        return jsonify({'count': 0})
    count = Notification.query.filter_by(
        user_id=current_user.user_id, is_read=False
    ).count()
    return jsonify({'count': count})


@api_bp.route('/notifications/latest')
@login_required
def latest_notifications():
    if current_user.role != 'user':
        return jsonify([])
    notes = Notification.query.filter_by(user_id=current_user.user_id)\
        .order_by(Notification.created_at.desc()).limit(5).all()
    return jsonify([{
        'id': n.notification_id,
        'message': n.message,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat(),
        'train_id': n.train_id
    } for n in notes])


@api_bp.route('/seat-availability/<int:train_id>/<journey_date>')
@login_required
def seat_availability(train_id, journey_date):
    from models import Booking
    booked = {b.seat_number for b in Booking.query.filter_by(
        train_id=train_id, journey_date=journey_date
    ).filter(Booking.status != 'CANCELLED').all()}
    rows = range(1, 10)
    cols = ['A', 'B', 'C', 'D', 'E', 'F']
    seat_map = {}
    for r in rows:
        for c in cols:
            seat = f"{r}{c}"
            seat_map[seat] = {
                'booked': seat in booked,
                'window': c in ['A', 'F']
            }
    return jsonify(seat_map)
