"""
The Tracking Dashboard as a drop-in package: routes, models, stages, the page,
its CSS and JS. A host Flask app adds it with

    from tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

after its .env has been loaded (models.py reads DATABASE_URL on import).
See README "Adding it to the live process dashboard".
"""

from .routes import bp

__all__ = ["bp"]
