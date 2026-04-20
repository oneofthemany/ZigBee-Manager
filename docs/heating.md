# Heating

## Overview

ZigBee Matter Manager has two complementary heating modules:

- **Heating Advisor** — a read-only analytical engine. It correlates outdoor weather with indoor temperatures and heating demand to produce an EPC-style rating for the property, pre-heat timing recommendations, cost estimates, and efficiency tips. It never sends commands to devices.
- **Heating Controller** — the active control layer. It watches room temperatures, decides which rooms are calling for heat, turns the boiler/zone valve on and off via the receiver, and coordinates TRV setpoints so a hot room can't steal heat from a cold one on the same circuit.

Everything the UI shows in the **Heating** tab is derived from these two modules plus the per-room **thermal profile** (heat loss physics) and **radiator sizing** (BTU/Watt capacity check). Both are purely calculated from the room's dimensions and insulation — you don't need sensors to get a baseline, but the more telemetry you give the system the more the numbers improve.

### What powers what

```
Weather (Open-Meteo)  ─┐
                       ├─► HeatingAdvisor   ──► EPC band, tips, preheat, costs
Devices (HVAC/TRVs)  ──┤
Telemetry DB (history) ┤
                       └─► ThermalProfile   ──► W/K per room, tau, anomaly baseline
                           RadiatorSizing   ──► required W vs installed W

Config (circuits/rooms) ──► HeatingController ──► boiler calls, TRV setpoints
                                             └──► AnomalyWatcher (fast-cool alerts)
```

---

## The Physics, Briefly

Heat loss through a building obeys **Newton's law of cooling**:

> A room loses heat to the outside at a rate proportional to the temperature difference between inside and outside.

The proportionality constant is called the **heat loss coefficient**, written *UA* or *W/K* — it's the power in watts needed to keep the room 1 °C warmer than outside. The whole heating model rests on four equations.

### 1. Steady-state heat loss

```
Q_loss  =  UA × (T_indoor − T_outdoor)     [watts]
```

A room with UA = 80 W/K on a 0 °C day with a 20 °C target needs `80 × 20 = 1600 W` of continuous heat input to hold that temperature.

### 2. Newton's law of cooling (with the heating off)

```
T(t)  =  T_outdoor  +  (T_0 − T_outdoor) × exp(−t / τ)
```

`τ` (tau) is the **thermal time constant** in seconds — roughly how long it takes the room to cool 63% of the way to outdoor temperature. A well-insulated heavy-mass room has τ ≈ 8–12 h; a cold-conservatory-style room can be under 1 h.

`τ` is related to UA by:

```
τ  =  (thermal mass, J/K)  /  UA
```

### 3. Heat-up time

Combining the two above, the time to heat from `T_from` to `T_to` with a radiator delivering `Q_rad` watts is:

```
T_steady  =  T_outdoor + (Q_rad / UA)
t  =  −τ × ln( (T_steady − T_to) / (T_steady − T_from) )
```

`T_steady` is where the room would plateau if you ran the radiator forever. If `T_steady ≤ T_to`, the radiator is undersized for the conditions and the target is **unreachable** — the controller flags this explicitly.

### 4. Radiator derating at lower flow temperatures

Radiators are rated at **ΔT50** (mean water temperature 70 °C, room 20 °C). A condensing boiler typically runs flow ~55 °C / return ~45 °C, so mean water temp is about 50 °C and ΔT is 30, not 50. Actual output follows an empirical exponent:

```
Q_actual / Q_rated  =  (ΔT_actual / 50) ^ 1.3
```

At ΔT30 that's `(30/50)^1.3 ≈ 0.52` — a radiator rated 2000 W at ΔT50 gives only about 1040 W in a condensing system. This is why correctly sizing radiators matters and why the BTU check uses your configured flow temperature.

---

## Thermal Profile (per-room)

Every room in the circuits config can have a `dimensions` block:

```yaml
rooms:
  - id: living
    name: Living room
    target_temp: 21
    dimensions:
      width_m: 4.2
      depth_m: 3.8
      ceiling_height_m: 2.4
      floor_type: carpet_over_concrete
      ceiling_type: insulated
      walls:
        front: { type: external }
        back:  { type: internal }
        left:  { type: party }
        right: { type: external }
      windows:
        - { wall: front, area_m2: 2.1, glazing: double }
      doors:
        - { wall: back, area_m2: 1.9, type: internal }
```

From that, `compute_static()` in `thermal_profile.py` produces a heat loss breakdown:

