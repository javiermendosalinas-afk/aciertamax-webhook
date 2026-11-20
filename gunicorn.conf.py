# Lo lee gunicorn automáticamente al arrancar ('gunicorn app:app').
# Las propuestas en PDF descargan fotos y el mapa: se da más margen que los
# 30 s por defecto para que la petición no se corte a la mitad.
timeout = 90

# Un solo proceso (las sesiones, cachés y candados viven en memoria), pero con
# hilos: así una plática web que espera al modelo no detiene los mensajes de
# WhatsApp ni a los demás clientes (19-nov-2026: con 1 hilo, 8 clientes a la
# vez esperaban ~36 s por respuesta).
workers = 1
worker_class = "gthread"
threads = 6

# Reinicio preventivo del proceso (por memoria): cada 5,000 peticiones como mínimo.
# 19-nov-2026: un valor bajo puesto fuera del código (variable de entorno) reiniciaba
# el proceso cada ~70 s, borrando los códigos de verificación de WhatsApp y las
# pláticas en curso. Este gancho corre después de leer TODA la configuración, así que
# sube el valor aunque venga de GUNICORN_CMD_ARGS.
MINIMO_PETICIONES = 5000


def on_starting(server):
    actual = server.cfg.max_requests or 0
    if 0 < actual < MINIMO_PETICIONES:
        server.cfg.set("max_requests", MINIMO_PETICIONES)
        server.cfg.set("max_requests_jitter", 500)
        server.log.info("max_requests subido de %s a %s", actual, MINIMO_PETICIONES)
