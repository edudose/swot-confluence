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

Verificadas contra los servicios reales el 25-oct-2026, salvo COPEL.

| Fuente | Qué aporta | Estado |
|---|---|---|
| `ons` | Regulación de las seis usinas: afluente, turbinado, vertido, defluente, nivel y volumen útil | **Verificada.** Base horaria con 1 a 2 h de rezago; usinas por `id_reservatorio`; hora de Brasilia. |
| `ana` | Caudal y nivel en Capanema, Cataratas y União da Vitória | **Verificada.** Servicio legacy sin credenciales; nivel en centímetros, convertido a metros. |
| `inpe_merge` | Lámina diaria media areal | **Verificada.** Acumulado de 12 a 12 UTC; el día en curso aún no publicado se saltea. |
| `copel_mhb` | Regulación COPEL y estaciones propias | **Desactivada.** Cada punto se carga en un diálogo JSF dinámico que exige un rango de fechas; pendiente de implementar. ONS cubre las mismas usinas. |

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
