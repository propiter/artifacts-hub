# La API no necesita nada fuera de la stdlib de Python: no hay requirements.
FROM python:3.13-alpine

RUN apk add --no-cache su-exec

WORKDIR /app
COPY app.py admin.py web.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && adduser -D -u 10001 -h /app hub

# El entrypoint arranca como root solo para arreglar el dueno del volumen y
# despues suelta privilegios: el servicio corre como `hub`, no como root.
ENTRYPOINT ["/entrypoint.sh"]
