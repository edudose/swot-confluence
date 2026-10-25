# Monitor de regulación y precipitación — cuenca del río Iguazú

Reúne en un solo estado tres cosas que normalmente se miran por separado:

- **Regulación** de la cascada del Iguazú (afluente, turbinada, vertida, defluente).
- **Precipitación media areal** sobre la cuenca, a partir de MERGE/INPE.
- **Caudal observado** en Porto Capanema y Hotel Cataratas.

## Instalación

```bash
pip install -e .
pip install -e ".[grib]"    # sólo si vas a usar la precipitación MERGE
```

La lectura de GRIB2 necesita `cfgrib` sobre `eccodes`. En Windows suele ser
más simple por conda:

```bash
conda install -c conda-forge cfgrib eccodes
```

## Uso

```bash
iguacu-monitor probe                 # ¿qué devuelve cada fuente ahora?
iguacu-monitor discover "Capanema"   # buscar el código ANA de una estación
iguacu-monitor fetch --days 7        # descargar y acumular
iguacu-monitor report                # estado de la cuenca
iguacu-monitor html                  # panel HTML de la cascada
iguacu-monitor html --demo           # el panel con datos de ejemplo, sin red
iguacu-monitor pluviometros          # rearmar la lista de pluviómetros ANA (API nueva)
iguacu-monitor pluviometros --red cemaden   # rearmar la lista de CEMADEN
iguacu-monitor subcuencas            # volver a delimitar las subcuencas (extra [geo])
```

El panel sigue la lógica del tablero operativo: una tarjeta por usina de
aguas arriba hacia aguas abajo, con estado según el caudal **afluente**
(IO-OR.PR.IGU Rev.44: Santiago y Osório atención ≥ 4.000 / alerta ≥ 7.000;
Caxias y Baixo atención ≥ 3.000 / alerta ≥ 5.000 m³/s; Foz do Areia y Segredo
operan por nivel), volumen útil, tendencia por hora y sparkline de 48 h. Los
datos de `--demo` son los del tablero del 23-oct 22:00 con series sintéticas.

Para dejarlo corriendo solo, basta un cron horario con `fetch`:

```cron
17 * * * * cd /ruta/al/repo && iguacu-monitor fetch --days 2 >> logs/fetch.log 2>&1
```

## Subcuencas

La lluvia se resume también por **área incremental de la cascada**: lo que
drena a cada punto de control descontando lo que drena al anterior. Es la
división que sirve para leer la operación, porque la lluvia sobre el área de
una usina le llega como aporte lateral sin pasar por la anterior.

Los puntos de corte son las seis usinas y los dos puntos de control del
tronco aguas abajo (Capanema y Cataratas). União da Vitória no regula y no es
un punto de corte: su cuenca (~24.200 km²) queda dentro del área de Foz do
Areia, aunque la estación sigue en el monitor con su caudal y nivel.

`config/subcuencas_iguacu.geojson` se generó con `iguacu-monitor subcuencas`
a partir de las direcciones de flujo condicionadas de HydroSHEDS v1 (15",
~450 m). Cada punto se ajusta a la celda más cercana del cauce principal
(la que drena al menos la mitad del área oficial) y recién después se compara
el área obtenida con la de ANA, así el ajuste no fuerza la validación:

| punto | drenaje (km²) | ANA (km²) | error | incremental (km²) |
|---|---:|---:|---:|---:|
| Foz do Areia | 30.144 | 30.100 | +0,1 % | 30.144 |
| Segredo | 34.364 | 34.400 | −0,1 % | 4.219 |
| Salto Santiago | 43.903 | 43.900 | +0,0 % | 9.539 |
| Salto Osório | 45.857 | 45.800 | +0,1 % | 1.954 |
| Salto Caxias | 57.095 | 57.100 | −0,0 % | 11.239 |
| Baixo Iguaçu | 61.709 | 61.580 | +0,2 % | 4.613 |
| Porto Capanema | 63.964 | 63.900 | +0,1 % | 2.255 |
| Hotel Cataratas | 67.223 | 67.100 | +0,2 % | 3.259 |

El comando falla si algún punto se aparta más de 2 % y entonces no escribe
la capa.

Primero se intentó con Copernicus GLO-90 y no sirvió: al ser un modelo de
superficie, el dosel de la planicie entre São Mateus do Sul y Fluviópolis y
los espejos de los embalses desvían la red (−10 % a −26 % de área). El código
de ese intento queda en `delineate.py` como referencia.

La capa se puede reemplazar por otra (p. ej. las subcuencas de un modelo HMS,
o una delimitación con MERIT Hydro) con un polígono por subcuenca y
propiedades `id` y `name`.

Por subcuenca se calcula:

- **Lluvia de los pluviómetros** en 6, 24 y 72 h, interpolada por distancia
  inversa a una grilla de 0,05° y promediada por polígono, ponderando por
  área de celda. La columna "pluv." cuenta los pluviómetros con dato dentro
  de cada área; con cero, el valor es una extrapolación de los vecinos.
- **Lámina diaria de MERGE**, promediando las celdas de 0,1° del polígono. La
  media de toda la cuenca usa ahora el contorno real y no la caja envolvente.

## Licencias de los datos