| Element              | Formula                                              |
|:---------------------|:-----------------------------------------------------|
| External walls       | `(wall_area − openings) × U_wall_ext`                |
| Party walls          | `area × U_party` (usually 0 — neighbour is heated)   |
| Internal walls       | 0 — loss-free to adjacent heated rooms               |
| Windows              | `area × U_glazing(single/double/triple)`             |
| External doors       | `area × U_door_ext`                                  |
| Floor                | `floor_area × U_floor(type)`                         |
| Ceiling              | `floor_area × U_ceiling(type)`                       |
| Ventilation          | `ρ_air × C_p × volume × ACH / 3600`                  |

Sum these and you get the room's **static W/K**. U-values come from SAP Appendix S and CIBSE Guide A, selected by the dwelling-wide `insulation` level (`none`, `partial`, `full`, `cavity_wall`).

### Measured W/K

`compute_measured()` does the same job from telemetry. It looks through the last N hours of temperature history, finds intervals where:

- The temperature is monotonically falling (with a small noise tolerance)
- The interval is at least 30 min and at most 6 h
- The total drop is at least 0.5 °C

For each window it fits Newton's cooling model by linear regression on `ln((T − T_out) / (T_0 − T_out)) = −t/τ` and keeps fits with R² ≥ 0.5. The median τ across all fits, combined with an estimated thermal mass of `3 × ρ_air × V × C_p` (the "3×" factor is CIBSE TM41 for furnishings/fabric), gives measured UA:

```
UA_measured  =  (3 × ρ × V × C_p) / τ_median
```

### Blending

If measured confidence is ≥ 0.3, the UI shows a **blended** W/K:

```
w  =  min(1.0, 0.7 × confidence / 0.7)
blended  =  w × measured + (1 − w) × static
```

Otherwise it falls back to static only. Confidence itself is `sample_factor × mean_R²` where `sample_factor` saturates at 10 good fits.

---

## Radiator Sizing

Given the blended W/K and a design outdoor temperature (−3 °C for UK MCS), the required radiator output is:

```
required_watts          =  W/K × (target_temp − design_outdoor)
required_with_margin    =  required_watts × 1.15       (15% headroom)
required_btu_hr         =  required_with_margin / 0.2931
```

If you've entered `installed_watts_at_dt50` for the room's radiator, the sizing module derates it for your actual flow temperature using the derate formula from section 4 above, then compares:

| Difference                         | Status        |
|:-----------------------------------|:--------------|
| installed < required − 50 W        | `undersized`  |
| installed > required × 1.5         | `oversized`   |
| otherwise                          | `adequate`    |

The UI shows this along with a deficit or surplus figure in watts.

---

## Efficiency Tips

The **per-room tips panel** in the Heating Controller modal runs two rule sets: a client-side mirror for instant feedback, and the backend `_generate_room_tips` for authoritative data. Both check roughly the same conditions:

| Trigger                                              | Severity | Notes                                                    |
|:-----------------------------------------------------|:---------|:---------------------------------------------------------|
| Dimensions missing                                   | info     | Cannot compute heat loss without them                    |
| No radiator capacity configured                      | info     | Required for sizing check                                |
| Radiator placement not set                           | info     | Affects efficiency flagging                              |
| Radiator under window                                | warning  | ~10% efficiency loss from warm/cold air mixing           |
| No reflective panel, radiator on external wall       | info     | Panel returns 3–8% more heat into the room               |
| No reflective panel, radiator on internal wall       | info     | Smaller gain (~1–3%) but helps TRV responsiveness        |
| Reflective panel status unknown                      | info     | Prompt to mark it either way                             |
| Single-panel radiator                                | info     | K2/P+ upgrade in same footprint roughly doubles output   |
| Single-glazed windows                                | warning  | ~4.8 W/m²/K vs ~1.6 for a good double                    |
| External door in room                                | info     | Check seals; heavy door curtain helps                    |
| Suspended or wooden floor                            | info     | Under-floor insulation is a fast retrofit payback        |

The **dashboard-level tips** in `heating_advisor._generate_tips` cover whole-dwelling patterns:

- Room is over-heating (indoor > target + 1 °C): each 1 °C cut saves ~3% on bills
- Mild weather (outdoor > 15 °C) while heating is active → consider turning off
- Cold snap forecast (next 6h min < 2 °C) → pre-heat now to avoid demand spike
- EPC band E/F/G → insulation upgrade would save 20–40%
- Single glazing
- Late-night heating → night setback could save ~10%
- Economy 7 / Agile tariffs → use the off-peak window
- Boiler < 90% efficient and gas → modern condensing saves ~£150/year
- Good insulation + fossil boiler → heat pump candidate (BUS grant £7,500)

