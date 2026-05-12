"""
modules/floor_plan.py
=====================
Floor-plan data model, geometry helpers, and projection back to the legacy
per-room ``dimensions`` blocks.

Pure module: no I/O, no FastAPI, no global state. Wired in later by
``routes/floor_plan_routes.py``.

The floor plan is an *editor surface*. The source of truth for circuits/rooms
remains ``heating.circuits`` in ``config.yaml``. On save, this module projects
the plan back into each existing room's ``dimensions`` / ``radiator`` /
``trvs`` / ``contact_sensors`` / ``temperature_sensor_ieee`` so that
``thermal_profile.py`` and ``heating_controller.py`` keep working unchanged.

Where the floor plan is richer than the legacy schema (multiple radiators per
room, multiple temperature sensors, contacts bound to specific openings), the
projection emits the *legacy single* fields AND the new plural fields:

    room["radiator"]               # legacy: largest-watts radiator in the room
    room["radiators"]              # NEW   : full list with TRV bindings
    room["temperature_sensor_ieee"]# legacy: primary sensor
    room["temperature_sensors"]    # NEW   : full list with heights
    room["contact_sensors"][i].opening_id   # NEW   : opening linkage

Coordinate convention
    +x = right, +y = up (standard maths).  ``north_offset_deg`` is the
    clockwise angle from plan-up to true-north (so 0 means plan-up = north,
    90 means true-north points right of the plan).

Compass / wall-bin convention
    A wall's outward-normal bearing relative to true-north decides:
      - its 8-point compass label (N/NE/.../NW)  -> opening orientation
      - its legacy 4-bin label                   -> dimensions.walls bin:
            back   = N-facing  (-45 .. +45)
            right  = E-facing  ( 45 .. 135)
            front  = S-facing  (135 .. 225)
            left   = W-facing  (225 .. 315)

The 4-bin labels are arbitrary; thermal_profile.py only cares about the
external/party/internal type stored against each bin.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("modules.floor_plan")

# ─────────────────────────── enums / constants ───────────────────────────

SCHEMA_VERSION = 1

VALID_WALL_TYPES = ("external", "party", "internal", "unknown")
VALID_OPENING_KINDS = ("window", "door")
VALID_GLAZING = ("single", "double", "triple")
VALID_DOOR_TYPES = ("external", "internal")
VALID_SENSOR_KINDS = ("thermostat", "temp_sensor", "room_stat")
VALID_RADIATOR_TYPES = (
    "single_panel",
    "double_panel_single_conv",
    "double_panel_double_conv",
    "triple_panel",
    "column",
    "towel_rail",
    "underfloor",
)
VALID_RADIATOR_PLACEMENT = ("under_window", "external_wall", "internal_wall")
VALID_FLOOR_TYPES = (
    "solid", "suspended",
    "carpet_over_concrete", "tile_over_concrete",
    "wooden", "carpet_over_wooden", "unknown",
)
VALID_CEILING_TYPES = ("insulated", "uninsulated", "flat_roof", "unknown")

LEGACY_WALL_BINS = ("front", "back", "left", "right")

DEFAULT_WINDOW_HEIGHT_M = 1.20
DEFAULT_DOOR_HEIGHT_M = 2.00
DEFAULT_CEILING_HEIGHT_M = 2.40
DEFAULT_NORTH_OFFSET_DEG = 0.0

# Wall-type precedence when multiple polygon walls fold into one legacy bin.
_WALL_TYPE_PRECEDENCE = {"external": 3, "party": 2, "internal": 1, "unknown": 0}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


# ─────────────────────────── coercion helpers ────────────────────────────

def _as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    f = _as_float(v, None)
    if f is None:
        return default
    try:
        return int(f)
    except (TypeError, ValueError):
        return default


def _as_bool(v: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
    return default


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "id"


def _valid_id(s: Any) -> Optional[str]:
    if not isinstance(s, str):
        return None
    s = s.strip().lower()
    return s if _ID_RE.match(s) else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─────────────────────────── geometry helpers ────────────────────────────

def polygon_area_m2(points: List[Tuple[float, float]]) -> float:
    """Signed shoelace area; absolute value returned. Empty/invalid → 0."""
    if not points or len(points) < 3:
        return 0.0
    s = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        s += (x1 * y2) - (x2 * y1)
    return abs(s) * 0.5


def polygon_bbox(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy). Empty → all zeros."""
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def polygon_centroid(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Geometric centroid (uses signed shoelace; falls back to bbox centre)."""
    if not points:
        return (0.0, 0.0)
    if len(points) < 3:
        # Fallback: average
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))
    cx = cy = 0.0
    a = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a *= 0.5
    if abs(a) < 1e-9:
        # Degenerate — fall back to bbox centre
        minx, miny, maxx, maxy = polygon_bbox(points)
        return ((minx + maxx) * 0.5, (miny + maxy) * 0.5)
    cx /= (6.0 * a)
    cy /= (6.0 * a)
    return (cx, cy)


def segment_length_m(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)


def _outward_normal_unit(
        x1: float, y1: float, x2: float, y2: float,
        cx: float, cy: float,
) -> Tuple[float, float]:
    """Unit normal pointing AWAY from (cx, cy). Tie → +x."""
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return (1.0, 0.0)
    # Two normals: (-dy, dx) and (dy, -dx)
    n1 = (-dy / L, dx / L)
    n2 = (dy / L, -dx / L)
    # Midpoint of segment
    mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
    # Pick the one whose midpoint+normal goes further from centroid
    d1 = (mx + n1[0] - cx) ** 2 + (my + n1[1] - cy) ** 2
    d2 = (mx + n2[0] - cx) ** 2 + (my + n2[1] - cy) ** 2
    return n1 if d1 >= d2 else n2


def normal_to_compass_bearing_deg(
        nx: float, ny: float, north_offset_deg: float = 0.0,
) -> float:
    """
    Compass bearing of a plan-space vector relative to TRUE north, in degrees.

    Plan coords: +x right, +y up.
    ``north_offset_deg``: clockwise angle from plan-up to true-north.
    Result is in [0, 360) where 0=N, 90=E, 180=S, 270=W.
    """
    # atan2(x, y) gives angle CW from +y axis (i.e. from plan-up).
    a_plan = math.degrees(math.atan2(nx, ny))
    bearing = (a_plan - north_offset_deg) % 360.0
    return bearing


def bearing_to_compass8(bearing_deg: float) -> str:
    """One of N/NE/E/SE/S/SW/W/NW. 22.5° bins centred on each cardinal."""
    b = bearing_deg % 360.0
    idx = int(((b + 22.5) % 360.0) // 45.0)
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[idx]


def bearing_to_legacy_wall_bin(bearing_deg: float) -> str:
    """
    Map a compass bearing to one of front/back/left/right.

    Convention:
        back  = N-facing  (-45 .. +45)
        right = E-facing  ( 45 .. 135)
        front = S-facing  (135 .. 225)
        left  = W-facing  (225 .. 315)
    """
    b = bearing_deg % 360.0
    if b < 45.0 or b >= 315.0:
        return "back"
    if b < 135.0:
        return "right"
    if b < 225.0:
        return "front"
    return "left"


# ─────────────────────────── topology helpers ────────────────────────────

def _wall_xy_endpoints(wall: dict) -> Tuple[float, float, float, float]:
    return (
        float(wall.get("x1", 0.0)),
        float(wall.get("y1", 0.0)),
        float(wall.get("x2", 0.0)),
        float(wall.get("y2", 0.0)),
    )


def _segment_overlaps_polygon_edge(
        sx1: float, sy1: float, sx2: float, sy2: float,
        polygon: List[Tuple[float, float]],
        tol: float = 0.05,  # metres
) -> bool:
    """
    True if the segment (sx1,sy1)-(sx2,sy2) is collinear with any edge of the
    polygon and overlaps it (length > tol). Cheap O(N) check.
    """
    if len(polygon) < 3:
        return False
    sdx, sdy = sx2 - sx1, sy2 - sy1
    sL = math.hypot(sdx, sdy)
    if sL < tol:
        return False

    for i in range(len(polygon)):
        ex1, ey1 = polygon[i]
        ex2, ey2 = polygon[(i + 1) % len(polygon)]
        edx, edy = ex2 - ex1, ey2 - ey1
        eL = math.hypot(edx, edy)
        if eL < tol:
            continue

        # Parallelism (cross product near zero relative to lengths)
        cross = sdx * edy - sdy * edx
        if abs(cross) > tol * max(sL, eL):
            continue

        # Collinearity: check that one endpoint of the segment lies on the
        # infinite line of the edge (perpendicular distance < tol)
        # Distance from (sx1,sy1) to edge line:
        #   |(sx1-ex1)*edy - (sy1-ey1)*edx| / eL
        perp = abs((sx1 - ex1) * edy - (sy1 - ey1) * edx) / eL
        if perp > tol:
            continue

        # Overlap: project both segments onto the edge axis (unit edx, edy / eL)
        ux, uy = edx / eL, edy / eL
        t_s1 = (sx1 - ex1) * ux + (sy1 - ey1) * uy
        t_s2 = (sx2 - ex1) * ux + (sy2 - ey1) * uy
        lo = max(min(t_s1, t_s2), 0.0)
        hi = min(max(t_s1, t_s2), eL)
        if hi - lo > tol:
            return True
    return False


def find_walls_for_room(level: dict, room: dict) -> List[dict]:
    """All walls whose segment lies on (or is collinear with) a polygon edge."""
    poly = [tuple(p) for p in (room.get("polygon") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not poly:
        return []
    out = []
    for w in level.get("walls", []) or []:
        x1, y1, x2, y2 = _wall_xy_endpoints(w)
        if _segment_overlaps_polygon_edge(x1, y1, x2, y2, poly):
            out.append(w)
    return out


def shared_edge_room_count(level: dict, wall: dict) -> int:
    """Number of rooms whose polygon shares an edge with this wall (0, 1, or 2)."""
    x1, y1, x2, y2 = _wall_xy_endpoints(wall)
    n = 0
    for r in level.get("rooms", []) or []:
        poly = [tuple(p) for p in (r.get("polygon") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        if poly and _segment_overlaps_polygon_edge(x1, y1, x2, y2, poly):
            n += 1
            if n >= 2:
                break
    return n


def infer_wall_type(level: dict, wall: dict, explicit: Optional[str]) -> str:
    """
    If user set an explicit type, respect it. Otherwise infer:
      shared by 0 rooms -> 'unknown' (orphan wall)
      shared by 1 room  -> 'external'
      shared by 2 rooms -> 'party' (heated neighbour)
    """
    if explicit and explicit in VALID_WALL_TYPES:
        return explicit
    n = shared_edge_room_count(level, wall)
    if n >= 2:
        return "party"
    if n == 1:
        return "external"
    return "unknown"


# ────────────────────────────── cleaners ─────────────────────────────────

def _clean_wall(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    wid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or "")
    if not wid:
        return None
    x1 = _as_float(raw.get("x1"))
    y1 = _as_float(raw.get("y1"))
    x2 = _as_float(raw.get("x2"))
    y2 = _as_float(raw.get("y2"))
    if None in (x1, y1, x2, y2):
        return None
    if math.hypot(x2 - x1, y2 - y1) < 0.05:
        return None  # degenerate
    typ = str(raw.get("type") or "").lower()
    if typ not in VALID_WALL_TYPES:
        typ = "unknown"
    return {
        "id": wid,
        "x1": round(x1, 3), "y1": round(y1, 3),
        "x2": round(x2, 3), "y2": round(y2, 3),
        "type": typ,
    }


def _clean_opening(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    oid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or "")
    wall_id = _valid_id(raw.get("wall_id"))
    if not oid or not wall_id:
        return None
    kind = str(raw.get("kind") or "").lower()
    if kind not in VALID_OPENING_KINDS:
        return None
    width_m = _as_float(raw.get("width_m"))
    if not width_m or width_m <= 0:
        return None
    if kind == "window":
        height_m = _as_float(raw.get("height_m"), DEFAULT_WINDOW_HEIGHT_M) or DEFAULT_WINDOW_HEIGHT_M
    else:
        height_m = _as_float(raw.get("height_m"), DEFAULT_DOOR_HEIGHT_M) or DEFAULT_DOOR_HEIGHT_M
    out: Dict[str, Any] = {
        "id": oid,
        "wall_id": wall_id,
        "kind": kind,
        "offset_m": round(_clamp(_as_float(raw.get("offset_m"), 0.0) or 0.0, 0.0, 1000.0), 3),
        "width_m": round(_clamp(width_m, 0.05, 1000.0), 3),
        "height_m": round(_clamp(height_m, 0.05, 1000.0), 3),
    }
    sill = _as_float(raw.get("sill_height_m"))
    if sill is not None and sill >= 0:
        out["sill_height_m"] = round(_clamp(sill, 0.0, 1000.0), 3)
    rid = _valid_id(raw.get("room_id"))
    if rid:
        out["room_id"] = rid
    if kind == "window":
        glz = str(raw.get("glazing") or "double").lower()
        if glz not in VALID_GLAZING:
            glz = "double"
        out["glazing"] = glz
    else:
        dt = str(raw.get("door_type") or "internal").lower()
        if dt not in VALID_DOOR_TYPES:
            dt = "internal"
        out["door_type"] = dt
    return out


def _clean_room_polygon(raw: Any) -> List[List[float]]:
    if not isinstance(raw, list):
        return []
    pts: List[List[float]] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        x = _as_float(p[0])
        y = _as_float(p[1])
        if x is None or y is None:
            continue
        pts.append([round(x, 3), round(y, 3)])
    if len(pts) < 3:
        return []
    return pts


def _clean_room(raw: Any, existing_ids: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    rid = _valid_id(raw.get("id")) or _slugify(name)
    if not rid:
        return None
    base = rid; n = 2
    while rid in existing_ids:
        rid = f"{base}_{n}"; n += 1
    existing_ids.add(rid)

    poly = _clean_room_polygon(raw.get("polygon"))
    if not poly:
        return None

    out: Dict[str, Any] = {
        "id": rid,
        "name": name or rid,
        "polygon": poly,
    }
    cid = _valid_id(raw.get("circuit_id"))
    if cid:
        out["circuit_id"] = cid
    ft = str(raw.get("floor_type") or "").lower()
    if ft in VALID_FLOOR_TYPES:
        out["floor_type"] = ft
    ct = str(raw.get("ceiling_type") or "").lower()
    if ct in VALID_CEILING_TYPES:
        out["ceiling_type"] = ct
    return out


def _clean_radiator(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    rid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or "")
    room_id = _valid_id(raw.get("room_id"))
    if not rid or not room_id:
        return None
    watts = _as_float(raw.get("watts_at_dt50"))
    btu = _as_float(raw.get("btu_hr_at_dt50"))
    if (not watts or watts <= 0) and btu and btu > 0:
        watts = btu * 0.2931
    if not watts or watts <= 0:
        return None
    out: Dict[str, Any] = {
        "id": rid,
        "room_id": room_id,
        "watts_at_dt50": round(watts, 0),
    }
    x = _as_float(raw.get("x")); y = _as_float(raw.get("y"))
    if x is not None and y is not None:
        out["x"] = round(x, 3); out["y"] = round(y, 3)
    length_m = _as_float(raw.get("length_m"))
    if length_m and length_m > 0:
        out["length_m"] = round(_clamp(length_m, 0.1, 10.0), 3)
    height_m = _as_float(raw.get("height_m"))
    if height_m and height_m > 0:
        out["height_m"] = round(_clamp(height_m, 0.05, 3.0), 3)
    flow_c = _as_float(raw.get("flow_temperature_c"))
    if flow_c and 30 <= flow_c <= 90:
        out["flow_temperature_c"] = round(flow_c, 1)
    desc = raw.get("description")
    if desc:
        out["description"] = str(desc)[:100]
    wall_id = _valid_id(raw.get("wall_id"))
    if wall_id:
        out["wall_id"] = wall_id
    # Wall-mounted offset from wall start (metres along the wall direction).
    # Always >= 0; the level-cleaner will additionally clamp it to wall length.
    offset_m = _as_float(raw.get("offset_m"))
    if offset_m is not None and offset_m >= 0:
        out["offset_m"] = round(_clamp(offset_m, 0.0, 1000.0), 3)
    placement = str(raw.get("placement") or "").lower()
    if placement in VALID_RADIATOR_PLACEMENT:
        out["placement"] = placement
    rtype = str(raw.get("type") or "").lower()
    if rtype in VALID_RADIATOR_TYPES:
        out["type"] = rtype
    refl = _as_bool(raw.get("reflective_panel"), None)
    if refl is not None:
        out["reflective_panel"] = refl
    trv = raw.get("trv_ieee")
    if isinstance(trv, str) and trv.strip():
        out["trv_ieee"] = trv.strip().lower()
    return out


def _clean_sensor(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    sid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or "")
    room_id = _valid_id(raw.get("room_id"))
    ieee = raw.get("ieee")
    if not sid or not room_id or not isinstance(ieee, str) or not ieee.strip():
        return None
    kind = str(raw.get("kind") or "temp_sensor").lower()
    if kind not in VALID_SENSOR_KINDS:
        kind = "temp_sensor"
    out: Dict[str, Any] = {
        "id": sid,
        "room_id": room_id,
        "ieee": ieee.strip().lower(),
        "kind": kind,
    }
    x = _as_float(raw.get("x")); y = _as_float(raw.get("y"))
    if x is not None and y is not None:
        out["x"] = round(x, 3); out["y"] = round(y, 3)
    h = _as_float(raw.get("height_m"))
    if h is not None and h >= 0:
        out["height_m"] = round(_clamp(h, 0.0, 10.0), 2)
    primary = _as_bool(raw.get("primary"), None)
    if primary is not None:
        out["primary"] = primary
    return out


def _clean_contact(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    cid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or "")
    opening_id = _valid_id(raw.get("opening_id"))
    ieee = raw.get("ieee")
    if not cid or not opening_id or not isinstance(ieee, str) or not ieee.strip():
        return None
    out: Dict[str, Any] = {
        "id": cid,
        "opening_id": opening_id,
        "ieee": ieee.strip().lower(),
        "debounce_open_seconds": int(_clamp(_as_float(raw.get("debounce_open_seconds"), 30) or 30, 0, 3600)),
        "require_temp_drop_c": round(_clamp(_as_float(raw.get("require_temp_drop_c"), 0.5) or 0.5, 0.0, 10.0), 2),
        "max_close_minutes": int(_clamp(_as_float(raw.get("max_close_minutes"), 60) or 60, 1, 1440)),
        "enabled": bool(_as_bool(raw.get("enabled"), True)),
    }
    name = raw.get("name")
    if name:
        out["name"] = str(name)[:80]
    return out


def _clean_plan_circuit(raw: Any, existing_ids: set) -> Optional[dict]:
    """
    Clean a plan-level circuit definition.

    These are stored in ``floor_plan.circuits`` and define which boiler
    receiver fires for rooms assigned to this circuit.  They are purely
    descriptive at the plan level; the actual controller circuit block is
    derived from them during projection.
    """
    if not isinstance(raw, dict):
        return None
    cid = _valid_id(raw.get("id")) or _slugify(raw.get("name") or raw.get("id") or "")
    if not cid:
        return None
    base = cid; n = 2
    while cid in existing_ids:
        cid = f"{base}_{n}"; n += 1
    existing_ids.add(cid)

    out: Dict[str, Any] = {
        "id": cid,
        "name": str(raw.get("name") or cid).strip()[:80],
    }
    ieee = raw.get("receiver_ieee")
    if isinstance(ieee, str) and ieee.strip():
        out["receiver_ieee"] = ieee.strip().lower()
    cmd = str(raw.get("receiver_command") or "thermostat").lower()
    if cmd in ("thermostat", "switch", "on_off"):
        out["receiver_command"] = cmd
    else:
        out["receiver_command"] = "thermostat"
    ep = raw.get("receiver_endpoint")
    if ep is not None:
        out["receiver_endpoint"] = ep
    return out


def _clean_level(raw: Any, existing_level_ids: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    lid = _valid_id(raw.get("id")) or _slugify(raw.get("id") or raw.get("name") or "")
    if not lid:
        return None
    base = lid; n = 2
    while lid in existing_level_ids:
        lid = f"{base}_{n}"; n += 1
    existing_level_ids.add(lid)

    out: Dict[str, Any] = {
        "id": lid,
        "name": str(raw.get("name") or lid).strip(),
        "index": int(_as_int(raw.get("index"), 0) or 0),
        "ceiling_height_m": round(_clamp(
            _as_float(raw.get("ceiling_height_m"), DEFAULT_CEILING_HEIGHT_M) or DEFAULT_CEILING_HEIGHT_M,
            1.5, 5.0,
            ), 2),
        "floor_above_ground_m": round(_clamp(
            _as_float(raw.get("floor_above_ground_m"), 0.0) or 0.0,
            -10.0, 100.0,
            ), 2),
    }

    # Optional background image metadata. The actual image bytes live at
    # /api/heating/floor-plan/image/{level_id}; this block stores the
    # calibration result (pixels-per-metre + origin) and the image dimensions
    # so the editor can render the SVG <image> at the correct world scale.
    bg = raw.get("background")
    if isinstance(bg, dict) and _as_bool(bg.get("present"), False):
        ppm = _as_float(bg.get("pixels_per_metre"))
        iw = _as_float(bg.get("image_width_px"))
        ih = _as_float(bg.get("image_height_px"))
        if iw and iw > 0 and ih and ih > 0:
            if not ppm or ppm <= 0:
                # Editor's import-time default; user will need to re-run Calibrate.
                ppm = 50.0
                logger.warning(
                    "level %r: background pixels_per_metre missing/invalid; "
                    "defaulting to 50 px/m — please recalibrate in the editor",
                    lid,
                )
            out["background"] = {
                "present": True,
                "pixels_per_metre": round(_clamp(ppm, 1.0, 10000.0), 3),
                "image_width_px": int(iw),
                "image_height_px": int(ih),
                "origin_x_m": round(_as_float(bg.get("origin_x_m"), 0.0) or 0.0, 3),
                "origin_y_m": round(_as_float(bg.get("origin_y_m"), 0.0) or 0.0, 3),
                "rotation_deg": round(((_as_float(bg.get("rotation_deg"), 0.0) or 0.0) % 360.0), 2),
                "opacity": round(_clamp(_as_float(bg.get("opacity"), 0.5) or 0.5, 0.05, 1.0), 2),
                "content_type": str(bg.get("content_type") or "image/png"),
            }
        else:
            logger.warning(
                "level %r: background marked present but image dimensions "
                "missing — block dropped",
                lid,
            )

    # Rooms first (we need ids to validate everything else)
    room_ids: set = set()
    rooms_clean = []
    for r in raw.get("rooms") or []:
        cr = _clean_room(r, room_ids)
        if cr:
            rooms_clean.append(cr)
    out["rooms"] = rooms_clean
    valid_room_ids = {r["id"] for r in rooms_clean}

    # Walls
    wall_ids: set = set()
    walls_clean = []
    for w in raw.get("walls") or []:
        cw = _clean_wall(w)
        if not cw:
            continue
        if cw["id"] in wall_ids:
            base = cw["id"]; k = 2
            while f"{base}_{k}" in wall_ids:
                k += 1
            cw["id"] = f"{base}_{k}"
        wall_ids.add(cw["id"])
        walls_clean.append(cw)
    out["walls"] = walls_clean
    valid_wall_ids = wall_ids

    # Openings — must reference a real wall
    opening_ids: set = set()
    openings_clean = []
    for o in raw.get("openings") or []:
        co = _clean_opening(o)
        if not co:
            continue
        if co["wall_id"] not in valid_wall_ids:
            continue
        if co.get("room_id") and co["room_id"] not in valid_room_ids:
            co.pop("room_id", None)
        if co["id"] in opening_ids:
            base = co["id"]; k = 2
            while f"{base}_{k}" in opening_ids:
                k += 1
            co["id"] = f"{base}_{k}"
        opening_ids.add(co["id"])
        openings_clean.append(co)
    out["openings"] = openings_clean
    valid_opening_ids = opening_ids

    # Radiators. Two modes coexist:
    #   wall-mounted: needs wall_id + offset_m; offset_m clamped to wall length
    #   freestanding: needs x + y
    # Determine effective mode here (after walls are cleaned), and strip
    # fields that don't belong to the chosen mode so the YAML stays tidy.
    walls_by_id = {w["id"]: w for w in walls_clean}
    rad_ids: set = set()
    rads_clean = []
    for r in raw.get("radiators") or []:
        cr = _clean_radiator(r)
        if not cr:
            continue
        if cr["room_id"] not in valid_room_ids:
            continue
        # Resolve wall reference and clamp offset to actual wall length.
        wid = cr.get("wall_id")
        wall = walls_by_id.get(wid) if wid else None
        if wid and not wall:
            # wall_id refers to a wall we don't have — treat as freestanding
            cr.pop("wall_id", None)
            cr.pop("offset_m", None)
        if wall:
            wlen = math.hypot(wall["x2"] - wall["x1"], wall["y2"] - wall["y1"])
            # Default offset = midpoint of wall if not supplied
            offset = cr.get("offset_m")
            if offset is None:
                offset = wlen / 2.0
            # Clamp so the radiator fits on the wall. We don't yet know the
            # length_m here in all cases (defaulted in JS), so the basic
            # constraint is just "starts inside the wall".
            cr["offset_m"] = round(_clamp(offset, 0.0, max(0.0, wlen)), 3)
            # Wall-mounted: drop stale free-coords
            cr.pop("x", None); cr.pop("y", None)
        else:
            # Freestanding: drop wall-only fields
            cr.pop("offset_m", None)
        if cr["id"] in rad_ids:
            base = cr["id"]; k = 2
            while f"{base}_{k}" in rad_ids:
                k += 1
            cr["id"] = f"{base}_{k}"
        rad_ids.add(cr["id"])
        rads_clean.append(cr)
    out["radiators"] = rads_clean

    # Sensors
    sens_ids: set = set()
    sens_clean = []
    for s in raw.get("sensors") or []:
        cs = _clean_sensor(s)
        if not cs:
            continue
        if cs["room_id"] not in valid_room_ids:
            continue
        if cs["id"] in sens_ids:
            base = cs["id"]; k = 2
            while f"{base}_{k}" in sens_ids:
                k += 1
            cs["id"] = f"{base}_{k}"
        sens_ids.add(cs["id"])
        sens_clean.append(cs)
    out["sensors"] = sens_clean

    # Contacts (require a valid opening_id)
    con_ids: set = set()
    cons_clean = []
    for c in raw.get("contacts") or []:
        cc = _clean_contact(c)
        if not cc:
            continue
        if cc["opening_id"] not in valid_opening_ids:
            continue
        if cc["id"] in con_ids:
            base = cc["id"]; k = 2
            while f"{base}_{k}" in con_ids:
                k += 1
            cc["id"] = f"{base}_{k}"
        con_ids.add(cc["id"])
        cons_clean.append(cc)
    out["contacts"] = cons_clean

    return out


def clean_floor_plan(raw: Any) -> Optional[dict]:
    """
    Top-level cleaner for the entire floor plan block.
    Returns ``None`` if the input is empty / fundamentally invalid.
    """
    if not isinstance(raw, dict):
        return None

    north = _as_float(raw.get("north_offset_deg"), DEFAULT_NORTH_OFFSET_DEG) or DEFAULT_NORTH_OFFSET_DEG
    north = north % 360.0

    scale_pxm = _as_float(raw.get("scale_pixels_per_metre"), 50.0) or 50.0
    scale_pxm = _clamp(scale_pxm, 5.0, 500.0)

    levels_raw = raw.get("levels") or []
    if not isinstance(levels_raw, list):
        levels_raw = []
    seen_level_ids: set = set()
    levels: List[dict] = []
    for lvl in levels_raw:
        cl = _clean_level(lvl, seen_level_ids)
        if cl:
            levels.append(cl)
    levels.sort(key=lambda l: l["index"])

    if not levels:
        return None

    # Plan-level circuit definitions (optional).  These let the floor plan
    # editor be the single place where circuits are configured when
    # config_mode = floor_plan.
    circuits_raw = raw.get("circuits") or []
    seen_circuit_ids: set = set()
    plan_circuits: List[dict] = []
    for c in circuits_raw:
        cc = _clean_plan_circuit(c, seen_circuit_ids)
        if cc:
            plan_circuits.append(cc)

    result: Dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "north_offset_deg": round(north, 2),
        "scale_pixels_per_metre": round(scale_pxm, 2),
        "levels": levels,
    }
    if plan_circuits:
        result["circuits"] = plan_circuits
    return result


# ───────────────────────────── projection ────────────────────────────────

def _wall_outward_bearing(level: dict, wall: dict, room_centroid: Tuple[float, float],
                          north_offset_deg: float) -> float:
    x1, y1, x2, y2 = _wall_xy_endpoints(wall)
    nx, ny = _outward_normal_unit(x1, y1, x2, y2, room_centroid[0], room_centroid[1])
    return normal_to_compass_bearing_deg(nx, ny, north_offset_deg)


def _fold_wall_types_to_legacy_bins(
        level: dict, room: dict, north_offset_deg: float,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns {bin: {"type": str}} for each of the four legacy bins.

    Multiple polygon walls may fall in the same bin; pick by precedence.
    Bins not touched by any wall remain "unknown".
    """
    out = {b: {"type": "unknown"} for b in LEGACY_WALL_BINS}
    poly = [tuple(p) for p in room.get("polygon") or []]
    if len(poly) < 3:
        return out
    centroid = polygon_centroid(poly)

    for w in find_walls_for_room(level, room):
        bearing = _wall_outward_bearing(level, w, centroid, north_offset_deg)
        bin_name = bearing_to_legacy_wall_bin(bearing)
        explicit = str(w.get("type") or "").lower()
        wtype = infer_wall_type(level, w, explicit if explicit in VALID_WALL_TYPES else None)
        cur = out[bin_name]["type"]
        if _WALL_TYPE_PRECEDENCE.get(wtype, 0) > _WALL_TYPE_PRECEDENCE.get(cur, 0):
            out[bin_name] = {"type": wtype}
    return out


def _opening_to_dimension_entry(
        level: dict, room: dict, opening: dict, north_offset_deg: float,
) -> Optional[dict]:
    """
    Convert a floor-plan opening into the legacy windows[]/doors[] entry.

    Returns dict with keys:  area_m2, wall (legacy bin), and either
    glazing+orientation (windows) or type (doors).  ``None`` if the opening
    isn't on a wall bordering this room.
    """
    wall = next((w for w in level.get("walls", []) if w.get("id") == opening.get("wall_id")), None)
    if not wall:
        return None

    # Confirm the wall borders this room
    walls_of_room = find_walls_for_room(level, room)
    if wall["id"] not in {w["id"] for w in walls_of_room}:
        return None

    centroid = polygon_centroid([tuple(p) for p in room.get("polygon") or []])
    bearing = _wall_outward_bearing(level, wall, centroid, north_offset_deg)
    bin_name = bearing_to_legacy_wall_bin(bearing)
    width = float(opening.get("width_m") or 0.0)
    height = float(opening.get("height_m") or 0.0)
    if width <= 0 or height <= 0:
        return None
    area = round(width * height, 2)

    if opening.get("kind") == "window":
        return {
            "area_m2": area,
            "glazing": opening.get("glazing", "double"),
            "orientation": bearing_to_compass8(bearing),
            "wall": bin_name,
        }
    elif opening.get("kind") == "door":
        return {
            "area_m2": area,
            "type": opening.get("door_type", "internal"),
            "wall": bin_name,
        }
    return None


def _bbox_dimensions_for_room(room: dict) -> Tuple[Optional[float], Optional[float]]:
    """Width (X-axis) and depth (Y-axis) from polygon bbox."""
    poly = [tuple(p) for p in room.get("polygon") or []]
    if len(poly) < 3:
        return None, None
    minx, miny, maxx, maxy = polygon_bbox(poly)
    w = round(maxx - minx, 2)
    d = round(maxy - miny, 2)
    if w <= 0 or d <= 0:
        return None, None
    return w, d


def project_level_to_room_dimensions(
        level: dict, north_offset_deg: float = 0.0,
) -> Dict[str, dict]:
    """
    For each room on the level, build a dimensions dict compatible with the
    existing `_clean_dimensions` schema in ``heating_controller_routes.py``.

    Returns ``{room_id: dimensions_dict}``.
    """
    ceiling_h = float(level.get("ceiling_height_m") or DEFAULT_CEILING_HEIGHT_M)
    out: Dict[str, dict] = {}

    for room in level.get("rooms", []) or []:
        rid = room["id"]
        width_m, depth_m = _bbox_dimensions_for_room(room)
        if width_m is None or depth_m is None:
            continue

        walls_legacy = _fold_wall_types_to_legacy_bins(level, room, north_offset_deg)
        windows: List[dict] = []
        doors: List[dict] = []
        for op in level.get("openings", []) or []:
            entry = _opening_to_dimension_entry(level, room, op, north_offset_deg)
            if not entry:
                continue
            if op.get("kind") == "window":
                windows.append(entry)
            else:
                doors.append(entry)

        floor_area = round(polygon_area_m2([tuple(p) for p in room.get("polygon") or []]), 2)

        dim: Dict[str, Any] = {
            "width_m": width_m,
            "depth_m": depth_m,
            "ceiling_height_m": round(_clamp(ceiling_h, 1.5, 5.0), 2),
            "floor_area_m2": floor_area or round(width_m * depth_m, 2),
            "walls": walls_legacy,
            "windows": windows,
            "doors": doors,
        }
        ft = room.get("floor_type")
        if ft in VALID_FLOOR_TYPES:
            dim["floor_type"] = ft
        ct = room.get("ceiling_type")
        if ct in VALID_CEILING_TYPES:
            dim["ceiling_type"] = ct

        out[rid] = dim

    return out


def per_wall_breakdown_for_room(
        level: dict, room: dict, north_offset_deg: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """
    Detailed geometry for one room in plan coordinates.

    Returns a dict the thermal-profile static calculator can use directly
    instead of the 4-bin `width × depth × height` approximation:

        {
            "floor_area_m2":    float,                # true polygon area
            "ceiling_height_m": float,                # from level
            "walls": [
                {
                    "length_m":      float,
                    "height_m":      float,           # = ceiling_height_m
                    "type":          'external' | 'party' | 'internal' | 'unknown',
                    "compass":       'N' | 'NE' | ... | 'NW',  # outward normal
                    "openings_area_m2": float,        # windows + doors on this wall
                },
                ...
            ],
            "windows": [{"area_m2", "glazing", "compass", "on_external"}],
            "doors":   [{"area_m2", "type", "on_external"}],
            "floor_type":   str | None,
            "ceiling_type": str | None,
        }

    The crucial wins over ``project_level_to_room_dimensions``:
      - true polygon floor area (L-shaped rooms compute correctly)
      - per-wall lengths (N polygon edges rather than 4 fold-buckets)
      - per-opening external/internal decision based on the *host wall's*
        actual type rather than a folded bin
      - per-opening compass orientation preserved (needed by step 6+ for
        solar gain timing)

    Returns ``None`` if the room polygon is degenerate or has no walls.
    """
    poly = [tuple(p) for p in (room.get("polygon") or [])
            if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(poly) < 3:
        return None

    ceiling_h = float(level.get("ceiling_height_m") or DEFAULT_CEILING_HEIGHT_M)
    centroid = polygon_centroid(poly)

    # All walls bordering this room (collinear-edge match)
    room_walls = find_walls_for_room(level, room)
    if not room_walls:
        return None

    # Map wall_id -> the host wall's inferred type + bearing, so openings
    # can be classified by their actual host rather than a folded bin.
    wall_meta: Dict[str, Dict[str, Any]] = {}
    walls_out: List[Dict[str, Any]] = []

    for w in room_walls:
        x1, y1, x2, y2 = _wall_xy_endpoints(w)
        length_m = segment_length_m(x1, y1, x2, y2)
        if length_m < 0.05:
            continue
        nx, ny = _outward_normal_unit(x1, y1, x2, y2, centroid[0], centroid[1])
        bearing = normal_to_compass_bearing_deg(nx, ny, north_offset_deg)
        explicit = str(w.get("type") or "").lower()
        wtype = infer_wall_type(level, w,
                                explicit if explicit in VALID_WALL_TYPES else None)
        wall_meta[w["id"]] = {"type": wtype, "compass": bearing_to_compass8(bearing),
                              "length_m": length_m}
        walls_out.append({
            "id": w["id"],
            "length_m": round(length_m, 3),
            "height_m": round(_clamp(ceiling_h, 1.5, 5.0), 2),
            "type": wtype,
            "compass": bearing_to_compass8(bearing),
            "openings_area_m2": 0.0,   # filled in below
        })

    # Walk openings; tag each with the host wall's type + compass, then
    # accumulate their area against the corresponding wall record.
    windows_out: List[Dict[str, Any]] = []
    doors_out:   List[Dict[str, Any]] = []
    for op in level.get("openings", []) or []:
        host = wall_meta.get(op.get("wall_id"))
        if not host:
            continue
        width = float(op.get("width_m") or 0.0)
        height = float(op.get("height_m") or 0.0)
        if width <= 0 or height <= 0:
            continue
        area = round(width * height, 3)
        on_external = host["type"] == "external"

        # Bump the host wall's openings_area_m2
        for w in walls_out:
            if w["id"] == op.get("wall_id"):
                w["openings_area_m2"] = round(w["openings_area_m2"] + area, 3)
                break

        if op.get("kind") == "window":
            windows_out.append({
                "area_m2": area,
                "glazing": op.get("glazing", "double"),
                "compass": host["compass"],
                "on_external": on_external,
            })
        else:
            doors_out.append({
                "area_m2": area,
                "type": op.get("door_type", "internal"),
                "on_external": on_external,
            })

    out: Dict[str, Any] = {
        "floor_area_m2": round(polygon_area_m2(poly), 3),
        "ceiling_height_m": round(_clamp(ceiling_h, 1.5, 5.0), 2),
        "walls": walls_out,
        "windows": windows_out,
        "doors": doors_out,
    }
    ft = room.get("floor_type")
    if ft in VALID_FLOOR_TYPES:
        out["floor_type"] = ft
    ct = room.get("ceiling_type")
    if ct in VALID_CEILING_TYPES:
        out["ceiling_type"] = ct
    return out


def per_wall_breakdown_from_plan(
        floor_plan: dict, level_id: str, room_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Convenience: locate the level+room by id and call per_wall_breakdown_for_room.

    Returns ``None`` if either the level or the room isn't found, the plan
    is malformed, or the room geometry is degenerate.
    """
    if not isinstance(floor_plan, dict):
        return None
    north = float(floor_plan.get("north_offset_deg") or 0.0)
    for level in floor_plan.get("levels") or []:
        if level.get("id") != level_id:
            continue
        for room in level.get("rooms") or []:
            if room.get("id") == room_id:
                return per_wall_breakdown_for_room(level, room, north)
        break
    return None

def _legacy_radiator_for_room(level_radiators: List[dict], room_id: str,
                              warnings: List[str]) -> Optional[dict]:
    """Pick the largest-watts radiator for the legacy single-radiator field."""
    candidates = [r for r in level_radiators if r.get("room_id") == room_id]
    if not candidates:
        return None
    candidates_sorted = sorted(candidates, key=lambda r: -float(r.get("watts_at_dt50") or 0.0))
    primary = candidates_sorted[0]
    if len(candidates) > 1:
        warnings.append(
            f"room '{room_id}' has {len(candidates)} radiators; legacy "
            f"`radiator` set to the largest ({primary.get('watts_at_dt50')} W)"
        )
    legacy = {"watts_at_dt50": primary.get("watts_at_dt50")}
    for k in ("flow_temperature_c", "description", "placement", "type", "reflective_panel",
              "length_m", "height_m"):
        if k in primary:
            legacy[k] = primary[k]
    # Wall: only meaningful for the legacy front/back/left/right scheme; we'd
    # need to look up the wall's bearing-derived bin. The plan→dimensions
    # projection already places windows/doors correctly, and the radiator's
    # `wall` field is used only by tip generation, so leave it off here. The
    # rich `radiators` plural field carries the wall_id for new code.
    return legacy


def _legacy_primary_sensor_ieee(level_sensors: List[dict], room_id: str) -> Optional[str]:
    candidates = [s for s in level_sensors if s.get("room_id") == room_id]
    if not candidates:
        return None
    primary = next((s for s in candidates if s.get("primary")), None) or candidates[0]
    return primary.get("ieee")


def _trvs_for_room(level_radiators: List[dict], room_id: str) -> List[dict]:
    """Collect TRV ieees from radiators bound to this room."""
    seen = set()
    out: List[dict] = []
    for r in level_radiators:
        if r.get("room_id") != room_id:
            continue
        ieee = r.get("trv_ieee")
        if not ieee or ieee in seen:
            continue
        seen.add(ieee)
        out.append({"ieee": ieee})
    return out


def _contacts_for_room(level: dict, room_id: str) -> List[dict]:
    """All contacts whose opening belongs to this room."""
    op_by_id = {o["id"]: o for o in level.get("openings", []) or []}
    out = []
    for c in level.get("contacts", []) or []:
        op = op_by_id.get(c.get("opening_id"))
        if not op:
            continue
        if op.get("room_id") != room_id:
            continue
        out.append({
            "ieee": c["ieee"],
            "name": c.get("name") or c["ieee"][-8:],
            "debounce_open_seconds": c["debounce_open_seconds"],
            "require_temp_drop_c": c["require_temp_drop_c"],
            "max_close_minutes": c["max_close_minutes"],
            "enabled": c["enabled"],
            "opening_id": c["opening_id"],   # NEW; ignored by legacy code
        })
    return out


def project_floor_plan_to_circuits(
        floor_plan: dict, circuits: List[dict],
) -> Tuple[List[dict], List[str]]:
    """
    Apply a cleaned floor_plan to produce a controller-ready circuits list.

    Two modes:

    **Plan-native mode** (new): used when ``floor_plan["circuits"]`` is
    non-empty.  Derives circuits entirely from the plan — each plan circuit
    becomes a controller circuit, and each plan room with a matching
    ``circuit_id`` is projected into that circuit.  The passed-in
    ``circuits`` list is used only to carry over per-room settings that
    can't be derived from the plan (target_temp, night_setback, min_temp,
    schedules) and are otherwise defaulted.

    **Legacy / reconcile mode** (original): used when ``floor_plan["circuits"]``
    is absent.  Walks the existing ``circuits`` list and overwrites per-room
    geometry/sensor/radiator/contact data from the matching plan room.

    Returns ``(updated_circuits, warnings)``.
    """
    if not isinstance(floor_plan, dict):
        return circuits, ["floor_plan empty/invalid; nothing projected"]

    north = float(floor_plan.get("north_offset_deg") or 0.0)
    levels = floor_plan.get("levels") or []
    warnings: List[str] = []

    # Build room lookup for both modes
    room_index: Dict[str, Tuple[dict, dict]] = {}   # plan_room_id -> (level, room)
    name_index: Dict[str, Tuple[dict, dict]] = {}   # lower_name -> (level, room)
    for level in levels:
        for room in level.get("rooms", []) or []:
            room_index[room["id"]] = (level, room)
            nm = (room.get("name") or "").strip().lower()
            if nm:
                name_index.setdefault(nm, (level, room))

    # Pre-compute per-level dimensions
    dims_by_level: Dict[str, Dict[str, dict]] = {}
    for level in levels:
        dims_by_level[level["id"]] = project_level_to_room_dimensions(level, north)

    # ── Helper: project one plan room onto a controller room dict ──────
    def _project_room(fp_room: dict, level: dict,
                      base_room: Optional[dict] = None) -> dict:
        """Build a controller room dict from a floor-plan room + level."""
        r2: Dict[str, Any] = dict(base_room) if base_room else {}
        r2["id"] = fp_room["id"]
        r2["name"] = fp_room.get("name") or fp_room["id"]

        level_id = level["id"]
        dim = dims_by_level.get(level_id, {}).get(fp_room["id"])
        if dim is not None:
            r2["dimensions"] = dim

        level_radiators = level.get("radiators") or []
        level_sensors   = level.get("sensors") or []

        rich_rads = [rd for rd in level_radiators if rd.get("room_id") == fp_room["id"]]
        r2["radiators"] = rich_rads
        legacy_rad = _legacy_radiator_for_room(level_radiators, fp_room["id"], warnings)
        if legacy_rad:
            r2["radiator"] = legacy_rad

        sensors_in_room = [s for s in level_sensors if s.get("room_id") == fp_room["id"]]
        r2["temperature_sensors"] = sensors_in_room
        primary_ieee = _legacy_primary_sensor_ieee(level_sensors, fp_room["id"])
        if primary_ieee:
            r2["temperature_sensor_ieee"] = primary_ieee

        r2["contact_sensors"] = _contacts_for_room(level, fp_room["id"])
        r2["trvs"] = _trvs_for_room(level_radiators, fp_room["id"]) or r2.get("trvs", [])
        r2["floor_plan_ref"] = {"level_id": level_id, "room_id": fp_room["id"]}
        return r2

    # ── Plan-native mode ───────────────────────────────────────────────
    plan_circuits = floor_plan.get("circuits") or []
    if plan_circuits:
        # Build a lookup of existing controller rooms keyed by plan room id
        # so we can carry over user-set fields (target_temp etc.).
        existing_room_by_plan_id: Dict[str, dict] = {}
        for c in circuits:
            for r in (c.get("rooms") or []):
                ref = (r.get("floor_plan_ref") or {})
                pid = ref.get("room_id") or r.get("id")
                if pid:
                    existing_room_by_plan_id[pid] = r

        # Group plan rooms by circuit_id
        rooms_by_circuit: Dict[str, List[Tuple[dict, dict]]] = {}
        unassigned: List[str] = []
        for level in levels:
            for fp_room in level.get("rooms", []) or []:
                cid = fp_room.get("circuit_id")
                if cid:
                    rooms_by_circuit.setdefault(cid, []).append((fp_room, level))
                else:
                    unassigned.append(fp_room.get("name") or fp_room["id"])

        if unassigned:
            warnings.append(
                f"{len(unassigned)} room(s) not assigned to any circuit: "
                + ", ".join(unassigned[:10])
                + (" …" if len(unassigned) > 10 else "")
            )

        out_circuits: List[dict] = []
        for pc in plan_circuits:
            cid = pc["id"]
            c2: Dict[str, Any] = {
                "id": cid,
                "name": pc["name"],
            }
            if pc.get("receiver_ieee"):
                c2["receiver_ieee"] = pc["receiver_ieee"]
            c2["receiver_command"] = pc.get("receiver_command", "thermostat")
            if "receiver_endpoint" in pc:
                c2["receiver_endpoint"] = pc["receiver_endpoint"]

            new_rooms = []
            for fp_room, level in rooms_by_circuit.get(cid, []):
                base = existing_room_by_plan_id.get(fp_room["id"])
                room_dict = _project_room(fp_room, level, base)
                # Apply defaults for fields the plan doesn't carry
                room_dict.setdefault("target_temp", 20.0)
                room_dict.setdefault("night_setback", 17.0)
                room_dict.setdefault("min_temp", 16.0)
                room_dict.setdefault("external_temp_mode", "push")
                room_dict.setdefault("external_temp_push_interval_sec", 300)
                room_dict.setdefault("schedule", [])
                new_rooms.append(room_dict)

            if not new_rooms:
                warnings.append(
                    f"circuit '{cid}' has no rooms assigned in the floor plan"
                )

            c2["rooms"] = new_rooms
            out_circuits.append(c2)

        return out_circuits, warnings

    # ── Legacy reconcile mode (original behaviour) ─────────────────────
    out_circuits = []
    for c in circuits:
        if not isinstance(c, dict):
            out_circuits.append(c)
            continue
        c2 = dict(c)
        new_rooms = []
        for r in c.get("rooms") or []:
            if not isinstance(r, dict):
                new_rooms.append(r)
                continue
            r2 = dict(r)
            rid = r2.get("id")
            match = room_index.get(rid) if rid else None
            if not match:
                nm = (r2.get("name") or "").strip().lower()
                match = name_index.get(nm) if nm else None
            if not match:
                new_rooms.append(r2)
                continue

            level, fp_room = match
            new_rooms.append(_project_room(fp_room, level, r2))
        c2["rooms"] = new_rooms
        out_circuits.append(c2)

    return out_circuits, warnings


# ─────────────────────────────── self test ───────────────────────────────

if __name__ == "__main__":
    # Smoke test: a 5×4 m room with one south wall, one window on south,
    # no walls clipped to other rooms (so all 'external'), north_offset=0.
    fp = {
        "north_offset_deg": 0.0,
        "levels": [{
            "id": "ground",
            "name": "Ground",
            "index": 0,
            "ceiling_height_m": 2.4,
            "rooms": [{
                "id": "lounge",
                "name": "Lounge",
                "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]],
                "floor_type": "carpet_over_concrete",
            }],
            "walls": [
                {"id": "ws", "x1": 0, "y1": 0, "x2": 5, "y2": 0, "type": "external"},
                {"id": "we", "x1": 5, "y1": 0, "x2": 5, "y2": 4, "type": "external"},
                {"id": "wn", "x1": 5, "y1": 4, "x2": 0, "y2": 4, "type": "external"},
                {"id": "ww", "x1": 0, "y1": 4, "x2": 0, "y2": 0, "type": "external"},
            ],
            "openings": [{
                "id": "win1", "wall_id": "ws", "kind": "window",
                "offset_m": 1.0, "width_m": 1.4, "height_m": 1.2,
                "glazing": "double", "room_id": "lounge",
            }],
            "radiators": [
                {"id": "r1", "room_id": "lounge", "watts_at_dt50": 1500,
                 "wall_id": "ws", "placement": "under_window",
                 "type": "double_panel_double_conv",
                 "trv_ieee": "00:11:22:33:44:55:66:77"},
                {"id": "r2", "room_id": "lounge", "watts_at_dt50": 800,
                 "wall_id": "wn"},
            ],
            "sensors": [{
                "id": "s1", "room_id": "lounge",
                "ieee": "aa:bb:cc:dd:ee:ff:00:11",
                "kind": "thermostat", "x": 2.5, "y": 2.0,
                "height_m": 1.5, "primary": True,
            }],
            "contacts": [{
                "id": "c1", "opening_id": "win1",
                "ieee": "11:22:33:44:55:66:77:88",
            }],
        }],
    }
    cleaned = clean_floor_plan(fp)
    assert cleaned, "cleaned floor plan should not be None"
    print("CLEANED floor_plan keys:", list(cleaned.keys()))

    # The south wall (y=0, going +x) has outward normal pointing -y (south).
    # In our plan-coords (+y=up = north), bearing for (-y) = 180° = S. ✓
    legal_circuits = [{
        "id": "zone1",
        "name": "Zone 1",
        "rooms": [{
            "id": "lounge",
            "name": "Lounge",
            "trvs": [],
        }],
    }]
    projected, warns = project_floor_plan_to_circuits(cleaned, legal_circuits)
    rm = projected[0]["rooms"][0]
    print("\nProjected room dimensions:")
    for k, v in rm["dimensions"].items():
        print(f"  {k}: {v}")
    print("\nLegacy radiator:", rm.get("radiator"))
    print("Plural radiators:", len(rm["radiators"]), "items")
    print("TRVs derived:", rm["trvs"])
    print("Primary sensor IEEE:", rm.get("temperature_sensor_ieee"))
    print("Plural sensors:", len(rm["temperature_sensors"]), "items")
    print("Contact sensors:", len(rm["contact_sensors"]), "items")
    print("\nWarnings:", warns)

    # Window orientation should be S (south-facing wall)
    win = rm["dimensions"]["windows"][0]
    assert win["orientation"] == "S", f"expected S, got {win['orientation']}"
    assert win["wall"] == "front", f"expected front, got {win['wall']}"
    print("\n✓ window orientation/wall checks passed")