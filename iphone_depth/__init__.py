"""Toolkit para extraer y procesar mapas de profundidad de fotos de iPhone.

Las fotos en modo Retrato de iPhone guardan la profundidad como imagen
auxiliar dentro del contenedor HEIC (o como imagen embebida MPF en JPEG),
no como nube de puntos LiDAR cruda. Este paquete la detecta, la extrae y
permite convertirla en una nube de puntos PLY.
"""

__version__ = "0.1.0"