---

## EPC Estimation

The Heating Advisor produces an EPC-style band from:

```
annual_kwh          =  UA_total × HDD × 24 / 1000        (HDD ~ 2200 UK average)
annual_fuel_kwh     =  annual_kwh / boiler_efficiency
kwh_per_m2_per_yr   =  annual_fuel_kwh / floor_area_m2
```

Where `UA_total` is the whole-dwelling coefficient `U × glazing_factor × floor_area`. `kwh_per_m2_per_yr` is the standard SAP metric and maps to letter bands:

| Band | kWh/m²/year |
|:-----|:------------|
| A    | 0–25        |
| B    | 25–50       |
| C    | 50–75       |
| D    | 75–100      |
| E    | 100–125     |
| F    | 125–150     |
| G    | 150+        |

The headline score is `max(1, min(100, 100 − kWh/m² × 0.6))` which keeps A ≈ 92–100, B ≈ 81–91, etc. The annual cost figure is `annual_fuel_kwh × unit_rate + 365 × standing_charge`.

These are **estimates, not formal EPCs**. They use SAP-style defaults, not the full BREDEM model an accredited assessor would run. Treat them as directional.

---

## Pre-heat Recommendation

The preheat calculator has two implementations. The quick one, on the dashboard, uses bulk thermal mass:

```
boiler_watts      =  boiler_kw × 1000 × efficiency
heat_loss_watts   =  UA × max(0, mean_indoor − outdoor)
net_watts         =  boiler_watts − heat_loss_watts
energy_needed_kJ  =  thermal_mass × (target − start)
minutes           =  ceil( energy_kJ × 1000 / net_watts / 60 )
```

(Thermal mass defaults to `80 kJ/m² × floor_area` for pre-1960 buildings, `60 kJ/m² × floor_area` otherwise, reflecting solid-wall heavyweight vs modern lightweight construction.)

If `net_watts ≤ 0` the boiler can't keep pace with losses at the current outdoor temp — preheat is clamped to the configured max (default 90 min) and the UI flags it.

The **per-room** version in `compute_preheat()` uses the more accurate Newton-of-heating formula from section 3 above, driven by the room's measured `τ` and derated radiator output. It also tells you the *steady-state* temperature the room would plateau at — if that's below your target, heating can't reach it at this outdoor temp regardless of how long you wait.

Confidence drops to **low** when there's no measured τ (fallback default 3 h) or no radiator capacity configured (assumes perfect sizing).

---

## Heating Controller — Active Control

### Data model

```
Circuit (receiver + zone valve → calls the boiler)
  └── Room (target temp, schedule, optional external sensor)
        └── TRV(s) (regulate flow into that room's radiators)
```

A circuit represents one call-for-heat signal to the boiler. Most UK homes have two (upstairs/downstairs) or one open zone. Rooms on the same circuit share the boiler call — the controller decides per-TRV how much of that heat each room gets.

### Tick cycle (every 60 s)

1. **Snapshot** every device's current state.
2. **Pick a room temperature source**, in priority order:
    - External sensor (`temperature_sensor_ieee`), if present and reading
    - Mean of the room's TRV `local_temperature`s
3. **Classify each room** against its effective target (schedule slot → night setback → default):
    - `cold`       if `current < target − 0.5 °C`   (hysteresis — see below)
    - `hot`        if `current > target + 0.3 °C`
    - `ontarget`   otherwise
4. **Circuit call-for-heat**: if any room in the circuit is `cold`, the circuit calls; all-`hot`-or-`ontarget` stands down.
5. **Receiver action**: send `system_mode=heat`/`off` (or relay on/off) to the receiver, only if different from the last command sent. In thermostat mode it also pushes a high setpoint (30 °C default) when calling and a low one (7 °C) when idle, so the receiver's internal comparator fires the boiler reliably.
6. **TRV setpoints**:
    - Room `cold` or `ontarget` → setpoint = target
    - Room `hot` → setpoint = `max(min_setpoint, min(room_temp − 1, target − 1))` — forces the valve shut even when the circuit is about to fire for a colder room
7. **Cooldowns and deltas** — skip the command if the TRV setpoint is already within 0.5 °C of the intended value, or if the same command was sent less than 5 min ago. Saves TRV battery airtime.

### Hysteresis — why those numbers

