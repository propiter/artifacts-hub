#!/usr/bin/env python3
"""CLI de administracion del artifacts-hub. Se corre del lado del servidor.

  admin.py user add piter --admin
  admin.py user list
  admin.py token add piter --label laptop --days 90     # imprime el token UNA vez
  admin.py token list [--user piter]
  admin.py token revoke <prefijo>
  admin.py events [--limit 20] [--user piter]
  admin.py stats
"""
import argparse
import hashlib
from contextlib import contextmanager
import json
import secrets
import sqlite3
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from app import Hub, DEFAULTS  # noqa: E402
from web import roller_team_code, make_login_code  # noqa: E402


@contextmanager
def db(cfg):
    c = sqlite3.connect(cfg["db_path"], timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def add_user(cfg, name, is_admin=False):
    if not name or len(name) > 40:
        sys.exit("nombre invalido")
    with db(cfg) as c:
        r = c.execute("SELECT id, is_admin FROM users WHERE name=?", (name,)).fetchone()
        if r:
            if is_admin and not r["is_admin"]:
                c.execute("UPDATE users SET is_admin=1 WHERE id=?", (r["id"],))
                return "actualizado a admin"
            return "ya existe"
        c.execute("INSERT INTO users (name,is_admin,created_at) VALUES (?,?,?)",
                  (name, 1 if is_admin else 0, int(time.time())))
    return "creado"


def new_token(cfg, user, label=None, days=None):
    import os
    secret = "wlart_" + secrets.token_hex(24)
    tid = hashlib.sha256(secret.encode()).hexdigest()
    # El prefijo es un identificador PUBLICO e INDEPENDIENTE: derivarlo del secreto
    # (secret[:14]) metia 32 bits del token real en la DB y en el log de auditoria.
    prefix = "tok_" + secrets.token_hex(6)
    expires = int(time.time()) + days * 86400 if days else None
    with db(cfg) as c:
        u = c.execute("SELECT id FROM users WHERE name=?", (user,)).fetchone()
        if not u:
            sys.exit("no existe el usuario %s" % user)
        c.execute("INSERT INTO tokens (id,prefix,user_id,label,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                  (tid, prefix, u["id"], label, int(time.time()), expires))
    return secret, prefix, expires


def revoke_token(cfg, prefix):
    with db(cfg) as c:
        n = c.execute("UPDATE tokens SET revoked_at=? WHERE prefix=? AND revoked_at IS NULL",
                      (int(time.time()), prefix)).rowcount
    return n


def list_tokens(cfg, user=None):
    q = ("SELECT t.prefix,t.label,t.created_at,t.expires_at,t.revoked_at,u.name"
         " FROM tokens t JOIN users u ON u.id=t.user_id")
    args = []
    if user:
        q += " WHERE u.name=?"
        args.append(user)
    q += " ORDER BY t.created_at DESC"
    with db(cfg) as c:
        return [dict(r) for r in c.execute(q, args)]


def main():
    ap = argparse.ArgumentParser(prog="admin.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pu = sub.add_parser("user"); pu.add_argument("action", choices=["add", "list"])
    pu.add_argument("name", nargs="?"); pu.add_argument("--admin", action="store_true")

    pt = sub.add_parser("token"); pt.add_argument("action", choices=["add", "list", "revoke"])
    pt.add_argument("arg", nargs="?"); pt.add_argument("--label"); pt.add_argument("--days", type=int)
    pt.add_argument("--user")

    pt2 = sub.add_parser("team"); pt2.add_argument("action", choices=["new", "show"])
    pi = sub.add_parser("invite"); pi.add_argument("name"); pi.add_argument("--days", type=int, default=7)

    pe = sub.add_parser("events"); pe.add_argument("--limit", type=int, default=20); pe.add_argument("--user")
    sub.add_parser("stats")

    a = ap.parse_args()
    cfg = dict(DEFAULTS)
    Hub(cfg)  # asegura schema

    if a.cmd == "user":
        if a.action == "add":
            if not a.name:
                sys.exit("falta el nombre")
            print("%s: %s" % (a.name, add_user(cfg, a.name, a.admin)))
        else:
            with db(cfg) as c:
                for r in c.execute("SELECT name,is_admin,created_at FROM users ORDER BY name"):
                    print("  %-16s %s" % (r["name"], "admin" if r["is_admin"] else ""))
    elif a.cmd == "token":
        if a.action == "add":
            if not a.arg:
                sys.exit("falta el usuario")
            secret, prefix, exp = new_token(cfg, a.arg, a.label, a.days)
            print("TOKEN (guardalo ahora, no se vuelve a mostrar):\n  %s" % secret)
            print("  prefijo %s%s" % (prefix, "\nexpira: %s" % time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "  sin expiracion"))
        elif a.action == "revoke":
            if not a.arg:
                sys.exit("falta el prefijo")
            print("revocados: %d" % revoke_token(cfg, a.arg))
        else:
            for t in list_tokens(cfg, a.user):
                state = "REVOCADO" if t["revoked_at"] else ("expirado" if t["expires_at"] and t["expires_at"] < time.time() else "activo")
                print("  %-14s %-10s %-14s %-9s %s" % (t["prefix"], t["name"], t["label"] or "-", state, time.strftime("%Y-%m-%d", time.localtime(t["created_at"]))))
    elif a.cmd == "team":
        from web import roller_team_code
        hub = Hub(cfg)
        if a.action == "show":
            with db(cfg) as c:
                r = c.execute("SELECT label, created_at FROM team_codes WHERE revoked_at IS NULL"
                              " ORDER BY created_at DESC LIMIT 1").fetchone()
            print("  hay un codigo activo (creado %s)" % (time.strftime("%Y-%m-%d", time.localtime(r["created_at"])) if r else "-")
                  if r else "  no hay ningun codigo de equipo activo")
        else:
            code = roller_team_code(hub, "cli")
            print("CODIGO DE EQUIPO (el anterior queda revocado):\n  %s" % code)
            print("  Pasaselo a tus companeros, o deciles que le pidan a su Hermes:\n"
                  "    wl-artifact join %s --name su-nombre" % code)
    elif a.cmd == "invite":
        from web import make_login_code
        hub = Hub(cfg)
        uid = hub.ensure_user(a.name)
        secret = hub.mint_token(uid, "invite")
        print("TOKEN de %s (se muestra UNA vez):\n  %s" % (a.name, secret))
        print("  y un link de un solo uso para que entre al navegador sin copiar nada (vence en 10 min):")
        print("    https://%s/e/%s" % (cfg.get("app_host") or "app.<tu-dominio>", make_login_code(hub, uid)))
    elif a.cmd == "events":
        with db(cfg) as c:
            q = "SELECT ts,user_name,action,artifact,bytes,detail FROM events"
            args = []
            if a.user:
                q += " WHERE user_name=?"; args.append(a.user)
            q += " ORDER BY ts DESC LIMIT ?"; args.append(a.limit)
            for r in c.execute(q, args):
                print("  %s %-9s %-9s %-34s %s" % (time.strftime("%d/%m %H:%M", time.localtime(r["ts"])),
                      r["user_name"] or "-", r["action"], (r["artifact"] or "")[:34], r["detail"] or ""))
    elif a.cmd == "stats":
        with db(cfg) as c:
            for label, q in [("usuarios", "SELECT COUNT(*) FROM users"),
                             ("tokens activos", "SELECT COUNT(*) FROM tokens WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at>%d)" % int(time.time())),
                             ("artifacts vivos", "SELECT COUNT(*) FROM artifacts WHERE deleted_at IS NULL"),
                             ("bytes guardados", "SELECT COALESCE(SUM(bytes),0) FROM artifacts WHERE deleted_at IS NULL"),
                             ("eventos", "SELECT COUNT(*) FROM events")]:
                print("  %-16s %s" % (label, c.execute(q).fetchone()[0]))


if __name__ == "__main__":
    main()
