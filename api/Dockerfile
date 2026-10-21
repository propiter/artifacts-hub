# La API no necesita nada fuera de la stdlib de Python: no hay requirements.
FROM python:3.13-alpine

# poppler-utils: pdftotext, para indexar los PDF de un proyecto (kb.py).
RUN apk add --no-cache su-exec poppler-utils

WORKDIR /app
# *.py y no una lista: con la lista, un modulo nuevo (meta.py) pasaba todos los tests —corren
# desde el fuente— y la imagen moria al arrancar con ModuleNotFoundError.
COPY *.py runtime.js ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && adduser -D -u 10001 -h /app hub

# El entrypoint arranca como root solo para arreglar el dueno del volumen y
# despues suelta privilegios: el servicio corre como `hub`, no como root.
ENTRYPOINT ["/entrypoint.sh"]
