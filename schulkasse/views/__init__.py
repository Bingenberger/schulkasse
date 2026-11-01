"""Blueprint registration."""


def register(app):
    from . import auswertungen, konto_auth, projekte, stammdaten, start, umsaetze, verwaltung
    for modul in (konto_auth, start, umsaetze, projekte, stammdaten, auswertungen, verwaltung):
        app.register_blueprint(modul.bp)
