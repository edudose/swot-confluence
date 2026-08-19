# iphone_depth — profundidad de fotos Retrato de iPhone

Toolkit para detectar, extraer y procesar el **mapa de profundidad** que el
iPhone guarda dentro de las fotos en modo Retrato, y convertirlo en una
**nube de puntos PLY**.

## Lo primero: qué guarda realmente una foto Retrato

- Una foto Retrato **no contiene la nube de puntos LiDAR cruda**. El iPhone
  fusiona LiDAR/TrueDepth + estéreo + ML y guarda un **mapa de disparidad**
  (imagen auxiliar, típicamente de baja resolución) dentro del contenedor
  HEIC, junto con *mattes* de segmentación (persona, pelo, piel, cielo).
- **Cámara frontal (selfies)** = sensor **TrueDepth** (luz estructurada
  infrarroja). El **LiDAR** está atrás, solo en los modelos Pro, y asiste al
  modo Retrato con la cámara trasera.
- Para capturar nubes de puntos LiDAR de verdad hace falta una app de
  escaneo (Polycam, 3D Scanner App, Record3D o una app propia con ARKit).

## Por qué "mi foto no tiene profundidad"

La profundidad viaja como datos auxiliares del HEIC y **casi cualquier
intermediario la elimina**: WhatsApp/Telegram, subir la foto a un chat web,
mandarla por mail en tamaño reducido, editarla, o exportarla como
`FullSizeRender.jpg`. También suelen depurar el EXIF.

**Para que la profundidad sobreviva:**

1. En el iPhone: Ajustes → Cámara → Formatos → **Alta eficiencia** (HEIC).
2. Conseguí el **original sin modificar**: AirDrop a una Mac ("todos los
   datos de la foto"), app Archivos, o `Importar` en una computadora.
3. Antes de subirlo a un chat o web, **meté el `.HEIC` dentro de un `.zip`**:
   un archivo comprimido no se re-codifica en el camino.
4. Alternativa: subí el `.HEIC` tal cual a este repositorio (GitHub no
   transcodifica archivos).

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
# 1. ¿Este archivo trae mapa de profundidad? (exit 0 = sí, 2 = no)
python -m iphone_depth diagnose IMG_1234.heic

# 2. Extraer disparidad (PNG 16-bit + .npy + visualización), mattes y metadatos
python -m iphone_depth extract IMG_1234.heic --out salida/

# 3. Convertir a nube de puntos PLY coloreada (abrir con MeshLab/CloudCompare)
python -m iphone_depth pointcloud \
    --depth salida/IMG_1234_disparity.npy \
    --photo IMG_1234.heic \
    --f35 26 \
    --out nube.ply
```

`--f35` es la focal equivalente a 35 mm del EXIF (`FocalLengthIn35mmFilm`);
`diagnose` la muestra cuando está disponible.

### Escala métrica

El mapa extraído es **disparidad normalizada** (0–1). Si el contenedor trae
metadatos métricos (`d_min`/`d_max` o `z_near`/`z_far`, los reporta
`extract` en `*_meta.json`), pasalos a `pointcloud` con `--dmin/--dmax`
(en 1/metros) para obtener una nube en **metros**. Sin ellos, la nube es
geométricamente correcta pero de escala relativa.

## Qué soporta

| Contenedor | Dónde vive la profundidad | Estado |
|---|---|---|
| HEIC | Imagen de profundidad HEIF + auxiliares por URN (`...aux:disparity`, mattes, gain map) | ✅ vía libheif/pillow-heif |
| JPEG | Imágenes embebidas indexadas por segmento APP2 **MPF** (CIPA DC-007) | ✅ parser propio + fallback de escaneo |
| JPEG "plano" (`FullSizeRender`, WhatsApp, etc.) | — | detecta y explica que no hay nada |

## Tests

```bash
python tests/test_selftest.py   # o: pytest tests/
```

Incluye un roundtrip con un JPEG+MPF construido a mano y verificación del
pipeline sintético disparidad → profundidad → PLY.

## Estructura

```
iphone_depth/
  __main__.py      CLI: diagnose | extract | pointcloud
  jpeg_segments.py Segmentos JPEG, parser MPF, imágenes embebidas
  heic.py          Auxiliares/profundidad HEIF (pillow-heif)
  exifdump.py      Resumen EXIF y clasificación frontal/trasera
  depthmap.py      Normalización, disparidad→profundidad, export
  pointcloud.py    Retroproyección pinhole → PLY binario
  colormap.py      Rampa de color para visualizar profundidad
```
