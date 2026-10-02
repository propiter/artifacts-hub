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
        self.assertEqual(b["bytes"], len(HTML))
        f = os.path.join(self.tmp, "a", "ana", "propuesta.html")
        self.assertTrue(os.path.exists(f))
        self.assertEqual(open(f, "rb").read(), HTML)
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
        self.assertEqual(open(os.path.join(d, "propuesta.html"), "rb").read(), HTML2)   # canonica = ultima
        self.assertEqual(open(os.path.join(d, "propuesta-v1.html"), "rb").read(), HTML)  # v1 inmutable

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
        self.assertEqual(a, HTML)
        self.assertEqual(b_, HTML2)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
