"""Blueprint registration."""


def register(app):
    from . import auswertungen, konto_auth, kontoauszuege, projekte, stammdaten, start, umsaetze, verwaltung
    for modul in (konto_auth, start, umsaetze, projekte, stammdaten, auswertungen, kontoauszuege, verwaltung):
        app.register_blueprint(modul.bp)
