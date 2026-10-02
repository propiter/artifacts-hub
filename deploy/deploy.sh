#!/usr/bin/env bash
# Ayudas para desplegar. Uso:  bash deploy/deploy.sh [files|build|push|all]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

[ -f "$HERE/deploy.env" ] || { echo "falta deploy/deploy.env (copialo de deploy.env.example)"; exit 1; }
# shellcheck disable=SC1091
. "$HERE/deploy.env"
: "${APP:?falta APP}"; : "${IMAGE:?falta IMAGE}"; : "${SERVER:?falta SERVER en deploy.env}"

FILES_DIR="$HERE/files"
REMOTE="/etc/dokploy/compose/$APP/files"

# La estructura de files/ es ESPEJO de store/: asi el compose solo necesita prefijar
# ../files/ y no hay casos especiales por archivo.
prep() {
  rm -rf "$FILES_DIR"
  mkdir -p "$FILES_DIR"
  cp -r "$ROOT/store" "$FILES_DIR/store"
  # el default.conf va tal cual: matchea la app por regex (^app.), asi no hay marcadores
  # que sustituir ni forma de olvidarse de hacerlo
  grep -q 'server_name ~^app' "$FILES_DIR/store/default.conf" || {
    echo "  AVISO: el default.conf no tiene el bloque de la app (^app.)"; }
}

build() { docker build --platform linux/amd64 -t "$IMAGE" "$ROOT/api"; }

push()  { docker push "$IMAGE"; }

# ESTE es el paso que se olvida: la fila del mount NO escribe el archivo. Si no lo subis,
# Docker crea un directorio con ese nombre y el contenedor muere con ExitCode 127.
files() {
  prep
  echo "--- subiendo el arbol a $REMOTE ---"
  ssh "$SERVER" "mkdir -p $REMOTE"
  tar -C "$FILES_DIR" -cf - . | ssh "$SERVER" "tar -C $REMOTE -xf -"
  # ssh SIN -n se come el stdin del bucle: el while daba UNA sola vuelta y "verificaba"
  # un archivo de tres, en silencio. De ahi la guarda de conteo.
  lista="$(cd "$FILES_DIR" && find . -type f | sed "s|^\./||" | sort)"
  n="$(printf '%s\n' "$lista" | grep -c . || true)"
  n_local="$(cd "$ROOT/store" && find . -type f | wc -l | tr -d ' ')"
  [ "$n" -eq "$n_local" ] || { echo "esperaba $n_local archivo(s) y hay $n: revisar prep"; exit 1; }

  echo "--- verificacion: $n archivo(s), hash contra hash ---"
  fail=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    r="$(ssh -n "$SERVER" sha256sum "$REMOTE/$f" | cut -d" " -f1)"
    l="$(sha256sum "$FILES_DIR/$f" | cut -d" " -f1)"
    if [ "$r" = "$l" ]; then echo "  OK      $f"; else echo "  DIFIERE $f"; fail=1; fi
  done <<< "$lista"
  [ "$fail" = 0 ] || exit 1

  echo "--- y cada uno es un ARCHIVO, no un directorio que creo Docker ---"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    ssh -n "$SERVER" "test -f $REMOTE/$f" || { echo "  NO ES ARCHIVO: $f"; exit 1; }
  done <<< "$lista"
  echo "  $n archivo(s), todos regulares"
}

case "${1:-all}" in
  files) files ;;
  build) build ;;
  push)  push ;;
  prep)  prep; echo "listo en $FILES_DIR" ;;
  all)   prep; files ;;
  *) echo "uso: bash deploy/deploy.sh [files|build|push|prep|all]"; exit 1 ;;
esac
