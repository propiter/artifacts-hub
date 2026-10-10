#!/usr/bin/env python3
"""Bateria de tests de la API. Sin dependencias: unittest + urllib.

Corre con:  python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import tarfile
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import admin  # noqa: E402
from app import Hub, Handler, DEFAULTS  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

HTML = b"<!doctype html><html><head><title>Propuesta</title></head><body><h1>hola</h1></body></html>"
HTML2 = b"<!doctype html><html><head><title>Propuesta</title></head><body><h1>v2</h1></body></html>"


class Base(unittest.TestCase):
    overrides = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-test-")
        self.cfg = dict(DEFAULTS)
        self.cfg.update({
            "data_dir": self.tmp,
            "db_path": os.path.join(self.tmp, "hub.sqlite3"),
            "base_url": "https://artifacts.test",
            "rate_per_min": 1000,
        })
        self.cfg.update(self.overrides)
        hub = Hub(self.cfg)
        Handler.hub = hub
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.base = "http://127.0.0.1:%d" % self.port

        admin.add_user(self.cfg, "ana")
        admin.add_user(self.cfg, "beto")
        admin.add_user(self.cfg, "root", is_admin=True)
        self.tok_ana, self.pfx_ana, _ = admin.new_token(self.cfg, "ana", "test")
        self.tok_beto, _, _ = admin.new_token(self.cfg, "beto")
        self.tok_root, _, _ = admin.new_token(self.cfg, "root")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers ----------
    def req(self, method, path, token=None, data=None, ctype="text/html", headers=None):
        url = self.base + path
        h = {}
        if ctype is not None:
            h["Content-Type"] = ctype
        if token:
            h["Authorization"] = "Bearer " + token
        if headers:
            h.update(headers)
        r = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def pub(self, token, slug, body=HTML, **kw):
        return self.req("PUT", "/api/v1/artifacts/" + slug, token=token, data=body, **kw)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def assertIntact(self, served, original):
        """El cuerpo del autor queda ENTERO: lo unico que agrega el hub va dentro del <head>,
        asi que el prefijo hasta el <head> y todo lo que sigue tienen que estar tal cual."""
        cut = original.lower().index(b"<head")
        cut = original.index(b">", cut) + 1
        self.assertTrue(served.startswith(original[:cut]), "cambio el principio")
        self.assertTrue(served.endswith(original[cut:]), "cambio algo despues del <head>")


class TestSaludYAuth(Base):
    def test_healthz_sin_token(self):
        s, b = self.req("GET", "/healthz")
        self.assertEqual(s, 200)
        self.assertTrue(b["ok"])

    def test_sin_token_401(self):
        s, b = self.req("GET", "/api/v1/whoami")
        self.assertEqual(s, 401)
        self.assertEqual(b["error"], "no_token")

    def test_token_invalido_401(self):
        s, b = self.req("GET", "/api/v1/whoami", token="wlart_" + "f" * 48)
        self.assertEqual(s, 401)
        self.assertEqual(b["error"], "bad_token")

    def test_token_revocado_401(self):
        self.assertEqual(admin.revoke_token(self.cfg, self.pfx_ana), 1)
        s, b = self.req("GET", "/api/v1/whoami", token=self.tok_ana)
        self.assertEqual(s, 401)
        self.assertEqual(b["error"], "revoked_token")

    def test_token_expirado_401(self):
        import time
        t, pfx, _ = admin.new_token(self.cfg, "ana")
        import sqlite3
        c = sqlite3.connect(self.cfg["db_path"])
        c.execute("UPDATE tokens SET expires_at=? WHERE prefix=?", (int(time.time()) - 10, pfx))
        c.commit(); c.close()
        s, b = self.req("GET", "/api/v1/whoami", token=t)
        self.assertEqual(s, 401)
        self.assertEqual(b["error"], "expired_token")

    def test_ruta_desconocida_404(self):
        s, b = self.req("GET", "/api/v1/nope", token=self.tok_ana)
        self.assertEqual(s, 404)

    def test_whoami_info(self):
        s, b = self.req("GET", "/api/v1/whoami", token=self.tok_ana)
        self.assertEqual(s, 200)
        self.assertEqual(b["user"], "ana")
        self.assertFalse(b["is_admin"])
        self.assertEqual(b["artifacts"], 0)
        self.assertEqual(b["max_bytes_per_artifact"], 2 * 1024 * 1024)


class TestPublicar(Base):
    def test_publicar_ok(self):
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 201, b)
        self.assertEqual(b["version"], 1)
        self.assertEqual(b["url"], "https://artifacts.test/a/ana/propuesta.html")
        f = os.path.join(self.tmp, "a", "ana", "propuesta.html")
        self.assertTrue(os.path.exists(f))
        served = open(f, "rb").read()
        # `bytes` es lo que se SIRVE: el cuerpo del autor mas los metadatos que agrega el hub.
        self.assertEqual(b["bytes"], len(served))
        self.assertTrue(b["bytes"] > len(HTML))
        self.assertIntact(served, HTML)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "ana", "propuesta-v1.html")))

    def test_mismo_contenido_no_crea_version(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 200)
        self.assertTrue(b["unchanged"])
        self.assertEqual(b["version"], 1)

    def test_contenido_nuevo_sube_version_y_snapshot(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.pub(self.tok_ana, "propuesta", body=HTML2)
        self.assertEqual(s, 201)
        self.assertEqual(b["version"], 2)
        self.assertEqual(b["snapshot"], "propuesta-v2.html")
        d = os.path.join(self.tmp, "a", "ana")
        canon = open(os.path.join(d, "propuesta.html"), "rb").read()
        v1 = open(os.path.join(d, "propuesta-v1.html"), "rb").read()
        self.assertIn(b"<h1>v2</h1>", canon)      # canonica = ultima
        self.assertIn(b"<h1>hola</h1>", v1)       # v1 inmutable
        self.assertNotIn(b"<h1>v2</h1>", v1)

    def test_slug_invalido(self):
        for bad in ["Mayuscula", "con espacio", "-empieza-guion", "ñandu", "a" * 70, "../../etc/passwd", "a/b"]:
            s, b = self.pub(self.tok_ana, urllib.parse.quote(bad, safe=""))
            self.assertIn(s, (400, 404), "slug %r deberia fallar" % bad)

    def test_slug_reservado(self):
        for bad in ["index", "api", "a", "healthz", "50x"]:
            s, b = self.pub(self.tok_ana, bad)
            self.assertEqual(s, 400)
            self.assertEqual(b["error"], "reserved_slug")

    def test_content_type_incorrecto(self):
        s, b = self.pub(self.tok_ana, "propuesta", ctype="application/pdf")
        self.assertEqual(s, 415)
        self.assertEqual(b["error"], "bad_type")

    def test_vacio(self):
        s, b = self.pub(self.tok_ana, "propuesta", body=b"")
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "empty")

    def test_demasiado_grande(self):
        s, b = self.pub(self.tok_ana, "propuesta", body=b"x" * (2 * 1024 * 1024 + 10))
        self.assertEqual(s, 413)
        self.assertEqual(b["error"], "too_large")

    def test_titulo_en_header(self):
        s, b = self.pub(self.tok_ana, "propuesta", headers={"X-Artifact-Title": "Propuesta Q3"})
        self.assertEqual(s, 201)
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"][0]["title"], "Propuesta Q3")

    def test_titulo_desde_el_body(self):
        """Sin header: el titulo sale del <title> del artifact."""
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"][0]["title"], "Propuesta")

    def test_titulo_unicode_no_se_corrompe(self):
        """Un <title> con acentos debe guardarse intacto (el header HTTP lo rompia)."""
        body = ('<!doctype html><html><head><title>Propuesta Q3 — Cliente Ñandú</title>'
                '</head><body>x</body></html>').encode("utf-8")
        self.pub(self.tok_ana, "acentos", body=body)
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"][0]["title"], "Propuesta Q3 — Cliente Ñandú")

    def test_header_no_ascii_se_ignora_y_manda_el_body(self):
        body = b"<!doctype html><html><head><title>Del cuerpo</title></head><body>x</body></html>"
        self.pub(self.tok_ana, "mixto", body=body,
                 headers={"X-Artifact-Title": "Titulo con acentos: Ñ"})
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"][0]["title"], "Del cuerpo")

    def test_sin_title_en_el_html(self):
        self.pub(self.tok_ana, "sin-titulo", body=b"<!doctype html><html><body>x</body></html>")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertIsNone(b["artifacts"][0]["title"])

    def test_dos_usuarios_mismo_slug_no_se_pisan(self):
        self.pub(self.tok_ana, "informe", body=HTML)
        self.pub(self.tok_beto, "informe", body=HTML2)
        a = open(os.path.join(self.tmp, "a", "ana", "informe.html"), "rb").read()
        b_ = open(os.path.join(self.tmp, "a", "beto", "informe.html"), "rb").read()
        self.assertIntact(a, HTML)
        self.assertIntact(b_, HTML2)
        # Cada uno con SU url en la vista previa: el mismo slug no los mezcla.
        self.assertIn(b"/a/ana/informe.html", a)
        self.assertIn(b"/a/beto/informe.html", b_)
        self.assertNotEqual(a, b_)


def bundle(files, symlink=None):
    """Arma un tar.gz en memoria con {ruta: bytes|str}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            tf.addfile(info)
    return buf.getvalue()


