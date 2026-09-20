"""
buscar_cerca_de.py
Filtra el inventario por cercanía a un punto de referencia (landmark),
usando las coordenadas de colonia ya geocodificadas (ver
geocodificar_colonias.py). Pensado para consultas tipo:
  "busco cerca de Andares, en un radio de 1 km"
  "algo por el Centro Magno, máximo 2 km"

LANDMARKS CONOCIDOS: se geocodifican una sola vez y quedan en caché junto
con las colonias (mismo archivo colonias_geocodificadas.csv), así que no
hay que mantener una lista aparte a mano.
"""

import math
import pandas as pd
from geocodificar_colonias import geocodificar, cargar_cache  # reusa el mismo geocodificador

ARCHIVO_INVENTARIO_CON_COORDS = "inventario_zmg_con_coordenadas.csv"


def distancia_km(lat1, lon1, lat2, lon2):
    """Distancia en línea recta (fórmula de haversine), en kilómetros."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def buscar_cerca_de(nombre_lugar, radio_km=1.0, operacion=None, tipo=None,
                     precio_max=None):
    """
    nombre_lugar: texto libre, ej. "Andares, Zapopan" o "Centro Magno, Guadalajara"
                  (mientras más específico, mejor geocodifica -- incluir el
                  municipio ayuda mucho).
    radio_km: qué tan lejos, en línea recta, se acepta desde ese punto.
    Regresa un DataFrame con las propiedades dentro del radio, más una
    columna 'distancia_km' para poder ordenar por cercanía real.
    """
    df = pd.read_csv(ARCHIVO_INVENTARIO_CON_COORDS)

    lat_centro, lon_centro = geocodificar(nombre_lugar, "")
    if lat_centro is None:
        print(f"[AVISO] No se pudo ubicar '{nombre_lugar}'. Prueba siendo "
              f"más específico, ej. '{nombre_lugar}, Zapopan, Jalisco'.")
        return pd.DataFrame()

    df = df.dropna(subset=["lat", "lon"]).copy()
    df["distancia_km"] = df.apply(
        lambda r: distancia_km(lat_centro, lon_centro, r["lat"], r["lon"]), axis=1)

    resultado = df[df["distancia_km"] <= radio_km].copy()

    if operacion:
        resultado = resultado[resultado["Operación"].str.upper() == operacion.upper()]
    if tipo:
        resultado = resultado[resultado["Tipo"].str.lower() == tipo.lower()]
    if precio_max:
        resultado = resultado[resultado["Precio"] <= precio_max]

    resultado = resultado.sort_values("distancia_km")
    print(f"'{nombre_lugar}' se ubicó en ({lat_centro:.5f}, {lon_centro:.5f})")
    print(f"{len(resultado)} propiedades dentro de {radio_km} km "
          f"(de un total de {len(df)} con coordenadas)")
    return resultado


if __name__ == "__main__":
    # Ejemplo real: lo que preguntó Javier
    res = buscar_cerca_de("Andares, Zapopan, Jalisco", radio_km=1.0)
    columnas = ["Título/Colonia", "Colonia", "Municipio", "Operación",
                "Precio", "distancia_km", "Liga"]
    if not res.empty:
        print(res[columnas].head(20).to_string(index=False))
