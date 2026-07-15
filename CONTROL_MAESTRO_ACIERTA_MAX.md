# CONTROL_MAESTRO_ACIERTA_MAX.md
**Proyecto:** MAX 2.0 — Agente Inmobiliario Acierta Max (ZMG)
**Versión vigente:** v2026.07.15
**Última actualización:** 15 de julio de 2026
**Responsable técnico:** Claude (arquitectura WATI/MAX) + ChatGPT (Manual Maestro Jurídico-Fiscal, 582 pp., v1.0)

---

## 1. DIVISIÓN DE TRABAJO DEL PROYECTO

| Frente | Responsable | Estado |
|---|---|---|
| Manual Maestro Inmobiliario Jurídico-Fiscal (24 módulos, RAG, matrices) | ChatGPT | v1.0 entregada (582 pp.); pendiente de validación con notario/especialista antes de citar cifras fiscales |
| Arquitectura conversacional WATI/MAX (Módulo III del prompt maestro) | Claude | En construcción activa — ver sección 3 |
| Publicidad, reels, plantillas de anuncio | Javier + ChatGPT (contenido) + Claude (integración con MAX) | En curso |
| Inventario y datos (scraper, EasyBroker) | Claude (código) + Javier (ejecución en su laptop) | Operativo |

**Regla de oro del proyecto:** ningún módulo del manual jurídico-fiscal se inyecta al prompt de MAX sin que Javier lo valide con su notario/especialista aliado. MAX nunca cita cifras, porcentajes, plazos o artículos legales inventados — eso quedó como regla dura desde el día 1.

---

## 2. MÓDULOS TERMINADOS (funcionando en producción hoy)

- **Webhook Wati + Flask en Render** (plan Starter, 24/7, sin cold-start).
- **Inventario propio** vía API EasyBroker (`buscar_propiedades`, `enviar_ficha`).
- **Bolsa compartida ZMG completa** vía lector propio (`inventario_zmg.py`, sin API):
  - Ventas desde $2,000,000 y rentas desde $13,000/mes.
  - 5 municipios: Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco.
  - Última corrida: **3,070 propiedades** cargadas (`buscar_inventario_zmg`, `seleccionar_de_lista`, `enviar_ficha_liga`).
  - Campo "Amueblado" detectado por texto de tarjeta (Sí/No/desconocido — nunca descarta por falta de dato).
- **4 campañas activas con ficha instantánea** (foto + datos + liga + aviso "propiedad compartida"):
  - The Block/ITESO — código **EB-WG7125**
  - Santa Ana 360 — código **EB-WL2602**
  - Bella Vittoria — código **EB-VI0277**
  - Villa Dhara/Parque Morelos — código **EB-WG7913**
  - Detección por nombre natural Y por código EB (ambos probados con código).
- **Modelo de calificación:** Querer-Poder-Cómo-Cuándo-Dónde + SPIN Compacto (Situación/Problema/Implicación/Necesidad-Beneficio), con válvula de escape ante impaciencia o rechazo explícito de zona.
- **Flujo vendedor (captación):** confirma ZMG, presume 20 años/AMPI, ofrece opinión de valor sin costo, pide cita, nunca da precio por chat.
- **Flujo comprador:** acceso a aciertamax.com + oferta de Coaching Inmobiliario con IA, no dispara búsqueda sin al menos una pregunta de calidad.
- **Casos especiales con categoría propia en la alerta:** reclamo de propietario, colaboración de agente, bolsa de trabajo.
- **Registro de leads:** Google Sheets, pestaña "Leads MAX", folio único ACIERTA-XXXX (candado anti-duplicado por teléfono/24h).
- **Aviso a Javier (🔥 LEAD CALIENTE)** vía WhatsApp, con encabezado distinto por categoría.
- **Agenda:** liga real de Calendly compartida al cerrar cita.
- **Cierre humanizado:** solo tras un handoff real verificado (nunca como promesa adelantada).
- **Regla de cita instantánea "\*":** atajo determinístico (no depende del modelo) que ofrece Calendly + avisa a Javier de inmediato.
- **Blindaje técnico:**
  - Fila anti-ráfaga (mensajes seguidos se juntan, una sola respuesta ordenada).
  - Anti-duplicados de eventos Wati.
  - Sanitización de historial ante reinicios (primer turno siempre usuario, sin vacíos).
  - Reintento automático ante fallas transitorias de la API de Claude.
  - **Verificación real de envío de fichas** (ya no se confirma "enviada" sin que Wati lo confirme de verdad) — bug crítico corregido y probado con código.
  - Prohibición explícita de inventar propiedades, datos, o confirmaciones de envío ("ya va la ficha" incluido).
  - Fallback de título vacío en fichas (usa Tipo + Municipio si el dato viene vacío).

