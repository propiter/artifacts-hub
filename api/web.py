#!/usr/bin/env python3
"""artifacts-hub — la interfaz web (galería, invitaciones, administración).

Se sirve SOLO cuando el request entra por el hostname de la app. Ese hostname es
distinto al de los artifacts a propósito y no es cosmética: los artifacts son HTML
de terceros, y si compartieran origen con esta interfaz, el JavaScript de cualquiera
de ellos podría leer la sesión de quien está mirando la galería. Distinto origen, y
el iframe de un artifact no alcanza la cookie.

Estado de sesión: cookie httpOnly + SameSite=Strict + Secure, con un id aleatorio;
en la base queda solo su sha256. Los POST llevan token CSRF por formulario.
"""
import hashlib
import html
import json
import os
import re
import secrets
import time

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
SESSION_DAYS = 30
LOGIN_CODE_MIN = 10
COOKIE = "hub_session"



def client_ip(handler):
    """La IP de quien hace el pedido. La pone nginx en X-Real-IP (con el modulo realip ya
    resuelto contra el proxy de adelante); el X-Forwarded-For lo puede escribir el cliente y
    con el se salteaban los cupos. La API no se publica: solo nginx llega hasta aca."""
    return (handler.headers.get("X-Real-IP") or handler.client_address[0]).strip()

def now():
    return int(time.time())


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def fmt_bytes(n):
    n = int(n or 0)
    for u, d in (("MB", 1048576), ("KB", 1024)):
        if n >= d:
            return "%.1f %s" % (n / d, u)
    return "%d B" % n