APP_FILES = {
    "index.html": "<!doctype html><html><head><title>Mi tablero — Ñandú</title></head>"
                  "<body><script src=\"app.js\"></script></body></html>",
    "app.js": "console.log('hola');",
    "css/style.css": "body{color:red}",
    "img/logo.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
}


class TestApps(Base):
    def pubapp(self, token, slug, packed=None, ctype="application/gzip"):
        return self.req("PUT", "/api/v1/apps/" + slug, token=token,
                        data=(packed if packed is not None else bundle(APP_FILES)), ctype=ctype)

    def test_desplegar_app_ok(self):
        s, b = self.pubapp(self.tok_ana, "tablero")
        self.assertEqual(s, 201, b)
        self.assertEqual(b["kind"], "app")
        self.assertEqual(b["files"], 4)
        self.assertEqual(b["url"], "https://artifacts.test/a/ana/tablero/")
        d = os.path.join(self.tmp, "a", "ana", "tablero")
        for rel in ["index.html", "app.js", "css/style.css", "img/logo.svg"]:
            self.assertTrue(os.path.exists(os.path.join(d, rel)), rel)
        self.assertEqual(open(os.path.join(d, "app.js"), "rb").read(), b"console.log('hola');")

    def test_titulo_desde_index(self):
        self.pubapp(self.tok_ana, "tablero")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"][0]["title"], "Mi tablero — Ñandú")
        self.assertEqual(b["artifacts"][0]["kind"], "app")

    def test_sin_index_html(self):
        s, b = self.pubapp(self.tok_ana, "tablero", bundle({"app.js": "x"}))
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "no_index")

    def test_traversal_con_punto_punto(self):
        """Lo mas importante: un paquete NO puede escribir fuera de su carpeta."""
        for evil in ["../escape.txt", "a/../../escape.txt", "..", "./../escape.txt"]:
            s, b = self.pubapp(self.tok_ana, "tablero", bundle({"index.html": "ok", evil: "MALO"}))
            self.assertEqual(s, 400, "deberia rechazar %r" % evil)
            self.assertEqual(b["error"], "bad_member")
        fuera = os.path.join(self.tmp, "a", "escape.txt")
        self.assertFalse(os.path.exists(fuera), "se escribio FUERA de la carpeta de la app")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))

    def test_ruta_absoluta(self):
        s, b = self.pubapp(self.tok_ana, "tablero", bundle({"index.html": "ok", "/tmp/evil.txt": "x"}))
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "bad_member")

    def test_symlink_rechazado(self):
        s, b = self.pubapp(self.tok_ana, "tablero",
                           bundle({"index.html": "ok"}, symlink=("link.txt", "/etc/passwd")))
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "bad_member")

    def test_extension_no_permitida(self):
        s, b = self.pubapp(self.tok_ana, "tablero", bundle({"index.html": "ok", "pwn.php": "<?php ?>"}))
        self.assertEqual(s, 415)
        self.assertEqual(b["error"], "bad_ext")

    def test_paquete_invalido(self):
        s, b = self.pubapp(self.tok_ana, "tablero", b"no soy un tar")
        self.assertEqual(s, 415)
        self.assertEqual(b["error"], "bad_pack")

    def test_content_type_incorrecto(self):
        s, b = self.pubapp(self.tok_ana, "tablero", ctype="text/html")
        self.assertEqual(s, 415)
        self.assertEqual(b["error"], "bad_type")

    def test_vacio(self):
        s, b = self.pubapp(self.tok_ana, "tablero", b"")
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "empty")

    def test_republicar_lo_mismo_no_versiona(self):
        self.pubapp(self.tok_ana, "tablero")
        s, b = self.pubapp(self.tok_ana, "tablero")
        self.assertEqual(s, 200, b)
        self.assertTrue(b["unchanged"])
        self.assertEqual(b["version"], 1)

    def test_redeploy_versiona_y_guarda_la_anterior(self):
        self.pubapp(self.tok_ana, "tablero")
        s, b = self.pubapp(self.tok_ana, "tablero", bundle({"index.html": "<title>v2</title>", "app.js": "v2"}))
        self.assertEqual(s, 201, b)
        self.assertEqual(b["version"], 2)
        d = os.path.join(self.tmp, "a", "ana", "tablero")
        self.assertEqual(open(os.path.join(d, "app.js"), "rb").read(), b"v2")
        viejo = os.path.join(self.tmp, "a", "ana", ".versions", "tablero-v1.tar.gz")
        self.assertTrue(os.path.exists(viejo), "no se guardo la version anterior")
        with tarfile.open(viejo) as tf:
            self.assertIn("index.html", [n.lstrip("./") for n in tf.getnames()])

    def test_borrar_app(self):
        self.pubapp(self.tok_ana, "tablero")
        s, b = self.req("DELETE", "/api/v1/artifacts/tablero", token=self.tok_ana, ctype=None)
        self.assertEqual(s, 200, b)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "ana", "tablero")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "ana", ".versions", "tablero-v1.tar.gz")))

    def test_no_se_puede_pisar_una_pagina_con_una_app(self):
        self.pub(self.tok_ana, "mismo")
        s, b = self.pubapp(self.tok_ana, "mismo")
        self.assertEqual(s, 409)
        self.assertEqual(b["error"], "slug_in_use")

    def test_aislamiento_entre_usuarios_en_apps(self):
        self.pubapp(self.tok_ana, "tablero")
        s, b = self.pubapp(self.tok_beto, "tablero", bundle({"index.html": "<title>de beto</title>"}))
        self.assertEqual(s, 201)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "ana", "tablero", "app.js")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "beto", "tablero", "index.html")))


