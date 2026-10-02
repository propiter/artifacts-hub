#!/usr/bin/env python3
"""Genera los SQL de despliegue A PARTIR del docker-compose.yml del repo.

Asi hay UNA sola fuente de verdad: si el compose cambia, los SQL se regeneran y no
quedan divergiendo. Ya paso una vez: se le agrego un alias de red al compose desplegado
y no al del repo, y el stack local dejo de funcionar sin que nada avisara.

Uso:  python3 deploy/gen-deploy-sql.py
Lee:  deploy/deploy.env  +  docker-compose.yml  +  store/default.conf  +  store/www/*
Escribe: deploy/<APP>-{02-compose,04-domain,05-mount}.sql
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

CABECERA = (
    "-- GENERADO por deploy/gen-deploy-sql.py — NO editar a mano.\n"
    "-- Fuente: docker-compose.yml del repo.\n"
    "\\set ON_ERROR_STOP on\n\n"
)

SQL_COMPOSE = """-- ==================== EDITAR ====================
\\set SVCNAME  'artifacts-hub'
\\set APP      '{app}'
\\set DESC     'Almacen de artifacts multi-persona: nginx (landing + artifacts + proxy) y api (tokens, cupos, versiones, auditoria)'
\\set ENVID    '{envid}'
-- ================================================

BEGIN;

SELECT count(*) = 1 AS env_ok FROM environment WHERE "environmentId" = :'ENVID' \\gset
\\if :env_ok
\\else
\\echo '*** ABORTADO: el environmentId destino no existe.'
SELECT 1/0;
\\endif

INSERT INTO compose (
  "composeId", name, "appName", description, env, "composeFile",
  "sourceType", "composeType", "composePath", "composeStatus",
  command, suffix, randomize, "isolatedDeployment", "isolatedDeploymentsVolume",
  "enableSubmodules", "autoDeploy", "refreshToken", "createdAt", "environmentId")
SELECT
  pg_temp.nanoid(), :'SVCNAME', :'APP', :'DESC', '', $YAML$
{yaml}$YAML$,
  'raw', 'docker-compose', './docker-compose.yml', 'idle',
  '', '', false, false, false, false,
  false, pg_temp.nanoid(), pg_temp.now_iso(), :'ENVID'
WHERE NOT EXISTS (SELECT 1 FROM compose WHERE "appName" = :'APP');

COMMIT;

\\echo '--- creado ---'
SELECT p.name AS project, e.name AS env, c."appName", c."composeStatus"
FROM compose c JOIN environment e ON e."environmentId" = c."environmentId"
JOIN project p ON p."projectId" = e."projectId" WHERE c."appName" = :'APP';
"""

SQL_DOMAIN = """-- ==================== EDITAR ====================
\\set APP      '{app}'
\\set HOST     '{host}'
\\set PORT     '80'
\\set SVC      'store'
\\set PATHPFX  '/'
\\set CERT     'letsencrypt'
-- ================================================

SELECT count(*) = 0 AS app_falta
FROM (SELECT "appName" FROM compose UNION ALL SELECT "appName" FROM application) s
WHERE s."appName" = :'APP' \\gset
\\if :app_falta
\\echo '*** ABORTADO: no existe compose/application con ese appName en este servidor.'
SELECT 1/0;
\\endif

BEGIN;

-- DOS dominios: el de los artifacts (contenido de terceros) y el de la app (sesion).
-- Tienen que ser distintos: si compartieran origen, el JavaScript de un artifact podria
-- leer la cookie de sesion de quien mira la galeria.
INSERT INTO domain (
  "domainId", host, https, port, path, "certificateType",
  "serviceName", "domainType", "internalPath", "stripPath",
  "applicationId", "composeId", "createdAt")
SELECT pg_temp.nanoid(), h.host, (:'CERT' <> 'none'), :'PORT'::int, :'PATHPFX', :'CERT'::"certificateType",
  NULLIF(:'SVC',''), 'compose'::"domainType", '/', false,
  NULL, c."composeId", pg_temp.now_iso()
FROM compose c
CROSS JOIN (VALUES (:'HOST'), (:'APPHOST')) AS h(host)
WHERE c."appName" = :'APP'
  AND NOT EXISTS (SELECT 1 FROM domain d WHERE d.host = h.host);

COMMIT;

