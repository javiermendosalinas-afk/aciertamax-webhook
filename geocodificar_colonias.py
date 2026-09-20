"""
geocodificar_colonias.py
Agrega latitud/longitud aproximadas al inventario, usando el geocodificador
GRATUITO de OpenStreetMap (Nominatim) -- sin necesidad de la API de pago
de EasyBroker ni de Google Maps.

CÓMO FUNCIONA:
- No geocodifica cada propiedad una por una (sería lentísimo con miles de
  filas) -- geocodifica cada combinación única de (Colonia, Municipio) UNA
  sola vez, y le pega esas coordenadas a todas las propiedades de esa
  colonia. Con ~500-800 colonias distintas en la ZMG, esto tarda unos
  10-15 minutos en vez de horas.
- Respeta la política de uso de Nominatim: máximo 1 solicitud por segundo,
  y un User-Agent identificable (obligatorio, si no te bloquean).
- Guarda un caché en colonias_geocodificadas.csv -- si vuelves a correr el
  script después, NO vuelve a geocodificar lo que ya tenía, solo lo nuevo.

PRECISIÓN: esto da el centro aproximado de la colonia, NO la dirección
exacta de cada propiedad (para eso sí haría falta la API de EasyBroker
o Google Maps con la dirección completa). Es suficiente para que un
cliente dibuje un área en un mapa y filtremos qué colonias caen dentro.

CÓMO CORRERLO:
    pip install requests pandas --break-system-packages   (si hace falta)
    python3 geocodificar_colonias.py
"""

import time
import requests
import pandas as pd

ARCHIVO_INVENTARIO = "inventario_zmg_ponderado.csv"
ARCHIVO_CACHE = "colonias_geocodificadas.csv"
ARCHIVO_SALIDA = "inventario_zmg_con_coordenadas.csv"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {
    # Nominatim EXIGE un User-Agent identificable con datos de contacto,
    # o bloquea las solicitudes. No lo quites ni lo dejes genérico.
    "User-Agent": "AciertaMaxInventarioBot/1.0 (javier.mendoza@acierta.com.mx)"
}
PAUSA_ENTRE_SOLICITUDES = 1.1  # segundos -- Nominatim pide máx 1/segundo


def cargar_cache():
    try:
        return pd.read_csv(ARCHIVO_CACHE)
    except FileNotFoundError:
        return pd.DataFrame(columns=["Colonia", "Municipio", "lat", "lon"])


def geocodificar(colonia, municipio):
    query = f"{colonia}, {municipio}, Jalisco, México"
    try:
        r = requests.get(NOMINATIM_URL, params={
            "q": query, "format": "json", "limit": 1,
            "countrycodes": "mx",
        }, headers=HEADERS, timeout=15)
        datos = r.json()
        if datos:
            return float(datos[0]["lat"]), float(datos[0]["lon"])
    except Exception as e:
        print(f"  [ERROR] geocodificando '{query}': {e}")
    return None, None


def main():
    df = pd.read_csv(ARCHIVO_INVENTARIO)
    if "Colonia" not in df.columns:
        print("[AVISO] El inventario no tiene columna 'Colonia' todavía.")
        print("Corre primero inventario_zmg_v6.py (versión actualizada) "
              "para generar un inventario con colonia separada del título.")
        return

    df["Colonia"] = df["Colonia"].fillna("").astype(str).str.strip()
    df = df[df["Colonia"] != ""]  # sin colonia, no se puede geocodificar

    combos = df[["Colonia", "Municipio"]].drop_duplicates().reset_index(drop=True)
    print(f"Combinaciones únicas de Colonia+Municipio: {len(combos)}")

    cache = cargar_cache()
    ya_geocodificadas = set(zip(cache["Colonia"], cache["Municipio"]))

    nuevas_filas = []
    for i, row in combos.iterrows():
        clave = (row["Colonia"], row["Municipio"])
        if clave in ya_geocodificadas:
            continue
        lat, lon = geocodificar(row["Colonia"], row["Municipio"])
        nuevas_filas.append({"Colonia": row["Colonia"], "Municipio": row["Municipio"],
                              "lat": lat, "lon": lon})
        estado = "OK" if lat else "sin resultado"
        print(f"  [{i+1}/{len(combos)}] {row['Colonia']}, {row['Municipio']} -> {estado}")
        time.sleep(PAUSA_ENTRE_SOLICITUDES)

    if nuevas_filas:
        cache = pd.concat([cache, pd.DataFrame(nuevas_filas)], ignore_index=True)
        cache.to_csv(ARCHIVO_CACHE, index=False)

    resultado = df.merge(cache, on=["Colonia", "Municipio"], how="left")
    resultado.to_csv(ARCHIVO_SALIDA, index=False)

    con_coords = resultado["lat"].notna().sum()
    print(f"\nListo: {ARCHIVO_SALIDA}")
    print(f"{con_coords} de {len(resultado)} propiedades con coordenadas "
          f"({con_coords/len(resultado)*100:.0f}%)")


if __name__ == "__main__":
    main()
