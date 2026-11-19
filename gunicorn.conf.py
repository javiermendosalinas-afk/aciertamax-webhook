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