class TestAislamiento(Base):
    def test_lista_solo_lo_propio(self):
        self.pub(self.tok_ana, "de-ana")
        self.pub(self.tok_beto, "de-beto")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual([x["slug"] for x in b["artifacts"]], ["de-ana"])
        self.assertEqual(b["artifacts"][0]["owner"], "ana")

    def test_no_admin_no_puede_ver_todo(self):
        s, b = self.req("GET", "/api/v1/artifacts?all=1", token=self.tok_ana)
        self.assertEqual(s, 403)
        self.assertEqual(b["error"], "not_admin")

    def test_admin_ve_todo(self):
        self.pub(self.tok_ana, "de-ana")
        self.pub(self.tok_beto, "de-beto")
        s, b = self.req("GET", "/api/v1/artifacts?all=1", token=self.tok_root)
        self.assertEqual(s, 200)
        self.assertEqual(sorted(x["slug"] for x in b["artifacts"]), ["de-ana", "de-beto"])

    def test_no_puedo_borrar_lo_ajeno(self):
        self.pub(self.tok_ana, "de-ana")
        s, b = self.req("DELETE", "/api/v1/artifacts/de-ana", token=self.tok_beto, ctype=None)
        self.assertEqual(s, 403)
        self.assertEqual(b["error"], "not_owner")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "ana", "de-ana.html")))

    def test_admin_si_puede_borrar_lo_ajeno(self):
        self.pub(self.tok_ana, "de-ana")
        s, b = self.req("DELETE", "/api/v1/artifacts/de-ana", token=self.tok_root, ctype=None)
        self.assertEqual(s, 200)


class TestBorrar(Base):
    def test_borrar_propio_mata_los_links(self):
        self.pub(self.tok_ana, "propuesta")
        self.pub(self.tok_ana, "propuesta", body=HTML2)
        s, b = self.req("DELETE", "/api/v1/artifacts/propuesta", token=self.tok_ana, ctype=None)
        self.assertEqual(s, 200, b)
        self.assertEqual(b["removed_files"], 3)  # canonica + v1 + v2
        d = os.path.join(self.tmp, "a", "ana")
        self.assertFalse(os.path.exists(os.path.join(d, "propuesta.html")))
        self.assertFalse(os.path.exists(os.path.join(d, "propuesta-v1.html")))
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertEqual(b["artifacts"], [])

    def test_slug_borrado_no_se_reusa(self):
        self.pub(self.tok_ana, "propuesta")
        self.req("DELETE", "/api/v1/artifacts/propuesta", token=self.tok_ana, ctype=None)
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 409)
        self.assertEqual(b["error"], "deleted_slug")

    def test_borrar_inexistente_404(self):
        s, b = self.req("DELETE", "/api/v1/artifacts/no-existe", token=self.tok_ana, ctype=None)
        self.assertEqual(s, 404)


class TestCuposApps(Base):
    overrides = {"max_app_files": 3, "max_app_unpacked": 1000}

    def test_demasiados_archivos(self):
        s, b = self.req("PUT", "/api/v1/apps/muchos", token=self.tok_ana, ctype="application/gzip",
                        data=bundle({"index.html": "ok", "a.js": "1", "b.js": "2", "c.js": "3"}))
        self.assertEqual(s, 413)
        self.assertEqual(b["error"], "too_many")

    def test_app_demasiado_grande_descomprimida(self):
        """gzip comprime 'xxxx' a casi nada: el limite que importa es el descomprimido."""
        s, b = self.req("PUT", "/api/v1/apps/grande", token=self.tok_ana, ctype="application/gzip",
                        data=bundle({"index.html": "x" * 4000}))
        self.assertEqual(s, 413)
        self.assertEqual(b["error"], "too_large")

    def test_paquete_demasiado_grande(self):
        overrides = self.cfg.get("max_app_packed")
        self.cfg["max_app_packed"] = 500          # limite chico: 500 bytes comprimidos
        Handler.hub.cfg["max_app_packed"] = 500
        try:
            s, b = self.req("PUT", "/api/v1/apps/grande2", token=self.tok_ana, ctype="application/gzip",
                            data=bundle({"index.html": os.urandom(3000).hex()}))
            self.assertEqual(s, 413)
            self.assertEqual(b["error"], "too_large")
        finally:
            self.cfg["max_app_packed"] = overrides
            Handler.hub.cfg["max_app_packed"] = overrides


