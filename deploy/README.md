# Desplegar el hub (Dokploy)

Cómo poner `artifacts-hub` en un servidor con Dokploy. Las coordenadas reales van en
`deploy/deploy.env` (**no se versiona**); esto es el procedimiento.

## Requisitos

- Un servidor con **Dokploy** y **Traefik**.
- **El DNS del dominio apuntando al servidor ANTES de pedir el dominio en Dokploy.** Si el nombre
  todavía resuelve a otro lado, el desafío HTTP-01 de Let's Encrypt llega al servidor equivocado y
  el certificado falla. Verificalo: `dig +short A <tu-dominio>`.
- Un **registry** donde publicar la imagen (Docker Hub, GHCR, uno propio). No tiene que ser público.

## Procedimiento

```bash
# 0. coordenadas
cp deploy/deploy.env.example deploy/deploy.env && $EDITOR deploy/deploy.env

# 1. construir y publicar la imagen
#    IMPORTANTE: un compose 'raw' de Dokploy NO puede construir. Dokploy regenera code/ en cada
#    despliegue y el contexto de build local desaparece ("unable to prepare context: path ...").
#    Hay que publicar la imagen y referenciarla por tag.
docker build --platform linux/amd64 -t "$IMAGE" api/
docker push "$IMAGE"

# 2. generar los SQL DESDE docker-compose.yml (una sola fuente de verdad)
python3 deploy/gen-deploy-sql.py

# 3. aplicar en Dokploy — dry PRIMERO (corre todo y revierte)
cd ~/.hermes/skills/claude-code-imports/dokploy/assets    # o tu copia de dk.sh
./dk.sh <servidor> dry <repo>/deploy/<APP>-02-compose.sql
./dk.sh <servidor> sql <repo>/deploy/<APP>-02-compose.sql
./dk.sh <servidor> sql <repo>/deploy/<APP>-05-mount.sql
./dk.sh <servidor> sql <repo>/deploy/<APP>-04-domain.sql

# 4. LOS ARCHIVOS de files/ HAY QUE SUBIRLOS A MANO. La fila del mount NO los escribe:
#    solo crea lo que la UI muestra en Advanced -> Mounts. Sin este paso, Docker crea un
#    DIRECTORIO con ese nombre y el contenedor muere con ExitCode 127.
bash deploy/deploy.sh files

# 5. check + deploy
./dk.sh <servidor> check <APP>
./dk.sh <servidor> deploy <APP>
```

## Tres cosas que rompen el ruteo (nos costaron 502 en producción)

1. **Un nombre de servicio genérico colisiona entre redes.** El contenedor que recibe el dominio
   también está en `dokploy-network`, donde otro servicio del mismo servidor puede responder al
   mismo nombre (`api`, `web`, `app`…). Docker devuelve el primero que encuentra y nginx le habla
   al contenedor equivocado: **502 con todos los contenedores reportando "healthy"**. Se resuelve
   con un **alias único** en la red interna y `proxy_pass` apuntando al alias.
2. **nginx resuelve el upstream UNA vez, al arrancar.** Cada redeploy recrea el contenedor con una
   IP nueva y nginx conserva la vieja → 502 hasta reiniciarlo. El síntoma es inconfundible:
   `api-1 | Up 12 seconds` al lado de `store-1 | Up 3 minutes`. Se resuelve con
   `resolver 127.0.0.11 valid=10s ipv6=off;` + **variable** en `proxy_pass`, que resuelve por
   request.
3. **La landing hay que sembrarla.** Docker copia el contenido de la imagen a todo volumen vacío
   montado sobre ese path, así que sin el paso de siembra el sitio sirve el "Welcome to nginx" en
   vez de la landing. Por eso el entrypoint de la API copia `store/www/*` al volumen.

Después de tocar un archivo que está bind-mounteado, aplicalo sin redesplegar:

```bash
docker exec <contenedor> nginx -t && docker exec <contenedor> nginx -s reload
```

## Verificar que quedó bien

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<dominio>/                        # 200
curl -s https://<dominio>/healthz                                                  # {"ok": true}
curl -s -o /dev/null -w '%{http_code}\n' https://<dominio>/api/v1/artifacts        # 401 (sin token)
curl -s -o /dev/null -w '%{http_code}\n' https://<dominio>/a/alguien/              # 403/404 (sin listado)
```

Y el primer usuario:

```bash
docker compose exec api python3 admin.py user add <nombre> --admin
docker compose exec api python3 admin.py token add <nombre> --label laptop   # se muestra UNA vez
```
