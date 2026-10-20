"""Géométrie plane minimale (pyproj seul) : projection, aire, découpage, distance.

Les polygones sont des listes d'anneaux [(x, y), ...] : le premier anneau est
l'extérieur, les suivants des trous. Un MultiPolygon est une liste de polygones.
Les calculs d'aire et de distance se font toujours dans un CRS métrique.
"""
from __future__ import annotations

from math import hypot

from pyproj import Transformer

WGS84 = "EPSG:4326"
LAMBERT93 = "EPSG:2154"
# Grille SAFRAN/SIM : Lambert II étendu (NTF), coordonnées publiées en hm.
LAMBERT2E = "EPSG:27572"


def transformer(source: str, target: str) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


def geojson_polygons(geometry: dict) -> list[list[list[tuple[float, float]]]]:
    if geometry["type"] == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError(f"Géométrie non surfacique : {geometry['type']}")
    if not polygons or any(len(ring) < 4 for polygon in polygons for ring in polygon):
        raise ValueError("Polygone vide ou anneau dégénéré")
    return [[[(float(x), float(y)) for x, y, *_ in ring] for ring in polygon] for polygon in polygons]


def project(polygons, source: str, target: str):
    t = transformer(source, target)
    return [[[t.transform(x, y) for x, y in ring] for ring in polygon] for polygon in polygons]


def ring_area(ring) -> float:
    """Aire signée (lacet)."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])) / 2


def area(polygons) -> float:
    return sum(abs(ring_area(p[0])) - sum(abs(ring_area(h)) for h in p[1:]) for p in polygons)


def centroid(polygons) -> tuple[float, float]:
    """Centroïde surfacique (trous déduits)."""
    total = cx = cy = 0.0
    for polygon in polygons:
        for i, ring in enumerate(polygon):
            signed = ring_area(ring)
            if signed == 0:
                continue
            pairs = list(zip(ring, ring[1:] + ring[:1]))
            rx = sum((x1 + x2) * (x1 * y2 - x2 * y1) for (x1, y1), (x2, y2) in pairs) / (6 * signed)
            ry = sum((y1 + y2) * (x1 * y2 - x2 * y1) for (x1, y1), (x2, y2) in pairs) / (6 * signed)
            weight = abs(signed) * (1 if i == 0 else -1)  # un trou retranche son aire
            cx, cy, total = cx + rx * weight, cy + ry * weight, total + weight
    if total <= 0:
        raise ValueError("Aire nulle")
    return cx / total, cy / total


def bounds(polygons) -> tuple[float, float, float, float]:
    xs = [x for p in polygons for ring in p for x, _ in ring]
    ys = [y for p in polygons for ring in p for _, y in ring]
    return min(xs), min(ys), max(xs), max(ys)


def clip_ring(ring, box) -> list[tuple[float, float]]:
    """Sutherland–Hodgman contre un rectangle (convexe) : l'aire du résultat
    est exacte même pour un anneau concave (arêtes dégénérées possibles)."""
    xmin, ymin, xmax, ymax = box
    edges = [(lambda p: p[0] >= xmin, lambda a, b: _cut_x(a, b, xmin)),
             (lambda p: p[0] <= xmax, lambda a, b: _cut_x(a, b, xmax)),
             (lambda p: p[1] >= ymin, lambda a, b: _cut_y(a, b, ymin)),
             (lambda p: p[1] <= ymax, lambda a, b: _cut_y(a, b, ymax))]
    points = list(ring[:-1] if ring[0] == ring[-1] else ring)
    for inside, cut in edges:
        if not points:
            break
        output = []
        for i, current in enumerate(points):
            previous = points[i - 1]
            if inside(current):
                if not inside(previous):
                    output.append(cut(previous, current))
                output.append(current)
            elif inside(previous):
                output.append(cut(previous, current))
        points = output
    return points


def _cut_x(a, b, x):
    t = (x - a[0]) / (b[0] - a[0])
    return x, a[1] + t * (b[1] - a[1])


def _cut_y(a, b, y):
    t = (y - a[1]) / (b[1] - a[1])
    return a[0] + t * (b[0] - a[0]), y


def intersection_area(polygons, box) -> float:
    total = 0.0
    for polygon in polygons:
        for i, ring in enumerate(polygon):
            clipped = clip_ring(ring, box)
            if len(clipped) >= 3:
                total += abs(ring_area(clipped)) * (1 if i == 0 else -1)
    return max(total, 0.0)


def _segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == dy == 0:
        return hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def box_distance(polygons, box) -> float:
    """Distance minimale entre les contours extérieurs et un rectangle
    (0 si intersection). Exacte pour deux polygones disjoints : le minimum
    est atteint en un sommet de l'un face à une arête de l'autre."""
    if intersection_area(polygons, box) > 0:
        return 0.0
    xmin, ymin, xmax, ymax = box
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    box_edges = list(zip(corners, corners[1:] + corners[:1]))
    best = float("inf")
    for polygon in polygons:
        ring = polygon[0]
        for p in ring:
            best = min(best, *(_segment_distance(p, a, b) for a, b in box_edges))
        for a, b in zip(ring, ring[1:]):
            best = min(best, *(_segment_distance(c, a, b) for c in corners))
    return best


def to_geojson(polygons) -> dict:
    return {"type": "MultiPolygon", "coordinates": [[[list(pt) for pt in ring] for ring in p] for p in polygons]}