class TestCupos(Base):
    overrides = {"max_artifacts": 2, "rate_per_min": 3}

    def test_cupo_de_artifacts(self):
        self.assertEqual(self.pub(self.tok_ana, "uno")[0], 201)
        self.assertEqual(self.pub(self.tok_ana, "dos")[0], 201)
        s, b = self.pub(self.tok_ana, "tres")
        self.assertEqual(s, 409)
        self.assertEqual(b["error"], "quota_artifacts")

    def test_rate_limit(self):
        codes = [self.pub(self.tok_ana, "a%d" % i)[0] for i in range(5)]
        self.assertIn(429, codes, "deberia frenar despues del limite: %s" % codes)


class TestAuditoria(Base):
    def test_eventos_y_sin_tokens(self):
        self.pub(self.tok_ana, "propuesta")
        self.req("DELETE", "/api/v1/artifacts/propuesta", token=self.tok_ana, ctype=None)
        s, b = self.req("GET", "/api/v1/events", token=self.tok_ana)
        self.assertEqual(s, 200)
        acciones = [e["action"] for e in b["events"]]
        self.assertIn("publish", acciones)
        self.assertIn("delete", acciones)
        blob = json.dumps(b)
        self.assertNotIn(self.tok_ana, blob, "el log NO debe contener el token")
        self.assertNotIn("wlart_", blob)

    def test_no_veo_eventos_de_otros(self):
        self.pub(self.tok_beto, "de-beto")
        s, b = self.req("GET", "/api/v1/events", token=self.tok_ana)
        self.assertNotIn("de-beto", json.dumps(b))

    def test_en_la_db_solo_va_el_hash_del_token(self):
        """Ni el token ni NINGUNA parte util del token puede estar en la DB."""
        import sqlite3
        c = sqlite3.connect(self.cfg["db_path"])
        dump = " ".join(str(x) for row in c.execute("SELECT * FROM tokens") for x in row)
        c.close()
        secret_hex = self.tok_ana.split("_", 1)[1]           # los 48 hex del secreto
        self.assertNotIn(secret_hex, dump)
        for i in range(0, len(secret_hex) - 12, 6):           # ninguna ventana de 12+
            self.assertNotIn(secret_hex[i:i + 12], dump)
        self.assertEqual(len(c.execute("SELECT 1").fetchone()), 1) if False else None


class TestRecursos(Base):
    def test_no_fuga_de_descriptores(self):
        """Un servicio que corre semanas no puede perder un descriptor por request."""
        for _ in range(5):
            self.req("GET", "/healthz")
        antes = len(os.listdir("/proc/self/fd"))
        for i in range(150):
            self.pub(self.tok_ana, "a%d" % i)
        despues = len(os.listdir("/proc/self/fd"))
        self.assertLess(despues - antes, 20,
                        "se fugan descriptores: %d -> %d" % (antes, despues))


