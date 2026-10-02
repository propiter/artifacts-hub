# artifacts-hub

**Servicio para alojar y compartir artifacts**: cada persona publica desde su Hermes y recibe una
URL pública. Con un token por persona, aislamiento, versiones, cupos y auditoría.

Para el lado de quien crea artifacts, el skill vive en
[`propiter/artifact-craft`](https://github.com/propiter/artifact-craft). Este repo es el servicio:
lo que se despliega en un servidor.

## Qué da

| | |
|---|---|
| Leer | **el link es la credencial** — sin login en el artifact, sin catálogo público |
| Escribir | un **token por persona**; el dueño sale del token, nunca del pedido |
| Aislar | cada uno escribe **sólo su namespace**; nadie toca artifacts ajenos |
| Versiones | el slug da una URL estable; cada versión queda inmutable en `-vN` |
| Revocar | `rm` borra la canónica y todas las versiones: los links compartidos mueren |
| Limitar | tamaño por artifact, cantidad por persona, ritmo de escritura |
| Auditar | quién publicó o borró qué; **nunca** se guarda el token |

Publica dos cosas: una **página** (un `.html`) y una **app** (una carpeta con html, js, css,
imágenes).

## Arquitectura

```
                 ┌──────────────── servidor ─────────────────┐
  Hermes ──HTTPS │  nginx:  /            landing, no enumera  │
  wl-artifact    │          /a/<user>/…  los artifacts        │
                 │          /api/…    → api (tokens, cupos)   │
                 └───────────────────────────────────────────┘
                              un solo volumen compartido
```

- **store** — nginx: sirve la landing, los artifacts y hace de proxy hacia la API.
- **api** — tokens, cupos, versiones y auditoría. **Sin dependencias**: stdlib de Python + sqlite.
  Un archivo, un volumen, un proceso.
- Los dos comparten el volumen: la API escribe en `/data`, nginx lo sirve como docroot.

## Correrlo

```bash
python3 tests/test_api.py                    # 53 tests, sin instalar nada
docker compose up -d                         # en http://127.0.0.1:18080
```

Administración:

```bash
docker compose exec api python3 admin.py user add ana --admin
docker compose exec api python3 admin.py token add ana --label laptop --days 90   # se muestra UNA vez
docker compose exec api python3 admin.py token list
docker compose exec api python3 admin.py token revoke tok_abc123
docker compose exec api python3 admin.py events --limit 20
docker compose exec api python3 admin.py stats
```

El token se muestra una sola vez y en la base queda **sólo su sha256**.

## Desplegar

Guía completa en [`deploy/README.md`](deploy/README.md): Dokploy, la imagen al registry, el DNS
antes del certificado, y las cuatro cosas que rompen el ruteo (nombre de servicio que colisiona
entre redes, nginx que resuelve el upstream una sola vez, un compose `raw` que no puede construir,
y un bind relativo que crea un directorio en vez de montar el archivo).

`deploy/gen-deploy-sql.py` genera los SQL **a partir del `docker-compose.yml`**, para que no haya
dos fuentes de verdad que se divergen.

## Estructura

| Ruta | Qué es |
|---|---|
| `api/app.py` | auth, publicación (página y app), versiones, borrado, auditoría |
| `api/admin.py` | CLI: usuarios, tokens, eventos, stats |
| `api/Dockerfile` + `entrypoint.sh` | imagen (corre como uid 10001, no root) |
| `store/default.conf` | nginx: `/`, `/a/`, `/api/`, `/healthz` |
| `store/www/` | semillas del docroot (landing + 50x) |
| `deploy/` | generador de SQL y ayudas de despliegue |
| `tests/test_api.py` | 53 tests: auth, cupos, aislamiento, apps, auditoría, recursos |

## Límites conocidos

- El límite de ritmo vive **en memoria del proceso**: alcanza con una réplica.
- Los artifacts son HTML de usuarios y se sirven desde el mismo origen que la API: por eso el token
  **nunca** se usa desde un navegador. Una interfaz web autenticada va en **otro hostname**.
- Los artifacts **no se ejecutan** en el servidor: nginx los sirve como estáticos.

## Licencia

Apache-2.0.