- **HydroSHEDS** (subcuencas): citar Lehner, B., Verdin, K., Jarvis, A.
  (2008), *New global hydrography derived from spaceborne elevation data*,
  Eos 89(10). Rige el acuerdo de licencia incluido en la descarga.
- **MERIT Hydro**, si se usa para regenerar las subcuencas: CC BY-NC 4.0 u
  ODbL 1.0 a elección (el autor confirmó la doble licencia). Para uso
  operativo corresponde ODbL: citar Yamazaki et al. (2019), *MERIT Hydro*,
  WRR 55, y compartir bajo ODbL las capas derivadas que se publiquen. Los
  mosaicos originales no se redistribuyen.
- ANA, ONS e INPE: datos públicos; citar la fuente.

## Fuentes y estado de verificación

Verificadas contra los servicios reales el 25-oct-2026, salvo COPEL.

| Fuente | Qué aporta | Estado |
|---|---|---|
| `ons` | Regulación de las seis usinas: afluente, turbinado, vertido, defluente, nivel y volumen útil | **Verificada.** Base horaria con 1 a 2 h de rezago; usinas por `id_reservatorio`; hora de Brasilia. |
| `ana` | Caudal y nivel en Capanema, Cataratas y União da Vitória | **Verificada.** Servicio legacy sin credenciales; nivel en centímetros, convertido a metros. |
| `inpe_merge` | Lámina diaria media areal | **Verificada.** Acumulado de 12 a 12 UTC; el día en curso aún no publicado se saltea. |
| `cemaden` | Pluviómetros automáticos municipales (146 en la cuenca y su borde) | **Verificada.** JSONP abierto del mapa interactivo (`resources.cemaden.gov.br/dados/311_24.json`); sólo el acumulado móvil de 24 h, sin series. Entra en la ventana de 24 h junto con ANA y la sostiene sola si ANA se cae. |
| `copel_mhb` | Regulación COPEL y estaciones propias | **Desactivada.** La página navega por PrimeFaces: hay que abrir la cuenca, elegir el punto y cargar su diálogo; así devuelve 72 h horarias de cada usina y puesto, más la previsión de SIMEPAR para União da Vitória. Pendiente de convertir en fuente. ONS cubre las mismas usinas. |

Los tiempos de viaje del tablero se confirmaron con correlación cruzada sobre
los datos del 23 y 24-oct: Baixo Iguaçu → Capanema ≈ 4 h (r = 0,98) y
Capanema → Cataratas ≈ 10 h (r = 0,99), contra 4 y 11 h del tablero.

### Qué queda por revisar

1. **Códigos ANA**: Capanema `65987000`, Cataratas `65992500` y União da
   Vitória `65310001` devuelven datos. `65310000` no.

2. **La banda de tendencia estable** (`trend_flat_band: 15` m³/s/h) se
   infirió del tablero (+11/h estable, +16/h sube). Ajustala si tu regla es otra.

3. **COPEL**: implementar el pedido del diálogo con rango de fechas si se
   quiere una segunda fuente para las usinas COPEL.

4. **La cuenca es una caja envolvente**, no el polígono real. Sobreestima la
   lluvia al incluir áreas vecinas. Para afinar, apuntá `basin.polygon` a un
   GeoJSON de la divisoria.

## Decisiones que conviene conocer

**No se usa `pandas.read_html`.** Aplica `thousands=","` y `decimal="."` por
defecto, lo que convierte un nivel de `741,20` m en `74120` y una vazão de
`10.030` m³/s en `10,03` — sin lanzar error. Sobre datos brasileños esa
inferencia corrompe justo lo que hay que leer bien. El módulo `tables` parsea
el HTML a texto puro y `parse_number` resuelve ambos formatos. Hay un test de
regresión que lo cubre.

**El parseo casa por nombre, no por posición.** Las filas se identifican por
la etiqueta del sitio y las columnas por la de la variable. Si el portal
reordena su tabla, sigue funcionando; si cambia un nombre, `probe` lo dice en
vez de devolver vacío en silencio.

**Un pluviómetro seco entre vecinos con lluvia se descarta.** El 24-oct-2026
doce estaciones de ANA marcaron 0 mm con vecinos de 70 a 140 mm: sensores
tapados o estaciones sin pluviómetro que transmiten ceros. Bajaban la lluvia
areal de Foz do Areia de 122 a 110 mm. El control compara cada total con la
mediana de sus vecinos a 50 km (`gauge_dry_*`) y lo informa en el panel.

**Todo se almacena en UTC.** ANA entrega hora de Brasilia (UTC−3) sin
declararlo; la conversión está cubierta por tests.

**El reporte marca incoherencias físicas, con tiempo de viaje.** Cada
estación se compara con el punto anterior de la cascada tal como estaba hace
`travel_h` horas. Comparar valores simultáneos daba falsas alarmas durante el
ascenso de una crecida, cuando la onda todavía no llegó aguas abajo.

## Estructura

```
iguacu_monitor/
  model.py      tipos, variables normalizadas, mapeo de etiquetas
  config.py     carga de config/iguacu.yml
  http.py       cliente con reintentos y espera exponencial
  tables.py     lectura de tablas HTML sin inferencia de tipos
  store.py      Parquet acumulativo, deduplicado por clave natural
  report.py     estado de la cuenca y control de coherencia
  cli.py        probe / discover / fetch / report
  sources/      un adaptador por fuente
```
