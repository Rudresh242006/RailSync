from extensions import db, login_manager
from flask_login import UserMixin
from datetime import datetime
from enum import Enum as PyEnum


@login_manager.user_loader
def load_user(user_id):
    if user_id.startswith('master_'):
        return StationMaster.query.get(int(user_id.split('_')[1]))
    return User.query.get(int(user_id.split('_')[1]))


class User(db.Model, UserMixin):
    __tablename__ = 'User'
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bookings = db.relationship('Booking', backref='user', lazy=True)
    payments = db.relationship('Payment', backref='user', lazy=True)
    notifications = db.relationship('Notification', backref='user', lazy=True)

    def get_id(self):
        return f'user_{self.user_id}'

    @property
    def role(self):
        return 'user'


class Station(db.Model):
    __tablename__ = 'Station'
    station_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    station_name = db.Column(db.String(150), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    state = db.Column(db.String(100), nullable=False)
    is_junction = db.Column(db.Boolean, default=False)   # ← NEW: junction stations wait 1hr


class StationMaster(db.Model, UserMixin):
    __tablename__ = 'StationMaster'
    master_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id', ondelete='CASCADE'), nullable=False)

    is_super_admin = db.Column(db.Boolean, default=False)
    is_on_leave = db.Column(db.Boolean, default=False)
    station = db.relationship('Station', backref='masters')

    def get_id(self):
        return f'master_{self.master_id}'

    @property
    def role(self):
        return 'super_admin' if self.is_super_admin else 'admin'


class Train(db.Model):
    __tablename__ = 'Train'
    train_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    train_number = db.Column(db.String(10), unique=True, nullable=False)
    train_name = db.Column(db.String(150), nullable=False)
    total_seats = db.Column(db.Integer, nullable=False)

    routes = db.relationship('TrainRoute', backref='train', lazy=True, cascade='all, delete-orphan')
    statuses = db.relationship('TrainStatus', backref='train', lazy=True, cascade='all, delete-orphan')
    bookings = db.relationship('Booking', backref='train', lazy=True)
    notifications = db.relationship('Notification', backref='train', lazy=True)
    platform_allocations = db.relationship('PlatformAllocation', backref='train', lazy=True, cascade='all, delete-orphan')


class Platform(db.Model):
    __tablename__ = 'Platform'
    platform_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id', ondelete='CASCADE'), nullable=False)
    platform_number = db.Column(db.String(10), nullable=False)
    is_available = db.Column(db.Boolean, default=True)

    __table_args__ = (db.UniqueConstraint('station_id', 'platform_number'),)
    station = db.relationship('Station', backref='platforms')
    allocations = db.relationship('PlatformAllocation', backref='platform', lazy=True)


class TrainRoute(db.Model):
    __tablename__ = 'TrainRoute'
    route_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    train_id = db.Column(db.Integer, db.ForeignKey('Train.train_id', ondelete='CASCADE'), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id', ondelete='CASCADE'), nullable=False)
    arrival_time = db.Column(db.Time, nullable=True)
    departure_time = db.Column(db.Time, nullable=True)
    stop_number = db.Column(db.Integer, nullable=False)
    distance_km = db.Column(db.Float, nullable=True)          # ← NEW: km from previous stop
    estimated_travel_min = db.Column(db.Integer, nullable=True)  # ← NEW: travel mins from prev stop

    __table_args__ = (db.UniqueConstraint('train_id', 'stop_number'),)
    station = db.relationship('Station', backref='routes')


class TrainStatus(db.Model):
    __tablename__ = 'TrainStatus'
    status_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    train_id = db.Column(db.Integer, db.ForeignKey('Train.train_id', ondelete='CASCADE'), nullable=False)
    current_station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id', ondelete='CASCADE'), nullable=False)
    expected_arrival = db.Column(db.DateTime, nullable=True)
    expected_departure = db.Column(db.DateTime, nullable=True)
    delay_minutes = db.Column(db.Integer, default=0)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    current_station = db.relationship('Station', backref='train_statuses')


class Booking(db.Model):
    __tablename__ = 'Booking'
    booking_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('User.user_id'), nullable=False)
    train_id = db.Column(db.Integer, db.ForeignKey('Train.train_id'), nullable=False)
    source_station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id'), nullable=False)
    destination_station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id'), nullable=False)
    booking_date = db.Column(db.DateTime, default=datetime.utcnow)
    journey_date = db.Column(db.Date, nullable=False)
    seat_number = db.Column(db.String(10), nullable=True)
    status = db.Column(db.Enum('CONFIRMED', 'WAITLISTED', 'CANCELLED'), default='CONFIRMED')

    __table_args__ = (db.UniqueConstraint('train_id', 'journey_date', 'seat_number', name='uq_train_date_seat'),)

    source_station = db.relationship('Station', foreign_keys=[source_station_id])
    destination_station = db.relationship('Station', foreign_keys=[destination_station_id])
    payment = db.relationship('Payment', backref='booking', uselist=False)
    change_requests = db.relationship('PassengerChangeRequest', backref='booking', lazy=True)

    @property
    def is_window_seat(self):
        if self.seat_number:
            return self.seat_number.upper().endswith(('A', 'F'))
        return False


class Payment(db.Model):
    __tablename__ = 'Payment'
    payment_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('Booking.booking_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('User.user_id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_method = db.Column(db.Enum('UPI', 'CARD', 'NETBANKING', 'WALLET', 'CASH'), nullable=False)
    transaction_id = db.Column(db.String(100), unique=True, nullable=True)
    payment_status = db.Column(db.Enum('PENDING', 'SUCCESS', 'FAILED', 'REFUNDED'), default='PENDING')
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)


class PlatformAllocation(db.Model):
    __tablename__ = 'PlatformAllocation'
    allocation_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    train_id = db.Column(db.Integer, db.ForeignKey('Train.train_id', ondelete='CASCADE'), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id', ondelete='CASCADE'), nullable=False)
    platform_id = db.Column(db.Integer, db.ForeignKey('Platform.platform_id', ondelete='CASCADE'), nullable=False)
    arrival_time = db.Column(db.DateTime, nullable=False)
    departure_time = db.Column(db.DateTime, nullable=False)

    station = db.relationship('Station', backref='allocations')


class Notification(db.Model):
    __tablename__ = 'Notification'
    notification_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('User.user_id', ondelete='CASCADE'), nullable=False)
    train_id = db.Column(db.Integer, db.ForeignKey('Train.train_id', ondelete='CASCADE'), nullable=True)
    message = db.Column(db.Text, nullable=False)
    notif_type = db.Column(db.String(30), default='general')  # 'general', 'change_request', 'delay'
    extra_id = db.Column(db.Integer, nullable=True)            # e.g. PassengerChangeRequest.request_id
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Passenger Change Request (admin proposes changes, passenger approves)
# ─────────────────────────────────────────────────────────────────────────────
class PassengerChangeRequest(db.Model):
    __tablename__ = 'PassengerChangeRequest'
    request_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('Booking.booking_id', ondelete='CASCADE'), nullable=False)
    admin_master_id = db.Column(db.Integer, db.ForeignKey('StationMaster.master_id'), nullable=False)
    admin_station_id = db.Column(db.Integer, db.ForeignKey('Station.station_id'), nullable=False)
    field_changed = db.Column(db.String(50), nullable=False)   # 'journey_date', 'seat_number', 'status'
    old_value = db.Column(db.String(255), nullable=True)
    new_value = db.Column(db.String(255), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    status = db.Column(db.Enum('PENDING', 'APPROVED', 'REJECTED'), default='PENDING')
    notification_id = db.Column(db.Integer, db.ForeignKey('Notification.notification_id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime, nullable=True)

    admin = db.relationship('StationMaster', backref='change_requests')
    admin_station = db.relationship('Station', foreign_keys=[admin_station_id])


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Chat Messages (admin <-> super admin)
# ─────────────────────────────────────────────────────────────────────────────
class ChatMessage(db.Model):
    __tablename__ = 'ChatMessage'
    message_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('StationMaster.master_id', ondelete='CASCADE'), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_broadcast = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship('StationMaster', backref='sent_chat_messages')
    recipients = db.relationship('ChatRecipient', backref='message', cascade='all, delete-orphan')


class ChatRecipient(db.Model):
    __tablename__ = 'ChatRecipient'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    message_id = db.Column(db.Integer, db.ForeignKey('ChatMessage.message_id', ondelete='CASCADE'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('StationMaster.master_id', ondelete='CASCADE'), nullable=False)
    is_read = db.Column(db.Boolean, default=False)

    recipient = db.relationship('StationMaster', backref='received_chat_messages')

    __table_args__ = (db.UniqueConstraint('message_id', 'recipient_id'),)


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Master Notification (for super admin alerts from station masters)
# ─────────────────────────────────────────────────────────────────────────────
class MasterNotification(db.Model):
    __tablename__ = 'MasterNotification'
    notif_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('StationMaster.master_id', ondelete='CASCADE'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('StationMaster.master_id', ondelete='CASCADE'), nullable=True)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    notif_type = db.Column(db.String(30), default='general')   # 'delay_alert', 'general', 'system'
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    recipient = db.relationship('StationMaster', foreign_keys=[recipient_id], backref='master_notifications')
    sender = db.relationship('StationMaster', foreign_keys=[sender_id])
