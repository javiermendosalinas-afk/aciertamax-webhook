"""
inventario_zmg_v6.py
Scraper de inventario ZMG para Acierta Max (aciertamax.com / EasyBroker)

QUÉ HACE:
- Recorre aciertamax.com/properties y aciertamax.com/rentals filtrando por
  los 5 municipios de la ZMG: Guadalajara, Zapopan, Tlaquepaque, Tonalá,
  Tlajomulco de Zúñiga.
- Pagina automáticamente hasta que no hay más resultados.
- Aplica los mismos pisos de la versión anterior: ventas >= $2,000,000,
  rentas >= $13,000/mes.
- Guarda inventario_zmg.csv con una columna nueva: "Fuente" (para poder
  mezclarlo después con GIG/San Carlos/Avitia y con tu inventario propio),
  y "Fecha_Corrida" para saber qué tan fresco está el dato.

CÓMO CORRERLO:
    pip install requests beautifulsoup4 --break-system-packages   (si hace falta)
    python3 inventario_zmg_v6.py

TIEMPO ESPERADO: con ~7,200+ propiedades en venta y pausas de cortesía
entre peticiones, esto puede tardar 20-40 minutos. Es normal.

PENDIENTE DE VERIFICAR (avísame si falla):
- Asumí que /rentals/mexico/jalisco/<municipio> sigue el mismo patrón que
  /properties/mexico/jalisco/<municipio>. Si el sitio usa otra ruta para
  rentas, el script lo reportará como "0 resultados" y hay que ajustarla.
"""

import csv
import json
import re
import time
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://www.aciertamax.com"

MUNICIPIOS = [
    "guadalajara",
    "zapopan",
    "tlaquepaque",
    "tonala",
    "tlajomulco-de-zuniga",
]

OPERACIONES = {
    "VENTA": "properties",
    "RENTA": "rentals",
}

PISO_PRECIO = {
    "VENTA": 2_000_000,
    "RENTA": 13_000,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AciertaMaxInventarioBot/1.0; "
                  "+https://www.aciertamax.com)"
}

PAUSA_ENTRE_PAGINAS = 1.5  # segundos, cortesía para no saturar el sitio


def limpiar_precio(texto):
    """'$31,500,000 MXN En Venta' -> (31500000.0, 'MXN')"""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(USD|MXN)?", texto)
    if not m:
        return None, None
    valor = float(m.group(1).replace(",", ""))
    moneda = m.group(2) or "MXN"
    return valor, moneda


def limpiar_m2(texto):
    """'508 m²' -> 508.0 ; soporta separador de miles '1,141 m²'"""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*m²", texto)
    if not m:
        return None, False
    valor = float(m.group(1).replace(",", ""))
    return valor, False


def extraer_codigo_eb(img_alt, url):
    m = re.search(r"EB-[A-Z0-9]+", img_alt or "")
    if m:
        return m.group(0)
    m = re.search(r"EB-[A-Z0-9]+", url or "")
    return m.group(0) if m else ""


def parsear_tarjetas(html):
    """Devuelve lista de dicts, una por propiedad, a partir del HTML de listado.

    CONFIRMADO CONTRA HTML REAL (no adivinado): cada propiedad vive en un
    <div class="property-listing" data-lat="..." data-long="..."
         data-popover-data='{"price":..., "title":..., "location":...,
                              "bedrooms":..., "bathrooms":..., "size":...,
                              "url":..., "image_url":...}'>
      <div class="property-photo"><a><img alt="EB-XXXXXX" src="..."></a></div>
      <div class="property-description">
        ...
        <div class="property-info">
          <p class="property-type">Casa en condominio</p>
          <p>4 recámaras, 4 baños</p>   (puede faltar si no aplica, ej. Edificio)
          <p>508 m²</p>
        </div>
      </div>
    </div>

    El JSON de data-popover-data ya trae todo estructurado y es más
    confiable que parsear el texto visible -- se usa como fuente principal.
    """
    soup = BeautifulSoup(html, "html.parser")
    resultados = []

    for tarjeta in soup.select("div.property-listing"):
        raw = tarjeta.get("data-popover-data", "")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        precio, moneda = limpiar_precio(data.get("price", "") or "")

        m2 = None
        size_texto = data.get("size") or ""
        m2_match = re.search(r"[\d,]+(?:\.\d+)?", size_texto)
        if m2_match:
            try:
                m2 = float(m2_match.group(0).replace(",", ""))
            except ValueError:
                m2 = None

        # Tipo limpio: viene aparte en el HTML visible (más confiable que
        # tratar de separarlo del texto de "location", que a veces el
        # tipo mismo contiene " en ", ej. "Casa en condominio").
        tipo_tag = tarjeta.select_one("p.property-type")
        tipo = tipo_tag.get_text(strip=True) if tipo_tag else ""

        # Colonia y Municipio: "location" = "<Tipo> en <Colonia>, <Municipio>"
        # -- se quita el prefijo de tipo ya conocido, luego se separa por
        # la ÚLTIMA coma (algunas colonias también podrían traer comas).
        location = data.get("location", "") or ""
        resto = location
        if tipo and resto.startswith(tipo):
            resto = resto[len(tipo):].strip()
            if resto.lower().startswith("en "):
                resto = resto[3:].strip()
        colonia, municipio_tarjeta = "", ""
        if "," in resto:
            colonia, municipio_tarjeta = resto.rsplit(",", 1)
            colonia = colonia.strip()
            municipio_tarjeta = municipio_tarjeta.strip()
        else:
            colonia = resto.strip()

        # Código EB: primero del alt de la imagen (más directo), si no,
        # del nombre de archivo de image_url.
        img = tarjeta.select_one("img")
        img_alt = img.get("alt", "") if img else ""
        codigo_eb = extraer_codigo_eb(img_alt, data.get("image_url", ""))

        url_rel = data.get("url", "") or ""
        href = url_rel if url_rel.startswith("http") else f"{BASE}{url_rel}"

        resultados.append({
            "href": href,
            "precio": precio,
            "moneda": moneda,
            "m2": m2,
            "recamaras": data.get("bedrooms"),
            "banos": data.get("bathrooms"),
            "colonia": colonia,
            "municipio_tarjeta": municipio_tarjeta,
            "tipo": tipo,
            "titulo": data.get("title", ""),
            "codigo_eb": codigo_eb,
            "lat": tarjeta.get("data-lat"),
            "lon": tarjeta.get("data-long"),
        })

    return resultados


