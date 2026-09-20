"""
generar_mapa.py
Genera mapa_zmg.html (mapa interactivo con herramienta de dibujo de área)
a partir de un inventario CSV que tenga columnas lat/lon (el scraper
inventario_zmg_v6.py ya las incluye desde la versión corregida).

CÓMO CORRERLO:
    python3 generar_mapa.py inventario_zmg_ponderado.csv mapa_zmg.html

El HTML resultante es autocontenido (se puede subir directo como
Artifact/página publicada, o abrir localmente en cualquier navegador).
"""

import sys
import json
import csv

PLANTILLA = "plantilla_mapa.html"  # debe tener el marcador __DATOS_PROPIEDADES__


def cargar_propiedades(archivo_csv):
    props = []
    with open(archivo_csv, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            lat, lon = fila.get("lat"), fila.get("lon")
            if not lat or not lon:
                continue
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                continue
            try:
                precio = float(fila.get("Precio") or 0)
            except ValueError:
                precio = None
            try:
                m2 = float(fila.get("m²") or 0) or None
            except ValueError:
                m2 = None
            props.append({
                "lat": lat, "lon": lon,
                "titulo": fila.get("Título/Colonia", ""),
                "tipo": fila.get("Tipo", ""),
                "precio": precio,
                "moneda": "MXN",
                "operacion": (fila.get("Operación") or "").upper(),
                "recamaras": fila.get("Recámaras") or None,
                "banos": fila.get("Baños") or None,
                "m2": m2,
                "liga": fila.get("Liga", ""),
                "codigo_eb": fila.get("codigo_eb", ""),
            })
    return props


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 generar_mapa.py inventario.csv [salida.html]")
        sys.exit(1)
    archivo_csv = sys.argv[1]
    salida = sys.argv[2] if len(sys.argv) > 2 else "mapa_zmg.html"

    props = cargar_propiedades(archivo_csv)
    print(f"{len(props)} propiedades con coordenadas encontradas en {archivo_csv}")
    if not props:
        print("[AVISO] Ninguna fila tiene lat/lon -- revisa que el CSV venga del "
              "scraper corregido (inventario_zmg_v6.py, versión con data-popover-data).")
        sys.exit(1)

    with open(PLANTILLA, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATOS_PROPIEDADES__", json.dumps(props, ensure_ascii=False))
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Listo: {salida} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