class _SinRedirect(urllib.request.HTTPRedirectHandler):
    """urllib sigue los 303 por defecto, y asi un test 've' el 200 de la pagina
    siguiente en vez del redirect que tiene que verificar."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestJoinAPI(Base):
    """El camino que usa Hermes: crear la cuenta sin que la persona copie nada."""

    def test_join_crea_la_cuenta_y_devuelve_token(self):
        import web as w
        code = w.roller_team_code(Handler.hub, "test")
        s, b = self.req("POST", "/api/v1/join", data=json.dumps({"name": "juan", "code": code}).encode(),
                        ctype="application/json")
        self.assertEqual(s, 201, b)
        self.assertTrue(b["token"].startswith("wlart_"))
        self.assertIn("/e/", b["login_url"], "tiene que traer el link para entrar sin escribir el token")
        self.assertIn("gallery_url", b)
        # y ese token sirve para publicar
        s2, b2 = self.pub(b["token"], "de-juan")
        self.assertEqual(s2, 201, b2)

    def test_join_sin_codigo_valido(self):
        s, b = self.req("POST", "/api/v1/join", data=json.dumps({"name": "juan", "code": "malo"}).encode(),
                        ctype="application/json")
        self.assertEqual(s, 403)
        self.assertEqual(b["error"], "bad_code")

    def test_join_con_usuario_invalido(self):
        import web as w
        code = w.roller_team_code(Handler.hub, "test")
        s, b = self.req("POST", "/api/v1/join", data=json.dumps({"name": "Juan Pérez", "code": code}).encode(),
                        ctype="application/json")
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "bad_name")

    def test_join_no_necesita_token(self):
        """La ruta de unirse NO lleva Authorization: el codigo es la credencial."""
        import web as w
        code = w.roller_team_code(Handler.hub, "test")
        s, b = self.req("POST", "/api/v1/join", data=json.dumps({"name": "pedro", "code": code}).encode(),
                        ctype="application/json")
        self.assertEqual(s, 201)
        self.assertNotIn("Authorization", json.dumps(b))

    def test_login_link_necesita_token(self):
        s, b = self.req("POST", "/api/v1/login-link", ctype="application/json", data=b"{}")
        self.assertEqual(s, 401)

    def test_login_link_con_token(self):
        s, b = self.req("POST", "/api/v1/login-link", token=self.tok_ana, ctype="application/json", data=b"{}")
        self.assertEqual(s, 200, b)
        self.assertIn("/e/", b["url"])
        self.assertNotIn(self.tok_ana, b["url"], "el link NO puede contener el token")

    def test_el_token_del_join_no_se_guarda_en_claro(self):
        import web as w
        code = w.roller_team_code(Handler.hub, "test")
        s, b = self.req("POST", "/api/v1/join", data=json.dumps({"name": "juan", "code": code}).encode(),
                        ctype="application/json")
        tok = b["token"]
        with Handler.hub.db() as c:
            dump = " ".join(str(x) for row in c.execute("SELECT * FROM tokens") for x in row)
        self.assertNotIn(tok, dump)
        self.assertNotIn(tok.split("_", 1)[1], dump)


class TestWeb(Base):
    """La interfaz web: sesiones, invitaciones, galeria y el aislamiento de origen."""
    overrides = {"app_host": "app.test"}

    def web(self, method, path, host="app.test", cookie=None, data=None, headers=None):
        url = self.base + path
        h = {"Host": host}
        if cookie:
            h["Cookie"] = cookie
        if data is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(data).encode()
        if headers:
            h.update(headers)
        r = urllib.request.Request(url, data=data, headers=h, method=method)
        opener = urllib.request.build_opener(_SinRedirect)
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

    def code(self):
        with self.cfg["_hub"].db() as c:
            pass
        import web as w
        return w.roller_team_code(self.hub, "test")

    def setUp(self):
        super().setUp()
        import web as w
        self.hub = Handler.hub
        self.w = w

    def cookie_of(self, headers):
        return headers.get("Set-Cookie", "").split(";")[0]

    # ---- sin sesion
    def test_sin_sesion_manda_a_entrar(self):
        s, h, b = self.web("GET", "/")
        self.assertEqual(s, 303)
        self.assertIn("/entrar", h.get("Location", ""))

    def test_el_hostname_de_la_app_NO_sirve_artifacts(self):
        """Lo mas importante: la app y los artifacts no comparten origen."""
        self.pub(self.tok_ana, "propuesta")
        s, h, b = self.web("GET", "/a/ana/propuesta.html")
        self.assertEqual(s, 404, "el hostname de la app no puede servir artifacts")

    def test_la_api_en_el_hostname_de_la_app_tampoco(self):
        s, h, b = self.web("GET", "/api/v1/artifacts")
        self.assertEqual(s, 404)

    # ---- crear cuenta con el codigo de equipo
    def test_unirse_con_codigo_valido(self):
        code = self.w.roller_team_code(self.hub, "test")
        s, h, b = self.web("POST", "/unirse", data={"name": "juan", "code": code})
        self.assertEqual(s, 200, b[:300])
        self.assertIn("wlart_", b)
        self.assertIn("app.test/e/", b)
        ck = self.cookie_of(h)
        self.assertTrue(ck.startswith("hub_session="))
        self.assertIn("HttpOnly", h.get("Set-Cookie", ""))
        self.assertIn("SameSite=Strict", h.get("Set-Cookie", ""))
        with self.hub.db() as c:
            row = c.execute("SELECT name FROM users WHERE name='juan'").fetchone()
        self.assertIsNotNone(row, "no creo el usuario")
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertEqual(s, 200)
        self.assertIn("Mi galería", b)

    def test_unirse_con_codigo_malo(self):
        s, h, b = self.web("POST", "/unirse", data={"name": "juan", "code": "no-existe"})
        self.assertEqual(s, 403)

    def test_unirse_con_usuario_invalido(self):
        code = self.w.roller_team_code(self.hub, "test")
        s, h, b = self.web("POST", "/unirse", data={"name": "Juan Pérez", "code": code})
        self.assertEqual(s, 400)

    def test_rotar_el_codigo_invalida_el_anterior(self):
        viejo = self.w.roller_team_code(self.hub, "test")
        nuevo = self.w.roller_team_code(self.hub, "test")
        s, h, b = self.web("POST", "/unirse", data={"name": "juan", "code": viejo})
        self.assertEqual(s, 403, "el codigo viejo tiene que quedar revocado")
        s, h, b = self.web("POST", "/unirse", data={"name": "juan", "code": nuevo})
        self.assertEqual(s, 200)

    # ---- entrar con token
    def test_entrar_con_token(self):
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        self.assertEqual(s, 303)
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertEqual(s, 200)
        self.assertIn("ana", b)

    def test_entrar_con_token_malo(self):
        s, h, b = self.web("POST", "/entrar", data={"token": "wlart_" + "f" * 48})
        self.assertEqual(s, 401)

    def test_entrar_con_token_revocado(self):
        admin.revoke_token(self.cfg, self.pfx_ana)
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        self.assertEqual(s, 401)

    # ---- link de un solo uso
    def test_link_de_un_solo_uso(self):
        with self.hub.db() as c:
            uid = c.execute("SELECT id FROM users WHERE name='ana'").fetchone()["id"]
        code = self.w.make_login_code(self.hub, uid)
        s, h, b = self.web("GET", "/e/" + code)
        self.assertEqual(s, 303)
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertEqual(s, 200)
        s, h, b = self.web("GET", "/e/" + code)     # segunda vez
        self.assertEqual(s, 410, "el link tiene que servir UNA sola vez")

    def test_link_vencido(self):
        import sqlite3
        with self.hub.db() as c:
            uid = c.execute("SELECT id FROM users WHERE name='ana'").fetchone()["id"]
        code = self.w.make_login_code(self.hub, uid)
        with self.hub.db() as c:
            c.execute("UPDATE login_codes SET expires_at = ?", (int(time.time()) - 5,))
        s, h, b = self.web("GET", "/e/" + code)
        self.assertEqual(s, 410)

    # ---- galeria
    def test_la_galeria_muestra_lo_propio_y_su_link(self):
        self.pub(self.tok_ana, "propuesta")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertIn("propuesta", b)
        self.assertIn("artifacts.test/a/ana/propuesta.html", b)
        self.assertNotIn("de-beto", b)

    def test_borrar_desde_la_web(self):
        self.pub(self.tok_ana, "propuesta")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        s, h, b = self.web("POST", "/borrar", cookie=ck, data={"slug": "propuesta", "csrf": session["csrf"]})
        self.assertEqual(s, 303)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "ana", "propuesta.html")))

    def test_borrar_sin_csrf_no_pasa(self):
        self.pub(self.tok_ana, "propuesta")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        s, h, b = self.web("POST", "/borrar", cookie=ck, data={"slug": "propuesta"})
        self.assertEqual(s, 403, "sin csrf tiene que rechazar")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "ana", "propuesta.html")))

    # ---- compartir y vista del equipo
    def test_compartir_es_opt_in(self):
        self.pub(self.tok_ana, "de-ana")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_beto})
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/equipo", cookie=ck)
        self.assertNotIn("ana", b, "sin compartir no deberia aparecer")

        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        self.web("POST", "/compartir", cookie=ck, data={"csrf": session["csrf"]})
        s, h, b = self.web("GET", "/equipo", cookie=ck)
        self.assertIn("ana", b)
        self.assertIn("de-ana", b)

    def test_compartir_un_artifact_suelto_sin_abrir_el_espacio(self):
        """Compartir UN artifact: el equipo lo ve, y NO ve el resto del espacio."""
        self.pub(self.tok_ana, "compartido")
        self.pub(self.tok_ana, "privado")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        s, h, b = self.web("POST", "/compartir", cookie=ck,
                           data={"csrf": session["csrf"], "slug": "compartido"})
        self.assertEqual(s, 303)
        s, h, b = self.web("GET", "/equipo", cookie=ck)
        self.assertIn("compartido", b)
        self.assertNotIn("privado", b, "un artifact suelto no puede abrir el resto del espacio")

    def test_compartir_el_espacio_lo_muestra_entero(self):
        """Compartir el espacio: se ven todos, sin marcarlos uno por uno."""
        self.pub(self.tok_ana, "uno-de-ana")
        self.pub(self.tok_ana, "dos-de-ana")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        self.web("POST", "/compartir", cookie=ck, data={"csrf": session["csrf"]})
        s, h, b = self.web("GET", "/equipo", cookie=ck)
        self.assertIn("uno-de-ana", b)
        self.assertIn("dos-de-ana", b)

    def test_no_puedo_compartir_un_artifact_ajeno(self):
        self.pub(self.tok_ana, "de-ana")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_beto})
        ck = self.cookie_of(h)
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        s, h, b = self.web("POST", "/compartir", cookie=ck,
                           data={"csrf": session["csrf"], "slug": "de-ana"})
        self.assertEqual(s, 404)
        s, h, b = self.web("GET", "/equipo", cookie=ck)
        self.assertNotIn("de-ana", b)

    def test_la_galeria_da_los_dos_controles_de_compartir(self):
        """El fallo que esto previene: la funcion existe en la API pero no hay boton que la dispare."""
        self.pub(self.tok_ana, "de-ana")
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertIn('action="/compartir"', b, "falta el boton de compartir un artifact")
        self.assertIn('name="slug"', b)
        self.assertIn("Compartir mi espacio completo", b, "falta el boton del espacio completo")
        session = self.w.get_session(self.hub, ck.split("=", 1)[1])
        self.web("POST", "/compartir", cookie=ck, data={"csrf": session["csrf"], "slug": "de-ana"})
        s, h, b = self.web("GET", "/", cookie=ck)
        self.assertIn("Compartido", b, "la tarjeta no refleja que ya esta compartido")

    def test_instalar_es_publica_y_dice_como(self):
        """El hub SOLO tiene que alcanzar para arrancar: /instalar se lee sin cuenta."""
        s, h, b = self.web("GET", "/instalar")
        self.assertEqual(s, 200, "la pagina de instalacion tiene que ser publica")
        self.assertIn("github.com/propiter/artifact-craft", b)
        self.assertIn("install.sh | bash", b)
        self.assertIn(self.hub.cfg["base_url"], b, "tiene que nombrar el hub")

    def test_instalar_no_publica_el_codigo_de_equipo(self):
        """Publicar el codigo ahi convertiria el alta en abierta para cualquiera que llegue al host."""
        codigo = self.w.roller_team_code(self.hub, "prueba-instalar")
        s, h, b = self.web("GET", "/instalar")
        self.assertNotIn(codigo, b, "el codigo de equipo no puede estar en una pagina publica")
        self.assertNotIn('name="code"', b, "tampoco un campo para escribirlo")
        self.assertIn("no esta publicado", b.lower().replace("\u00e1", "a"))

    def test_admin_solo_para_admin(self):
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/admin", cookie=ck)
        self.assertEqual(s, 403)
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_root})
        ck = self.cookie_of(h)
        s, h, b = self.web("GET", "/admin", cookie=ck)
        self.assertEqual(s, 200)
        self.assertIn("Auditoría", b)

    def test_la_cookie_no_guarda_el_token(self):
        s, h, b = self.web("POST", "/entrar", data={"token": self.tok_ana})
        ck = h.get("Set-Cookie", "")
        self.assertNotIn(self.tok_ana, ck)
        self.assertNotIn("wlart_", ck)

    def test_csp_en_las_paginas_de_la_app(self):
        s, h, b = self.web("GET", "/entrar")
        csp = h.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-src https:", csp)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # cabecera PNG valida + relleno


class TestVistaPrevia(Base):
    """El og:* que hace que el link se vea bien AL PEGARLO, en cualquier chat."""

    def served(self, owner, name):
        with open(os.path.join(self.tmp, "a", owner, name), "rb") as f:
            return f.read()

    def test_el_head_lleva_los_metadatos_y_el_contenido_del_autor_no_se_toca(self):
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 201, b)
        h = self.served("ana", "propuesta.html")
        self.assertIntact(h, HTML)                        # el cuerpo del autor, intacto
        self.assertIn(b'property="og:title" content="Propuesta"', h)
        self.assertIn(b'property="og:url" content="https://artifacts.test/a/ana/propuesta.html"', h)
        self.assertIn(b'property="og:type" content="website"', h)
        self.assertIn(b'property="og:site_name" content="artifacts.test"', h)
        self.assertIn(b"artifact-craft:meta v1", h)       # se puede reconocer

    def test_lo_que_declara_el_autor_gana(self):
        body = (b"<!doctype html><html><head><title>T</title>"
                b'<meta property="og:image" content="https://mia.com/foto.png">'
                b"</head><body>x</body></html>")
        self.pub(self.tok_ana, "con-su-imagen", body=body)
        h = self.served("ana", "con-su-imagen.html")
        self.assertIn(b"https://mia.com/foto.png", h)
        self.assertNotIn(b"preview.png", h)               # no le pisamos la suya

    def test_sin_head_no_se_toca_nada(self):
        body = b"<div>fragmento suelto</div>"
        self.pub(self.tok_ana, "fragmento", body=body)
        self.assertEqual(self.served("ana", "fragmento.html"), body)

    def test_la_descripcion_sale_del_meta_del_autor(self):
        body = (b"<!doctype html><html><head><title>T</title>"
                b'<meta name="description" content="Un resumen corto"></head><body>x</body></html>')
        self.pub(self.tok_ana, "con-desc", body=body)
        self.assertIn(b'property="og:description" content="Un resumen corto"',
                      self.served("ana", "con-desc.html"))

    def test_sin_descripcion_no_se_inventa_una(self):
        self.pub(self.tok_ana, "propuesta")
        self.assertNotIn(b"og:description", self.served("ana", "propuesta.html"))

    def test_republicar_lo_mismo_sigue_siendo_un_no_op(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 200)
        self.assertTrue(b["unchanged"])
        self.assertEqual(b["version"], 1)

    def test_el_snapshot_inmutable_tambien_lleva_los_metadatos(self):
        self.pub(self.tok_ana, "propuesta")
        self.assertIn(b"og:title", self.served("ana", "propuesta-v1.html"))

    def test_una_app_tambien(self):
        s, b = self.req("PUT", "/api/v1/apps/tablero", token=self.tok_ana,
                        data=bundle(APP_FILES), ctype="application/gzip")
        self.assertEqual(s, 201, b)
        idx = self.read(os.path.join(self.tmp, "a", "ana", "tablero", "index.html"))
        self.assertIn(b"og:title", idx)
        self.assertIn(b"/a/ana/tablero/", idx)
        # y el resto de la app queda como vino
        self.assertEqual(self.read(os.path.join(self.tmp, "a", "ana", "tablero", "app.js")),
                         b"console.log('hola');")


class TestMiniatura(Base):
    def test_subir_una_vista_previa(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("PUT", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana,
                        data=PNG, ctype="image/png")
        self.assertEqual(s, 201, b)
        self.assertTrue(b["thumb_url"].endswith("/a/ana/propuesta.preview.png"))
        self.assertEqual(open(os.path.join(self.tmp, "a", "ana", "propuesta.preview.png"), "rb").read(), PNG)

    def test_entra_como_og_image_en_la_publicacion_siguiente(self):
        self.req("PUT", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana,
                 data=PNG, ctype="image/png")
        self.pub(self.tok_ana, "propuesta")
        h = open(os.path.join(self.tmp, "a", "ana", "propuesta.html"), "rb").read()
        self.assertIn(b'property="og:image" content="https://artifacts.test/a/ana/propuesta.preview.png"', h)
        self.assertIn(b'property="og:image:width" content="1200"', h)
        self.assertIn(b'name="twitter:card" content="summary_large_image"', h)

    def test_tiene_que_ser_un_png_de_verdad(self):
        s, b = self.req("PUT", "/api/v1/artifacts/x/thumb", token=self.tok_ana,
                        data=b"no soy un png", ctype="image/png")
        self.assertEqual(s, 415)
        self.assertEqual(b["error"], "bad_thumb")

    def test_content_type_equivocado(self):
        s, b = self.req("PUT", "/api/v1/artifacts/x/thumb", token=self.tok_ana,
                        data=PNG, ctype="text/html")
        self.assertEqual(s, 415)

    def test_demasiado_grande(self):
        big = PNG + b"\x00" * int(self.cfg["thumb_max_bytes"])
        s, b = self.req("PUT", "/api/v1/artifacts/x/thumb", token=self.tok_ana,
                        data=big, ctype="image/png")
        self.assertEqual(s, 413)

    def test_borrar(self):
        self.req("PUT", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana, data=PNG, ctype="image/png")
        s, b = self.req("DELETE", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana)
        self.assertEqual(s, 200, b)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "ana", "propuesta.preview.png")))

    def test_la_lista_la_informa(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertIsNone(b["artifacts"][0]["thumb_url"])
        self.req("PUT", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana, data=PNG, ctype="image/png")
        s, b = self.req("GET", "/api/v1/artifacts", token=self.tok_ana)
        self.assertTrue(b["artifacts"][0]["thumb_url"].endswith(".preview.png"))


class TestEstado(Base):
    """El artifact que DEVUELVE algo. La credencial es una clave POR ARTIFACT que el hub le
    inyecta a la pagina; nunca es el token de la persona."""

    def key_of(self, slug, token=None):
        s, b = self.req("GET", "/api/v1/artifacts/%s/key" % slug, token=token or self.tok_ana)
        self.assertEqual(s, 200, b)
        return b

    def put_with_key(self, owner, slug, key, data):
        return self.req("PUT", "/api/v1/state/%s/%s" % (owner, slug), data=data,
                        ctype="application/json", headers={"X-Artifact-Key": key})

    def get_with_key(self, owner, slug, key):
        return self.req("GET", "/api/v1/state/%s/%s" % (owner, slug), ctype=None,
                        headers={"X-Artifact-Key": key})

    def test_la_clave_queda_inyectada_y_nunca_es_el_token(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")
        h = open(os.path.join(self.tmp, "a", "ana", "propuesta.html"), "rb").read()
        self.assertIn(b"window.ARTIFACT=", h)
        self.assertIn(b"artifact-craft:runtime v1", h)
        self.assertIn(k["key"].encode(), h)
        self.assertNotIn(self.tok_ana.encode(), h)          # el token NUNCA entra a la pagina
        self.assertTrue(k["key"].startswith("art_"))

    def test_ida_y_vuelta_con_la_clave(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        s, b = self.get_with_key("ana", "propuesta", k)
        self.assertEqual(s, 200, b)
        self.assertEqual(b, {})                             # todavia no hay estado
        s, b = self.put_with_key("ana", "propuesta", k, b'{"aprobado":["a","b"]}')
        self.assertEqual(s, 200, b)
        s, b = self.get_with_key("ana", "propuesta", k)
        self.assertEqual(s, 200)
        self.assertEqual(b, {"aprobado": ["a", "b"]})

    def test_el_estado_no_se_puede_pedir_por_http_directo(self):
        """Vive bajo un directorio con punto justo para que nginx lo niegue."""
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        self.put_with_key("ana", "propuesta", k, b'{"secreto":1}')
        d = os.path.join(self.tmp, "a", "ana", ".state")
        self.assertTrue(os.path.exists(os.path.join(d, "propuesta.json")))
        # lo que nginx niega es el segmento que empieza con punto (location ~ /[.] { deny all; })
        self.assertIn(os.sep + ".state" + os.sep, os.path.join(d, "propuesta.json"))

    def test_la_clave_de_uno_no_sirve_en_el_artifact_de_otro(self):
        self.pub(self.tok_ana, "propuesta")
        self.pub(self.tok_beto, "propuesta")
        k_ana = self.key_of("propuesta")["key"]
        s, b = self.put_with_key("beto", "propuesta", k_ana, b'{"x":1}')
        self.assertEqual(s, 401)

    def test_clave_inventada(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.put_with_key("ana", "propuesta", "art_" + "0" * 40, b'{"x":1}')
        self.assertEqual(s, 401)

    def test_sin_clave_ni_token(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("PUT", "/api/v1/state/ana/propuesta", data=b'{"x":1}',
                        ctype="application/json")
        self.assertEqual(s, 401)

    def test_rotar_mata_la_vieja_y_la_nueva_sirve(self):
        self.pub(self.tok_ana, "propuesta")
        vieja = self.key_of("propuesta")["key"]
        s, b = self.req("POST", "/api/v1/artifacts/propuesta/key", token=self.tok_ana)
        self.assertEqual(s, 200, b)
        s, b = self.put_with_key("ana", "propuesta", vieja, b'{"x":1}')
        self.assertEqual(s, 401)
        self.pub(self.tok_ana, "propuesta", body=HTML2)     # republicar inyecta la nueva
        nueva = self.key_of("propuesta")["key"]
        self.assertNotEqual(vieja, nueva)
        s, b = self.put_with_key("ana", "propuesta", nueva, b'{"x":2}')
        self.assertEqual(s, 200, b)

    def test_apagar_el_estado(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        s, b = self.req("DELETE", "/api/v1/artifacts/propuesta/key", token=self.tok_ana)
        self.assertEqual(s, 200, b)
        s, b = self.put_with_key("ana", "propuesta", k, b'{"x":1}')
        self.assertEqual(s, 403)
        self.assertEqual(b["error"], "revoked_key")
        # y al republicar la pagina ya no lleva runtime
        self.pub(self.tok_ana, "propuesta", body=HTML2)
        h = open(os.path.join(self.tmp, "a", "ana", "propuesta.html"), "rb").read()
        self.assertNotIn(b"window.ARTIFACT=", h)

    def test_json_invalido(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        s, b = self.put_with_key("ana", "propuesta", k, b"no soy json")
        self.assertEqual(s, 400)
        self.assertEqual(b["error"], "bad_json")

    def test_vacio(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        s, b = self.put_with_key("ana", "propuesta", k, b"")
        self.assertEqual(s, 400)

    def test_demasiado_grande(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        grande = b'{"x":"' + b"a" * int(self.cfg["state_max_bytes"]) + b'"}'
        s, b = self.put_with_key("ana", "propuesta", k, grande)
        self.assertEqual(s, 413)

    def test_con_token_el_dueno_lee_y_escribe(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("PUT", "/api/v1/state/ana/propuesta", token=self.tok_ana,
                        data=b'{"v":1}', ctype="application/json")
        self.assertEqual(s, 200, b)
        s, b = self.req("GET", "/api/v1/state/ana/propuesta", token=self.tok_ana, ctype=None)
        self.assertEqual(s, 200)
        self.assertEqual(b, {"v": 1})

    def test_con_token_ajeno_no(self):
        self.pub(self.tok_ana, "propuesta")
        s, b = self.req("PUT", "/api/v1/state/ana/propuesta", token=self.tok_beto,
                        data=b'{"v":1}', ctype="application/json")
        self.assertEqual(s, 403)
        s, b = self.req("GET", "/api/v1/state/ana/propuesta", token=self.tok_beto, ctype=None)
        self.assertEqual(s, 403)

    def test_borrar_el_estado(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        self.put_with_key("ana", "propuesta", k, b'{"x":1}')
        s, b = self.req("DELETE", "/api/v1/state/ana/propuesta", token=self.tok_ana)
        self.assertEqual(s, 200, b)
        s, b = self.get_with_key("ana", "propuesta", k)
        self.assertEqual(b, {})

    def test_borrar_el_artifact_se_lleva_miniatura_estado_y_clave(self):
        self.pub(self.tok_ana, "propuesta")
        self.req("PUT", "/api/v1/artifacts/propuesta/thumb", token=self.tok_ana, data=PNG, ctype="image/png")
        k = self.key_of("propuesta")["key"]
        self.put_with_key("ana", "propuesta", k, b'{"x":1}')
        d = os.path.join(self.tmp, "a", "ana")
        self.assertTrue(os.path.exists(os.path.join(d, "propuesta.preview.png")))
        self.assertTrue(os.path.exists(os.path.join(d, ".state", "propuesta.json")))
        s, b = self.req("DELETE", "/api/v1/artifacts/propuesta", token=self.tok_ana)
        self.assertEqual(s, 200, b)
        self.assertFalse(os.path.exists(os.path.join(d, "propuesta.preview.png")))
        self.assertFalse(os.path.exists(os.path.join(d, ".state", "propuesta.json")))
        # El slug borrado NO se reusa, a proposito: asi un link viejo no revive con otro
        # contenido. Y su clave se fue con el artifact.
        s, b = self.pub(self.tok_ana, "propuesta")
        self.assertEqual(s, 409)
        self.assertEqual(b["error"], "deleted_slug")
        import sqlite3
        c = sqlite3.connect(self.cfg["db_path"])
        n = c.execute("SELECT COUNT(*) FROM artifact_keys WHERE owner='ana' AND slug='propuesta'").fetchone()[0]
        c.close()
        self.assertEqual(n, 0)

    def test_la_base_no_guarda_material_de_la_clave(self):
        """La clave se DERIVA del secreto del servidor: en la base no queda ni ella ni su hash."""
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        import glob
        import sqlite3
        for f in glob.glob(self.cfg["db_path"] + "*"):
            self.assertNotIn(k.encode(), open(f, "rb").read(), f)
        c = sqlite3.connect(self.cfg["db_path"])
        n = c.execute("SELECT COUNT(*) FROM artifact_keys").fetchone()[0]
        c.close()
        self.assertEqual(n, 0)          # ni siquiera hace falta una fila

    def test_rotar_deja_el_contador_y_nada_mas(self):
        self.pub(self.tok_ana, "propuesta")
        k = self.key_of("propuesta")["key"]
        self.req("POST", "/api/v1/artifacts/propuesta/key", token=self.tok_ana)
        import glob
        import sqlite3
        for f in glob.glob(self.cfg["db_path"] + "*"):
            self.assertNotIn(k.encode(), open(f, "rb").read(), f)
        c = sqlite3.connect(self.cfg["db_path"])
        rows = c.execute("SELECT owner, slug, epoch, revoked_at FROM artifact_keys").fetchall()
        c.close()
        self.assertEqual(rows, [("ana", "propuesta", 1, None)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
