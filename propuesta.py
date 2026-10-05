"""Propuesta personalizada en PDF que el coach le envía al cliente.

generar_pdf(datos) -> bytes
  datos = {
    "cliente": {"nombre": str, "folio": str},
    "coach": {"nombre": str, "telefono": str},
    "criterios": str,          # "venta de casa en Zapopan, hasta $5 millones, 3 recámaras"
    "nota": str,               # párrafo personal del coach (lo que platicaron)
    "propiedades": [ {eb, operacion, titulo, tipo, precio, municipio, colonia,
                      recamaras, banos, m2, foto, lat, lon, liga, resumen, ventajas[]} ],
  }
Nada de esta carta inventa datos: precio, ubicación y medidas vienen del
inventario; el resumen y las ventajas los escribe (o revisa) el coach.
"""
import io
import os
import datetime
import concurrent.futures as cf

import requests
from fpdf import FPDF

BASE = os.path.dirname(os.path.abspath(__file__))
REC = os.path.join(BASE, "recursos")
NAVY, RED, CREAM, GRAY, INK = (15, 31, 61), (214, 40, 40), (247, 245, 240), (107, 114, 128), (26, 26, 26)
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
UA = {"User-Agent": "AciertaMax-Propuestas/1.0 (+https://acierta.pro)"}
WHATSAPP_ACIERTA_TXT = "33 3377 7337"


def _precio(p):
    try:
        n = float(p.get("precio") or 0)
    except (TypeError, ValueError):
        n = 0
    if not n:
        return "Precio a consultar"
    txt = f"${n:,.0f} MXN"
    return txt + (" al mes" if (p.get("operacion") or "").upper() == "RENTA" else "")


def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def _datos_linea(p):
    partes = []
    r, b, m = _num(p.get("recamaras")), _num(p.get("banos")), _num(p.get("m2"))
    if r:
        partes.append(f"{r} recámara" + ("s" if r != 1 else ""))
    if b:
        partes.append(f"{b} baño" + ("s" if b != 1 else ""))
    if m:
        partes.append(f"{m:,} m²")
    return " · ".join(partes)


def _foto_grande(url):
    if not url:
        return ""
    return url.replace("height=300", "height=600").replace("width=450", "width=900")


def _descargar(url, timeout=15):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        if r.status_code == 200 and r.content[:4] != b"<!DO":
            return r.content
    except Exception:
        pass
    return None