def recorrer_municipio(operacion_slug, municipio, session):
    """Genera todas las filas de un municipio para una operación (properties/rentals)."""
    filas = []
    pagina = 1
    while True:
        if pagina == 1:
            url = f"{BASE}/{operacion_slug}/mexico/jalisco/{municipio}?sort_by=price-desc"
        else:
            url = (f"{BASE}/{operacion_slug}/mexico/jalisco/{municipio}"
                   f"?page={pagina}&sort_by=price-desc")

        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            print(f"  [ERROR] {url} -> {e}")
            break

        if resp.status_code != 200:
            print(f"  [HTTP {resp.status_code}] {url} — deteniendo paginación aquí")
            break

        tarjetas = parsear_tarjetas(resp.text)
        if not tarjetas:
            break

        filas.extend(tarjetas)
        print(f"  {operacion_slug}/{municipio} página {pagina}: "
              f"{len(tarjetas)} propiedades (acumulado {len(filas)})")

        if "Siguiente" not in resp.text or f"page={pagina + 1}" not in resp.text:
            break

        pagina += 1
        time.sleep(PAUSA_ENTRE_PAGINAS)

    return filas


def main():
    session = requests.Session()
    todas_las_filas = []
    fecha_corrida = datetime.now().strftime("%Y-%m-%d")

    for operacion, slug in OPERACIONES.items():
        for municipio in MUNICIPIOS:
            print(f"\n== {operacion} — {municipio} ==")
            filas = recorrer_municipio(slug, municipio, session)
            piso = PISO_PRECIO[operacion]
            for f in filas:
                if f["precio"] is None or f["precio"] < piso:
                    continue
                todas_las_filas.append({
                    # El municipio real de la tarjeta (si vino) es más
                    # confiable que asumirlo del segmento de la URL que
                    # se recorrió -- evita el bug ya visto antes de
                    # "aviso_municipio_no_coincide" con datos cruzados.
                    "Municipio": f["municipio_tarjeta"] or municipio.replace("-", " ").title(),
                    "Colonia": f["colonia"],
                    "Operación": operacion,
                    "Precio": f["precio"],
                    "Moneda": f["moneda"],
                    "Título/Colonia": f["titulo"] or f["href"],
                    "Tipo": f["tipo"],
                    "Recámaras": f["recamaras"],
                    "Baños": f["banos"],
                    "m²": f["m2"],
                    "codigo_eb": f["codigo_eb"],
                    "Liga": f["href"],
                    "lat": f["lat"],
                    "lon": f["lon"],
                    "Fuente": "EasyBroker-aciertamax",
                    "Fecha_Corrida": fecha_corrida,
                })

    if not todas_las_filas:
        print("\n[AVISO] No se obtuvo ninguna fila. Revisa selectores/rutas antes "
              "de sobrescribir el CSV vigente.")
        sys.exit(1)

    columnas = ["Municipio", "Colonia", "Operación", "Precio", "Moneda", "Título/Colonia",
                "Tipo", "Recámaras", "Baños", "m²", "codigo_eb", "Liga", "lat", "lon",
                "Fuente", "Fecha_Corrida"]

    salida = f"inventario_zmg_{fecha_corrida}.csv"
    with open(salida, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columnas)
        writer.writeheader()
        writer.writerows(todas_las_filas)

    print(f"\nListo: {len(todas_las_filas)} propiedades guardadas en {salida}")
    print("Revísalo y si está bien, renómbralo a inventario_zmg.csv y sube el "
          "cambio al repositorio (o dile a Claude que lo revise primero).")


if __name__ == "__main__":
    main()
