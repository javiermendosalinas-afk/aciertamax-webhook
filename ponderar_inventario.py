"""
ponderar_inventario.py
Aplica un modelo de ponderación comercial a TODO el inventario ZMG
(no solo un Top 100 piloto), extendiendo la metodología que ya existía
en la hoja "Top 100 Inventario ZMG - Ranking Comercial (agosto 2026)".

CRITERIOS DEL SCORE (0-100), de mayor a menor peso:
  1. Competitividad de precio (45%): qué tan por debajo está el precio/m²
     de una propiedad respecto a la MEDIANA de su mismo Municipio+Tipo+Operación.
     Más barato que la mediana = más "susceptible de venta" (se mueve más rápido).
  2. Completitud de ficha (25%): castiga propiedades sin m², sin recámaras/
     baños, sin tipo definido — datos incompletos generan menos confianza
     y MAX no puede calificar bien al cliente con ellas.
  3. Confiabilidad de precio (20%): descarta como "no confiable" precios
     absurdos (errores de captura tipo $450,000,000 en un estacionamiento)
     que de otro modo ganarían el ranking por parecer "baratísimos" por m².
  4. Prioridad comercial propia (10%, aditivo directo): bono fijo para tus
     desarrollos propios (BellaVittoria, Villa Dhara/Parque Morelos,
     The Block/ITESO, Eleve) porque tienen mayor rentabilidad/comisión.

SALIDA: inventario_zmg_ponderado.csv, TODAS las filas con su Score_Comercial,
ordenado de mayor a menor.
"""

import pandas as pd
import numpy as np

# Códigos EB conocidos de desarrollos propios (mayor rentabilidad/comisión)
DESARROLLOS_PROPIOS = {
    "EB-VI0277": "BellaVittoria",
    "EB-WG7913": "Villa Dhara / Parque Morelos",
    "EB-WG7125": "The Block / ITESO",
    "EB-WM2996": "Eleve Valle Real",
}

BONO_PROPIO = 10  # puntos que se suman directo al score si es desarrollo propio

df = pd.read_csv("inventario_zmg.csv")

# --- 1. Precio por m² ---
df["precio_m2"] = np.where(df["m²"] > 0, df["Precio"] / df["m²"], np.nan)

# --- 2. Confiabilidad de precio ---
# Regla simple y explicable: se descarta como NO confiable si el precio/m²
# está a más de 4x o menos de 0.2x la mediana de su grupo (Municipio+Tipo+Operación),
# o si faltan los datos para calcularlo.
grupo = df.groupby(["Municipio", "Tipo", "Operación"])["precio_m2"]
mediana_grupo = grupo.transform("median")
df["precio_m2_vs_mediana"] = (mediana_grupo - df["precio_m2"]) / mediana_grupo

df["Precio_Confiable"] = (
    df["precio_m2"].notna()
    & (df["precio_m2"] <= mediana_grupo * 4)
    & (df["precio_m2"] >= mediana_grupo * 0.2)
)

# --- 3. Completitud de ficha (0 a 1) ---
campos_clave = ["Recámaras", "Baños", "m²", "Tipo"]
df["Completitud"] = sum(df[c].notna() & (df[c].astype(str).str.strip() != "")
                         for c in campos_clave) / len(campos_clave)

# --- 4. Score comercial (0-100) ---
# Competitividad: clip a [0,1] antes de pesar (evita que un outlier negativo
# hunda desproporcionadamente el score de una propiedad por lo demás sana)
competitividad = df["precio_m2_vs_mediana"].clip(lower=0, upper=1).fillna(0)

score_base = (
    competitividad * 45
    + df["Completitud"] * 25
    + df["Precio_Confiable"].astype(int) * 20
)

df["Es_Desarrollo_Propio"] = df["codigo_eb"].map(DESARROLLOS_PROPIOS)
bono = df["Es_Desarrollo_Propio"].notna().astype(int) * BONO_PROPIO

df["Score_Comercial"] = (score_base + bono).clip(upper=100).round(1)

# Sin dato confiable de precio -> no se puede evaluar bien, al fondo de la lista
df.loc[~df["Precio_Confiable"], "Score_Comercial"] = (
    df.loc[~df["Precio_Confiable"], "Score_Comercial"] * 0.3
)

df = df.sort_values("Score_Comercial", ascending=False)

columnas_salida = [
    "Score_Comercial", "Es_Desarrollo_Propio", "Precio_Confiable", "Municipio",
    "Operación", "Tipo", "Título/Colonia", "Precio", "precio_m2",
    "precio_m2_vs_mediana", "Recámaras", "Baños", "m²", "Completitud",
    "codigo_eb", "Liga",
]

df[columnas_salida].to_csv("inventario_zmg_ponderado.csv", index=False)

print(f"Total propiedades ponderadas: {len(df)}")
print(f"Confiables: {df['Precio_Confiable'].sum()} / No confiables: {(~df['Precio_Confiable']).sum()}")
print(f"Desarrollos propios detectados en el CSV: {df['Es_Desarrollo_Propio'].notna().sum()}")
print("\nTop 10:")
print(df[columnas_salida].head(10).to_string(index=False))
