#!/usr/bin/env python3
"""artifacts-hub API — almacen de artifacts con tokens, cupos, expiracion y auditoria.

Sin dependencias externas: stdlib + sqlite3. Un archivo, un volumen, un proceso.

Modelo:
  * El DUEÑO de un artifact sale SIEMPRE del token, nunca del request.
  * Los archivos viven en <DATA>/a/<usuario>/ y los sirve nginx directo (sin pasar por aca).
  * URL canonica  /a/<usuario>/<slug>.html        -> siempre la ultima version (mutable)
    Snapshot      /a/<usuario>/<slug>-v<N>.html   -> inmutable, la version N
  * De la credencial solo se guarda sha256; el log de auditoria nunca guarda el token.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from contextlib import contextmanager
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAX_BODY_HARD = 16 * 1024 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
RESERVED = {"index", "50x", "healthz", "api", "a"}
ALLOWED_TYPES = {"text/html": "html"}

DEFAULTS = {
    "data_dir": os.environ.get("HUB_DATA", "/data"),
    "db_path": os.environ.get("HUB_DB", "/data/hub.sqlite3"),
    "max_bytes": int(os.environ.get("HUB_MAX_BYTES", 2 * 1024 * 1024)),
    "max_artifacts": int(os.environ.get("HUB_MAX_ARTIFACTS", 200)),
    "rate_per_min": int(os.environ.get("HUB_RATE_PER_MIN", 30)),
    "base_url": os.environ.get("HUB_BASE_URL", ""),
    # apps (varios archivos): limites del paquete que se sube
    "max_app_packed": int(os.environ.get("HUB_MAX_APP_PACKED", 12 * 1024 * 1024)),
    "max_app_unpacked": int(os.environ.get("HUB_MAX_APP_UNPACKED", 40 * 1024 * 1024)),
    "max_app_files": int(os.environ.get("HUB_MAX_APP_FILES", 1000)),
}
# Tipos que aceptamos DENTRO de una app. Denylist no: allowlist.
APP_TYPES = (".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg",
             ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".txt", ".md",
             ".csv", ".wasm", ".map", ".webmanifest", ".mp3", ".mp4", ".webm", ".vtt", ".xml")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY,               -- sha256 del secreto
  prefix TEXT NOT NULL,              -- para poder identificarlo sin exponerlo
  user_id INTEGER NOT NULL REFERENCES users(id),
  label TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  slug TEXT NOT NULL,
  title TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER,
  UNIQUE(user_id, slug)
);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  version INTEGER NOT NULL,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(artifact_id, version)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  user_name TEXT,
  token_prefix TEXT,
  action TEXT NOT NULL,
  artifact TEXT,
  bytes INTEGER,
  ip TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
"""


