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
import hmac
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

import web as hub_web
import meta as hub_meta
import ai as hub_ai
import kb as hub_kb

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
    # hostname de la INTERFAZ WEB. Tiene que ser distinto al de los artifacts: los artifacts
    # son HTML de terceros y no pueden compartir origen con la sesion.
    "app_host": os.environ.get("HUB_APP_HOST", ""),
    # apps (varios archivos): limites del paquete que se sube
    "max_app_packed": int(os.environ.get("HUB_MAX_APP_PACKED", 12 * 1024 * 1024)),
    "max_app_unpacked": int(os.environ.get("HUB_MAX_APP_UNPACKED", 40 * 1024 * 1024)),
    "max_app_files": int(os.environ.get("HUB_MAX_APP_FILES", 1000)),
    # ESTADO de un artifact vivo: lo que la pagina guarda y el agente lee.
    "state_max_bytes": int(os.environ.get("HUB_STATE_MAX_BYTES", 64 * 1024)),
    # Cupos de la clave de una pagina. La clave la comparten TODOS sus visitantes, asi que el
    # cupo es por VISITANTE (clave + IP), aparte para leer y para escribir; y un techo de
    # escrituras por pagina, que protege el disco aunque cambien las IPs.
    "state_reads_per_min": int(os.environ.get("HUB_STATE_READS_PER_MIN", 120)),
    "state_writes_per_min": int(os.environ.get("HUB_STATE_WRITES_PER_MIN", 30)),
    "state_writes_per_min_total": int(os.environ.get("HUB_STATE_WRITES_PER_MIN_TOTAL", 600)),
    # VISTA PREVIA (og:image) que el cliente genera al publicar.
    "thumb_max_bytes": int(os.environ.get("HUB_THUMB_MAX_BYTES", 1024 * 1024)),
    # IA para las paginas (ai.py). La clave vive SOLO en el servidor; acepta los nombres de
    # siempre (OPENAI_API_KEY / OPENAI_BASE_URL). Sin clave, sin URL o sin modelo: apagada.
    "ai_key": os.environ.get("HUB_AI_KEY") or os.environ.get("OPENAI_API_KEY", ""),
    "ai_base_url": os.environ.get("HUB_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL", ""),
    "ai_model": os.environ.get("HUB_AI_MODEL", ""),
    "ai_models": os.environ.get("HUB_AI_MODELS", ""),      # otros permitidos, separados por coma
    "ai_per_visitor_hour": int(os.environ.get("HUB_AI_PER_VISITOR_HOUR", 20)),
    "ai_per_page_day": int(os.environ.get("HUB_AI_PER_PAGE_DAY", 500)),
    "ai_max_tokens": int(os.environ.get("HUB_AI_MAX_TOKENS", 2000)),
    "ai_max_input_chars": int(os.environ.get("HUB_AI_MAX_INPUT_CHARS", 60000)),
    "ai_max_messages": int(os.environ.get("HUB_AI_MAX_MESSAGES", 60)),
    "ai_tokens_param": os.environ.get("HUB_AI_TOKENS_PARAM", "max_tokens"),
    "ai_timeout": int(os.environ.get("HUB_AI_TIMEOUT", 120)),
    # PROYECTOS y su conocimiento (kb.py). Embeddings del mismo proveedor que la IA.
    "kb_embed_model": os.environ.get("HUB_KB_EMBED_MODEL", ""),
    "kb_max_doc_bytes": int(os.environ.get("HUB_KB_MAX_DOC_BYTES", 15 * 1024 * 1024)),
    "kb_max_chunks": int(os.environ.get("HUB_KB_MAX_CHUNKS", 60000)),   # por proyecto
    "kb_passages": int(os.environ.get("HUB_KB_PASSAGES", 6)),
    "kb_searches_per_hour": int(os.environ.get("HUB_KB_SEARCHES_PER_HOUR", 120)),
    # Secreto para derivar las claves de estado. Vacio = se genera y queda en settings.
    "state_key_secret": os.environ.get("HUB_STATE_SECRET", ""),
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
  shared INTEGER NOT NULL DEFAULT 0,   -- compartido con el equipo, artifact por artifact
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