\\echo '--- dominio ---'
SELECT d.host, d.port, d."serviceName", d."certificateType"
FROM domain d JOIN compose c ON c."composeId" = d."composeId" WHERE c."appName" = :'APP';
"""

SQL_MOUNT_ROW = """INSERT INTO mount ("mountId", type, "serviceType", "mountPath", "filePath", content, "composeId")
SELECT pg_temp.nanoid(), 'file', 'compose', '', '{f}', $FILE$
$FILE$, c."composeId"
FROM compose c
WHERE c."appName" = :'APP'
  AND NOT EXISTS (SELECT 1 FROM mount m WHERE m."composeId" = c."composeId" AND m."filePath" = '{f}');"""

SQL_MOUNT_PIE = """\\echo '--- mounts ---'
SELECT m."filePath", length(m.content) AS len
FROM mount m JOIN compose c ON c."composeId" = m."composeId" WHERE c."appName" = :'APP' ORDER BY 1;
"""


def build_deploy_yaml(image, host, app_host=""):
    """Devuelve el compose que se despliega: con la imagen publicada y en dokploy-network.

    El repo NO puede declarar `dokploy-network` (es externa y solo existe en el servidor),
    asi que la inyectamos aca. Con PyYAML si esta; si no, insercion por texto CON asserts,
    para que falle ruidosamente en vez de generar un compose roto.
    """
    texto = (ROOT / "docker-compose.yml").read_text()

    try:
        import yaml
    except ImportError:
        yaml = None

    if yaml is not None:
        doc = yaml.safe_load(texto)
        svc = doc["services"]
        # la imagen ya publicada reemplaza el build
        svc["api"].pop("build", None)
        svc["api"]["image"] = image
        # el servicio que recibe el dominio TIENE que estar en dokploy-network
        store_nets = svc["store"].get("networks") or []
        if isinstance(store_nets, list) and "dokploy-network" not in store_nets:
            store_nets.append("dokploy-network")
        svc["store"]["networks"] = store_nets
        doc.setdefault("networks", {})["dokploy-network"] = {"external": True}

        # HUB_BASE_URL tiene que ser la URL PUBLICA: de aca salen los links que se
        # reparten. Con el default del compose de local devolvia
        # http://127.0.0.1:18080/... — la URL del PC, que no le sirve a nadie.
        entorno = svc["api"].setdefault("environment", {})
        entorno["HUB_BASE_URL"] = "https://" + host
        # el hostname de la app: separa la sesion del contenido de terceros
        if app_host:
            entorno["HUB_APP_HOST"] = app_host

        # Los binds del repo son `./store/...` (relativo al repo). En Dokploy el compose
        # corre desde `code/` y los archivos viven en `files/`: hay que prefijarlos con
        # ../files/. Si no, Docker NO encuentra el origen y crea un DIRECTORIO con ese
        # nombre: nginx no arranca y el contenedor queda en `Created`. Es la regla
        # unica: `./X` -> `../files/X`, y deploy.sh sube el arbol igual.
        for nombre, svc_def in doc["services"].items():
            nuevos = []
            for vol in svc_def.get("volumes") or []:
                if isinstance(vol, str) and vol.startswith("./"):
                    origen, resto = vol.split(":", 1)
                    vol = "../files/" + origen[2:] + ":" + resto
                nuevos.append(vol)
            if nuevos:
                svc_def["volumes"] = nuevos
        salida = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)
    else:
        salida = texto.replace("    build: ./api\n", "    image: %s\n" % image)
        assert host in salida or "HUB_BASE_URL" in salida
        salida = salida.replace("    networks: [internal]\n", "    networks: [internal, dokploy-network]\n")
        # y la URL publica (el default del repo es el de local)
        import re as _re
        salida = _re.sub(r"(HUB_BASE_URL: ).*", r"\1https://" + host.replace("\\", "\\\\"), salida)
        salida = salida.replace("networks:\n  internal:\n",
                                "networks:\n  internal:\n  dokploy-network:\n    external: true\n")
        assert "dokploy-network" in salida, "no pude inyectar dokploy-network (¿cambio el compose?)"
        assert "    build: ./api\n" not in salida, "quedo un build: en el compose desplegable"
        assert image in salida, "no pude poner la imagen publicada"

    if yaml is None:
        # el dump de PyYAML no existe en este camino: el texto ya es valido
        pass
    # el compose desplegable no lleva el nombre de host del nginx: se reemplaza aca y en
    # el default.conf que se sube a files/ (ver deploy.sh)
    cabecera = (
        "# GENERADO por deploy/gen-deploy-sql.py desde docker-compose.yml — no editar a mano.\n"
        "# Diferencias con el del repo: usa la imagen YA PUBLICADA (un compose 'raw' no puede\n"
        "# construir: Dokploy regenera code/ y el contexto de build desaparece) y suma la\n"
        "# red externa dokploy-network al servicio que recibe el dominio.\n"
    )
    return cabecera + salida


def load_env():
    f = HERE / "deploy.env"
    if not f.exists():
        sys.exit("falta deploy/deploy.env (copialo de deploy.env.example)")
    env = {}
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("APP", "ENVID", "HOST", "IMAGE"):
        if not env.get(k):
            sys.exit("falta %s en deploy/deploy.env" % k)
    return env


def main():
    e = load_env()
    app, envid, host, image = e["APP"], e["ENVID"], e["HOST"], e["IMAGE"]
    env_app_host = e.get("APP_HOST") or ("app." + host)

    yaml_texto = build_deploy_yaml(image, host, env_app_host)

    (HERE / ("%s-02-compose.sql" % app)).write_text(
        CABECERA + SQL_COMPOSE.format(app=app, envid=envid, yaml=yaml_texto))
    (HERE / ("%s-04-domain.sql" % app)).write_text(
        CABECERA + SQL_DOMAIN.format(app=app, host=host, apphost=env_app_host))

    # filePath es relativo a files/, y files/ es espejo de store/
    archivos = sorted(
        "store/" + str(f.relative_to(ROOT / "store"))
        for f in (ROOT / "store").rglob("*") if f.is_file()
    )
    cuerpo = "\n\n".join(SQL_MOUNT_ROW.format(f=f) for f in archivos)
    (HERE / ("%s-05-mount.sql" % app)).write_text(
        CABECERA
        + "-- Los archivos de configuracion. OJO: este INSERT NO escribe los archivos en\n"
          "-- disco, solo crea la fila que la UI muestra en Advanced -> Mounts. Hay que\n"
          "-- subirlos a files/ a mano (lo hace deploy/deploy.sh) o el contenedor muere.\n"
          "\\set ON_ERROR_STOP on\n"
          "\\set APP '%s'\n\nBEGIN;\n\n%s\n\nCOMMIT;\n\n%s" % (app, cuerpo, SQL_MOUNT_PIE)
    )

    # Actualizar un servicio YA existente (imagen nueva, cambio en el compose). Sin esto
    # habria que escribir el UPDATE a mano cada vez, que es de donde salen las dos
    # fuentes de verdad.
    (HERE / ("%s-03-update-compose.sql" % app)).write_text(
        CABECERA
        + "-- Actualiza el composeFile de un servicio que YA existe.\n"
          "-- Uso tipico: sacaste una imagen nueva y hay que apuntar el compose a ella.\n"
          "\\set ON_ERROR_STOP on\n"
          "\\set APP '%s'\n\n"
          "UPDATE compose SET \"composeFile\" = $YAML$\n%s$YAML$\n"
          "WHERE \"appName\" = :'APP';\n\n"
          "\\echo '--- actualizado ---'\n"
          "SELECT \"appName\", length(\"composeFile\") AS len,\n"
          "       (\"composeFile\" like '%%%s%%') AS con_imagen\n"
          "FROM compose WHERE \"appName\" = :'APP';\n" % (app, yaml_texto, image)
    )

    if not env_app_host:
        sys.exit("falta APP_HOST (el hostname de la interfaz web, distinto al de los artifacts)")
    if env_app_host == host:
        sys.exit("APP_HOST no puede ser igual a HOST: la app y los artifacts NO pueden compartir origen")
    if ("127.0.0.1" in yaml_texto or "localhost" in yaml_texto) and "HUB_BASE_URL" in yaml_texto:
        sys.exit("el compose desplegable quedo con una URL local: revisar HUB_BASE_URL")
    if ("https://" + host) not in yaml_texto:
        sys.exit("el compose desplegable no lleva la URL publica %s" % host)

    print("generados para %s (imagen %s):" % (app, image))
    for s in sorted(HERE.glob("%s-*.sql" % app)):
        print("   ", s.name, s.stat().st_size, "bytes")
    print("montajes declarados:", archivos)
    print("\nOJO: subi los archivos de files/ con deploy/deploy.sh — la fila del mount NO los escribe.")


if __name__ == "__main__":
    main()
