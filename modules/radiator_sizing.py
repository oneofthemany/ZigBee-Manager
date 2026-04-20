"""
Radiator / BTU sizing for heated rooms.

Given a thermal profile (W/K) and design parameters, compute:
  - required heat output at design conditions (W and BTU/hr)
  - flow-temperature-adjusted output for existing radiators
  - over/undersized flags

All functions are pure; no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Constants ─────────────────────────────────────────────────────────
W_PER_BTU_HR = 0.2931        # 1 BTU/hr = 0.2931 W, so W ÷ 0.2931 = BTU/hr
DEFAULT_DESIGN_OUTDOOR_C = -3.0   # UK standard (MCS)
DEFAULT_OVERSIZE_FACTOR = 1.15    # 15% oversize headroom

# Radiator output is rated at ΔT50 (mean water temp 70°C, room 20°C).
# To derate for lower flow temps, use the manufacturer exponent ~1.3:
#   Q_actual / Q_rated = (ΔT_actual / 50) ** 1.3
RADIATOR_DERATE_EXPONENT = 1.3


@dataclass
class RadiatorSizing:
    room_id: str
    target_temp_c: float
    design_outdoor_c: float
    delta_t: float
    w_per_k: Optional[float]
    required_watts: Optional[float] = None
    required_watts_with_margin: Optional[float] = None
    required_btu_hr: Optional[float] = None
    oversize_factor: float = DEFAULT_OVERSIZE_FACTOR

    # If the user has told us about existing radiators
    installed_watts_at_dt50: Optional[float] = None
    flow_temperature_c: Optional[float] = None   # e.g. 55 for a condenser
    installed_watts_at_flow_temp: Optional[float] = None
    status: Optional[str] = None          # "undersized" | "adequate" | "oversized" | "unknown"
    deficit_watts: Optional[float] = None
    surplus_watts: Optional[float] = None

    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "target_temp_c": self.target_temp_c,
            "design_outdoor_c": self.design_outdoor_c,
            "delta_t": self.delta_t,
            "w_per_k": self.w_per_k,
            "required_watts": self.required_watts,
            "required_watts_with_margin": self.required_watts_with_margin,
            "required_btu_hr": self.required_btu_hr,
            "oversize_factor": self.oversize_factor,
            "installed_watts_at_dt50": self.installed_watts_at_dt50,
            "flow_temperature_c": self.flow_temperature_c,
            "installed_watts_at_flow_temp": self.installed_watts_at_flow_temp,
            "status": self.status,
            "deficit_watts": self.deficit_watts,
            "surplus_watts": self.surplus_watts,
            "warnings": self.warnings,
        }


def derate_radiator(rated_watts_at_dt50: float,
                    flow_temp_c: float,
                    room_temp_c: float) -> float:
    """
    Adjust a rated radiator output (at ΔT50) to the actual delta-T between
    mean water temp and room temp.

    MWT is approximately flow_temp - 5°C (i.e. 10°C drop / 2). For a boiler
    running flow 55°C return 45°C, MWT = 50°C.
    """
    mwt = flow_temp_c - 5.0
    actual_dt = mwt - room_temp_c
    if actual_dt <= 0:
        return 0.0
    return rated_watts_at_dt50 * (actual_dt / 50.0) ** RADIATOR_DERATE_EXPONENT


def compute_sizing(
        room_id: str,
        w_per_k: Optional[float],
        target_temp_c: float,
        design_outdoor_c: float = DEFAULT_DESIGN_OUTDOOR_C,
        oversize_factor: float = DEFAULT_OVERSIZE_FACTOR,
        installed_watts_at_dt50: Optional[float] = None,
        flow_temperature_c: Optional[float] = None,
) -> RadiatorSizing:
    """
    Build a full sizing recommendation for one room.

    If w_per_k is None (no dimensions / no thermal profile), returns a mostly
    empty result with a warning.

    If installed_watts_at_dt50 is provided and flow_temperature_c differs
    from 70°C, the installed capacity is derated accordingly.
    """
    delta_t = round(target_temp_c - design_outdoor_c, 1)
    out = RadiatorSizing(
        room_id=room_id,
        target_temp_c=target_temp_c,
        design_outdoor_c=design_outdoor_c,
        delta_t=delta_t,
        w_per_k=w_per_k,
        oversize_factor=oversize_factor,
    )

    if w_per_k is None or w_per_k <= 0:
        out.warnings.append(
            "no thermal profile available — set room dimensions to compute sizing"
        )
        out.status = "unknown"
        return out

    required = w_per_k * delta_t
    required_with_margin = required * oversize_factor

    out.required_watts = round(required, 0)
    out.required_watts_with_margin = round(required_with_margin, 0)
    out.required_btu_hr = round(required_with_margin / W_PER_BTU_HR, 0)

    if installed_watts_at_dt50 is None:
        out.status = "unknown"
        return out

    out.installed_watts_at_dt50 = float(installed_watts_at_dt50)

    if flow_temperature_c is not None and flow_temperature_c != 70.0:
        out.flow_temperature_c = flow_temperature_c
        out.installed_watts_at_flow_temp = round(
            derate_radiator(installed_watts_at_dt50, flow_temperature_c, target_temp_c),
            0,
        )
        installed_effective = out.installed_watts_at_flow_temp
    else:
        installed_effective = installed_watts_at_dt50

    # Compare with margin: adequate if installed ≥ required_with_margin
    diff = installed_effective - required_with_margin
    if diff < -50:     # >50 W short → flag undersized
        out.status = "undersized"
        out.deficit_watts = round(-diff, 0)
    elif diff > installed_effective * 0.5:   # >50% surplus → oversized
        out.status = "oversized"
        out.surplus_watts = round(diff, 0)
    else:
        out.status = "adequate"

    return out