## 3. MÓDULOS PENDIENTES (Módulo III del prompt maestro — arquitectura WATI)

- [ ] **Códigos AM-GUIA-XX** (contenido educativo: fraude, compra, gravamen, escritura, notario, Infonavit, renta) — requiere que Javier entregue el texto/liga real de cada guía; ya hay 2 reels grabados ("Errores al rentar", "Qué revisar antes de comprar") pendientes de conectar.
- [ ] **Códigos AM-SERV-XX** (comprar, vender, rentar, admin, valuar, crédito, inversión, exclusiva, consultoría) — mismo patrón que los códigos EB, pendiente de definir el texto de respuesta de cada uno.
- [ ] **Menú de rescate universal** — existe parcialmente (MAX no inventa y ofrece alternativas), falta formalizarlo con las 10 opciones numeradas exactas del prompt maestro.
- [ ] **Bitácora de contactos al 100%** (no solo los que llegan a folio) — diseño propuesto, no construido.
- [ ] **Radio/proximidad real a puntos de referencia** (tipo "5 km del ITESO") — requiere verificar si las fichas de EasyBroker exponen coordenadas GPS; pendiente de investigar.
- [ ] **Reporte de eventos de conversión a Meta/TikTok** — depende de la cuenta de Meta Business de Ubaldo.
- [ ] **Actualización automática semanal del inventario ZMG** — hoy es manual (Javier corre el script); se puede programar como cron job en Render.

## 4. DECISIONES APROBADAS POR JAVIER

- Acrónimo A-C-I-E-R-T-A: se adopta como marco oficial del método, con Querer-Poder-Cómo-Cuándo-Dónde como herramienta operativa dentro de la fase "Conocer".
- "Pasaporte Inmobiliario" = nombre comercial del Expediente Digital Único (mismo objeto, un solo nombre de cara al cliente).
- Precios de servicios ($500 diagnóstico, $200 honorario de gestión) confirmados como definidos por Javier, no por la IA.
- Anuncios: colores oficiales azul/rojo/blanco (no dorado), formato 4:5 estático y 9:16 vertical para reels.
- Mezcla de contenido: 40% propiedades destacadas / 20% inventario por segmento / 20% contenido de confianza / 20% marca y prueba social.
- No se anuncian las 3,070 propiedades indiscriminadamente — piloto de propiedades seleccionadas + contenido educativo.

## 5. RIESGOS Y PENDIENTES DE VALIDACIÓN

- El "amanecer con contestador en inglés" (bug de Wati, regla "Mensaje de bienvenida en WA") — se apagó, pero reapareció una vez; **revisar "Acción por defecto (Heredado)" en Wati**, nunca se completó ese diagnóstico.
- Todas las cifras fiscales/legales del manual de ChatGPT requieren validación con notario antes de usarse en producción.
- Confirmar `HUMAN_HANDOFF_NUMBER` sigue apuntando al celular personal de Javier (se corrigió una vez, verificar que no haya vuelto a cambiar).

## 6. ARCHIVOS VIGENTES DEL PROYECTO

- `app.py` — cerebro de MAX (webhook + agente + herramientas). **Versión vigente: la de esta sesión, con atajo "\*" incluido.**
- `inventario_zmg.py` — lector de bolsa ZMG (ventas + rentas, sin API).
- `inventario_zmg.csv` — datos vigentes (3,070 propiedades, corrida del 14/07/2026).
- `campanas.py` — ranking del inventario propio + generación de copys.
- `requirements.txt`, `Procfile` — infraestructura de Render.
- `ACIERTA_MAX_Manual_Inmobiliario_Completo_v1_0.pdf` — manual jurídico-fiscal (ChatGPT), en revisión.

---
*Este archivo se actualiza al final de cada sesión de trabajo. Antes de iniciar una fase nueva, consultarlo primero.*
