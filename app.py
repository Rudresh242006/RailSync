from flask import Flask
import models
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_socketio import SocketIO
from flask_compress import Compress
from dotenv import load_dotenv
load_dotenv()
import os
from extensions import db, bcrypt, login_manager, socketio

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'railsync-dev-secret-2024')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL',
        'mysql+pymysql://root:nnm24cc046@localhost/Train_DB'
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Gzip compression for all JSON + HTML responses (cuts payload 70-80%)
    Compress(app)

    db.init_app(app)
    bcrypt.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*")

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'

    from routes.auth import auth_bp
    from routes.user import user_bp
    from routes.admin import admin_bp
    from routes.api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp, url_prefix='/user')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api')

    @socketio.on('join')
    def on_join(data):
        from flask_socketio import join_room
        room = data.get('room')
        if room:
            join_room(room)

    with app.app_context():
        db.create_all()
    return app

app = create_app()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
