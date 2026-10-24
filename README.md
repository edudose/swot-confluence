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

## Fuentes y estado de verificación

Este código se escribió en un entorno **sin acceso de red a ninguna de las
cuatro fuentes** (bloqueadas por política del proxy). La lógica está probada
con 58 tests sobre muestras sintéticas, pero **el contacto real con cada API
no pudo ejercitarse**. Por eso existe `probe`: ejecutarlo es el primer paso.

| Fuente | Qué aporta | Estado |
|---|---|---|
| `copel_mhb` | Regulación de usinas COPEL + estaciones Capanema y Cataratas | **Por validar.** URL confirmada; estructura del DOM no. |
| `ana` | Caudal y nivel telemétricos | **Por validar.** Servicio legacy sin credenciales; esquema XML deducido. |
| `inpe_merge` | Precipitación en grilla 0,1° | **Por validar.** Patrón de URL confirmado por dos fuentes. |
| `ons` | Regulación de toda la cascada (respaldo) | **Por validar.** Descubrimiento vía CKAN, sin URL fija. |

### Qué falta resolver en la primera corrida

1. **Códigos ANA del tronco**: Capanema `65987000` y Cataratas `65992500`,
   tomados del tablero operativo. Una fuente secundaria citaba `65992502`
   para Cataratas, posiblemente el sensor telemétrico de la misma sección:
   si el servicio no devuelve datos con `65992500`, probá con
   `discover "Cataratas"`.

2. **La banda de tendencia estable** (`trend_flat_band: 15` m³/s/h) se
   infirió del tablero (+11/h estable, +16/h sube). Ajustala si tu regla es otra.

3. **Etiquetas `copel_label` y `ons_label`** son conjeturas razonables sobre
   cómo cada portal nombra las usinas. `probe` reporta cuáles no casaron y
   vuelca los encabezados reales para corregirlas.

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

**Todo se almacena en UTC.** ANA entrega hora de Brasilia (UTC−3) sin
declararlo; la conversión está cubierta por tests.

**El reporte marca incoherencias físicas.** Si el caudal en Cataratas cae por
debajo de la defluencia de Salto Caxias, avisa: aguas abajo hay aporte
incremental, así que lo contrario indica dato viejo o error de escala.

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
