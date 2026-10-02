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

# El link a la interfaz tiene que ser ABSOLUTO (vive en otro hostname: es la frontera de origen),
# pero el dominio no va en el repo. Se sustituye aca. El default es local a proposito: si
# HUB_APP_HOST no estuviera, el link queda muerto en vez de quedar con un marcador sin reemplazar.
APP_HOST="${HUB_APP_HOST:-app.localhost}"
if [ -f /data/index.html ]; then
  sed -i "s|__APP_HOST__|${APP_HOST}|g" /data/index.html
  if grep -q '__APP_HOST__' /data/index.html; then
    echo "AVISO: la landing quedo con __APP_HOST__ sin sustituir" >&2
  fi
fi

chown -R hub:hub /data 2>/dev/null || true
exec su-exec hub python3 -u /app/app.py
