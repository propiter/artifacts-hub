#!/bin/sh
set -e
mkdir -p /data/a

# Un volumen vacio montado sobre el docroot hace que Docker copie ahi el
# "Welcome to nginx" de la imagen. Sembramos SIEMPRE nuestra landing encima:
# la landing es parte del despliegue, no un paso manual que se puede olvidar.
if [ -d /www ]; then
  cp -f /www/index.html /data/index.html 2>/dev/null || true
  cp -f /www/50x.html   /data/50x.html   2>/dev/null || true
fi

chown -R hub:hub /data 2>/dev/null || true
exec su-exec hub python3 -u /app/app.py