-- INTERFAZ WEB
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,              -- sha256 del id de la cookie
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_seen INTEGER,
  ip TEXT,
  csrf TEXT
);
CREATE TABLE IF NOT EXISTS login_codes (
  code TEXT PRIMARY KEY,             -- sha256
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);
CREATE TABLE IF NOT EXISTS team_codes (
  code TEXT PRIMARY KEY,             -- sha256
  label TEXT,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- ESTADO DE UN ARTIFACT VIVO
-- De la clave NO se guarda nada: se DERIVA con HMAC del secreto del servidor, el dueño, el
-- slug y un contador. Asi se puede REINYECTAR en cada publicacion sin almacenar material
-- secreto, y subir `epoch` invalida la anterior sin tocar el resto.
-- Va por (owner, slug) y no por id para que la clave exista ANTES de que el artifact exista:
-- la publicacion necesita inyectarla, y el id todavia no esta.
CREATE TABLE IF NOT EXISTS artifact_keys (
  owner TEXT NOT NULL,
  slug TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (owner, slug)
);
-- Poca cosa de configuracion del propio hub (ej. el secreto generado la primera vez).
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
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
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        # campos ademas de error/message que el cliente necesita para resolver (ej. un 412)
        self.extra = extra or {}


def state_etag(raw):
    """ETag del estado = hash de lo GUARDADO. Sin estado cuenta como "{}", que es lo que se lee.

    Al derivarse del contenido no hay contador que guardar ni que se desincronice: dos estados
    iguales tienen el mismo ETag, y cualquier cambio lo cambia.
    """
    return '"%s"' % hashlib.sha256(raw if raw is not None else b"{}").hexdigest()[:24]


def same_etag(a, b):
    """Compara ETags como lo hace HTTP para If-Match: sin el prefijo debil W/ y sin espacios."""
    norm = lambda s: (s or "").strip().removeprefix("W/")
    return norm(a) == norm(b)


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
        # Aparte del lock general: comparar el If-Match y escribir tiene que ser UNA operacion,
        # o dos escritores con el mismo ETag ganan los dos y uno pierde lo suyo sin enterarse.
        self.state_lock = threading.Lock()
        self.rate = {}
        self.attempts = {}
        self.kb_lock = threading.Lock()
        self._vectors = {}          # project_id -> (firma, [(chunk_id, vector)])
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
            # Migracion: la base de produccion ya existe, asi que las columnas nuevas se
            # agregan si faltan (CREATE TABLE IF NOT EXISTS no las agrega).
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            if "share_team" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN share_team INTEGER NOT NULL DEFAULT 0")
            if "display_name" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
            acols = {r["name"] for r in c.execute("PRAGMA table_info(artifacts)")}
            if "shared" not in acols:
                c.execute("ALTER TABLE artifacts ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
            c.executescript(hub_kb.SCHEMA)
            if "project_id" not in acols:
                c.execute("ALTER TABLE artifacts ADD COLUMN project_id INTEGER REFERENCES projects(id)")

    # ---------- proyectos: artifacts + documentos, de todo el equipo ----------
    def project_row(self, c, slug):
        row = c.execute("SELECT * FROM projects WHERE slug = ?", (slug or "",)).fetchone()
        if row is None:
            raise ApiError(404, "no_project", "no existe el proyecto '%s' (wl-artifact projects lista los que hay)" % slug)
        return row

    def project_create(self, ctx, slug, title, description="", ip=None):
        if not SLUG_RE.match(slug or ""):
            raise ApiError(400, "bad_slug", "el proyecto debe ser [a-z0-9-], empezar con letra o numero, max 60")
        title = (title or slug).strip()[:120]
        with self.db() as c:
            if c.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise ApiError(409, "exists", "ya existe el proyecto '%s'" % slug)
            c.execute("INSERT INTO projects (slug, title, description, created_by, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?)", (slug, title, (description or "").strip()[:500],
                                                ctx["user_id"], now(), now()))
        self.log("project", ctx["name"], ctx.get("prefix"), slug, None, ip, title)
        return self.project_info(slug)

    def project_update(self, ctx, slug, title=None, description=None):
        with self.db() as c:
            row = self.project_row(c, slug)
            c.execute("UPDATE projects SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                      ((title or row["title"]).strip()[:120],
                       (row["description"] if description is None else description).strip()[:500], now(), row["id"]))
        return self.project_info(slug)

    def projects(self):
        with self.db() as c:
            rows = c.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM artifacts a WHERE a.project_id = p.id AND a.deleted_at IS NULL) AS artifacts,"
                " (SELECT COUNT(*) FROM docs d WHERE d.project_id = p.id) AS docs,"
                " (SELECT COUNT(*) FROM docs d WHERE d.project_id = p.id AND d.visibility = 'public') AS public_docs"
                " FROM projects p ORDER BY p.updated_at DESC").fetchall()
        return [{"slug": r["slug"], "title": r["title"], "description": r["description"],
                 "artifacts": r["artifacts"], "docs": r["docs"], "public_docs": r["public_docs"],
                 "updated_at": r["updated_at"]} for r in rows]

    def project_info(self, slug):
        for p in self.projects():
            if p["slug"] == slug:
                return p
        raise ApiError(404, "no_project", "no existe el proyecto '%s'" % slug)

    def project_detail(self, slug):
        info = self.project_info(slug)
        with self.db() as c:
            pid = self.project_row(c, slug)["id"]
            docs = [dict(r) for r in c.execute(
                "SELECT path, title, visibility, bytes, chars, chunks, semantic, updated_at FROM docs"
                " WHERE project_id = ? ORDER BY path", (pid,))]
            arts = [{"owner": r["owner"], "slug": r["slug"], "title": r["title"], "updated_at": r["updated_at"],
                     "url": self.url_for(r["owner"], r["slug"] + ".html")} for r in c.execute(
                "SELECT a.slug, a.title, a.updated_at, u.name AS owner FROM artifacts a JOIN users u ON u.id = a.user_id"
                " WHERE a.project_id = ? AND a.deleted_at IS NULL ORDER BY a.updated_at DESC", (pid,))]
        return dict(info, documents=docs, artifact_list=arts)

    def doc_put(self, ctx, slug, path, data, visibility=None, ip=None):
        """Indexa (o reindexa) un documento. Mismo contenido y misma visibilidad: no-op."""
        path = self._kb(hub_kb.clean_path, path)
        if visibility is not None and visibility not in hub_kb.VISIBILITY:
            raise ApiError(400, "bad_visibility", "la visibilidad es internal o public")
        if len(data) > self.cfg["kb_max_doc_bytes"]:
            raise ApiError(413, "too_large", "el documento supera %d bytes" % self.cfg["kb_max_doc_bytes"])
        digest = hub_kb.sha(data)
        with self.db() as c:
            pid = self.project_row(c, slug)["id"]
            old = c.execute("SELECT id, sha256, visibility FROM docs WHERE project_id = ? AND path = ?",
                            (pid, path)).fetchone()
        vis = visibility or (old["visibility"] if old else "internal")
        if old and old["sha256"] == digest:
            if old["visibility"] != vis:
                self.doc_visibility(ctx, slug, [path], vis)
                return {"path": path, "status": "visibility", "visibility": vis}
            return {"path": path, "status": "unchanged", "visibility": vis}
        text = self._kb(hub_kb.extract, path, data)
        if not text.strip():
            raise ApiError(422, "empty", "no encontre texto en '%s'" % path)
        leaks = hub_kb.scan_secrets(text)
        if leaks:
            raise ApiError(422, "secrets", "'%s' tiene algo con forma de credencial: no se indexa. Sacalo del"
                           " documento (o rotalo si es real) y volve a subirlo" % path, {"findings": leaks, "path": path})
        pieces = hub_kb.chunk(text)
        vectors = hub_kb.embed(self.cfg, [(h + "\n" + t) if h else t for h, t in pieces], "passage")
        with self.kb_lock, self.db() as c:
            have = c.execute("SELECT COUNT(*) FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE d.project_id = ?"
                             " AND d.path != ?", (pid, path)).fetchone()[0]
            if have + len(pieces) > self.cfg["kb_max_chunks"]:
                raise ApiError(413, "project_full", "el proyecto llego a su limite de %d pasajes" % self.cfg["kb_max_chunks"])
            if old:
                self._drop_chunks(c, old["id"])
                c.execute("UPDATE docs SET title=?, visibility=?, bytes=?, chars=?, sha256=?, chunks=?, semantic=?,"
                          " updated_at=? WHERE id=?", (hub_kb.title_of(path, text), vis, len(data), len(text), digest,
                                                       len(pieces), 1 if vectors else 0, now(), old["id"]))
                doc_id = old["id"]
            else:
                doc_id = c.execute(
                    "INSERT INTO docs (project_id, path, title, visibility, bytes, chars, sha256, chunks, semantic,"
                    " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, path, hub_kb.title_of(path, text), vis, len(data), len(text), digest, len(pieces),
                     1 if vectors else 0, ctx["user_id"], now(), now())).lastrowid
            for i, (head, body) in enumerate(pieces):
                cid = c.execute("INSERT INTO chunks (doc_id, ord, heading, text, vec) VALUES (?,?,?,?,?)",
                                (doc_id, i, head, body, vectors[i] if vectors else None)).lastrowid
                c.execute("INSERT INTO chunks_fts (text, heading, chunk_id) VALUES (?,?,?)", (body, head, cid))
            c.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), pid))
        self._vectors.pop(pid, None)
        self.log("doc", ctx["name"], ctx.get("prefix"), slug, len(data), ip, "%s (%s)" % (path, vis))
        return {"path": path, "status": "updated" if old else "created", "visibility": vis,
                "chunks": len(pieces), "semantic": bool(vectors)}

    def _drop_chunks(self, c, doc_id):
        ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            c.execute("DELETE FROM chunks_fts WHERE chunk_id IN (%s)" % ",".join("?" * len(part)), part)
        c.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def _docs_matching(self, c, pid, paths=None, prefix=None):
        if paths:
            clean = [self._kb(hub_kb.clean_path, p) for p in paths]
            return c.execute("SELECT id, path FROM docs WHERE project_id = ? AND path IN (%s)"
                             % ",".join("?" * len(clean)), [pid] + clean).fetchall()
        if prefix is not None:
            pre = prefix.strip().lstrip("/")
            return c.execute("SELECT id, path FROM docs WHERE project_id = ? AND (? = '' OR path = ? OR path LIKE ?)",
                             (pid, pre, pre.rstrip("/"), pre.rstrip("/") + "/%")).fetchall()
        raise ApiError(400, "bad_request", "indica paths o prefix")

    def doc_visibility(self, ctx, slug, paths=None, visibility="internal", prefix=None, ip=None):
        if visibility not in hub_kb.VISIBILITY:
            raise ApiError(400, "bad_visibility", "la visibilidad es internal o public")
        with self.db() as c:
            pid = self.project_row(c, slug)["id"]
            rows = self._docs_matching(c, pid, paths, prefix)
            for r in rows:
                c.execute("UPDATE docs SET visibility = ?, updated_at = ? WHERE id = ?", (visibility, now(), r["id"]))
        self.log("doc-vis", ctx["name"], ctx.get("prefix"), slug, None, ip, "%d -> %s" % (len(rows), visibility))
        return {"changed": [r["path"] for r in rows], "visibility": visibility}

    def doc_delete(self, ctx, slug, paths=None, prefix=None, ip=None):
        with self.kb_lock, self.db() as c:
            pid = self.project_row(c, slug)["id"]
            rows = self._docs_matching(c, pid, paths, prefix)
            for r in rows:
                self._drop_chunks(c, r["id"])
                c.execute("DELETE FROM docs WHERE id = ?", (r["id"],))
        self._vectors.pop(pid, None)
        self.log("doc-rm", ctx["name"], ctx.get("prefix"), slug, None, ip, "%d documentos" % len(rows))
        return {"removed": [r["path"] for r in rows]}

    def kb_search(self, project_id, query, public_only, k=None):
        k = max(1, min(int(k or self.cfg["kb_passages"]), 12))
        with self.db() as c:
            sig = c.execute("SELECT COUNT(*), COALESCE(MAX(c.id), 0) FROM chunks c JOIN docs d ON d.id = c.doc_id"
                            " WHERE d.project_id = ? AND c.vec IS NOT NULL", (project_id,)).fetchone()
            cached = self._vectors.get(project_id)
            if not cached or cached[0] != tuple(sig):
                cached = (tuple(sig), hub_kb.load_vectors(c, project_id))
                self._vectors[project_id] = cached
            return hub_kb.search(c, self.cfg, project_id, query, public_only, k, vectors=cached[1])

    def project_of_artifact(self, owner, slug):
        """(id, slug, title) del proyecto de un artifact, o None."""
        with self.db() as c:
            r = c.execute("SELECT p.id, p.slug, p.title FROM artifacts a JOIN users u ON u.id = a.user_id"
                          " JOIN projects p ON p.id = a.project_id WHERE u.name = ? AND a.slug = ?"
                          " AND a.deleted_at IS NULL", (owner, slug)).fetchone()
        return tuple(r) if r else None

    def set_artifact_project(self, ctx, slug, project, ip=None):
        """Mete un artifact (propio, o cualquiera si sos admin) en un proyecto; None lo saca."""
        row = self.mine_of(ctx, slug)
        with self.db() as c:
            pid = self.project_row(c, project)["id"] if project else None
            c.execute("UPDATE artifacts SET project_id = ? WHERE id = ?", (pid, row["id"]))
            if pid:
                c.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), pid))
        self.log("move", ctx["name"], ctx.get("prefix"), slug, None, ip, project or "(sin proyecto)")
        return {"slug": slug, "project": project}

    def _kb(self, fn, *a):
        try:
            return fn(*a)
        except hub_kb.KbError as e:
            raise ApiError(e.status, e.code, e.message, e.extra)

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

    def check_attempts(self, key, limit=10, window=300, sweep_over=5000):
        """Cupo en memoria (alcanza con una replica): fuerza bruta del codigo de equipo y cupos
        de estado. Las claves llevan IPs, asi que se barren las vencidas: si no, el dict crece
        con cada visitante que paso alguna vez."""
        now = time.time()
        with self.lock:
            if len(self.attempts) > sweep_over:
                for k in [k for k, (w, h) in self.attempts.items() if not h or h[-1] <= now - w]:
                    del self.attempts[k]
            hits = [t for t in self.attempts.get(key, (window, []))[1] if t > now - window]
            if len(hits) >= limit:
                raise ApiError(429, "too_many_attempts", "demasiados intentos, esperá unos minutos",
                               {"retry_after": int(hits[0] + window - now) + 1})
            hits.append(now)
            self.attempts[key] = (window, hits)

    def join(self, name, code, ip=None):
        if not SLUG_RE.match(name or ""):
            raise ApiError(400, "bad_name", "el usuario debe ser [a-z0-9-], empezar con letra o numero, max 60")
        self.check_attempts("join:" + (ip or "?"))
        if not hub_web.team_code_ok(self, code):
            raise ApiError(403, "bad_code", "el codigo de equipo no es valido (o fue rotado)")
        uid = self.ensure_user(name)
        token = self.mint_token(uid, "cli", ip)
        self.log("join", name, None, None, None, ip, "por codigo de equipo")
        return token, uid

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

    # ---------- cuentas (para la interfaz web) ----------
    def ensure_user(self, name, is_admin=False):
        with self.db() as c:
            row = c.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
            if row:
                return row["id"]
            cur = c.execute("INSERT INTO users (name, is_admin, created_at) VALUES (?,?,?)",
                            (name, 1 if is_admin else 0, now()))
            return cur.lastrowid

    def mint_token(self, user_id, label="web", ip=None):
        """Devuelve el token EN CLARO una sola vez; en la base queda su sha256."""
        secret = "wlart_" + secrets.token_hex(24)
        with self.db() as c:
            c.execute("INSERT INTO tokens (id,prefix,user_id,label,created_at) VALUES (?,?,?,?,?)",
                      (hashlib.sha256(secret.encode()).hexdigest(), "tok_" + secrets.token_hex(6),
                       user_id, label, now()))
        return secret

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
                version = row["version"] + 1

        # Recien ahora la vista previa y el runtime. Va DESPUES de los chequeos de existencia
        # para que un slug borrado o alguien pasado de cupo no toquen nada, y ANTES de hashear:
        # el hash es del cuerpo SERVIDO, asi que republicar lo mismo sigue siendo un no-op.
        body = self._prepare_page(body, user, slug, title, self.url_for(user, canonical))
        sha = hashlib.sha256(body).hexdigest()

        if row is not None and row["sha256"] == sha:
            return {"name": canonical, "slug": slug, "version": row["version"],
                    "sha256": sha, "bytes": len(body), "unchanged": True}

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
                    # El index de una app tambien lleva su vista previa y su runtime, con las
                    # MISMAS reglas que una pagina suelta.
                    if rel.lower() == "index.html":
                        data = self._prepare_page(data, ctx["name"], slug, extract_title(data),
                                                  self.url_for(ctx["name"], slug + "/"))
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

    # ---------- vista previa y estado: lo que hace VIVO a un artifact ----------
    def thumb_path(self, owner, slug):
        # Hermano del artifact (no adentro: una app es una CARPETA y su arbol se reemplaza
        # entero en cada deploy). `a/<u>/<slug>.preview.png` no choca con nada y nginx lo
        # sirve como estatico, sin pasar por la API.
        return os.path.join(self.user_dir(owner), slug + ".preview.png")

    def thumb_url(self, owner, slug):
        if not os.path.exists(self.thumb_path(owner, slug)):
            return None
        return self.url_for(owner, slug + ".preview.png")

    def _state_path(self, owner, slug):
        # Dentro de un directorio con punto a proposito: nginx NIEGA cualquier ruta con un
        # segmento que empieza con punto, asi que el estado no es alcanzable por HTTP directo.
        return os.path.join(self.user_dir(owner), ".state", slug + ".json")

    def _site_name(self):
        base = (self.cfg.get("base_url") or "").rstrip("/")
        return base.split("//")[-1].split("/")[0] or None

    def _state_secret(self):
        """Con lo que se DERIVAN las claves de estado.

        No se guarda ninguna clave en claro: se recalculan. Por eso reinyectarlas en cada
        publicacion no obliga a almacenar material secreto en ningun lado. Sale del entorno si
        esta; si no, se genera una vez y queda en settings (no es una credencial de nadie).
        """
        s = (self.cfg.get("state_key_secret") or "").strip()
        if s:
            return s.encode()
        with self.db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='state_secret'").fetchone()
            if row is not None:
                return row["value"].encode()
            value = secrets.token_hex(32)
            c.execute("INSERT INTO settings (key,value) VALUES ('state_secret',?)", (value,))
        return value.encode()

    def _state_key(self, owner, slug, epoch):
        mac = hmac.new(self._state_secret(),
                       ("%s:%s:%d" % (owner, slug, epoch)).encode(), hashlib.sha256)
        return "art_" + mac.hexdigest()[:40]

    def key_epoch(self, owner, slug):
        """(epoch, revocado) SIN escribir nada. Es lo que usan la publicacion y la
        verificacion: leer un artifact no tiene por que crearle fila."""
        with self.db() as c:
            row = c.execute("SELECT epoch, revoked_at FROM artifact_keys WHERE owner=? AND slug=?",
                            (owner, slug)).fetchone()
        if row is None:
            return 0, False
        return row["epoch"], bool(row["revoked_at"])

    def key_row(self, owner, slug):
        """Igual que key_epoch, pero MATERIALIZA la fila. Solo para rotar o apagar: son las
        operaciones en las que el contador tiene que existir para poder subirlo."""
        with self.db() as c:
            row = c.execute("SELECT epoch, revoked_at FROM artifact_keys WHERE owner=? AND slug=?",
                            (owner, slug)).fetchone()
            if row is not None:
                return row["epoch"], bool(row["revoked_at"])
            c.execute("INSERT INTO artifact_keys (owner, slug, epoch, created_at) VALUES (?,?,0,?)",
                      (owner, slug, now()))
        return 0, False

    def rotate_key(self, owner, slug):
        """Cambia la clave. La vieja muere y la nueva se inyecta al republicar.

        OJO: rotar NO apaga el estado (eso es `revoke_key`). Son cosas distintas: una cambia
        la credencial, la otra cierra la puerta.
        """
        self.key_row(owner, slug)
        with self.db() as c:
            c.execute("UPDATE artifact_keys SET epoch=epoch+1, revoked_at=NULL"
                      " WHERE owner=? AND slug=?", (owner, slug))
        epoch, _ = self.key_row(owner, slug)
        return epoch

    def revoke_key(self, owner, slug, off=True):
        """Apaga (o vuelve a prender) el estado de un artifact.

        Apagado, la proxima publicacion NO inyecta runtime: la pagina deja de poder hablar
        con el hub. La clave vieja deja de valer en el acto.
        """
        self.key_row(owner, slug)
        with self.db() as c:
            c.execute("UPDATE artifact_keys SET revoked_at=? WHERE owner=? AND slug=?",
                      (now() if off else None, owner, slug))
        return off

    def _prepare_page(self, body, owner, slug, title, url):
        """El cuerpo tal como se va a servir: vista previa + runtime, inyectados.

        Deterministico a proposito: publicar los mismos bytes con la misma vista previa da el
        mismo resultado, y por eso la republicacion sigue siendo un no-op.
        """
        epoch, revoked = self.key_epoch(owner, slug)
        runtime = None
        if not revoked:
            runtime = {"api": (self.cfg.get("base_url") or "").rstrip("/"), "owner": owner,
                       "slug": slug, "key": self._state_key(owner, slug, epoch),
                       "max": self.cfg["state_max_bytes"]}
        return hub_meta.inject(
            body,
            title=title,
            description=hub_meta.description_from(body),
            url=url,
            image=self.thumb_url(owner, slug),
            site=self._site_name(),
            runtime=runtime,
        )

    # ---------- vista previa (og:image) ----------
    def put_thumb(self, ctx, slug, body, ip=None):
        if not SLUG_RE.match(slug or ""):
            raise ApiError(400, "bad_slug", "el slug debe ser [a-z0-9-], empezar con letra o numero, max 60")
        if len(body) < 8 or body[:8] != b"\x89PNG\r\n\x1a\n":
            raise ApiError(415, "bad_thumb", "la vista previa tiene que ser un PNG")
        if len(body) > self.cfg["thumb_max_bytes"]:
            raise ApiError(413, "too_large", "la vista previa supera %d bytes" % self.cfg["thumb_max_bytes"])
        self.check_rate(ctx["token_id"])
        self._atomic_write(self.thumb_path(ctx["name"], slug), body)
        self.log("thumb", ctx["name"], ctx["prefix"], slug, len(body), ip, None)
        return {"slug": slug, "bytes": len(body),
                "thumb_url": self.url_for(ctx["name"], slug + ".preview.png"),
                "note": "se agrega al <head> en la proxima publicacion de la pagina"}

    def delete_thumb(self, ctx, slug, ip=None):
        p = self.thumb_path(ctx["name"], slug)
        if not os.path.exists(p):
            raise ApiError(404, "not_found", "ese artifact no tiene vista previa")
        os.remove(p)
        self.log("thumb-rm", ctx["name"], ctx["prefix"], slug, None, ip, None)
        return {"slug": slug, "removed": True,
                "note": "el og:image sigue en lo ya publicado hasta que la vuelvas a publicar"}

    # ---------- estado (el artifact que devuelve algo) ----------
    def artifact_of(self, owner, slug):
        """Fila del artifact para un (dueño, slug). El dueño es explicito: el slug solo no
        alcanza, porque dos personas pueden tener el mismo."""
        slug = re.sub(r"\.html?$", "", slug or "", flags=re.I)
        with self.db() as c:
            row = c.execute(
                "SELECT a.*, u.name AS owner FROM artifacts a JOIN users u ON u.id=a.user_id"
                " WHERE a.slug=? AND u.name=? AND a.deleted_at IS NULL", (slug, owner)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "no existe ese artifact")
        return row

    def mine_of(self, ctx, slug):
        """Fila del artifact por slug, del que firma el token (o de cualquiera si es admin)."""
        slug = re.sub(r"\.html?$", "", slug or "", flags=re.I)
        with self.db() as c:
            if ctx["is_admin"]:
                row = c.execute(
                    "SELECT a.*, u.name AS owner FROM artifacts a JOIN users u ON u.id=a.user_id"
                    " WHERE a.slug=? AND a.deleted_at IS NULL", (slug,)).fetchone()
            else:
                row = c.execute(
                    "SELECT a.*, u.name AS owner FROM artifacts a JOIN users u ON u.id=a.user_id"
                    " WHERE a.slug=? AND a.user_id=? AND a.deleted_at IS NULL",
                    (slug, ctx["user_id"])).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "no existe ese artifact")
        return row

    def check_artifact_key(self, owner, slug, key):
        """Valida una clave de artifact. Tiempo constante: no se filtra por donde falla."""
        if not key or not key.startswith("art_") or len(key) > 64:
            raise ApiError(401, "bad_key", "clave de artifact invalida")
        row = self.artifact_of(owner, slug)
        epoch, revoked = self.key_epoch(row["owner"], row["slug"])
        if revoked:
            raise ApiError(403, "revoked_key", "la clave de ese artifact fue revocada")
        if not hmac.compare_digest(self._state_key(row["owner"], row["slug"], epoch), key):
            raise ApiError(401, "bad_key", "clave de artifact invalida")
        return row

    def check_ai_quota(self, key, ip):
        """Cupos de IA de una pagina: por visitante por hora y por pagina por dia. Se miran LOS
        DOS antes de descontar: un pedido rechazado por uno no gasta el otro (si no, un visitante
        insistiendo contra su tope le comeria el cupo del dia a toda la pagina)."""
        k, now = key[:32], time.time()
        rules = [("aih:%s:%s" % (k, ip), self.cfg["ai_per_visitor_hour"], 3600, "ai_rate_limited",
                  "llegaste a %d pedidos de IA por hora: proba en un rato" % self.cfg["ai_per_visitor_hour"]),
                 ("aid:" + k, self.cfg["ai_per_page_day"], 86400, "ai_page_limit",
                  "esta pagina llego a sus %d pedidos de IA de hoy" % self.cfg["ai_per_page_day"])]
        with self.lock:
            live = []
            for name, limit, window, code, message in rules:
                hits = [h for h in self.attempts.get(name, (window, []))[1] if h > now - window]
                if len(hits) >= limit:
                    raise ApiError(429, code, message, {"retry_after": int(hits[0] + window - now) + 1})
                live.append((name, window, hits))
            for name, window, hits in live:
                self.attempts[name] = (window, hits + [now])

    def check_state_quota(self, key, method, ip):
        """Cupo de la clave de una pagina, por visitante. Ver state_reads_per_min."""
        k = key[:32]
        if method in ("GET", "HEAD"):
            self.check_attempts("sr:%s:%s" % (k, ip), limit=self.cfg["state_reads_per_min"], window=60)
        else:
            self.check_attempts("sw:%s:%s" % (k, ip), limit=self.cfg["state_writes_per_min"], window=60)
            self.check_attempts("swt:" + k, limit=self.cfg["state_writes_per_min_total"], window=60)

    def reveal_key(self, owner, slug):
        """La clave de estado de un artifact, para quien firma el token (nunca en un log)."""
        row = self.artifact_of(owner, slug)
        epoch, revoked = self.key_epoch(row["owner"], row["slug"])
        return {"owner": row["owner"], "slug": row["slug"], "epoch": epoch, "revoked": revoked,
                "key": None if revoked else self._state_key(row["owner"], row["slug"], epoch),
                "state_url": "%s/api/v1/state/%s/%s" % (
                    (self.cfg.get("base_url") or "").rstrip("/"), row["owner"], row["slug"])}

    def get_state(self, owner, slug):
        p = self._state_path(owner, slug)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read(self.cfg["state_max_bytes"] + 1)

    def put_state(self, owner, slug, raw, ip=None, via="token", if_match=None):
        """Escribe el estado. Devuelve (bytes, etag_nuevo).

        Con `if_match`, escribe SOLO si lo guardado sigue siendo lo que el que escribe leyo; si
        no, 412 con el estado actual y su ETag, para mezclar y reintentar. Sin `if_match`,
        escribe como siempre (las paginas ya publicadas no lo mandan).
        """
        if len(raw) > self.cfg["state_max_bytes"]:
            raise ApiError(413, "too_large", "el estado supera %d bytes" % self.cfg["state_max_bytes"])
        if not raw.strip():
            raise ApiError(400, "empty", "el estado esta vacio")
        try:
            json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_json", "el estado tiene que ser JSON valido")
        p = self._state_path(owner, slug)
        with self.state_lock:
            if if_match is not None:
                current = self.get_state(owner, slug)
                if not same_etag(if_match, state_etag(current)):
                    try:
                        shown = json.loads((current or b"{}").decode("utf-8"))
                    except Exception:
                        shown = {}
                    raise ApiError(412, "conflict",
                                   "alguien cambio el estado despues de que lo leiste: mezcla y reintenta",
                                   {"etag": state_etag(current), "state": shown})
            os.makedirs(os.path.dirname(p), exist_ok=True)
            self._atomic_write(p, raw)
        self.log("state", owner, None, slug, len(raw), ip, "por %s" % via)
        return len(raw), state_etag(raw)

    def delete_state(self, owner, slug, ip=None):
        p = self._state_path(owner, slug)
        if not os.path.exists(p):
            raise ApiError(404, "not_found", "ese artifact no tiene estado guardado")
        os.remove(p)
        self.log("state-rm", owner, None, slug, None, ip, None)
        return True

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
            r["thumb_url"] = self.thumb_url(r["owner"], r["slug"])
            r["has_state"] = os.path.exists(self._state_path(r["owner"], r["slug"]))
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
        # La vista previa y el estado se van con el artifact: si no, quedan huerfanos y el
        # slug no se puede reusar limpio.
        for extra in (os.path.join(d, slug + ".preview.png"), self._state_path(row["owner"], slug)):
            if os.path.exists(extra):
                os.remove(extra)
                removed += 1
        with self.db() as c:
            c.execute("DELETE FROM artifact_keys WHERE owner=? AND slug=?", (row["owner"], slug))
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
    def _send(self, status, payload, ctype="application/json", headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, e):
        payload = {"ok": False, "error": e.code, "message": e.message}
        payload.update(getattr(e, "extra", None) or {})
        etag = payload.get("etag")
        self._send(e.status, payload, headers={"ETag": etag} if etag else None)

    def _state_io(self, method, owner, slug, ip, via):
        """GET y PUT del estado, iguales por clave o por token. El ETag viaja en los dos."""
        hub = self.hub
        if method in ("GET", "HEAD"):
            raw = hub.get_state(owner, slug)
            self._send(200, raw if raw is not None else b"{}", "application/json",
                       headers={"ETag": state_etag(raw)})
            return True
        if method == "PUT":
            n, etag = hub.put_state(owner, slug, self._body(), ip, via=via,
                                    if_match=self.headers.get("If-Match"))
            self._send(200, {"ok": True, "bytes": n, "etag": etag}, headers={"ETag": etag})
            return True
        return False

    def _json_body(self):
        try:
            b = json.loads(self._body().decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(400, "bad_json", "el cuerpo tiene que ser JSON")
        if not isinstance(b, dict):
            raise ApiError(400, "bad_json", "el cuerpo tiene que ser un objeto JSON")
        return b

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
    def _web(self, method, raw_path, q):
        """Sirve la interfaz web. Solo cuando el request entra por el hostname de la app."""
        hub = self.hub
        form = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 65536:
                try:
                    form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
                except Exception:
                    form = {}
        status, ctype, payload, cookie, location = hub_web.serve(self, hub, method, raw_path, q, form)
        body = payload if isinstance(payload, bytes) else (payload or "").encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # la app es server-rendered; sin inline externo salvo el filtro del buscador
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; frame-src https:; form-action 'self'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)

    def _kb_with_key(self, owner, slug, key, ip):
        hub = self.hub
        try:
            row = hub.check_artifact_key(owner, slug, key or "")
            proj = hub.project_of_artifact(row["owner"], row["slug"])
            if not proj:
                raise ApiError(409, "no_project", "esta pagina no esta en un proyecto")
            b = self._json_body()
            query = (b.get("q") or "").strip()
            if not query:
                raise ApiError(400, "empty", "falta lo que se busca (q)")
            hub.check_attempts("kbs:%s:%s" % (key[:32], ip), limit=hub.cfg["kb_searches_per_hour"], window=3600)
            passages = hub.kb_search(proj[0], query, True, b.get("k"))
            self._send(200, {"ok": True, "passages": [
                {k: p[k] for k in ("title", "heading", "path", "text")} for p in passages]})
        except ApiError as e:
            self._err(e)

    def _ai_info(self, owner, slug, key):
        """Si la pagina tiene IA, con que modelos y que topes. No gasta cupo."""
        hub = self.hub
        try:
            row = hub.check_artifact_key(owner, slug, key or "")
            on = hub_ai.enabled(hub.cfg)
            proj = hub.project_of_artifact(row["owner"], row["slug"])
            project = None
            if proj:
                project = {"slug": proj[1], "title": proj[2]}
                with hub.db() as c:
                    project["public_docs"] = c.execute(
                        "SELECT COUNT(*) FROM docs WHERE project_id = ? AND visibility = 'public'", (proj[0],)).fetchone()[0]
            self._send(200, {"ok": True, "available": on,
                             "model": hub.cfg["ai_model"] if on else None,
                             "models": hub_ai.models(hub.cfg) if on else [],
                             "per_visitor_hour": hub.cfg["ai_per_visitor_hour"],
                             "per_page_day": hub.cfg["ai_per_page_day"], "project": project})
        except ApiError as e:
            self._err(e)

    def _ai_with_key(self, owner, slug, key, ip):
        hub = self.hub
        try:
            if not hub_ai.enabled(hub.cfg):
                raise ApiError(503, "ai_off", "este hub no tiene IA configurada")
            row = hub.check_artifact_key(owner, slug, key or "")
            try:
                body = json.loads(self._body().decode("utf-8") or "{}")
            except ValueError:
                raise ApiError(400, "bad_json", "el pedido tiene que ser JSON")
            sources = None
            if isinstance(body, dict) and body.get("knowledge"):
                proj = hub.project_of_artifact(row["owner"], row["slug"])
                if not proj:
                    raise ApiError(409, "no_project", "esta pagina no esta en un proyecto: no tiene documentos que consultar")
                msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
                users = [m.get("content") for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
                query = body.get("prompt") if isinstance(body.get("prompt"), str) and body.get("prompt").strip() else (users[-1] if users else "")
                if isinstance(query, str) and query.strip():
                    passages = hub.kb_search(proj[0], query[:2000], True)          # SOLO lo publico
                    sources = hub_kb.sources_of(passages)
                    body = dict(body, system=((body.get("system") or "") + "\n\n" + hub_kb.grounding(passages, proj[2])).strip())
            try:
                payload, stream = hub_ai.build(hub.cfg, body)
            except hub_ai.AiError as e:
                raise ApiError(e.status, e.code, e.message)
            hub.check_ai_quota(key, ip)                 # recien aca: un pedido invalido no gasta cupo
            hub.log("ai", row["owner"], None, row["slug"], None, ip, payload["model"])
            try:
                if stream:
                    return self._ai_stream(payload, sources)
                out = hub_ai.complete(hub.cfg, payload)
                if sources is not None:
                    out["sources"] = sources
            except hub_ai.AiError as e:
                raise ApiError(e.status, e.code, e.message)
            out["ok"] = True
            self._send(200, out)
        except ApiError as e:
            self._err(e)

    def _ai_stream(self, payload, sources=None):
        """Respuesta en vivo: un evento SSE por pedazo de texto. Si el proveedor falla antes de
        empezar, es un error JSON normal; si falla en el medio, un evento {error}."""
        resp = hub_ai.open_stream(self.hub.cfg, payload)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")      # nginx: no juntar, mandar ya
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.end_headers()

            def event(obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()
            if sources is not None:
                event({"sources": sources})
            try:
                for piece in hub_ai.iter_stream(self.hub.cfg, resp):
                    event({"t": piece})
                event({"done": True})
            except hub_ai.AiError as e:
                event({"error": e.code, "message": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass                                         # el visitante cerro la pagina
        finally:
            resp.close()

    def _state_with_key(self, method, owner, slug, key, ip):
        """Lectura y escritura de estado con la clave del artifact. Sin token."""
        hub = self.hub
        try:
            row = hub.check_artifact_key(owner, slug, key)
            hub.check_state_quota(key, method, ip)
            if not self._state_io(method, row["owner"], row["slug"], ip, via="clave"):
                raise ApiError(405, "method_not_allowed", "el estado se lee con GET y se escribe con PUT")
        except ApiError as e:
            self._err(e)

    def _handle(self, method):
        hub = self.hub
        ip = hub_web.client_ip(self)
        try:
            seg, q = self._parts()
            # ¿viene del hostname de la interfaz web? Entonces la atiende web.py.
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            if hub_web.is_app_host(host, hub.cfg):
                return self._web(method, urlparse(self.path).path, q)
            if seg[:1] == ["healthz"]:
                return self._send(200, {"ok": True, "ts": now()})
            if seg[:2] != ["api", "v1"]:
                raise ApiError(404, "not_found", "ruta desconocida")
            rest = seg[2:]

            # la ruta de unirse no lleva token: el codigo de equipo es la credencial
            if rest == ["join"] and method == "POST":
                body = json.loads(self._body().decode("utf-8", "replace") or "{}")
                name = (body.get("name") or "").strip().lower()
                code = (body.get("code") or "").strip()
                token, uid = hub.join(name, code, ip)
                base = hub.cfg.get("app_host") or ""
                return self._send(201, {"ok": True, "user": name, "token": token,
                                        "gallery_url": "https://%s/" % base,
                                        "login_url": "https://%s/e/%s" % (base,
                                                    hub_web.make_login_code(hub, uid))})

            # ESTADO por CLAVE DE ARTIFACT. La credencial es la clave que el hub le inyecto a
            # la pagina, no el token de nadie: por eso va ANTES de authenticate.
            art_key = self.headers.get("X-Artifact-Key")
            if art_key and len(rest) == 3 and rest[0] == "state":
                return self._state_with_key(method, rest[1], rest[2], art_key, ip)
            # CONOCIMIENTO por CLAVE DE ARTIFACT: los pasajes PUBLICOS del proyecto de la pagina.
            if len(rest) == 3 and rest[0] == "kb":
                if method != "POST":
                    raise ApiError(405, "method_not_allowed", "se busca con POST")
                return self._kb_with_key(rest[1], rest[2], art_key, ip)
            # IA por CLAVE DE ARTIFACT: la pagina pide, el hub llama al proveedor con SU clave.
            if len(rest) == 3 and rest[0] == "ai":
                if method == "GET":
                    return self._ai_info(rest[1], rest[2], art_key)
                if method != "POST":
                    raise ApiError(405, "method_not_allowed", "la IA se pide con POST (y se consulta con GET)")
                return self._ai_with_key(rest[1], rest[2], art_key, ip)

            ctx = hub.authenticate(self.headers.get("Authorization"))

            # primero QUE ruta es, despues QUE metodo: una ruta inexistente es 404,
            # una ruta valida con metodo equivocado es 405.
            if rest == ["whoami"]:
                route = "whoami"
            elif rest == ["artifacts"]:
                route = "list"
            elif rest == ["events"]:
                route = "events"
            elif len(rest) == 1 and rest[0] in ("join", "login-link"):
                route = rest[0]
            elif len(rest) == 2 and rest[0] == "artifacts":
                route = "one"
            elif len(rest) == 2 and rest[0] == "apps":
                route = "app"
            elif len(rest) == 3 and rest[0] == "artifacts" and rest[2] in ("thumb", "key", "project"):
                route = "artifact-project" if rest[2] == "project" else rest[2]
            elif rest == ["projects"]:
                route = "projects"
            elif len(rest) == 2 and rest[0] == "projects":
                route = "project"
            elif len(rest) == 3 and rest[0] == "projects" and rest[2] in ("docs", "ask", "search"):
                route = "project-" + rest[2]
            elif len(rest) == 3 and rest[0] == "state":
                route = "state"
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

            # ---- link de un solo uso para entrar al navegador SIN escribir el token
            if route == "login-link":
                if method != "POST":
                    raise ApiError(405, "method_not_allowed", "el link se pide con POST")
                hub.check_attempts("link:" + ctx["token_id"], limit=20)
                base = hub.cfg.get("app_host") or ""
                return self._send(200, {"ok": True,
                                        "url": "https://%s/e/%s" % (base, hub_web.make_login_code(hub, ctx["user_id"])),
                                        "gallery_url": "https://%s/" % base,
                                        "expires_in_minutes": hub_web.LOGIN_CODE_MIN})

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
                project = (q.get("project") or [None])[0]
                if project:
                    with hub.db() as c:
                        hub.project_row(c, project)
                r = hub.deploy_app(ctx, rest[1], body, ip)
                if project:
                    hub.set_artifact_project(ctx, r["slug"], project, ip)
                    r["project"] = project
                return self._send(200 if r["unchanged"] else 201,
                                  {"ok": True, "url": hub.url_for(ctx["name"], r["slug"] + "/"),
                                   "thumb_url": hub.thumb_url(ctx["name"], r["slug"]), **r})

            if route == "projects":
                if method == "GET":
                    return self._send(200, {"ok": True, "projects": hub.projects()})
                if method == "POST":
                    b = self._json_body()
                    return self._send(201, {"ok": True, **hub.project_create(
                        ctx, (b.get("slug") or "").strip().lower(), b.get("title"), b.get("description"), ip)})
                raise ApiError(405, "method_not_allowed", "los proyectos se listan con GET y se crean con POST")

            if route == "project":
                if method == "GET":
                    return self._send(200, {"ok": True, **hub.project_detail(rest[1])})
                if method == "PATCH":
                    b = self._json_body()
                    return self._send(200, {"ok": True, **hub.project_update(ctx, rest[1], b.get("title"), b.get("description"))})
                raise ApiError(405, "method_not_allowed", "un proyecto se lee con GET y se edita con PATCH")

            if route == "project-docs":
                if method == "PUT":
                    path = (q.get("path") or [""])[0]
                    vis = (q.get("visibility") or [None])[0]
                    return self._send(200, {"ok": True, **hub.doc_put(ctx, rest[1], path, self._body(), vis, ip)})
                if method == "PATCH":
                    b = self._json_body()
                    return self._send(200, {"ok": True, **hub.doc_visibility(
                        ctx, rest[1], b.get("paths"), b.get("visibility"), b.get("prefix"), ip)})
                if method == "DELETE":
                    b = self._json_body()
                    return self._send(200, {"ok": True, **hub.doc_delete(ctx, rest[1], b.get("paths"), b.get("prefix"), ip)})
                raise ApiError(405, "method_not_allowed", "documentos: PUT sube, PATCH cambia visibilidad, DELETE borra")

            if route in ("project-search", "project-ask"):
                if method != "POST":
                    raise ApiError(405, "method_not_allowed", "se consulta con POST")
                b = self._json_body()
                query = (b.get("q") or "").strip()
                if not query:
                    raise ApiError(400, "empty", "falta la pregunta (q)")
                hub.check_rate(ctx["token_id"])
                with hub.db() as c:
                    prow = hub.project_row(c, rest[1])
                passages = hub.kb_search(prow["id"], query, bool(b.get("public_only")), b.get("k"))
                if route == "project-search":
                    return self._send(200, {"ok": True, "passages": passages})
                if not hub_ai.enabled(hub.cfg):
                    raise ApiError(503, "ai_off", "este hub no tiene IA configurada")
                try:
                    payload, _ = hub_ai.build(hub.cfg, {"system": hub_kb.grounding(passages, prow["title"]),
                                                        "prompt": query, "model": b.get("model")})
                    out = hub_ai.complete(hub.cfg, payload)
                except hub_ai.AiError as e:
                    raise ApiError(e.status, e.code, e.message)
                return self._send(200, {"ok": True, "text": out["text"], "sources": hub_kb.sources_of(passages)})

            if route == "artifact-project":
                if method != "PUT":
                    raise ApiError(405, "method_not_allowed", "el proyecto de un artifact se cambia con PUT")
                b = self._json_body()
                return self._send(200, {"ok": True, **hub.set_artifact_project(ctx, rest[1], b.get("project") or None, ip)})

            if route == "thumb":
                slug = rest[1]
                if method == "PUT":
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype != "image/png":
                        raise ApiError(415, "bad_type",
                                       "la vista previa se sube como image/png (recibi '%s')" % (ctype or "vacio"))
                    return self._send(201, {"ok": True, **hub.put_thumb(ctx, slug, self._body(), ip)})
                if method == "DELETE":
                    return self._send(200, {"ok": True, **hub.delete_thumb(ctx, slug, ip)})
                raise ApiError(405, "method_not_allowed", "la vista previa se sube con PUT y se borra con DELETE")

            if route == "key":
                row = hub.mine_of(ctx, rest[1])
                if method == "GET":
                    return self._send(200, {"ok": True, **hub.reveal_key(row["owner"], row["slug"])})
                if method == "POST":
                    epoch = hub.rotate_key(row["owner"], row["slug"])
                    return self._send(200, {"ok": True, "slug": row["slug"], "epoch": epoch,
                                            "note": "la pagina ya servida dejo de poder; republica para que tenga la clave nueva"})
                if method == "DELETE":
                    off = (q.get("on", ["0"])[0] not in ("1", "true", "yes"))
                    hub.revoke_key(row["owner"], row["slug"], off=off)
                    return self._send(200, {"ok": True, "slug": row["slug"], "revoked": off,
                                            "note": "apagado: la proxima publicacion no inyecta runtime"})
                raise ApiError(405, "method_not_allowed",
                               "la clave se lee con GET, se rota con POST y se apaga con DELETE")

            # El estado siempre responde el DOCUMENTO (vacio si no hay nada), para que una
            # pagina y el CLI lean lo mismo. PUT devuelve el acuse, no el documento.
            if route == "state":
                row = hub.artifact_of(rest[1], rest[2])
                if not ctx["is_admin"] and row["owner"] != ctx["name"]:
                    raise ApiError(403, "not_owner", "ese artifact no es tuyo")
                if method in ("GET", "HEAD", "PUT"):
                    return self._state_io(method, row["owner"], row["slug"], ip, via="token")
                if method == "DELETE":
                    hub.delete_state(row["owner"], row["slug"], ip)
                    return self._send(200, {"ok": True, "removed": True})
                raise ApiError(405, "method_not_allowed",
                               "el estado se lee con GET, se escribe con PUT y se borra con DELETE")

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
                    project = (q.get("project") or [None])[0]
                    if project:
                        with hub.db() as c:
                            hub.project_row(c, project)          # que exista ANTES de publicar
                    r = hub.publish(ctx, rest[1], body, title or extract_title(body), ip)
                    if project:
                        hub.set_artifact_project(ctx, r["slug"], project, ip)
                        r["project"] = project
                    return self._send(200 if r["unchanged"] else 201,
                                      {"ok": True, "url": hub.url_for(ctx["name"], r["name"]),
                                       "snapshot_url": hub.url_for(ctx["name"], r["snapshot"]) if r.get("snapshot") else None,
                                       "thumb_url": hub.thumb_url(ctx["name"], r["slug"]),
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

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")


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