```python
COLD_BAND = 0.5   # room is COLD if temp < target − 0.5
HOT_BAND  = 0.3   # room is HOT  if temp > target + 0.3
```

These bands prevent the controller from oscillating on each tick as the temperature crosses the setpoint. A 0.5/0.3 asymmetry reflects that it's more important to stop calling for heat promptly (over-shoot costs money) than to start calling aggressively (under-shoot costs comfort). Inside the dead band (target − 0.5 → target + 0.3), rooms stay in their current state.

### External temperature modes

Many wall-mounted thermostats read hot-pipe temperature, not air temperature, and report 2–4 °C higher than the room actually is. Three modes handle this:

| Mode       | Controller classifies using | TRV regulates using        | Use when                                                                |
|:-----------|:----------------------------|:---------------------------|:------------------------------------------------------------------------|
| `off`      | TRV local temp              | TRV local temp             | No external sensor configured                                           |
| `advisory` | External sensor             | TRV local temp             | Safe default when an external sensor exists — fixes controller-side bias |
| `push`     | External sensor             | External value written to TRV (Aqara 0xFCC0 attr 0x0280) | Aqara TRVs with external sensor mode enabled        |

### Force-close logic

A common multi-room problem: circuit A has a cold room and a hot room. When the boiler fires, hot water flows through both TRVs. If the hot room's TRV isn't explicitly closed, it'll over-shoot further. The controller handles this by writing a setpoint *below* the hot room's current temperature, so the TRV's own thermostat clamps the valve shut. The setpoint is clamped to `min_setpoint` (5 °C default for Aqara E1) to stay within the TRV's valid range.

This is done **pre-emptively** — even on a tick where the circuit isn't currently calling, a hot-room TRV gets force-closed so it's already shut the next time the circuit fires for a different room.

### Per-TRV persistent config

On controller start, each configured TRV has its Aqara-cluster settings applied:

| Setting            | Cluster        | Attribute | Effect                                                   |
|:-------------------|:---------------|:----------|:---------------------------------------------------------|
| `window_detection` | 0xFCC0         | 0x0273    | Close valve when a rapid temp drop suggests open window  |
| `child_lock`       | 0xFCC0         | 0x0277    | Disable manual TRV adjustment                            |
| `valve_detection`  | 0xFCC0         | 0x0274    | Detect stuck/unresponsive valves                         |

These are **one-shot on startup** per TRV (deduplicated via `_trv_config_applied`), then re-applied via the `/apply-trv-config` API if the user changes them.

---

## Anomaly Detection

The **Heating Anomaly Watcher** scans every 5 minutes. For each room with a known baseline τ, it pulls the last ~3 h of temperature history and fits Newton's cooling over recent cool-down windows. It then compares observed vs baseline τ:

| Ratio (observed / baseline) | Severity   | Interpretation                                           |
|:----------------------------|:-----------|:---------------------------------------------------------|
| < 0.3                       | `critical` | Cooling 3× faster than baseline — window open, broken seal, boiler off |
| < 0.5                       | `warning`  | Cooling 2× faster than baseline                          |
| ≥ 0.5                       | none       | Within normal variation                                  |

Active anomalies surface on the dashboard as "Room X cooling faster than expected" cards. Once the condition resolves (subsequent scans show normal τ), the anomaly moves into a 6-hour history buffer so users can see the "was" card briefly before it drops off.

This is how the system catches problems like: a window left open overnight, a TRV that's stopped responding, a broken door seal, or a room whose insulation has degraded — all without the user needing to watch temperature graphs.

---

## Configuration Reference

### Dwelling-level config (`heating:` block)

```yaml
heating:
  enabled: true
  property:
    type: semi-detached        # detached | semi-detached | mid-terrace | flat
    age: 1960                  # build year — affects thermal mass estimate
    insulation: partial        # none | partial | full | cavity_wall
    glazing: double            # single | double | triple
    floor_area_m2: 85
    floors: 2
  tariff:
    type: fixed                # fixed | economy7 | agile | variable
    unit_rate_p: 24.5
    standing_charge_p: 46.36
    off_peak_start: "00:00"
    off_peak_end: "07:00"
    off_peak_rate_p: 7.5
  boiler:
    type: gas                  # gas | oil | electric | heat_pump
    efficiency_percent: 89
    output_kw: 24
  comfort:
    min_temp: 18.0
    target_temp: 21.0
    night_setback: 16.0
    preheat_max_minutes: 90
```

### Controller config (`heating.controller:`)