def fmt_rel(ts):
    s = now() - int(ts or 0)
    if s < 90:
        return "recién"
    if s < 3600:
        return "hace %d min" % (s // 60)
    if s < 86400:
        return "hace %d h" % (s // 3600)
    if s < 2592000:
        return "hace %d d" % (s // 86400)
    return time.strftime("%d/%m/%Y", time.localtime(ts))


# ---------------------------------------------------------------- sesiones

def new_cookie_token():
    return secrets.token_urlsafe(32)


def hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


def create_session(hub, user_id, ip=None):
    sid = new_cookie_token()
    csrf = new_cookie_token()
    with hub.db() as c:
        c.execute(
            "INSERT INTO sessions (sid, user_id, created_at, expires_at, last_seen, ip, csrf)"
            " VALUES (?,?,?,?,?,?,?)",
            (hash_token(sid), user_id, now(), now() + SESSION_DAYS * 86400, now(), ip, csrf),
        )
    return sid, csrf


def get_session(hub, sid):
    if not sid:
        return None
    with hub.db() as c:
        row = c.execute(
            "SELECT s.user_id, s.expires_at, s.csrf, u.name, u.is_admin, u.share_team, u.display_name"
            "  FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.sid = ?",
            (hash_token(sid),),
        ).fetchone()
    if not row or row["expires_at"] < now():
        return None
    return {"user_id": row["user_id"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "share_team": bool(row["share_team"]), "display_name": row["display_name"],
            "csrf": row["csrf"]}


def drop_session(hub, sid):
    if sid:
        with hub.db() as c:
            c.execute("DELETE FROM sessions WHERE sid = ?", (hash_token(sid),))


def check_csrf(session, form):
    """Token sincronizador: el valor de la sesion viaja en el formulario y se compara.
    SameSite=Strict ya frena el CSRF clasico; esto es la segunda baranda."""
    got = (form.get("csrf") or [""])[0]
    want = session.get("csrf") or ""
    return bool(got) and bool(want) and secrets.compare_digest(got, want)


# ---------------------------------------------------------------- codigos

def make_login_code(hub, user_id, minutes=LOGIN_CODE_MIN):
    code = secrets.token_urlsafe(24)
    with hub.db() as c:
        c.execute("INSERT INTO login_codes (code, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                  (hash_token(code), user_id, now(), now() + minutes * 60))
    return code


def consume_login_code(hub, code):
    if not code:
        return None
    with hub.db() as c:
        row = c.execute("SELECT code, user_id, expires_at, used_at FROM login_codes WHERE code = ?",
                        (hash_token(code),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now():
            return None
        c.execute("UPDATE login_codes SET used_at = ? WHERE code = ?", (now(), row["code"]))
        return row["user_id"]


def roller_team_code(hub, label=None):
    code = secrets.token_urlsafe(18)
    with hub.db() as c:
        c.execute("UPDATE team_codes SET revoked_at = ? WHERE revoked_at IS NULL", (now(),))
        c.execute("INSERT INTO team_codes (code, label, created_at) VALUES (?,?,?)",
                  (hash_token(code), label, now()))
    return code


def team_code_ok(hub, code):
    if not code:
        return False
    with hub.db() as c:
        row = c.execute("SELECT revoked_at FROM team_codes WHERE code = ?", (hash_token(code),)).fetchone()
    return bool(row) and not row["revoked_at"]


# ---------------------------------------------------------------- datos

def my_artifacts(hub, user_id):
    with hub.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT a.slug, a.title, a.version, a.bytes, a.created_at, a.updated_at, a.shared,"
            "       u.name AS owner"
            "  FROM artifacts a JOIN users u ON u.id = a.user_id"
            " WHERE a.user_id = ? AND a.deleted_at IS NULL ORDER BY a.updated_at DESC", (user_id,))]
    return [_with_url(hub, r) for r in rows]


def _with_url(hub, r):
    owner = r.get("owner")
    if not owner:
        raise RuntimeError("falta el dueno del artifact: la URL quedaria mal")
    is_app = os.path.isdir(os.path.join(hub.data, "a", owner, r["slug"]))
    name = r["slug"] + ("/" if is_app else ".html")
    r["kind"] = "app" if is_app else "page"
    r["url"] = hub.url_for(owner, name)
    return r


def team_spaces(hub):
    """Lo que el equipo ve. Dos formas de compartir, y la diferencia importa:

    - `users.share_team = 1`  -> el espacio ENTERO (todos sus artifacts)
    - `artifacts.shared = 1`   -> SOLO ese artifact, sin abrir el resto del espacio

    Alguien aparece acá si compartió el espacio o si marcó al menos un artifact. Un espacio
    compartido no necesita que sus artifacts estén marcados uno por uno: el flag del espacio manda.
    """
    cols = ("SELECT slug, title, version, bytes, created_at, updated_at, shared, 'x' AS owner"
            "  FROM artifacts WHERE user_id = ? AND deleted_at IS NULL")
    with hub.db() as c:
        users = [dict(r) for r in c.execute(
            "SELECT id, name, display_name, share_team FROM users"
            " WHERE share_team = 1 OR id IN (SELECT DISTINCT user_id FROM artifacts"
            "        WHERE shared = 1 AND deleted_at IS NULL)"
            " ORDER BY name")]
        for u in users:
            if u["share_team"]:
                u["artifacts"] = [dict(r) for r in c.execute(cols + " ORDER BY updated_at DESC", (u["id"],))]
            else:
                u["artifacts"] = [dict(r) for r in c.execute(
                    cols + " AND shared = 1 ORDER BY updated_at DESC", (u["id"],))]
    for u in users:
        for a in u["artifacts"]:
            a["owner"] = u["name"]
            _with_url(hub, a)
    return users


def admin_rows(hub):
    with hub.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT u.name, u.is_admin, u.share_team,"
            "       (SELECT COUNT(*) FROM artifacts a WHERE a.user_id = u.id AND a.deleted_at IS NULL) AS n,"
            "       (SELECT COALESCE(SUM(bytes),0) FROM artifacts a WHERE a.user_id = u.id AND a.deleted_at IS NULL) AS bytes,"
            "       (SELECT MAX(ts) FROM events e WHERE e.user_name = u.name) AS last_ts,"
            "       (SELECT COUNT(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS tokens"
            "  FROM users u ORDER BY u.name")]


def admin_events(hub, limit=40):
    with hub.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT ts, user_name, action, artifact, bytes, detail FROM events"
            " ORDER BY ts DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- paginas

CSS = """
:root{--bg:#f6f7f9;--fg:#14161a;--muted:#6b7280;--line:rgba(17,24,39,.10);--card:#fff;
--accent:#3b6ef0;--chip:rgba(17,24,39,.05);--soft:#eef1f6;
--shadow:0 1px 2px rgba(16,24,40,.05),0 10px 30px -18px rgba(16,24,40,.35)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e9eaee;--muted:#9aa1ac;
--line:rgba(255,255,255,.11);--card:#171a20;--accent:#8ab4ff;--chip:rgba(255,255,255,.07);
--soft:#1d2129;--shadow:0 1px 2px rgba(0,0,0,.4),0 14px 34px -20px rgba(0,0,0,.8)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:26px 18px 60px}
.top{display:flex;align-items:center;gap:12px;padding:8px 0 20px;flex-wrap:wrap}
.logo{width:30px;height:30px;border-radius:9px;background:var(--accent);display:grid;place-items:center;color:#fff;font-weight:700;font-size:14px}
.top h1{font-size:17px;margin:0;letter-spacing:-.01em}
.who{margin-left:auto;display:flex;align-items:center;gap:9px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.av{width:26px;height:26px;border-radius:999px;background:var(--soft);display:grid;place-items:center;font-size:11px;font-weight:700;color:var(--fg)}
.tabs{display:flex;gap:4px;border:1px solid var(--line);border-radius:11px;padding:4px;background:var(--card);width:fit-content;margin-bottom:22px;flex-wrap:wrap}
.tabs a{color:var(--muted);font-size:13.5px;padding:7px 14px;border-radius:8px;text-decoration:none}
.tabs a[aria-current=page]{background:var(--accent);color:#fff}
h2{font-size:20px;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13.5px;margin:0 0 20px}
.bar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
input[type=search],input[type=text],input[type=password]{flex:1 1 200px;min-width:170px;padding:10px 13px;
border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;outline:none}
input:focus{border-color:var(--accent)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(270px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow);display:flex;flex-direction:column}
.thumb{position:relative;aspect-ratio:4/3;background:var(--soft);overflow:hidden;border-bottom:1px solid var(--line)}
.thumb iframe{position:absolute;top:0;left:0;width:150%;height:150%;border:0;transform:scale(.6667);transform-origin:top left;pointer-events:none}
.thumb:after{content:'';position:absolute;left:0;right:0;bottom:0;height:40px;background:linear-gradient(to bottom,transparent,var(--card));pointer-events:none}
.meta{padding:13px 14px 14px;display:flex;flex-direction:column;gap:8px}
.ttl{font-weight:600;font-size:14px;letter-spacing:-.01em;word-break:break-word}
.file{font:11.5px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);word-break:break-all}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;color:var(--muted);font-size:12.5px;flex-wrap:wrap}
.acts{display:flex;gap:7px}
.btn{font:inherit;font-size:12.5px;text-decoration:none;padding:6px 11px;border-radius:8px;border:1px solid var(--line);
color:var(--fg);background:var(--chip);cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.p{background:var(--accent);color:#fff;border-color:transparent}.btn.p:hover{color:#fff;filter:brightness(1.06)}
.tag{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;padding:3px 7px;border-radius:6px;background:var(--chip);color:var(--muted);font-weight:600}
.tag.app{background:rgba(59,110,240,.16);color:var(--accent)}
.box{border:1px solid var(--line);border-radius:14px;background:var(--card);padding:24px;box-shadow:var(--shadow);max-width:660px}
.box h3{margin:0 0 5px;font-size:16px}.box p{margin:0 0 16px;color:var(--muted);font-size:13.5px}
code,.code,.code a{overflow-wrap:anywhere;word-break:break-word}
.code{font:12.5px/1.6 ui-monospace,Menlo,monospace;background:var(--soft);border:1px solid var(--line);
border-radius:9px;padding:11px 13px;word-break:break-all;margin:8px 0}
.step{display:flex;gap:11px;margin-bottom:15px}
.num{flex:0 0 22px;height:22px;border-radius:999px;background:var(--accent);color:#fff;display:grid;place-items:center;font-size:11.5px;font-weight:700;margin-top:2px}
.warn{background:rgba(245,158,11,.14);border:1px solid rgba(245,158,11,.35);border-radius:10px;padding:11px 13px;font-size:13px;margin:14px 0 4px}
.ok{background:rgba(16,185,129,.13);border:1px solid rgba(16,185,129,.35);border-radius:10px;padding:11px 13px;font-size:13px;margin:14px 0 4px}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:10px 13px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}
.empty{color:var(--muted);text-align:center;padding:52px 20px;border:1px dashed var(--line);border-radius:14px}
.person{margin:26px 0 0}
.person h3{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.note{margin-top:26px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);padding-top:14px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 5px}
.err{background:rgba(239,68,68,.13);border:1px solid rgba(239,68,68,.35);border-radius:10px;padding:11px 13px;font-size:13.5px;margin:0 0 14px}

  .pgrid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));margin-bottom:26px}
  .pcard{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;
    box-shadow:var(--shadow);color:var(--fg);text-decoration:none}
  .pcard:hover{border-color:var(--accent)}
  .pcard b{display:block;font-size:15.5px;letter-spacing:-.01em;margin-bottom:4px}
  .pcard span{display:block;color:var(--muted);font-size:13px}
  .sec{margin:30px 0 0}.sec h3{font-size:16px;margin:0 0 4px}.sec>p{color:var(--muted);font-size:13.5px;margin:0 0 12px}
  .ask{display:flex;gap:8px;flex-wrap:wrap}
  .ask input{flex:1 1 260px}
  .answer{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:12px;font-size:14.5px}
  .answer:empty{display:none}
  .srcs{margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:12.5px}
  .vis{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;padding:3px 7px;border-radius:6px;font-weight:700}
  .vis.pub{background:rgba(16,185,129,.15);color:#0f8a5f}.vis.int{background:var(--chip);color:var(--muted)}
  .drop{border:1.5px dashed var(--line);border-radius:12px;padding:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--card)}
  .drop.on{border-color:var(--accent)}
  .log{font:12.5px/1.6 ui-monospace,Menlo,monospace;color:var(--muted);margin-top:8px;white-space:pre-wrap}
  .tbl td.p{font:12.5px ui-monospace,Menlo,monospace;word-break:break-all}
  @media(max-width:700px){.tbl thead{display:none}.tbl table,.tbl tbody,.tbl tr,.tbl td{display:block;width:100%}
    .tbl tr{border-bottom:1px solid var(--line);padding:10px 13px}.tbl td{border:0;padding:3px 0}
    .tbl td.n:before{content:"Pasajes: ";color:var(--muted)}}
  .box input[type=text]{width:100%}
  .answer p{margin:0 0 8px}.answer p:last-child{margin:0}.answer ul{margin:0 0 8px;padding-left:20px}
  .arts{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
  .art{display:flex;justify-content:space-between;gap:8px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 13px;font-size:13.5px}
  .art span{min-width:0;overflow-wrap:anywhere}
  .sharebar {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 14px; margin: 0 0 14px; font-size: 14px;
  }
  .sharebar .txt { color: var(--muted); }
  .sharebar .txt b { color: var(--fg); font-weight: 640; }
  .btn.on { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, transparent); font-weight: 640; }
"""


def layout(title, body, session=None, cfg=None, active=""):
    who = ""
    if session:
        tabs = [("galeria", "Mi galería", "/"), ("proyectos", "Proyectos", "/proyectos"),
                ("equipo", "Del equipo", "/equipo")]
        if session["is_admin"]:
            tabs.append(("admin", "Administración", "/admin"))
        nav = "".join(
            '<a href="%s"%s>%s</a>' % (h, ' aria-current="page"' if k == active else "", t)
            for k, t, h in tabs)
        who = ('<div class="who"><span class="av">%s</span> %s'
               ' <form method="post" action="/salir" style="display:inline">%s'
               '<button class="btn">Salir</button></form></div>'
               % (esc(session["name"][:2].upper()), esc(session["display_name"] or session["name"]),
                  '<input type="hidden" name="csrf" value="%s">' % esc(session.get("_csrf", ""))))
        body = ('<div class="tabs">%s</div>%s' % (nav, body))
    return ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">"
            "<div class=\"top\"><div class=\"logo\">a</div><h1>Artifacts · Whitelabel</h1>%s</div>"
            "%s</div></body></html>" % (esc(title), CSS, who, body))


def card(a, show_owner=False, csrf="", can_delete=True):
    thumb = ('<div class="thumb"><iframe loading="lazy" src="%s" tabindex="-1" '
             'title="Vista previa"></iframe></div>' % esc(a["url"]))
    owner = ""
    if show_owner:
        owner = '<div class="file">%s · %s</div>' % (esc(a.get("owner", "")), esc(a["slug"]))
    else:
        owner = '<div class="file">%s</div>' % esc(a["slug"] + ("/" if a["kind"] == "app" else ".html"))
    share_btn = ""
    if can_delete and csrf:
        on = bool(a.get("shared"))
        share_btn = ('<form method="post" action="/compartir" style="display:inline">'
                     '<input type="hidden" name="csrf" value="%s">'
                     '<input type="hidden" name="slug" value="%s">'
                     '<button class="btn%s" title="%s">%s</button></form>'
                     % (esc(csrf), esc(a["slug"]), " on" if on else "",
                        "Que el equipo lo vea en «Del equipo»" if not on else "Dejar de compartirlo",
                        "Compartido" if on else "Compartir"))
    del_btn = ""
    if can_delete and csrf:
        del_btn = ('<form method="post" action="/borrar" style="display:inline" '
                   'onsubmit="return confirm(\'Esto borra el artifact y TODAS sus versiones: '
                   'todo link que hayas compartido deja de funcionar.\')">'
                   '<input type="hidden" name="csrf" value="%s">'
                   '<input type="hidden" name="slug" value="%s">'
                   '<button class="btn">Borrar</button></form>' % (esc(csrf), esc(a["slug"])))
    return ('<article class="card">%s<div class="meta">'
            '<div class="ttl">%s</div>%s'
            '<div class="row"><span><span class="tag%s">%s</span> v%d · %s</span>'
            '<span class="acts"><a class="btn p" href="%s" target="_blank" rel="noreferrer">Abrir</a>'
            '%s%s</span></div></div></article>'
            % (thumb, esc(a.get("title") or a["slug"]), owner,
               " app" if a["kind"] == "app" else "", a["kind"],
               a["version"], fmt_bytes(a["bytes"]), esc(a["url"]), share_btn, del_btn))


def page_login(error=None, notice=None):
    e = '<div class="err">%s</div>' % esc(error) if error else ""
    n = '<div class="ok">%s</div>' % esc(notice) if notice else ""
    return layout("Entrar", """
      <h2>Entrar</h2>
      <p class="sub">Pegá tu token una sola vez. Queda la sesión guardada en este navegador y no te lo
        vuelve a pedir.</p>
      <div class="box">%s%s
        <form method="post" action="/entrar">
          <label>Tu token (wlart_…)</label>
          <input type="password" name="token" autocomplete="off" spellcheck="false" placeholder="wlart_…">
          <div style="margin-top:16px"><button class="btn p">Entrar</button></div>
        </form>
        <div class="note">También podés entrar con el link de un solo uso que te da
          <b>wl-artifact join</b>. Si todavía no tenés cuenta, pedile el código de equipo a quien
          administra el hub.</div>
      </div>""" % (e, n))


def page_join(error=None):
    e = '<div class="err">%s</div>' % esc(error) if error else ""
    return layout("Crear cuenta", """
      <h2>Crear cuenta</h2>
      <p class="sub">Con el código de equipo, en un paso.</p>
      <div class="box">%s
        <form method="post" action="/unirse">
          <label>Usuario (así vas a aparecer)</label>
          <input type="text" name="name" placeholder="juan" autocapitalize="off">
          <label>Código de equipo</label>
          <input type="password" name="code" autocomplete="off" spellcheck="false">
          <div style="margin-top:16px"><button class="btn p">Crear mi cuenta</button></div>
        </form>
        <div class="note">Lo más fácil: pedile a Hermes que lo haga —
          <b>wl-artifact join &lt;código&gt;</b> te crea la cuenta, guarda tu token en tu máquina
          y te deja la galería abierta. Nunca tenés que copiar el token a mano.</div>
      </div>""" % e)


def page_gallery(hub, session, arts, notice=None):
    n = '<div class="ok">%s</div>' % esc(notice) if notice else ""
    if arts:
        cards = "".join(card(a, csrf=session.get("_csrf")) for a in arts)
        body = '<div class="grid">%s</div>' % cards
    else:
        body = ('<div class="empty">Todavía no publicaste nada.<br><br>'
                'Pedile a Hermes un artifact, o publicá con<br>'
                '<span class="code" style="display:inline-block;margin-top:12px">'
                'wl-artifact publish mi-pagina.html mi-slug</span></div>')
    total = sum(a["bytes"] for a in arts)
    share_team = bool(session.get("share_team"))
    if share_team:
        txt = ('Tu espacio <b>completo</b> está compartido: el equipo lo ve entero en «Del equipo». '
               'Los botones de cada tarjeta quedan de más mientras esto esté activo.')
        btn = "Dejar de compartir mi espacio"
    else:
        txt = ('Tu espacio <b>no</b> está compartido. El equipo sólo ve los artifacts que marques '
               'con <b>Compartir</b> en su tarjeta.')
        btn = "Compartir mi espacio completo"
    esp = ('<div class="sharebar"><span class="txt">%s</span>'
           '<form method="post" action="/compartir" style="display:inline">'
           '<input type="hidden" name="csrf" value="%s">'
           '<button class="btn%s">%s</button></form></div>'
           % (txt, esc(session.get("_csrf", "")), " on" if share_team else "", btn))
    return layout("Mi galería", """
      <h2>Mi galería</h2>
      <p class="sub">%d artifact(s) · %s usados · cada tarjeta tiene su link público: eso es lo que
        compartís, nunca el archivo de tu computadora.</p>
      %s%s%s%s""" % (len(arts), fmt_bytes(total), n, esp,
                   '<div class="bar"><input type="search" id="q" placeholder="Buscar…"></div>', body),
        session=session, active="galeria") + _FILTER_JS


_FILTER_JS = """<script>
(function(){var q=document.getElementById('q');if(!q)return;
q.addEventListener('input',function(){var v=q.value.toLowerCase();
document.querySelectorAll('.card').forEach(function(c){
c.style.display=c.textContent.toLowerCase().indexOf(v)>-1?'':'none';});});})();
</script>"""


def page_team(hub, session, spaces):
    if not spaces:
        body = ('<div class="empty">Nadie está compartiendo su espacio todavía.<br><br>'
                'En <b>Mi galería</b> podés <b>compartir tu espacio completo</b>, o marcar '
                '<b>Compartir</b> en la tarjeta de un artifact suelto.</div>')
    else:
        bloques = []
        for u in spaces:
            titulo = u["display_name"] or u["name"]
            if u["artifacts"]:
                tarjetas = "".join(card(a, show_owner=False, can_delete=False) for a in u["artifacts"])
                bloques.append('<div class="person"><h3><span class="av">%s</span>%s '
                               '<span class="tag">%d</span></h3><div class="grid" style="margin-top:12px">%s</div></div>'
                               % (esc(titulo[:2].upper()), esc(titulo), len(u["artifacts"]), tarjetas))
        body = "".join(bloques)
    return layout("Del equipo", """
      <h2>Del equipo</h2>
      <p class="sub">Lo que cada persona decidió compartir: su espacio completo, o artifacts sueltos.</p>
      %s""" % body, session=session, active="equipo")


def _hidden_csrf(session):
    return '<input type="hidden" name="csrf" value="%s">' % esc(session.get("_csrf", ""))


def page_projects(hub, session, projects, error=None):
    e = '<div class="err">%s</div>' % esc(error) if error else ""
    if projects:
        cards = "".join(
            '<a class="pcard" href="/p/%s"><b>%s</b><span>%s</span>'
            '<span style="margin-top:8px">%d artifacts · %d documentos (%d públicos) · %s</span></a>'
            % (esc(p["slug"]), esc(p["title"]), esc(p["description"] or p["slug"]),
               p["artifacts"], p["docs"], p["public_docs"], esc(fmt_rel(p["updated_at"])))
            for p in projects)
        body = '<div class="pgrid">%s</div>' % cards
    else:
        body = ('<div class="empty" style="margin-bottom:26px">Todavía no hay proyectos.<br><br>'
                'Un proyecto junta las páginas y los documentos de un tema (un cliente, un producto),'
                ' y la IA de sus páginas responde con esos documentos.</div>')
    form = ('<div class="box"><h3>Nuevo proyecto</h3><p>Lo ve y lo usa todo el equipo.</p>'
            '<form method="post" action="/proyectos">%s'
            '<label>Nombre corto (va en los comandos)</label><input type="text" name="slug" required '
            'pattern="[a-z0-9][a-z0-9-]{0,60}" placeholder="zellia">'
            '<label>Título</label><input type="text" name="title" required placeholder="Zellia — ecosistema y ventas">'
            '<label>De qué se trata</label><input type="text" name="description" placeholder="opcional">'
            '<div style="margin-top:14px"><button class="btn p">Crear</button></div></form></div>'
            % _hidden_csrf(session))
    return layout("Proyectos", """
      <h2>Proyectos</h2>
      <p class="sub">Páginas y documentos de un mismo tema, de todo el equipo. Los agentes los usan
        solos: si pedís algo sobre un tema con proyecto, publican ahí y consultan sus documentos.</p>
      %s%s%s""" % (e, body, form), session=session, active="proyectos")


# Hacer publico un documento lo pone al alcance de CUALQUIERA con el link de una pagina del
# proyecto (preguntandole a su IA). Un clic suelto lo hacia sin decirlo: paso con un precio
# cotizado a un cliente.
_CONFIRM_PUBLIC = (' onsubmit="return confirm(\'Público = cualquiera con el link de una página de este proyecto '
                   'puede preguntarle a su IA por este documento. Precios a clientes, reuniones y datos internos '
                   'NO van públicos. ¿Hacerlo público?\')"')


def page_project(hub, session, d):
    slug = d["slug"]
    arts = "".join(
        '<div class="art"><span><b>%s</b><br><span class="file">%s · %s</span></span>'
        '<a class="btn p" href="%s" target="_blank" rel="noreferrer">Abrir</a></div>'
        % (esc(a["title"] or a["slug"]), esc(a["owner"]), esc(a["slug"]), esc(a["url"]))
        for a in d["artifact_list"]) or '<div class="empty" style="padding:24px">Sin páginas todavía.</div>'
    rows = []
    for x in d["documents"]:
        pub = x["visibility"] == "public"
        rows.append(
            '<tr><td class="p">%s</td><td>%s</td><td><span class="vis %s">%s</span></td><td class="n">%d</td>'
            '<td style="white-space:nowrap">'
            '<form method="post" action="/p/%s/visibilidad" style="display:inline"%s>%s'
            '<input type="hidden" name="path" value="%s"><input type="hidden" name="visibility" value="%s">'
            '<button class="btn" title="%s">%s</button></form> '
            '<form method="post" action="/p/%s/sacar" style="display:inline" '
            'onsubmit="return confirm(\'¿Sacar este documento del proyecto?\')">%s'
            '<input type="hidden" name="path" value="%s"><button class="btn">Sacar</button></form></td></tr>'
            % (esc(x["path"]), esc(x["title"]), "pub" if pub else "int", "público" if pub else "interno",
               x["chunks"], esc(slug), "" if pub else _CONFIRM_PUBLIC, _hidden_csrf(session), esc(x["path"]),
               "internal" if pub else "public",
               "Solo el equipo y los agentes" if pub else "También la IA de las páginas (cualquiera con el link)",
               "Hacer interno" if pub else "Hacer público", esc(slug), _hidden_csrf(session), esc(x["path"])))
    table = ('<div class="tbl"><table><thead><tr><th>Documento</th><th>Título</th><th>Quién lo ve</th>'
             '<th>Pasajes</th><th></th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows)
             if rows else '<div class="empty" style="padding:24px">Sin documentos todavía.</div>')
    body = """
      <p class="sub" style="margin:0 0 4px"><a href="/proyectos" style="color:inherit">← Proyectos</a></p>
      <h2>%(title)s</h2>
      <p class="sub">%(desc)s · %(nd)d documentos (%(np)d públicos) · <code>%(slug)s</code></p>

      <div class="sec"><h3>Preguntale al proyecto</h3>
        <p>Responde con TODOS sus documentos, internos incluidos, y dice de dónde lo sacó.</p>
        <form class="ask" id="ask">%(csrf)s<input type="text" name="q" required placeholder="¿Qué se decidió sobre precios?">
          <button class="btn p">Preguntar</button></form>
        <div class="answer" id="answer" aria-live="polite"></div><ol class="srcs" id="srcs"></ol></div>

      <div class="sec"><h3>Páginas</h3><p>Su IA consulta solo los documentos públicos.</p>
        <div class="arts">%(arts)s</div></div>

      <div class="sec"><h3>Documentos</h3>
        <p><b>Interno</b>: lo consultan el equipo y los agentes. <b>Público</b>: además, la IA de las páginas,
          o sea cualquiera con el link. Un archivo con contraseñas o tokens se rechaza.</p>
        <div class="drop" id="drop"><input type="file" id="files" multiple
          accept=".md,.markdown,.txt,.csv,.tsv,.json,.yaml,.yml,.rst,.html,.htm,.pdf,.docx">
          <label style="margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="pub"> públicos</label>
          <button class="btn p" id="up" type="button">Subir</button></div>
        <div class="log" id="log"></div>
        <div style="margin-top:14px">%(table)s</div></div>
    """ % {"title": esc(d["title"]), "desc": esc(d["description"] or "Proyecto del equipo"),
           "nd": d["docs"], "np": d["public_docs"], "slug": esc(slug), "csrf": _hidden_csrf(session),
           "arts": arts, "table": table}
    js = r"""<script>
(function(){
var slug=%s, csrf=document.querySelector('input[name=csrf]').value;
// markdown minimo (parrafos, listas, **negrita**) armado como DOM: la respuesta nunca es HTML
function inl(el, s){ s.split(/(\*\*[^*]+\*\*)/).forEach(function(p){ if(!p) return;
  if(/^\*\*[^*]+\*\*$/.test(p)){ var b=document.createElement('strong'); b.textContent=p.slice(2,-2); el.appendChild(b); }
  else el.appendChild(document.createTextNode(p)); }); }
function paint(box, text){ box.textContent=''; var ul=null;
  String(text||'').split('\n').forEach(function(line){ var m=line.match(/^\s*(?:[-*\u2022]|\d+[.)])\s+(.*)$/);
    if(m){ if(!ul){ ul=document.createElement('ul'); box.appendChild(ul); } var li=document.createElement('li'); inl(li, m[1]); ul.appendChild(li); return; }
    ul=null; if(!line.trim()) return; var p=document.createElement('p'); inl(p, line.replace(/^#+\s*/,'')); box.appendChild(p); }); }
var ask=document.getElementById('ask'), ans=document.getElementById('answer'), srcs=document.getElementById('srcs');
ask.addEventListener('submit', async function(ev){ ev.preventDefault();
  var btn=ask.querySelector('button'); btn.disabled=true; ans.textContent='Buscando en los documentos…'; srcs.textContent='';
  try{ var r=await fetch('/p/'+slug+'/preguntar',{method:'POST',body:new URLSearchParams(new FormData(ask))});
    var d=await r.json(); if(!r.ok) throw new Error(d.message||'no se pudo');
    paint(ans, d.text);
    (d.sources||[]).forEach(function(s){ var li=document.createElement('li');
      li.textContent=s.path+(s.heading?' › '+s.heading:''); srcs.appendChild(li); });
  }catch(e){ ans.textContent='Error: '+e.message; } finally { btn.disabled=false; } });
var drop=document.getElementById('drop'), files=document.getElementById('files'), log=document.getElementById('log');
['dragover','dragenter'].forEach(function(t){drop.addEventListener(t,function(e){e.preventDefault();drop.classList.add('on');});});
['dragleave','drop'].forEach(function(t){drop.addEventListener(t,function(){drop.classList.remove('on');});});
drop.addEventListener('drop',function(e){e.preventDefault(); files.files=e.dataTransfer.files;});
document.getElementById('up').addEventListener('click', async function(){
  var list=[].slice.call(files.files), vis=document.getElementById('pub').checked?'public':'internal', okAny=false;
  if(!list.length){ log.textContent='Elegí uno o más archivos.'; return; }
  log.textContent='';
  for(var i=0;i<list.length;i++){ var f=list[i];
    var line=document.createElement('div'); line.textContent='… '+f.name; log.appendChild(line);
    try{ var r=await fetch('/p/'+slug+'/documento?path='+encodeURIComponent(f.name)+'&visibility='+vis,
           {method:'PUT',body:f,headers:{'X-CSRF':csrf}});
      var d=await r.json();
      if(r.ok){ okAny=true; line.textContent='✓ '+f.name+' — '+({created:'nuevo',updated:'actualizado',unchanged:'sin cambios',visibility:'cambió quién lo ve'})[d.status]; }
      else if(d.error==='secrets'){ line.textContent='✗ '+f.name+' — tiene credenciales en '+d.findings.map(function(x){return 'línea '+x.line+' ('+x.kind+')';}).join(', ')+'. No se subió.'; }
      else line.textContent='✗ '+f.name+' — '+(d.message||r.status);
    }catch(e){ line.textContent='✗ '+f.name+' — '+e.message; } }
  if(okAny) setTimeout(function(){ location.reload(); }, 900); });
})();
</script>""" % json.dumps(slug)
    return layout(d["title"], body, session=session, active="proyectos") + js


def page_admin(hub, session, rows, events, team_code=None, notice=None):
    n = '<div class="ok">%s</div>' % esc(notice) if notice else ""
    filas = "".join(
        '<tr><td><span class="av" style="display:inline-grid;vertical-align:middle">%s</span> %s%s</td>'
        '<td>%d</td><td>%s</td><td>%s</td><td>%d</td></tr>'
        % (esc((r["name"] or "?")[:2].upper()), esc(r["name"]),
           ' <span class="tag">admin</span>' if r["is_admin"] else "",
           r["n"], fmt_bytes(r["bytes"]),
           fmt_rel(r["last_ts"]) if r["last_ts"] else "nunca", r["tokens"])
        for r in rows)
    evs = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                  % (fmt_rel(e["ts"]), esc(e["user_name"] or "-"), esc(e["action"]),
                     esc(e["artifact"] or "") + (" · " + esc(e["detail"]) if e["detail"] else ""))
                  for e in events)
    inv = ""
    if team_code:
        inv = ('<div class="ok">Código de equipo nuevo (se muestra UNA vez, el anterior queda revocado):'
               '<div class="code">%s</div>Pasáselo a tus compañeros, o deciles que le pidan a su Hermes:<div class="code">'
               'wl-artifact join %s --name su-nombre</div></div>' % (esc(team_code), esc(team_code)))
    return layout("Administración", """
      <h2>Administración</h2>
      <p class="sub">Quién está, cuánto ocupa, y quién publicó o borró qué.</p>
      %s%s
      <div class="bar">
        <form method="post" action="/codigo">%s<button class="btn p">Rotar el código de equipo</button></form>
      </div>
      <table><tr><th>Persona</th><th>Artifacts</th><th>Espacio</th><th>Última vez</th><th>Tokens</th></tr>%s</table>
      <h2 style="margin-top:34px">Auditoría</h2>
      <p class="sub">Nunca se guarda el token: solo quién hizo qué y cuándo.</p>
      <table><tr><th>Cuándo</th><th>Quién</th><th>Qué</th><th>Artifact</th></tr>%s</table>
      """ % (n, inv, '<input type="hidden" name="csrf" value="%s">' % esc(session.get("_csrf", "")),
             filas, evs), session=session, active="admin")


def page_token_once(token, gallery_url, login_url, name):
    return layout("Tu cuenta está lista", """
      <h2>Listo, %s</h2>
      <p class="sub">Tu cuenta quedó creada. Esto se muestra <b>una sola vez</b>.</p>
      <div class="box">
        <div class="step"><div class="num">1</div><div><b>Tu token</b> — ya quedó guardado en tu
          computadora si lo hiciste con <span class="code" style="display:inline">wl-artifact join</span>.
          Sirve para publicar desde Hermes; no lo pongas en un artifact ni en un repo.
          <div class="code">%s</div></div></div>
        <div class="step"><div class="num">2</div><div><b>Tu galería</b>
          <div class="code">%s</div></div></div>
        <div class="step"><div class="num">3</div><div><b>Entrar sin copiar nada</b> — este link
          te deja la sesión abierta en el navegador (vence en 10 minutos, sirve una sola vez):
          <div class="code"><a href="%s">%s</a></div></div></div>
        <div class="warn">Guardá el token: no se vuelve a mostrar. Si lo perdés, quien administra el
          hub emite otro.</div>
      </div>""" % (esc(name), esc(token), esc(gallery_url), esc(login_url), esc(login_url)))


def page_install(cfg):
    """La página de arranque: el hub SOLO alcanza para empezar.

    Es pública a propósito — hay que poder leerla ANTES de tener cuenta. NO lleva el código de
    equipo: publicarlo acá convertiría el alta en abierta para cualquiera que llegue al hostname.
    El código viaja por el canal privado (el mensaje de quien ya está adentro)."""
    base = (cfg.get("base_url") or "").rstrip("/")
    one = "curl -fsSL https://raw.githubusercontent.com/propiter/artifact-craft/main/install.sh | bash"
    repo = "https://github.com/propiter/artifact-craft"
    return layout("Instalar", """
      <h2>Instalar</h2>
      <p class="sub">Este es el hub de artifacts del equipo. Sirve para entregar <b>páginas</b> —un
        informe, un tablero, un comparativo, una propuesta— como artifacts publicados con su URL, en
        vez de archivos sueltos que sólo abren en la máquina de quien los hizo.</p>
      <div class="box">
        <div class="step"><div class="num">1</div><div><b>Instalá el skill en tu Hermes.</b> Un comando:
          <div class="code">%s</div>
          <div class="note">Te va a pedir la URL del hub y el <b>código de equipo</b>. Ese código te lo
            pasa quien ya está adentro: <b>no está publicado acá</b>, a propósito.
            Si preferís no usar la terminal, pegale esto a tu Hermes:
            <div class="code" style="margin-top:8px">Instalá el skill de artifacts de Whitelabel y creá
              mi cuenta. El skill está en %s y el hub es %s. Después abrime la galería.</div>
          </div></div></div>
        <div class="step"><div class="num">2</div><div><b>Creá tu cuenta.</b> El instalador la crea y
          guarda tu token en tu máquina: nunca lo copiás a mano. Después te abre tu galería.</div></div>
        <div class="step"><div class="num">3</div><div><b>Pedí un artifact.</b> Decile a Hermes "hacé un
          artifact con esto" y te devuelve el link. Para publicar a mano:
          <div class="code">wl-artifact publish archivo.html mi-slug</div></div></div>
      </div>
      <div class="note">Acá no hay catálogo, y no lo va a haber: cada artifact vive en su dirección y
        <b>el enlace es la credencial</b>. Mandalo a la persona, no al grupo.</div>
      <div class="warn">Tu token no va nunca en un artifact, en un repo ni en un chat. Si lo perdés,
        quien administra el hub emite otro.</div>
    """ % (esc(one), esc(repo), esc(base)))


def page_error(msg, code=404):
    return layout("Error", '<h2>%s</h2><p class="sub">%s</p>' % (esc(msg), esc(code)))


# ---------------------------------------------------------------- router web

def _ctx(session):
    """La sesion web con la forma del contexto de un token, para los metodos del hub."""
    return {"user_id": session["user_id"], "name": session["name"], "is_admin": session["is_admin"],
            "token_id": None, "prefix": None}


def _serve_project(hub, session, method, slug, action, query, form, raw, ip, out, csrf_header):
    def jout(status, obj):
        return out(status, json.dumps(obj, ensure_ascii=False), ctype="application/json; charset=utf-8")

    def fail(exc, as_json):
        status = getattr(exc, "status", 500)
        if not hasattr(exc, "status"):
            raise exc
        if as_json:
            return jout(status, dict({"ok": False, "error": exc.code, "message": exc.message}, **(exc.extra or {})))
        return out(status, page_error(exc.message, status))

    if action is None:
        try:
            return out(200, page_project(hub, session, hub.project_detail(slug)))
        except Exception as exc:
            return fail(exc, False)

    if action == "documento":
        # el archivo viaja crudo (no es un formulario): el token CSRF va en un header
        want = session.get("csrf") or ""
        if not (csrf_header and want and secrets.compare_digest(csrf_header, want)):
            return jout(403, {"ok": False, "error": "csrf", "message": "la sesion vencio: recarga la pagina"})
        if not raw:
            return jout(400, {"ok": False, "error": "empty", "message": "el archivo esta vacio"})
        try:
            r = hub.doc_put(_ctx(session), slug, (query.get("path") or [""])[0], raw,
                            (query.get("visibility") or [None])[0], ip)
            return jout(200, dict(r, ok=True))
        except Exception as exc:
            return fail(exc, True)

    if not check_csrf(session, form):
        if action == "preguntar":
            return jout(403, {"ok": False, "error": "csrf", "message": "la sesion vencio: recarga la pagina"})
        return out(403, page_error("El formulario venció. Volvé a intentar.", 403))
    path = (form.get("path") or [""])[0]
    try:
        if action == "visibilidad":
            hub.doc_visibility(_ctx(session), slug, [path], (form.get("visibility") or [""])[0], ip=ip)
            return out(303, "", location="/p/" + slug)
        if action == "sacar":
            hub.doc_delete(_ctx(session), slug, [path], ip=ip)
            return out(303, "", location="/p/" + slug)
        q = (form.get("q") or [""])[0].strip()
        if not q:
            return jout(400, {"ok": False, "error": "empty", "message": "escribi una pregunta"})
        hub.check_attempts("webask:%s" % session["user_id"], limit=hub.cfg["kb_searches_per_hour"], window=3600)
        return jout(200, dict(hub.project_ask(slug, q), ok=True))
    except Exception as exc:
        return fail(exc, action == "preguntar")


def is_app_host(host, cfg):
    want = (cfg.get("app_host") or "").strip().lower()
    if not want:
        return False
    host = (host or "").split(":")[0].strip().lower()
    return host == want


RUTAS_GET = {"/", "/galeria", "/equipo", "/admin", "/entrar", "/unirse", "/instalar", "/proyectos"}
RUTAS_POST = {"/entrar", "/unirse", "/salir", "/compartir", "/borrar", "/codigo", "/proyectos"}
# /p/<proyecto> y sus acciones: (metodo, accion o None)
PROJECT_RE = re.compile(r"^/p/([a-z0-9][a-z0-9-]{0,60})(?:/(visibilidad|sacar|preguntar|documento))?$")
PROJECT_ROUTES = {("GET", None), ("HEAD", None), ("POST", "visibilidad"), ("POST", "sacar"),
                  ("POST", "preguntar"), ("PUT", "documento")}


def serve(handler, hub, method, path, query, form, raw=None):
    """Atiende un request del hostname de la app. Devuelve (status, ctype, body, cookie, location)."""
    cfg = hub.cfg
    sid = None
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            sid = v
    session = get_session(hub, sid)
    if session:
        session["_csrf"] = session["csrf"]
    ip = client_ip(handler)
    def out(status, body, ctype="text/html; charset=utf-8", cookie=None, location=None):
        return status, ctype, body.encode() if isinstance(body, str) else body, cookie, location

    # ---- link de un solo uso: entra y queda la sesion
    if path.startswith("/e/"):
        uid = consume_login_code(hub, path[3:])
        if not uid:
            return out(410, page_error("Ese link ya se usó o venció.", 410))
        sid, csrf = create_session(hub, uid, ip)
        cookie = "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
            COOKIE, sid, SESSION_DAYS * 86400)
        return out(303, "", cookie=cookie, location="/")

    # Solo las rutas de la app existen aca. Cualquier otra cosa es 404 — y eso incluye
    # /a/..., que es lo que mantiene los artifacts FUERA del origen de la sesion.
    conocidas = RUTAS_GET if method in ("GET", "HEAD") else RUTAS_POST
    pm = PROJECT_RE.match(path)
    if pm and (method, pm.group(2)) not in PROJECT_ROUTES:
        pm = None
    if path not in conocidas and not pm:
        return out(404, page_error("No existe esa página.", 404))

    # Publica: se lee ANTES de tener cuenta. Va antes del muro de sesion.
    if method == "GET" and path == "/instalar":
        return out(200, page_install(cfg))

    if method == "GET" and path in ("/entrar", "/unirse"):
        if session:
            return out(303, "", location="/")
        return out(200, page_join() if path == "/unirse" else page_login())

    # ---- POST: entrar con token
    if method == "POST" and path == "/entrar":
        tok = (form.get("token") or [""])[0].strip()
        with hub.db() as c:
            row = c.execute(
                "SELECT t.user_id, t.expires_at, t.revoked_at FROM tokens t WHERE t.id = ?",
                (hash_token(tok),)).fetchone()
        if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] < now()):
            return out(401, page_login(error="Ese token no es válido, está revocado o venció."))
        sid, csrf = create_session(hub, row["user_id"], ip)
        hub.log("login", None, None, None, None, ip, "por token")
        cookie = "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
            COOKIE, sid, SESSION_DAYS * 86400)
        return out(303, "", cookie=cookie, location="/")

    # ---- POST: crear cuenta con el codigo de equipo
    if method == "POST" and path == "/unirse":
        code = (form.get("code") or [""])[0].strip()
        name = (form.get("name") or [""])[0].strip().lower()
        if not SLUG_RE.match(name or ""):
            return out(400, page_join("El usuario tiene que ser [a-z0-9-], empezar con letra o número, máx 60."))
        if not team_code_ok(hub, code):
            return out(403, page_join("El código de equipo no es válido (o fue rotado). Pedí el nuevo."))
        uid = hub.ensure_user(name)
        token = hub.mint_token(uid, "web", ip)
        sid, csrf = create_session(hub, uid, ip)
        login_url = "https://%s/e/%s" % (cfg.get("app_host"), make_login_code(hub, uid))
        hub.log("join", name, None, None, None, ip, "por código de equipo")
        cookie = "%s=%s; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=%d" % (
            COOKIE, sid, SESSION_DAYS * 86400)
        return out(200, page_token_once(token, "https://" + cfg.get("app_host", "") + "/", login_url, name),
                   cookie=cookie)

    # ---- de aca para abajo hace falta sesion
    if not session:
        return out(303, "", location="/entrar")

    if method == "POST" and path == "/salir":
        drop_session(hub, sid)
        return out(303, "", cookie="%s=; Path=/; Max-Age=0" % COOKIE, location="/entrar")

    if pm:
        return _serve_project(hub, session, method, pm.group(1), pm.group(2), query, form, raw, ip, out,
                              handler.headers.get("X-CSRF") or "")

    if method == "POST":
        if not check_csrf(session, form):
            return out(403, page_error("El formulario venció. Volvé a intentar.", 403))

    if method == "POST" and path == "/proyectos":
        slug = (form.get("slug") or [""])[0].strip().lower()
        try:
            hub.project_create(_ctx(session), slug, (form.get("title") or [""])[0],
                               (form.get("description") or [""])[0], ip)
        except Exception as exc:
            return out(getattr(exc, "status", 400), page_projects(hub, session, hub.projects(),
                                                                   error=getattr(exc, "message", str(exc))))
        return out(303, "", location="/p/" + slug)

    if method == "POST" and path == "/compartir":
        slug = (form.get("slug") or [""])[0].strip()
        with hub.db() as c:
            if slug:
                # un artifact suelto: se comparte SIN abrir el resto del espacio
                cur = c.execute("UPDATE artifacts SET shared = 1 - shared"
                                " WHERE user_id = ? AND slug = ? AND deleted_at IS NULL",
                                (session["user_id"], slug))
                if cur.rowcount == 0:
                    return out(404, page_error("No tenés un artifact con ese nombre.", 404))
            else:
                c.execute("UPDATE users SET share_team = 1 - share_team WHERE id = ?",
                          (session["user_id"],))
        return out(303, "", location="/")

    if method == "POST" and path == "/borrar":
        slug = (form.get("slug") or [""])[0].strip()
        ctx = {"user_id": session["user_id"], "name": session["name"],
               "is_admin": session["is_admin"], "token_id": None, "prefix": None}
        try:
            hub.delete_artifact(ctx, slug, ip)
            return out(303, "", location="/")
        except Exception as exc:
            return out(400, page_error(getattr(exc, "message", str(exc)), 400))

    if method == "POST" and path == "/codigo":
        if not session["is_admin"]:
            return out(403, page_error("Solo un admin.", 403))
        code = roller_team_code(hub, session["name"])
        return out(200, page_admin(hub, session, admin_rows(hub), admin_events(hub), team_code=code))

    # ---- GET
    if path in ("/", "/galeria"):
        arts = my_artifacts(hub, session["user_id"])
        return out(200, page_gallery(hub, session, arts))
    if path == "/equipo":
        return out(200, page_team(hub, session, team_spaces(hub)))
    if path == "/proyectos":
        return out(200, page_projects(hub, session, hub.projects()))
    if path == "/admin":
        if not session["is_admin"]:
            return out(403, page_error("Solo un admin.", 403))
        return out(200, page_admin(hub, session, admin_rows(hub), admin_events(hub)))
    if path == "/qr.png":
        return out(404, page_error("No hay QR.", 404))
    return out(404, page_error("No existe esa página.", 404))



