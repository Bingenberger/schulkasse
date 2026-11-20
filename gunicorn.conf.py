# Container-Betrieb (Schulplattform). Nativ startet deploy/schulkasse.service Gunicorn direkt.
bind = "0.0.0.0:8000"
workers = 2
threads = 2
timeout = 120             # PDF-Erzeugung (Kassenbuch, Briefe) kann dauern
graceful_timeout = 30
accesslog = None          # keine Zugriffsprotokolle mit Pfaden und IPs
errorlog = "-"
worker_tmp_dir = "/tmp"
umask = 0o077
forwarded_allow_ips = ""  # die App wertet X-Forwarded-* nur von SCHULKASSE_PROXY_IPS aus
wsgi_app = "schulkasse:create_app()"