def _safe_relpath(name):
    """Devuelve la ruta relativa normalizada, o None si el miembro es peligroso.

    Nunca usamos extractall: cada miembro se valida y se escribe a mano, porque un
    tar puede traer '..', rutas absolutas o symlinks que escriben fuera del destino.
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return None
    if "\x00" in name:
        return None
    parts = []
    for chunk in name.replace("\\", "/").split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            return None
        parts.append(chunk)
    if not parts:
        return None
    rel = "/".join(parts)
    if os.path.isabs(rel) or rel.startswith("/"):
        return None
    return rel


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def now():
    return int(time.time())


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def extract_title(body):
    """El titulo vive en el propio artifact. Leerlo del cuerpo evita el infierno de
    encoding de los headers HTTP (son latin-1 por especificacion: un <title> con
    acentos viajaba corrupto)."""
    try:
        head = body[:16384].decode("utf-8", "replace")
    except Exception:
        return None
    m = TITLE_RE.search(head)
    if not m:
        return None
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    return t[:200] or None


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.data = cfg["data_dir"]
        self.db_path = cfg["db_path"]
        self.lock = threading.Lock()
        self.rate = {}
        os.makedirs(os.path.join(self.data, "a"), exist_ok=True)
        self._init_db()

    # ---------- infra ----------
    @contextmanager
    def db(self):
        """`with sqlite3.connect(...)` COMMITEA pero NO CIERRA: usarlo directo fuga
        descriptores hasta agotar el proceso. Aca se cierra siempre."""
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            yield c
            c.commit()
        finally:
            c.close()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self.db() as c:
            c.executescript(SCHEMA)

    def user_dir(self, name):
        d = os.path.join(self.data, "a", name)
        os.makedirs(d, exist_ok=True)
        return d

    # ---------- auditoria ----------
    def log(self, action, user=None, token_prefix=None, artifact=None, bytes_=None, ip=None, detail=None):
        with self.db() as c:
            c.execute(
                "INSERT INTO events (ts,user_name,token_prefix,action,artifact,bytes,ip,detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now(), user, token_prefix, action, artifact, bytes_, ip, detail),
            )

    # ---------- auth ----------
    def authenticate(self, header):
        if not header or not header.lower().startswith("bearer "):
            raise ApiError(401, "no_token", "falta el header Authorization: Bearer <token>")
        secret = header.split(None, 1)[1].strip()
        if not secret.startswith("wlart_") or len(secret) < 20:
            raise ApiError(401, "bad_token", "token invalido")
        tid = hashlib.sha256(secret.encode()).hexdigest()
        with self.db() as c:
            row = c.execute(
                "SELECT t.id, t.prefix, t.label, t.expires_at, t.revoked_at,"
                "       u.id AS uid, u.name, u.is_admin"
                "  FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.id = ?",
                (tid,),
            ).fetchone()
        if not row:
            raise ApiError(401, "bad_token", "token invalido")
        if row["revoked_at"]:
            raise ApiError(401, "revoked_token", "token revocado")
        if row["expires_at"] and row["expires_at"] < now():
            raise ApiError(401, "expired_token", "token expirado")
        return {
            "user_id": row["uid"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "token_id": row["id"], "prefix": row["prefix"], "label": row["label"],
        }

    def check_rate(self, token_id):
        limit = self.cfg["rate_per_min"]
        if limit <= 0:
            return
        cutoff = time.time() - 60
        with self.lock:
            hits = [t for t in self.rate.get(token_id, []) if t > cutoff]
            if len(hits) >= limit:
                raise ApiError(429, "rate_limited", "demasiadas escrituras, esperá un momento")
            hits.append(time.time())
            self.rate[token_id] = hits

    # ---------- publicar ----------
    def publish(self, ctx, slug, body, title, ip=None):
        if not SLUG_RE.match(slug or ""):
            raise ApiError(400, "bad_slug", "el slug debe ser [a-z0-9-], empezar con letra o numero, max 60")
        if slug in RESERVED:
            raise ApiError(400, "reserved_slug", "ese nombre esta reservado")
        max_bytes = self.cfg["max_bytes"]
        if len(body) == 0:
            raise ApiError(400, "empty", "el artifact esta vacio")
        if len(body) > max_bytes:
            raise ApiError(413, "too_large", "el artifact supera el limite de %d bytes" % max_bytes)

        self.check_rate(ctx["token_id"])
        sha = hashlib.sha256(body).hexdigest()
        user = ctx["name"]
        d = self.user_dir(user)
        canonical = "%s.html" % slug

        with self.db() as c:
            row = c.execute(
                "SELECT id, version, sha256, deleted_at FROM artifacts WHERE user_id=? AND slug=?",
                (ctx["user_id"], slug),
            ).fetchone()
            if row is None:
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM artifacts WHERE user_id=? AND deleted_at IS NULL",
                    (ctx["user_id"],),
                ).fetchone()["n"]
                if n >= self.cfg["max_artifacts"]:
                    raise ApiError(409, "quota_artifacts",
                                   "alcanzaste el limite de %d artifacts" % self.cfg["max_artifacts"])
                version = 1
            else:
                if row["deleted_at"]:
                    raise ApiError(409, "deleted_slug", "ese slug fue borrado; usa otro nombre")
                if row["sha256"] == sha:
                    return {"name": canonical, "slug": slug, "version": row["version"],
                            "sha256": sha, "bytes": len(body), "unchanged": True}
                version = row["version"] + 1

        snapshot = "%s-v%d.html" % (slug, version)
        # escribir el snapshot inmutable primero, despues la canonica (atomico)
        self._atomic_write(os.path.join(d, snapshot), body)
        self._atomic_write(os.path.join(d, canonical), body)

        with self.db() as c:
            if row is None:
                cur = c.execute(
                    "INSERT INTO artifacts (user_id,slug,title,version,sha256,bytes,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ctx["user_id"], slug, title, version, sha, len(body), now(), now()),
                )
                aid = cur.lastrowid
            else:
                aid = row["id"]
                c.execute(
                    "UPDATE artifacts SET version=?, sha256=?, bytes=?, title=COALESCE(?,title),"
                    " updated_at=? WHERE id=?",
                    (version, sha, len(body), title, now(), aid),
                )
            c.execute(
                "INSERT INTO versions (artifact_id,version,name,sha256,bytes,created_at) VALUES (?,?,?,?,?,?)",
                (aid, version, snapshot, sha, len(body), now()),
            )
        self.log("publish", user, ctx["prefix"], canonical, len(body), ip, "v%d" % version)
        return {"name": canonical, "slug": slug, "version": version, "sha256": sha,
                "bytes": len(body), "snapshot": snapshot, "unchanged": False}

    # ---------- publicar una APP (varios archivos) ----------
    def deploy_app(self, ctx, slug, packed, ip=None):
        """Recibe un tar.gz con la carpeta de la app y la publica en /a/<user>/<slug>/."""
        if not SLUG_RE.match(slug or ""):
            raise ApiError(400, "bad_slug", "el slug debe ser [a-z0-9-], empezar con letra o numero, max 60")
        if slug in RESERVED:
            raise ApiError(400, "reserved_slug", "ese nombre esta reservado")
        if not packed:
            raise ApiError(400, "empty", "el paquete esta vacio")
        if len(packed) > self.cfg["max_app_packed"]:
            raise ApiError(413, "too_large", "el paquete supera el limite de %d bytes" % self.cfg["max_app_packed"])

        self.check_rate(ctx["token_id"])

        import io
        import tarfile

        user = ctx["name"]
        base = self.user_dir(user)
        staging = os.path.join(base, ".staging-%s-%d" % (slug, os.getpid()))
        if os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)

        total = 0
        files = 0
        # gzip no es determinista (lleva timestamp): hashear el comprimido haria que
        # republicar lo mismo creara una version nueva. El hash va sobre el CONTENIDO.
        digest = hashlib.sha256()
        try:
            try:
                tf = tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz")
            except Exception:
                raise ApiError(415, "bad_pack", "el paquete no es un tar.gz valido")
            with tf:
                for m in tf:
                    if m.isdir():
                        continue
                    if not m.isfile():          # symlinks, hardlinks, devices: fuera
                        raise ApiError(400, "bad_member", "el paquete trae algo que no es un archivo regular (%s)" % m.name)
                    rel = _safe_relpath(m.name)
                    if rel is None:
                        raise ApiError(400, "bad_member", "ruta peligrosa en el paquete: %s" % m.name)
                    if not rel.lower().endswith(APP_TYPES):
                        raise ApiError(415, "bad_ext", "extension no permitida en una app: %s" % rel)
                    files += 1
                    if files > self.cfg["max_app_files"]:
                        raise ApiError(413, "too_many", "la app supera los %d archivos" % self.cfg["max_app_files"])
                    f = tf.extractfile(m)
                    if f is None:
                        raise ApiError(400, "bad_member", "no pude leer %s del paquete" % m.name)
                    data = f.read()
                    total += len(data)
                    if total > self.cfg["max_app_unpacked"]:
                        raise ApiError(413, "too_large", "la app descomprimida supera %d bytes" % self.cfg["max_app_unpacked"])
                    digest.update(rel.encode("utf-8", "replace") + b"\0" + data + b"\0")
                    dest = os.path.join(staging, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as out:
                        out.write(data)

            if not os.path.exists(os.path.join(staging, "index.html")):
                raise ApiError(400, "no_index", "la app necesita un index.html en la raiz")

            # el titulo sale del index.html, igual que en una pagina suelta
            with open(os.path.join(staging, "index.html"), "rb") as fh:
                title = extract_title(fh.read(32768))

            sha = digest.hexdigest()
            with self.db() as c:
                # OJO con las columnas: el codigo de abajo usa sha256 y deleted_at.
                row = c.execute(
                    "SELECT id, version, sha256, deleted_at FROM artifacts WHERE user_id=? AND slug=?",
                    (ctx["user_id"], slug),
                ).fetchone()
                if row and row["version"] and os.path.exists(os.path.join(base, slug + ".html")):
                    raise ApiError(409, "slug_in_use", "ese slug ya es una pagina suelta; usa otro nombre")
                if row and row["sha256"] == sha:
                    final = os.path.join(base, slug)
                    if os.path.isdir(final):
                        return {"name": slug + "/", "slug": slug, "version": row["version"],
                                "sha256": sha, "bytes": total, "files": files, "unchanged": True, "kind": "app"}
                if row is None:
                    n = c.execute("SELECT COUNT(*) AS n FROM artifacts WHERE user_id=? AND deleted_at IS NULL",
                                  (ctx["user_id"],)).fetchone()["n"]
                    if n >= self.cfg["max_artifacts"]:
                        raise ApiError(409, "quota_artifacts",
                                       "alcanzaste el limite de %d artifacts" % self.cfg["max_artifacts"])
                    version = 1
                else:
                    if row["deleted_at"]:
                        raise ApiError(409, "deleted_slug", "ese slug fue borrado; usa otro nombre")
                    version = row["version"] + 1

            # swap atomico: la version anterior se guarda comprimida en .versions/ (no publica)
            final = os.path.join(base, slug)
            versions_dir = os.path.join(base, ".versions")
            if os.path.isdir(final):
                os.makedirs(versions_dir, exist_ok=True)
                keep = os.path.join(versions_dir, "%s-v%d.tar.gz" % (slug, version - 1))
                with tarfile.open(keep, "w:gz") as ko:
                    ko.add(final, arcname=".")
                shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
            staging = None

            with self.db() as c:
                if row is None:
                    cur = c.execute(
                        "INSERT INTO artifacts (user_id,slug,title,version,sha256,bytes,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (ctx["user_id"], slug, title, version, sha, total, now(), now()),
                    )
                    aid = cur.lastrowid
                else:
                    aid = row["id"]
                    c.execute("UPDATE artifacts SET version=?, sha256=?, bytes=?, title=COALESCE(?,title),"
                              " updated_at=? WHERE id=?",
                              (version, sha, total, title, now(), aid))
                c.execute("INSERT INTO versions (artifact_id,version,name,sha256,bytes,created_at)"
                          " VALUES (?,?,?,?,?,?)",
                          (aid, version, slug + "/", sha, total, now()))
            self.log("deploy", user, ctx["prefix"], slug + "/", total, ip, "v%d, %d archivo(s)" % (version, files))
            return {"name": slug + "/", "slug": slug, "version": version, "sha256": sha,
                    "bytes": total, "files": files, "unchanged": False, "kind": "app"}
        finally:
            if staging and os.path.exists(staging):
                shutil.rmtree(staging, ignore_errors=True)

    def _atomic_write(self, path, body):
        tmp = path + ".tmp-%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---------- leer / borrar ----------
    def list_artifacts(self, ctx, all_users=False, only_user=None):
        q = ("SELECT a.slug, a.title, a.version, a.sha256, a.bytes, a.created_at, a.updated_at,"
             "       u.name AS owner FROM artifacts a JOIN users u ON u.id=a.user_id"
             " WHERE a.deleted_at IS NULL")
        args = []
        if only_user:
            q += " AND u.name = ?"
            args.append(only_user)
        elif not all_users:
            q += " AND a.user_id = ?"
            args.append(ctx["user_id"])
        q += " ORDER BY a.updated_at DESC"
        with self.db() as c:
            rows = [dict(r) for r in c.execute(q, args)]
        for r in rows:
            is_app = os.path.isdir(os.path.join(self.data, "a", r["owner"], r["slug"]))
            r["kind"] = "app" if is_app else "page"
            r["url"] = self.url_for(r["owner"], r["slug"] + ("/" if is_app else ".html"))
        return rows

    def delete_artifact(self, ctx, name, ip=None):
        slug = name[:-5] if name.endswith(".html") else name
        slug = re.sub(r"-v\d+$", "", slug)
        with self.db() as c:
            row = c.execute(
                "SELECT a.id, a.slug, u.name AS owner, u.id AS uid FROM artifacts a"
                " JOIN users u ON u.id=a.user_id WHERE a.slug=? AND a.deleted_at IS NULL",
                (slug,),
            ).fetchone()
            if not row:
                raise ApiError(404, "not_found", "no existe ese artifact")
            if row["uid"] != ctx["user_id"] and not ctx["is_admin"]:
                raise ApiError(403, "not_owner", "ese artifact no es tuyo")
            versions = c.execute("SELECT name FROM versions WHERE artifact_id=?", (row["id"],)).fetchall()
            c.execute("UPDATE artifacts SET deleted_at=? WHERE id=?", (now(), row["id"]))
        d = self.user_dir(row["owner"])
        removed = 0
        app_dir = os.path.join(d, slug)
        if os.path.isdir(app_dir):
            removed += sum(len(f) for _, _, f in os.walk(app_dir))
            shutil.rmtree(app_dir, ignore_errors=True)
        for v in list(versions) + [{"name": slug + ".html"}]:
            p = os.path.join(d, v["name"])
            if os.path.exists(p):
                os.remove(p)
                removed += 1
        vdir = os.path.join(d, ".versions")
        if os.path.isdir(vdir):
            for f in os.listdir(vdir):
                if f.startswith(slug + "-v"):
                    os.remove(os.path.join(vdir, f))
                    removed += 1
        self.log("delete", ctx["name"], ctx["prefix"], slug, None, ip, "%d archivo(s)" % removed)
        return {"slug": slug, "removed_files": removed}

    def whoami(self, ctx):
        with self.db() as c:
            used = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b FROM artifacts"
                " WHERE user_id=? AND deleted_at IS NULL",
                (ctx["user_id"],),
            ).fetchone()
        return {"user": ctx["name"], "is_admin": ctx["is_admin"], "label": ctx["label"],
                "artifacts": used["n"], "max_artifacts": self.cfg["max_artifacts"],
                "bytes_stored": used["b"], "max_bytes_per_artifact": self.cfg["max_bytes"]}

    def events(self, ctx, limit=50):
        q = "SELECT ts,user_name,token_prefix,action,artifact,bytes,detail FROM events"
        args = []
        if not ctx["is_admin"]:
            q += " WHERE user_name = ?"
            args.append(ctx["name"])
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.db() as c:
            return [dict(r) for r in c.execute(q, args)]

    def url_for(self, owner, name):
        base = self.cfg["base_url"].rstrip("/")
        return "%s/a/%s/%s" % (base, owner, name)


class Handler(BaseHTTPRequestHandler):
    server_version = "artifacts-hub/1.0"
    hub = None

    def log_message(self, fmt, *a):
        pass  # nginx ya loguea; no ensuciamos stderr

    # ---------- helpers ----------
    def _send(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, e):
        self._send(e.status, {"ok": False, "error": e.code, "message": e.message})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_BODY_HARD:
            raise ApiError(413, "too_large", "cuerpo demasiado grande")
        return self.rfile.read(n)

    def _parts(self):
        u = urlparse(self.path)
        seg = [s for s in u.path.split("/") if s]
        return seg, parse_qs(u.query)

    # ---------- router ----------
    def _handle(self, method):
        hub = self.hub
        ip = self.headers.get("X-Forwarded-For", self.client_address[0])
        try:
            seg, q = self._parts()
            if seg[:1] == ["healthz"]:
                return self._send(200, {"ok": True, "ts": now()})
            if seg[:2] != ["api", "v1"]:
                raise ApiError(404, "not_found", "ruta desconocida")
            rest = seg[2:]

            ctx = hub.authenticate(self.headers.get("Authorization"))

            # primero QUE ruta es, despues QUE metodo: una ruta inexistente es 404,
            # una ruta valida con metodo equivocado es 405.
            if rest == ["whoami"]:
                route = "whoami"
            elif rest == ["artifacts"]:
                route = "list"
            elif rest == ["events"]:
                route = "events"
            elif len(rest) == 2 and rest[0] == "artifacts":
                route = "one"
            elif len(rest) == 2 and rest[0] == "apps":
                route = "app"
            else:
                raise ApiError(404, "not_found", "ruta desconocida")

            if route == "whoami":
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "whoami es GET")
                return self._send(200, {"ok": True, **hub.whoami(ctx)})

            if route == "list":
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "la lista es GET")
                all_users = q.get("all", ["0"])[0] in ("1", "true", "yes")
                only = (q.get("user", [None])[0])
                if (all_users or only) and not ctx["is_admin"]:
                    raise ApiError(403, "not_admin", "solo un token admin puede ver los de otros")
                return self._send(200, {"ok": True,
                                        "artifacts": hub.list_artifacts(ctx, all_users=all_users, only_user=only)})

            if route == "events":
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "los eventos son GET")
                limit = int(q.get("limit", ["50"])[0])
                return self._send(200, {"ok": True, "events": hub.events(ctx, limit)})

            if route == "app":
                if method != "PUT":
                    raise ApiError(405, "method_not_allowed", "una app se publica con PUT")
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in ("application/gzip", "application/x-gzip", "application/octet-stream", "application/x-tar"):
                    raise ApiError(415, "bad_type",
                                   "una app se sube como tar.gz (Content-Type: application/gzip); recibi '%s'" % (ctype or "vacio"))
                body = self._body()
                r = hub.deploy_app(ctx, rest[1], body, ip)
                return self._send(200 if r["unchanged"] else 201,
                                  {"ok": True, "url": hub.url_for(ctx["name"], r["slug"] + "/"), **r})

            if route == "one":
                if method == "PUT":
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype not in ALLOWED_TYPES:
                        raise ApiError(415, "bad_type",
                                       "Content-Type debe ser text/html (recibi '%s')" % (ctype or "vacio"))
                    body = self._body()
                    # el header es solo un override ASCII; si trae no-ASCII se ignora
                    # y manda el <title> del cuerpo (que si viaja en UTF-8 correcto)
                    title = self.headers.get("X-Artifact-Title")
                    if title and (len(title) > 200 or not title.isascii()):
                        title = None
                    r = hub.publish(ctx, rest[1], body, title or extract_title(body), ip)
                    return self._send(200 if r["unchanged"] else 201,
                                      {"ok": True, "url": hub.url_for(ctx["name"], r["name"]),
                                       "snapshot_url": hub.url_for(ctx["name"], r["snapshot"]) if r.get("snapshot") else None,
                                       **r})
                if method == "DELETE":
                    return self._send(200, {"ok": True, **hub.delete_artifact(ctx, rest[1], ip)})
                raise ApiError(405, "method_not_allowed", "en un artifact: PUT o DELETE")

            raise ApiError(404, "not_found", "ruta desconocida")
        except ApiError as e:
            self._err(e)
        except Exception as exc:  # nunca filtrar la traza al cliente
            try:
                hub.log("error", None, None, None, None, ip, type(exc).__name__)
            except Exception:
                pass
            self._send(500, {"ok": False, "error": "internal", "message": "error interno"})

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def build_server(cfg):
    hub = Hub(cfg)
    Handler.hub = hub
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("HUB_PORT", 8080))), Handler)
    srv.daemon_threads = True
    return srv, hub


def main():
    cfg = dict(DEFAULTS)
    srv, hub = build_server(cfg)
    print("artifacts-hub escuchando en :%s (data=%s)" % (srv.server_address[1], cfg["data_dir"]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