def _imagen_jpeg(contenido, max_lado=1000):
    """Normaliza cualquier imagen (png/webp/jpg) a JPEG RGB para el PDF."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(contenido)).convert("RGB")
        im.thumbnail((max_lado, max_lado))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82)
        out.seek(0)
        return out
    except Exception:
        return None


def crear_mapa(props, ancho=1400, alto=760):
    """Mapa con OpenStreetMap y un marcador numerado por propiedad.
    Regresa un BytesIO PNG o None si no hay coordenadas o no hay red."""
    puntos = [(i + 1, float(p["lon"]), float(p["lat"])) for i, p in enumerate(props)
              if p.get("lat") not in (None, "") and p.get("lon") not in (None, "")]
    if not puntos:
        return None
    try:
        from staticmap import StaticMap, CircleMarker
        from staticmap.staticmap import _lon_to_x, _lat_to_y
        from PIL import ImageDraw, ImageFont
        m = StaticMap(ancho, alto, padding_x=70, padding_y=70,
                      url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers=UA,
                      tile_request_timeout=6)
        for _, lon, lat in puntos:
            m.add_marker(CircleMarker((lon, lat), "#ffffff", 46))
            m.add_marker(CircleMarker((lon, lat), "#d62828", 38))
        img = m.render(zoom=15 if len(puntos) == 1 else None)
        d = ImageDraw.Draw(img)
        try:
            f = ImageFont.truetype(os.path.join(REC, "fuentes", "Poppins-Bold.ttf"), 22)
        except Exception:
            f = ImageFont.load_default()
        for n, lon, lat in puntos:
            x = m._x_to_px(_lon_to_x(lon, m.zoom))
            y = m._y_to_px(_lat_to_y(lat, m.zoom))
            d.text((x, y), str(n), fill="#ffffff", font=f, anchor="mm")
        d.rectangle((ancho - 290, alto - 26, ancho, alto), fill="#ffffff")
        d.text((ancho - 284, alto - 22), "© colaboradores de OpenStreetMap", fill="#444444",
               font=ImageFont.truetype(os.path.join(REC, "fuentes", "Poppins-Regular.ttf"), 15))
        out = io.BytesIO()
        img.save(out, "PNG")
        out.seek(0)
        return out
    except Exception as e:
        print(f"[PROPUESTA] No se pudo generar el mapa: {e}", flush=True)
        return None


class _PDF(FPDF):
    def footer(self):
        self.set_y(-13)
        self.set_draw_color(*RED)
        self.set_line_width(0.4)
        self.line(18, self.get_y() - 1.5, 192, self.get_y() - 1.5)
        self.set_font("Poppins", "", 7.5)
        self.set_text_color(*GRAY)
        self.cell(0, 5, f"Acierta Max · Profesionales Inmobiliarios · acierta.pro · WhatsApp {WHATSAPP_ACIERTA_TXT}", align="L")
        self.cell(0, 5, f"Página {self.page_no()} de {{nb}}", align="R")


def _fecha_hoy():
    hoy = datetime.datetime.utcnow() - datetime.timedelta(hours=6)  # Guadalajara
    return f"{hoy.day} de {MESES[hoy.month - 1]} de {hoy.year}"


def _limpiar_descripcion(txt, maximo=1400):
    """Descripción del anunciante, limpia: sin emojis ni caracteres invisibles,
    sin líneas en blanco de más y acotada en longitud."""
    import re
    import unicodedata
    s = str(txt or "").replace("\r", "")
    s = "".join(ch for ch in s if (ord(ch) < 0x2190 or 0x2500 <= ord(ch) < 0x2600)
                and unicodedata.category(ch) not in ("Cf", "Co", "Cs"))
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = "\n".join(l.strip() for l in s.split("\n"))
    s = re.sub(r"\n{2,}", "\n", s).strip()
    if len(s) > maximo:
        s = s[:maximo].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def generar_pdf(datos, detalle_fn=None):
    cliente = datos.get("cliente") or {}
    coach = datos.get("coach") or {}
    props = (datos.get("propiedades") or [])[:12]
    nombre = (cliente.get("nombre") or "").strip() or "Estimado cliente"
    primer = nombre.split()[0] if nombre != "Estimado cliente" else ""

    # Fotos en paralelo (cada una con su límite de tiempo)
    # Fotos y mapa en paralelo, con límite de tiempo (Render corta a los 30 s)
    anexo = bool(datos.get("anexo", True)) and detalle_fn is not None
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        fut_mapa = ex.submit(crear_mapa, props)
        futs_det = [ex.submit(detalle_fn, p) for p in props] if anexo else []
        fotos = list(ex.map(lambda p: _descargar(_foto_grande(p.get("foto")), timeout=8), props))
        try:
            mapa = fut_mapa.result(timeout=18)
        except Exception:
            mapa = None
        detalles = []
        for f in futs_det:
            try:
                detalles.append(f.result(timeout=20) or {})
            except Exception:
                detalles.append({})
        # galería del anexo: hasta 3 fotos adicionales por propiedad
        galerias = []
        if anexo:
            urls = [[u for u in (d.get("fotos") or []) if u][:4] for d in detalles]
            planas = [u for lista in urls for u in lista]
            bajadas = dict(zip(planas, ex.map(lambda u: _descargar(u, timeout=8), planas)))
            galerias = [[_imagen_jpeg(bajadas[u], 1200) for u in lista if bajadas.get(u)] for lista in urls]
    fotos = [_imagen_jpeg(f) if f else None for f in fotos]

    pdf = _PDF(format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, margin=18)
    for estilo, archivo in (("", "Regular"), ("B", "Bold"), ("I", "Italic")):
        pdf.add_font("Poppins", estilo, os.path.join(REC, "fuentes", f"Poppins-{archivo}.ttf"))
    pdf.add_font("PoppinsM", "", os.path.join(REC, "fuentes", "Poppins-Medium.ttf"))
    pdf.alias_nb_pages()
    pdf.set_title(f"Propuesta Acierta Max para {nombre}")
    pdf.set_author(coach.get("nombre") or "Acierta Max")
    pdf.add_page()
    W = 174  # ancho útil

    # Encabezado
    pdf.image(os.path.join(REC, "logos", "acierta-max.png"), x=18, y=14, w=66)
    pdf.set_xy(110, 15)
    pdf.set_font("Poppins", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.cell(82, 6, "Propuesta personalizada", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(110)
    pdf.set_font("Poppins", "", 8.5)
    pdf.set_text_color(*GRAY)
    linea_folio = f"Folio {cliente['folio']} · " if cliente.get("folio") else ""
    pdf.cell(82, 5, f"{linea_folio}{len(props)} propiedad" + ("es" if len(props) != 1 else ""), align="R")
    pdf.set_draw_color(*RED)
    pdf.set_line_width(0.8)
    pdf.line(18, 32, 192, 32)

    # Carta
    pdf.set_xy(18, 37)
    pdf.set_text_color(*INK)
    pdf.set_font("Poppins", "", 9.5)
    pdf.cell(W, 5, f"Guadalajara, Jalisco, a {_fecha_hoy()}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Poppins", "B", 11)
    pdf.cell(W, 6, nombre, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Poppins", "", 9.5)
    pdf.cell(W, 5, "Presente", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    def parrafo(texto, alto=5.1, tam=9.8, estilo=""):
        pdf.set_font("PoppinsM" if estilo == "M" else "Poppins", "" if estilo == "M" else estilo, tam)
        pdf.set_text_color(*INK)
        pdf.multi_cell(W, alto, texto, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.2)

    parrafo(f"Estimado(a) {primer or nombre}:" if primer else "Estimado(a) cliente:", estilo="M")
    parrafo("Es un gusto acompañarte en la búsqueda de tu próxima propiedad. En Acierta Max creemos que "
            "encontrar el lugar correcto es un proyecto que se construye en equipo, y queremos lograrlo juntos.")
    criterios = (datos.get("criterios") or "").strip().rstrip(".")
    nota = (datos.get("nota") or "").strip()
    base = "Estudié las propiedades que revisaste en acierta.pro"
    base += f", los criterios que me indicaste ({criterios})" if criterios else ", los criterios que me indicaste"
    base += " y lo que platicamos"
    base += f": {nota.rstrip('.')}." if nota else "."
    base += (" Con base en ello seleccioné las siguientes opciones de la Zona Metropolitana de Guadalajara, "
             "que a mi juicio responden mejor a lo que buscas.")
    parrafo(base)

    # Mapa
    if mapa:
        alto_mapa = W * 760 / 1400
        if pdf.get_y() + alto_mapa + 10 > 279:
            pdf.add_page()
        pdf.set_font("Poppins", "B", 10)
        pdf.set_text_color(*NAVY)
        pdf.cell(W, 6, "Ubicación de las propiedades sugeridas", new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 1
        pdf.image(mapa, x=18, y=y, w=W, h=alto_mapa)
        pdf.set_y(y + alto_mapa + 4)

    # Fichas
    for i, p in enumerate(props):
        ventajas = [v.strip(" •-\t") for v in (p.get("ventajas") or []) if str(v).strip(" •-\t")][:4]
        resumen = (p.get("resumen") or "").strip()
        # alto estimado del bloque para no partir una ficha entre páginas
        alto_txt = 26 + len(ventajas) * 5 + (len(resumen) // 85 + 1) * 4.6
        alto_bloque = max(52, alto_txt) + 8
        if pdf.get_y() + alto_bloque > 279:
            pdf.add_page()
        y0 = pdf.get_y() + 2
        # barra de título
        pdf.set_fill_color(*NAVY)
        pdf.rect(18, y0, W, 8, "F")
        pdf.set_fill_color(*RED)
        pdf.rect(18, y0, 9, 8, "F")
        pdf.set_xy(18, y0 + 0.6)
        pdf.set_font("Poppins", "B", 10)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(9, 7, str(i + 1), align="C")
        titulo = (p.get("titulo") or f"{(p.get('tipo') or 'Propiedad').capitalize()} en {p.get('municipio') or 'la ZMG'}").strip()
        if titulo.isupper():
            titulo = titulo.capitalize()
        titulo_orig = titulo
        pdf.set_font("PoppinsM", "", 9)
        max_tit = 118
        while pdf.get_string_width(titulo) > max_tit and len(titulo) > 10:
            titulo = titulo[:-2]
        if titulo != titulo_orig:
            titulo = titulo.rstrip() + "…"
        pdf.cell(124, 7, " " + titulo)
        pdf.set_font("Poppins", "B", 9)
        pdf.cell(W - 9 - 124 - 2, 7, p.get("eb") or "", align="R")
        # foto
        yc = y0 + 11
        foto = fotos[i]
        if foto:
            pdf.image(foto, x=18, y=yc, w=62, h=41.3, keep_aspect_ratio=True)
        else:
            pdf.set_fill_color(*CREAM)
            pdf.rect(18, yc, 62, 41.3, "F")
            pdf.set_xy(18, yc + 18)
            pdf.set_font("Poppins", "", 8)
            pdf.set_text_color(*GRAY)
            pdf.cell(62, 5, "Foto en la ficha en línea", align="C")
        # datos
        x2, w2 = 84, W - 66
        pdf.set_xy(x2, yc - 0.5)
        pdf.set_font("Poppins", "B", 13)
        pdf.set_text_color(*RED)
        pdf.cell(w2, 7, _precio(p), new_x="LEFT", new_y="NEXT")
        pdf.set_font("PoppinsM", "", 9)
        pdf.set_text_color(*NAVY)
        ubic = ", ".join(x for x in (p.get("colonia"), p.get("municipio")) if x)
        pdf.cell(w2, 5, ubic, new_x="LEFT", new_y="NEXT")
        dl = _datos_linea(p)
        if dl:
            pdf.set_font("Poppins", "", 8.5)
            pdf.set_text_color(*GRAY)
            pdf.cell(w2, 5, dl, new_x="LEFT", new_y="NEXT")
        if resumen:
            pdf.ln(1)
            pdf.set_x(x2)
            pdf.set_font("Poppins", "", 8.8)
            pdf.set_text_color(*INK)
            pdf.multi_cell(w2, 4.6, resumen, new_x="LEFT", new_y="NEXT")
        if ventajas:
            pdf.ln(0.8)
            for v in ventajas:
                pdf.set_x(x2)
                pdf.set_font("Poppins", "B", 8.8)
                pdf.set_text_color(*RED)
                pdf.cell(4, 4.8, "•")
                pdf.set_font("Poppins", "", 8.8)
                pdf.set_text_color(*INK)
                pdf.multi_cell(w2 - 4, 4.8, v, new_x="LEFT", new_y="NEXT")
        # ligas
        pdf.ln(1)
        pdf.set_x(x2)
        pdf.set_font("PoppinsM", "", 8.3)
        pdf.set_text_color(*NAVY)
        op = "R" if (p.get("operacion") or "").upper() == "RENTA" else "V"
        ficha = f"https://acierta.pro/ficha.html?eb={p.get('eb', '')}&op={op}"
        pdf.cell(32, 5, "Ver ficha completa ›", link=ficha)
        if p.get("lat") and p.get("lon"):
            pdf.cell(32, 5, "Ver en el mapa ›", link=f"https://www.google.com/maps?q={p['lat']},{p['lon']}")
        pdf.set_y(max(pdf.get_y() + 6, yc + 41.3 + 5))

    # Cierre
    if pdf.get_y() + 120 > 279:
        pdf.add_page()
    pdf.ln(2)
    parrafo("Te invito a revisarlas con calma. Tu retroalimentación es muy valiosa: respóndeme por WhatsApp "
            "con las claves EB que te interesen y programamos una visita en el día y horario que mejor te acomode.")
    # Recuadro Verifica
    y = pdf.get_y() + 1
    pdf.set_fill_color(*CREAM)
    pdf.rect(18, y, W, 25, "F")
    pdf.set_fill_color(*RED)
    pdf.rect(18, y, 2.2, 25, "F")
    pdf.set_xy(24, y + 2.5)
    pdf.set_font("Poppins", "B", 10.5)
    pdf.set_text_color(*NAVY)
    pdf.cell(W - 10, 6, "Antes de firmar, verifica.", new_x="LEFT", new_y="NEXT")
    pdf.set_font("Poppins", "", 8.8)
    pdf.set_text_color(*INK)
    pdf.multi_cell(W - 10, 4.6,
                   "Te recomiendo Acierta Verifica, nuestra revisión física y documental preventiva del inmueble, "
                   "desde $45 por m² (mínimo $3,500 MXN en la ZMG). Si compras o rentas esa propiedad con "
                   "Acierta Max, te bonificamos su costo.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(y + 29)
    parrafo("En Acierta Max somos profesionales inmobiliarios. Operamos conforme a la NOM-247-SE-2021, con "
            "contratos de adhesión registrados ante PROFECO, y somos miembros de la Asociación Mexicana de "
            "Profesionales Inmobiliarios (AMPI) y, en Estados Unidos, de la National Association of REALTORS® (NAR).")
    pdf.ln(1)
    pdf.set_font("Poppins", "", 9.5)
    pdf.cell(W, 5, "Atentamente,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font("Poppins", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.cell(W, 6, coach.get("nombre") or "Acierta Max", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Poppins", "", 9)
    pdf.set_text_color(*GRAY)
    pdf.cell(W, 5, "Coach inmobiliario · Acierta Max", new_x="LMARGIN", new_y="NEXT")
    contacto = " · ".join(x for x in (f"WhatsApp {coach['telefono']}" if coach.get("telefono") else "", "acierta.pro") if x)
    pdf.cell(W, 5, contacto, new_x="LMARGIN", new_y="NEXT")

    # Logotipos y credenciales
    if pdf.get_y() + 34 > 279:
        pdf.add_page()
    y = pdf.get_y() + 6
    pdf.set_draw_color(*CREAM)
    pdf.set_line_width(0.4)
    pdf.line(18, y - 2, 192, y - 2)
    col = W / 3
    logos = [("ampi.png", 26, "Miembro de la Asociación Mexicana\nde Profesionales Inmobiliarios"),
             ("nar.png", 34, "Miembro de la National\nAssociation of REALTORS®"),
             ("conocer.png", 21, "Dirección General certificada en el\nestándar EC0110.02 · Folio D-0027837023")]
    for k, (archivo, ancho, texto) in enumerate(logos):
        cx = 18 + col * k + col / 2
        pdf.image(os.path.join(REC, "logos", archivo), x=cx - ancho / 2, y=y, w=ancho)
        pdf.set_xy(18 + col * k, y + 17)
        pdf.set_font("Poppins", "", 6.8)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(col, 3.3, texto, align="C")
    pdf.set_xy(18, y + 26)
    pdf.set_font("PoppinsM", "", 7.2)
    pdf.set_text_color(*NAVY)
    pdf.cell(W, 4, "Contratos de adhesión registrados ante PROFECO · Conforme a la NOM-247-SE-2021", align="C")

    if anexo:
        _anexo_fichas(pdf, props, detalles, galerias, fotos)
    return bytes(pdf.output())


def _anexo_fichas(pdf, props, detalles, galerias, fotos_principales):
    W = 174
    for i, p in enumerate(props):
        d = detalles[i] if i < len(detalles) else {}
        gal = galerias[i] if i < len(galerias) else []
        pdf.add_page()
        # encabezado del anexo
        pdf.set_font("Poppins", "B", 8)
        pdf.set_text_color(*RED)
        pdf.cell(W, 5, f"ANEXO {i + 1} · FICHA COMPLETA", new_x="LMARGIN", new_y="NEXT")
        titulo = (d.get("titulo") or p.get("titulo") or "").strip()
        if titulo.isupper():
            titulo = titulo.capitalize()
        pdf.set_font("Poppins", "B", 13)
        pdf.set_text_color(*NAVY)
        pdf.multi_cell(W, 6.5, titulo or "Propiedad", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("PoppinsM", "", 9.5)
        pdf.set_text_color(*GRAY)
        ubic = d.get("ubicacion") or ", ".join(x for x in (p.get("colonia"), p.get("municipio")) if x)
        pdf.cell(W - 40, 5.5, ubic)
        pdf.set_font("Poppins", "B", 9.5)
        pdf.set_text_color(*NAVY)
        pdf.cell(40, 5.5, p.get("eb") or "", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Poppins", "B", 15)
        pdf.set_text_color(*RED)
        pdf.cell(W, 8, _precio(p) + ("  ·  " + ("Renta" if (p.get("operacion") or "").upper() == "RENTA" else "Venta")),
                 new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 1.5
        # galería: foto grande + hasta 3 chicas
        principal = gal[0] if gal else (fotos_principales[i] if i < len(fotos_principales) else None)
        resto = gal[1:4] if gal else []
        if principal:
            if resto:
                pdf.image(principal, x=18, y=y, w=114, h=76, keep_aspect_ratio=True)
                for k, im in enumerate(resto):
                    pdf.image(im, x=135, y=y + k * 25.6, w=57, h=24.6, keep_aspect_ratio=True)
            else:
                pdf.image(principal, x=18, y=y, w=W, h=80, keep_aspect_ratio=True)
            pdf.set_y(y + (77 if resto else 81) + 3)
        # tabla de datos
        filas = []
        def agrega(etq, val, suf=""):
            if val not in (None, "", 0, "0"):
                v = _num(val)
                filas.append((etq, (f"{v:,}" if isinstance(v, (int, float)) else str(val)) + suf))
        agrega("Tipo", (p.get("tipo") or "").capitalize())
        agrega("Recámaras", d.get("recamaras") or p.get("recamaras"))
        agrega("Baños", d.get("banos") or p.get("banos"))
        agrega("Medios baños", d.get("medio_banos"))
        agrega("Estacionamientos", d.get("estacionamientos"))
        agrega("Construcción", d.get("construccion_m2") or p.get("m2"), " m²")
        agrega("Terreno", d.get("terreno_m2"), " m²")
        agrega("Niveles", d.get("niveles"))
        ant = _num(d.get("antiguedad"))
        if ant and ant >= 1900:
            filas.append(("Año de construcción", str(int(ant))))
        elif ant:
            filas.append(("Antigüedad", f"{int(ant)} año" + ("s" if ant != 1 else "")))
        agrega("Mantenimiento", d.get("mantenimiento"))
        if filas:
            pdf.set_font("Poppins", "B", 10)
            pdf.set_text_color(*NAVY)
            pdf.cell(W, 6, "Datos de la propiedad", new_x="LMARGIN", new_y="NEXT")
            colw = W / 2
            for k in range(0, len(filas), 2):
                for etq, val in filas[k:k + 2]:
                    pdf.set_fill_color(*CREAM)
                    pdf.set_font("Poppins", "", 8.6)
                    pdf.set_text_color(*GRAY)
                    pdf.cell(colw * 0.45, 6, " " + etq, fill=True)
                    pdf.set_font("PoppinsM", "", 8.8)
                    pdf.set_text_color(*INK)
                    pdf.cell(colw * 0.55 - 1, 6, val, fill=True)
                    pdf.cell(1, 6, "")
                pdf.ln(6.6)
            pdf.ln(1.5)
        # amenidades
        amen = [a for a in (d.get("amenidades") or []) if a][:24]
        if amen:
            pdf.set_font("Poppins", "B", 10)
            pdf.set_text_color(*NAVY)
            pdf.cell(W, 6, "Amenidades y características", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Poppins", "", 8.6)
            pdf.set_text_color(*INK)
            colw = W / 3
            for k in range(0, len(amen), 3):
                for a_ in amen[k:k + 3]:
                    txt = a_ if pdf.get_string_width(a_) < colw - 6 else a_[:34] + "…"
                    pdf.set_text_color(*RED)
                    pdf.cell(4, 5, "•")
                    pdf.set_text_color(*INK)
                    pdf.cell(colw - 4, 5, txt)
                pdf.ln(5)
            pdf.ln(2)
        # descripción
        desc = _limpiar_descripcion(d.get("descripcion"))
        max_lineas = int((279 - 24 - pdf.get_y() - 6) / 4.4)   # lo que cabe antes de ligas y aviso
        if desc and max_lineas >= 3:
            pdf.set_font("Poppins", "", 8.6)
            lineas = pdf.multi_cell(W, 4.4, desc, dry_run=True, output="LINES")
            if len(lineas) > max_lineas:
                desc = "\n".join(lineas[:max_lineas - 1]).rstrip(" ,.;:") + "…"
        else:
            desc = ""
        if desc:
            pdf.set_font("Poppins", "B", 10)
            pdf.set_text_color(*NAVY)
            pdf.cell(W, 6, "Descripción", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Poppins", "", 8.6)
            pdf.set_text_color(*INK)
            pdf.multi_cell(W, 4.4, desc, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        # ligas y aviso
        pdf.set_font("PoppinsM", "", 8.5)
        pdf.set_text_color(*NAVY)
        op = "R" if (p.get("operacion") or "").upper() == "RENTA" else "V"
        pdf.cell(45, 5, "Ver ficha en línea ›", link=f"https://acierta.pro/ficha.html?eb={p.get('eb', '')}&op={op}")
        if p.get("lat") and p.get("lon"):
            pdf.cell(45, 5, "Ver ubicación en el mapa ›", link=f"https://www.google.com/maps?q={p['lat']},{p['lon']}")
        pdf.ln(7)
        pdf.set_font("Poppins", "I", 7.2)
        pdf.set_text_color(*GRAY)
        pdf.multi_cell(W, 3.6, "Información proporcionada por el anunciante a través de EasyBroker. Precio, disponibilidad y "
                               "características sujetos a confirmación al momento de la visita.", new_x="LMARGIN", new_y="NEXT")



def nombre_archivo(cliente_nombre):
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", cliente_nombre or "cliente").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "cliente"
    return f"Propuesta_Acierta_Max_{s}.pdf"