```yaml
heating:
  controller:
    enabled: true
    dry_run: false             # true logs what it would do without sending
  circuits:
    - id: downstairs
      name: Downstairs
      receiver_ieee: "00:15:8d:00:00:aa:bb:cc"
      receiver_command: thermostat    # 'thermostat' or 'switch'
      receiver_endpoint: 1
      receiver_call_setpoint: 30.0    # pushed when calling
      receiver_idle_setpoint: 7.0     # pushed when idle
      rooms:
        - id: living
          name: Living
          target_temp: 20.5
          night_setback: 17.0
          min_temp: 16.0
          temperature_sensor_ieee: "00:1e:5e:09:02:a3:e4:c1"
          external_temp_mode: advisory
          external_temp_push_interval_sec: 300
          dimensions: { ... }          # see Thermal Profile
          radiator:
            watts_at_dt50: 1800
            flow_temperature_c: 55
            type: double_panel_double_conv
            wall: front
            placement: external_wall   # under_window | external_wall | internal_wall
            reflective_panel: true
          trvs:
            - ieee: "54:ef:44:10:00:67:3e:a6"
              window_detection: true
              child_lock: false
              valve_detection: true
              min_setpoint: 5.0
          schedule:
            - days: [mon, tue, wed, thu, fri]
              start: "07:00"
              end:   "22:00"
              temp:  20.5
```

### Key constants (non-configurable defaults)

| Constant                       | Value      | Module                       |
|:-------------------------------|:-----------|:-----------------------------|
| `COLD_BAND`                    | 0.5 °C     | `heating_controller.py`      |
| `HOT_BAND`                     | 0.3 °C     | `heating_controller.py`      |
| `FORCE_CLOSE_OFFSET`           | 1.0 °C     | `heating_controller.py`      |
| `MIN_SETPOINT_DELTA`           | 0.5 °C     | `heating_controller.py`      |
| `TICK_INTERVAL_SEC`            | 60 s       | `heating_controller.py`      |
| `COMMAND_COOLDOWN_SEC`         | 300 s      | `heating_controller.py`      |
| `DEFAULT_OVERSIZE_FACTOR`      | 1.15       | `radiator_sizing.py`         |
| `DEFAULT_DESIGN_OUTDOOR_C`     | −3.0 °C    | `radiator_sizing.py`         |
| `RADIATOR_DERATE_EXPONENT`     | 1.3        | `radiator_sizing.py`         |
| `ROOM_THERMAL_MASS_FACTOR`     | 3.0        | `thermal_profile.py`         |
| `SCAN_INTERVAL_SEC` (anomaly)  | 300 s      | `heating_anomaly_watcher.py` |

---

## Why the Two Modules Are Separate

The Advisor is **read-only and cannot cause harm**. It runs analysis, surfaces recommendations, and nothing it does changes the state of a radiator valve or a boiler. You can run the Advisor with the Controller disabled and still get the dashboard, EPC, tips and preheat advice.

The Controller **actively commands hardware**, so it's gated behind a separate `heating.controller.enabled` flag and has a `dry_run` mode. In dry-run the full tick cycle runs, decisions are logged, and the UI shows what *would* happen, but no commands are sent. This is the recommended way to validate a new circuit/room config before letting it touch real TRVs.

Both modules share the same underlying thermal profile and telemetry, so the numbers in the Advisor's preheat estimate are the same ones the Controller will see once you turn it on.

---

## Troubleshooting

**"No tips are showing for my room"** — check that `dimensions` is populated and that at least one wall is typed `external`. Most tips are gated on data completeness.

**"The controller says my room is cold but the TRV reports it's at target"** — you probably need `external_temp_mode: advisory` and a separate wall-mounted temperature sensor. TRVs read the hot pipe, not the room.

**"Preheat says 90 minutes every morning"** — that's the clamp hitting `preheat_max_minutes`. Either your boiler is undersized for the conditions (check the net watts calculation), your outdoor temp sensor is wrong, or your room has no measured τ yet. Check the preheat warnings — they'll say which.

**"Radiator sizing status is unknown"** — you need both `dimensions` (to compute W/K) and `radiator.watts_at_dt50` (to compare against). Missing either one returns `unknown`.

**"Anomaly watcher never fires"** — it needs a baseline τ. The thermal profile has to produce a measured W/K first, which needs ~10 h of temperature history with at least one clean cool-down window. Give it a day of data after you configure dimensions.

**"The EPC number looks wrong"** — remember it's a SAP-style estimate using UK average 2200 heating degree-days, not the full BREDEM methodology. It's a relative indicator for seeing whether changes help, not a formal certification.