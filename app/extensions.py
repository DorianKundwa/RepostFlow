"""
app/extensions.py - Shared Flask extension instances.

Instantiated here (without an app) so they can be imported anywhere
without circular imports. Bound to the app in app/__init__.py via init_app().
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

# SQLAlchemy ORM instance
db = SQLAlchemy()

# Flask-Login session management
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access RepostFlow."
login_manager.login_message_category = "warning"

# CSRF protection for all forms
csrf = CSRFProtect()
