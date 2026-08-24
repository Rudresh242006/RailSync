from flask import Flask, render_template
import models

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_socketio import SocketIO
from flask_compress import Compress
import os
from dotenv import load_dotenv
load_dotenv()
from extensions import db, bcrypt, login_manager, socketio, csrf, limiter

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
    if not app.config['SECRET_KEY']:
        raise RuntimeError("SECRET_KEY is not set in .env")
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
    if not app.config['SQLALCHEMY_DATABASE_URI']:
        raise RuntimeError("DATABASE_URL is not set in .env")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Gzip compression for all JSON + HTML responses (cuts payload 70-80%)
    Compress(app)

    db.init_app(app)
    bcrypt.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Restrict SocketIO CORS origins
    allowed_origins_env = os.environ.get('ALLOWED_ORIGIN', 'http://localhost:5000,http://127.0.0.1:5000')
    allowed_origins = [o.strip() for o in allowed_origins_env.split(',') if o.strip()]
    socketio.init_app(app, cors_allowed_origins=allowed_origins)


    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'

    from routes.auth import auth_bp
    from routes.user import user_bp
    from routes.admin import admin_bp
    from routes.api import api_bp
    from routes.driver import driver_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp, url_prefix='/user')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(driver_bp, url_prefix='/driver')

    @app.route('/manifest.json')
    def manifest():
        return app.send_static_file('manifest.json')

    @app.route('/sw.js')
    def service_worker():
        response = app.send_static_file('sw.js')
        response.headers['Content-Type'] = 'application/javascript'
        response.headers['Service-Worker-Allowed'] = '/'
        return response

    @app.route('/offline')
    def offline():
        return render_template('offline.html')


    @socketio.on('join')
    def on_join(data):
        from flask_socketio import join_room
        room = data.get('room')
        if room:
            join_room(room)

    with app.app_context():
        db.create_all()
        
    def train_progress_task(app):
        with app.app_context():
            from extensions import socketio
            from models import TrainStatus
            from datetime import datetime
            while True:
                try:
                    statuses = TrainStatus.query.filter(TrainStatus.journey_direction.in_(['forward', 'reverse'])).all()
                    now = datetime.now()
                    for status in statuses:
                        details = status.get_route_details()
                        if not details: continue
                        
                        current_idx = next((i for i, d in enumerate(details) if d['station_id'] == status.current_station_id), -1)
                        if current_idx == -1: continue
                        
                        current_detail = details[current_idx]
                        
                        # Auto-Depart Logic
                        if status.state == 'stopped' and now >= current_detail['actual_departure']:
                            if current_idx < len(details) - 1:
                                status.state = 'en_route'
                                status.current_departure_time = current_detail['actual_departure']
                                db.session.commit()
                                print(f"[Auto-Depart] Train {status.train_id} departed {status.current_station_id}")
                                
                        # Auto-Arrive Logic
                        elif status.state == 'en_route':
                            next_detail = details[current_idx + 1] if current_idx + 1 < len(details) else None
                            if next_detail and now >= next_detail['actual_arrival']:
                                status.current_station_id = next_detail['station_id']
                                status.state = 'stopped'
                                status.current_departure_time = None
                                
                                # Handle final destination turnaround
                                if current_idx + 1 == len(details) - 1:
                                    train = status.train
                                    if train.cycle_enabled:
                                        from datetime import timedelta
                                        wait_td = timedelta(days=train.return_wait_days, hours=train.return_wait_hours)
                                        delay_td = timedelta(minutes=status.delay_minutes)
                                        actual_wait = wait_td - delay_td
                                        if actual_wait.total_seconds() < 0:
                                            actual_wait = timedelta(seconds=0)
                                        status.journey_direction = 'reverse' if status.journey_direction == 'forward' else 'forward'
                                        status.journey_start_datetime = datetime.now() + actual_wait
                                        status.delay_minutes = 0
                                    else:
                                        status.journey_direction = 'idle'
                                        status.journey_start_datetime = None
                                        status.delay_minutes = 0
                                
                                db.session.commit()
                                print(f"[Auto-Arrive] Train {status.train_id} arrived at {status.current_station_id}")
                                
                except Exception as e:
                    print(f"Error in train_progress_task: {e}")
                    db.session.rollback()
                socketio.sleep(10)
                
    socketio.start_background_task(train_progress_task, app)
    return app

app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=False, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
