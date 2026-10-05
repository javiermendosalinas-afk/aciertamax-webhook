# Lo lee gunicorn automáticamente al arrancar ('gunicorn app:app').
# Las propuestas en PDF descargan fotos y el mapa: se da más margen que los
# 30 s por defecto para que la petición no se corte a la mitad.
timeout = 90
