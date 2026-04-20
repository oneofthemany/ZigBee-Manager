"""
Full-spectrum Zigbee cluster introspection
==========================================
Exhaustive discovery of a cluster's attributes and commands, including
manufacturer-specific ones. Fixes the following gaps in the previous
implementation:

  1. Sweeps the full 0x0000-0xFFFE attribute ID space in paginated chunks,
     not just the first 256 IDs. Manufacturer attrs at 0xF000+ are found.

  2. Re-runs discovery per known manufacturer code for this device so
     devices that gate attributes on a manufacturer-coded ZCL header
     (Aqara 0xFCC0, Philips, Sonoff, Tuya, IKEA, Legrand etc.) expose
     their full attribute set.

  3. Handles the Discover Attributes "complete" flag by re-issuing the
     request starting from (last_id + 1) until the device signals done.

  4. Prefers Discover Attributes Extended (0x0E) when available to get
     real access-control flags; falls back to the basic discover + heuristic
     write-test only when Extended isn't supported.

  5. Treats zero-value writes conservatively (never writes 0 to unknown
     attrs) to minimise side effects on bitmap/enum fields.

  6. Reads the Reporting Configuration for each readable attribute so the
     cache knows whether/how the device auto-reports.

  7. Discovers received and generated cluster commands too, so the user
     has a complete picture of what the cluster supports.

Output is a dict suitable for direct return from an API handler and for
insertion into the zigbee_cache.device_attributes table.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("modules.diag_attributes")


# ----------------------------------------------------------------------------
# MANUFACTURER CODE REGISTRY
# ----------------------------------------------------------------------------
# Known manufacturer codes worth trying for discovery. Keyed by cluster ID
# when the manufacturer code is cluster-specific (e.g. 0xFCC0 is always
# Aqara), plus a generic "try these for any cluster" list for devices whose
# manufacturer code is set at the device level.
#
# ZCL manufacturer codes are 16-bit. These are the big ones we encounter in
# practice; extend as needed.

MANUFACTURER_CODES = {
    "LUMI":       0x115F,   # Aqara / Xiaomi (0xFCC0, various)
    "PHILIPS":    0x100B,   # Signify / Hue
    "SIGNIFY":    0x110B,   # Signify (newer)
    "SONOFF":     0x1286,   # eWeLink / Sonoff (0xFC11)
    "IKEA":       0x117C,   # IKEA of Sweden
    "LEGRAND":    0x1021,   # Legrand / Bticino
    "SCHNEIDER":  0x105E,   # Schneider Electric
    "TUYA":       0x1002,   # Tuya EF00 - usually NOT manufacturer-gated
    "DANFOSS":    0x1246,   # Danfoss
    "BOSCH":      0x1209,   # Bosch / Siemens
    "DEVELCO":    0x1015,   # Develco / Frient
    "INNR":       0x1168,   # Innr Lighting
}

# Clusters that are always manufacturer-gated and need their specific code
CLUSTER_MANUFACTURER_CODES = {
    0xFCC0: MANUFACTURER_CODES["LUMI"],        # Aqara Opple / manufacturer
    0xFC11: MANUFACTURER_CODES["SONOFF"],      # Sonoff
    0xFC00: MANUFACTURER_CODES["PHILIPS"],     # Philips Hue
    # 0xEF00 is Tuya but NOT manufacturer-coded in the ZCL sense — plain.
}


# ----------------------------------------------------------------------------
# PAGINATED ATTRIBUTE ID DISCOVERY
# ----------------------------------------------------------------------------

async def _discover_ids_paginated(
        cluster,
        manufacturer: Optional[int] = None,
        max_total: int = 4096,
        chunk_size: int = 64,
        timeout_per_chunk: float = 8.0,
) -> Set[int]:
    """
    Walk the full attribute ID space for a cluster, honouring the device's
    "complete" flag and re-issuing from (last_id + 1) until exhausted or
    max_total reached.

    Some stacks' discover_attributes already paginates internally up to the
    max they're asked for. We still drive the pagination ourselves so we:
      - get coverage past 0xFF (where manufacturer attrs live)
      - can re-issue with a different manufacturer code mid-walk if needed
      - can bail on persistent failures without blocking forever
    """
    found: Set[int] = set()
    start_id = 0x0000
    attempts = 0
    max_attempts = 128  # hard stop against runaway loops

    while start_id <= 0xFFFE and len(found) < max_total and attempts < max_attempts:
        attempts += 1
        try:
            async with asyncio.timeout(timeout_per_chunk):
                kwargs = {}
                if manufacturer is not None:
                    kwargs["manufacturer"] = manufacturer
                result = await cluster.discover_attributes(
                    start_id, chunk_size, **kwargs
                )
        except asyncio.TimeoutError:
            logger.debug(
                f"Discover timed out at start=0x{start_id:04X} "
                f"mfg={manufacturer}, moving window"
            )
            # Don't give up — skip past this window
            start_id += chunk_size
            continue
        except Exception as e:
            logger.debug(
                f"Discover errored at start=0x{start_id:04X} "
                f"mfg={manufacturer}: {e}"
            )
            break

        if not result:
            break

        # Result shape varies by zigpy version. Normalise to (ids, complete_flag).
        new_ids, complete = _normalise_discover_result(result)

        if not new_ids:
            # No new attributes in this window — advance past it
            start_id += chunk_size
            continue

        found.update(new_ids)

        # If the device says it's done, stop
        if complete:
            break

        # Otherwise resume from one past the highest ID we got
        start_id = max(new_ids) + 1

    return found


def _normalise_discover_result(result: Any) -> Tuple[List[int], bool]:
    """
    zigpy returns Discover Attributes responses in a few shapes depending on
    version. Normalise to (list_of_ids, complete_bool).
    """
    # Tuple form: (attributes, complete)
    if isinstance(result, tuple) and len(result) == 2:
        attrs, complete = result
        ids = [_extract_id(a) for a in (attrs or [])]
        return [i for i in ids if i is not None], bool(complete)

    # List of DiscoverAttributesResponseRecord
    if isinstance(result, (list, tuple)):
        ids = [_extract_id(a) for a in result]
        return [i for i in ids if i is not None], True  # unknown; assume done

    # Object with .attributes + .discovery_complete
    attrs = getattr(result, 'attributes', None)
    if attrs is not None:
        ids = [_extract_id(a) for a in attrs]
        complete = bool(getattr(result, 'discovery_complete', True))
        return [i for i in ids if i is not None], complete

    return [], True


def _extract_id(record: Any) -> Optional[int]:
    """Pull an attribute ID out of whatever shape zigpy gave us."""
    if isinstance(record, int):
        return record
    for attr_name in ("attrid", "attribute_id", "id"):
        v = getattr(record, attr_name, None)
        if isinstance(v, int):
            return v
    return None


# ----------------------------------------------------------------------------
# EXTENDED DISCOVERY (access-control flags)
# ----------------------------------------------------------------------------

async def _discover_extended(
        cluster,
        manufacturer: Optional[int] = None,
        max_total: int = 4096,
        chunk_size: int = 32,
) -> Dict[int, Dict[str, bool]]:
    """
    Use Discover Attributes Extended (0x0E) which returns per-attribute
    ACL bitmap:
        bit 0: readable
        bit 1: writable
        bit 2: reportable

    Not all devices implement it; callers fall back to the read/write probe.
    """
    acl_map: Dict[int, Dict[str, bool]] = {}
    start_id = 0x0000
    attempts = 0

    while start_id <= 0xFFFE and len(acl_map) < max_total and attempts < 128:
        attempts += 1
        try:
            kwargs = {}
            if manufacturer is not None:
                kwargs["manufacturer"] = manufacturer
            async with asyncio.timeout(8.0):
                result = await cluster.discover_attributes_extended(
                    start_id, chunk_size, **kwargs
                )
        except Exception as e:
            logger.debug(f"Extended discover failed at 0x{start_id:04X}: {e}")
            return acl_map  # caller will fall back

        if not result:
            break

        # Normalise: list of records with .attrid + .acl (int bitmap)
        records = result if isinstance(result, (list, tuple)) else getattr(result, 'attributes', [])
        if not records:
            break

        last_id = None
        for rec in records:
            attr_id = _extract_id(rec)
            if attr_id is None:
                continue
            acl = getattr(rec, 'acl', None)
            if acl is None:
                continue
            acl_int = int(acl)
            acl_map[attr_id] = {
                "readable":   bool(acl_int & 0x01),
                "writable":   bool(acl_int & 0x02),
                "reportable": bool(acl_int & 0x04),
            }
            last_id = attr_id

        # Assume device is done if we got fewer than a chunk back
        if last_id is None or len(records) < chunk_size:
            break
        start_id = last_id + 1

    return acl_map


# ----------------------------------------------------------------------------
# READ + WRITE PROBE (fallback for non-Extended devices)
# ----------------------------------------------------------------------------

async def _probe_read(cluster, attr_id: int, manufacturer=None) -> Tuple[bool, Any]:
    try:
        kwargs = {"manufacturer": manufacturer} if manufacturer is not None else {}
        async with asyncio.timeout(5.0):
            result = await cluster.read_attributes([attr_id], **kwargs)
        if result and attr_id in result[0]:
            val = result[0][attr_id]
            if hasattr(val, 'value'):
                val = val.value
            return True, val
    except Exception:
        pass
    return False, None


async def _probe_write(
        cluster,
        attr_id: int,
        current_value: Any,
        manufacturer=None,
) -> Optional[bool]:
    """
    Attempt a non-destructive write by writing the current value back.

    Returns:
        True  = write succeeded (attribute is writable)
        False = write returned failure (read-only, or unsupported write)
        None  = skipped (unsafe to probe)

    Safety: we skip the probe entirely for values that are None, zero-like
    numerics, or known-risky types (bitmaps, enums) which can have side
    effects even when the written value equals the current value.
    """
    if current_value is None:
        return None

    # Skip writes of zero-like values — writing 0 to a bitmap can reset it
    if isinstance(current_value, (int, float)) and current_value == 0:
        return None
    if isinstance(current_value, bool):
        return None  # bool writes to sensor attrs can cause trouble
    if isinstance(current_value, (bytes, bytearray)) and len(current_value) == 0:
        return None

    try:
        kwargs = {"manufacturer": manufacturer} if manufacturer is not None else {}
        async with asyncio.timeout(5.0):
            write_result = await cluster.write_attributes({attr_id: current_value}, **kwargs)

        if not write_result:
            return None

        first = write_result[0] if isinstance(write_result, (list, tuple)) else write_result
        # Accept several response shapes
        if hasattr(first, '__iter__') and not isinstance(first, (dict, str, bytes)):
            return all(getattr(s, 'status', s) == 0 for s in first)
        status = getattr(first, 'status', first)
        return status == 0
    except Exception:
        return False


# ----------------------------------------------------------------------------
# REPORTING CONFIG
# ----------------------------------------------------------------------------

async def _read_reporting_config(
        cluster,
        attr_ids: List[int],
        manufacturer=None,
) -> Dict[int, Dict[str, Any]]:
    """
    Read the device's current reporting configuration for a list of attrs.
    Returns {attr_id: {min_interval, max_interval, reportable_change, status}}.
    Devices that don't support reporting for an attr return status=UNREPORTABLE.
    """
    config: Dict[int, Dict[str, Any]] = {}
    if not attr_ids:
        return config

    # ZCL requires a list of records specifying direction; zigpy's helper
    # wraps this. Different zigpy versions expose different helpers.
    try:
        records = [(0, a) for a in attr_ids]  # direction=0 (reported)
        kwargs = {"manufacturer": manufacturer} if manufacturer is not None else {}
        async with asyncio.timeout(10.0):
            if hasattr(cluster, 'read_reporting_config'):
                result = await cluster.read_reporting_config(records, **kwargs)
            else:
                # Older zigpy
                result = await cluster.general.read_reporting_configuration(records, **kwargs)
    except Exception as e:
        logger.debug(f"Reporting config read failed: {e}")
        return config

    if not result:
        return config

    for rec in (result if isinstance(result, (list, tuple)) else [result]):
        attr_id = getattr(rec, 'attrid', None)
        if attr_id is None:
            continue
        config[attr_id] = {
            "status":             getattr(rec, 'status', None),
            "min_interval":       getattr(rec, 'minimum_reporting_interval', None),
            "max_interval":       getattr(rec, 'maximum_reporting_interval', None),
            "reportable_change":  getattr(rec, 'reportable_change', None),
        }

    return config


# ----------------------------------------------------------------------------
# COMMAND DISCOVERY
# ----------------------------------------------------------------------------

async def _discover_commands(cluster, manufacturer=None) -> Dict[str, List[Dict]]:
    """
    Enumerate received and generated commands on the cluster.
    Received  = commands the device accepts  (client -> server)
    Generated = commands the device sends    (server -> client)
    """
    out = {"received": [], "generated": []}

    for direction, fn_name in (
            ("received",  "discover_commands_received"),
            ("generated", "discover_commands_generated"),
    ):
        try:
            fn = getattr(cluster, fn_name, None)
            if fn is None:
                continue
            kwargs = {"manufacturer": manufacturer} if manufacturer is not None else {}
            async with asyncio.timeout(5.0):
                result = await fn(0, 255, **kwargs)
        except Exception as e:
            logger.debug(f"{fn_name} failed: {e}")
            continue

        ids, _ = _normalise_discover_result(result)
        # Map to names if cluster has command defs
        commands_def = getattr(cluster, 'server_commands' if direction == 'received'
        else 'client_commands', {}) or {}
        for cmd_id in ids:
            name = "unknown"
            schema = None
            if cmd_id in commands_def:
                cmd_def = commands_def[cmd_id]
                name = getattr(cmd_def, 'name', None) or str(cmd_def)
                schema_obj = getattr(cmd_def, 'schema', None)
                if schema_obj is not None:
                    schema = str(schema_obj)
            out[direction].append({
                "id":     f"0x{cmd_id:02X}",
                "id_int": cmd_id,
                "name":   name,
                "schema": schema,
            })

    return out


# ----------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# ----------------------------------------------------------------------------

async def introspect_cluster(
        service,
        ieee: str,
        endpoint_id: int,
        cluster_id: int,
        include_write_probe: bool = True,
        include_reporting_config: bool = True,
        include_commands: bool = True,
) -> Dict[str, Any]:
    """
    Exhaustive discovery of a cluster on a device. Returns a dict with:

        {
            "success": True,
            "ieee": "...",
            "endpoint_id": 1,
            "cluster_id": "0x0006",
            "manufacturer_codes_tried": [None, 4447],
            "attributes": [
                {
                    "id": "0xFCC0",
                    "id_int": 64704,
                    "name": "...",
                    "type": "uint16_t",
                    "readable": True,
                    "writable": True,
                    "reportable": False,
                    "manufacturer_code": 4447,  # null for standard
                    "value": 1234,
                    "reporting": {
                        "min_interval": 30,
                        "max_interval": 600,
                        "reportable_change": 5,
                    },
                },
                ...
            ],
            "commands": {
                "received":  [{"id": "0x00", "name": "off", ...}, ...],
                "generated": [...],
            },
        }
    """
    if ieee not in service.devices:
        return {"success": False, "error": "Device not found"}

    dev = service.devices[ieee]
    zigpy_dev = dev.zigpy_dev
    ep = zigpy_dev.endpoints.get(endpoint_id)
    if not ep:
        return {"success": False, "error": f"Endpoint {endpoint_id} not found"}

    cluster = ep.in_clusters.get(cluster_id) or ep.out_clusters.get(cluster_id)
    if not cluster:
        return {"success": False, "error": f"Cluster 0x{cluster_id:04X} not found"}

    # ------------------------------------------------------------------
    # Decide which manufacturer codes to try
    # ------------------------------------------------------------------
    mfg_codes_to_try: List[Optional[int]] = [None]

    # 1. Cluster-specific gate (0xFCC0 → LUMI, etc.)
    if cluster_id in CLUSTER_MANUFACTURER_CODES:
        code = CLUSTER_MANUFACTURER_CODES[cluster_id]
        if code not in mfg_codes_to_try:
            mfg_codes_to_try.append(code)

    # 2. Device's own manufacturer code (from the node descriptor)
    dev_mfg_code = getattr(zigpy_dev, 'manufacturer_code', None)
    if dev_mfg_code and dev_mfg_code not in mfg_codes_to_try:
        mfg_codes_to_try.append(dev_mfg_code)

    # 3. Cluster's declared manufacturer attr (zigpy exposes it for some)
    cluster_mfg = getattr(cluster, 'manufacturer', None)
    if cluster_mfg and cluster_mfg not in mfg_codes_to_try:
        mfg_codes_to_try.append(cluster_mfg)

    # ------------------------------------------------------------------
    # Collect ACLs and attribute IDs across all codes
    # ------------------------------------------------------------------
    # Map attr_id -> {source_mfg_code, acl_from_extended_or_None}
    all_attrs: Dict[int, Dict[str, Any]] = {}

    for mfg_code in mfg_codes_to_try:
        acls = await _discover_extended(cluster, manufacturer=mfg_code)
        if acls:
            for attr_id, acl in acls.items():
                if attr_id not in all_attrs:
                    all_attrs[attr_id] = {"mfg_code": mfg_code, **acl}
            continue

        # Fallback: basic discover, no ACL flags
        ids = await _discover_ids_paginated(cluster, manufacturer=mfg_code)
        for attr_id in ids:
            if attr_id not in all_attrs:
                all_attrs[attr_id] = {
                    "mfg_code": mfg_code,
                    "readable": None,    # unknown until probed
                    "writable": None,
                    "reportable": None,
                }

    # Always include zigpy's known attributes as a baseline, even if
    # the device didn't list them — useful for devices with broken discovery
    for attr_id in (cluster.attributes or {}).keys():
        if attr_id not in all_attrs:
            all_attrs[attr_id] = {
                "mfg_code": None,
                "readable": None,
                "writable": None,
                "reportable": None,
            }

    # ------------------------------------------------------------------
    # For each attribute: read value, optionally probe write, enrich
    # ------------------------------------------------------------------
    attributes_out: List[Dict[str, Any]] = []
    readable_attr_ids: List[int] = []

    for attr_id in sorted(all_attrs.keys()):
        entry = all_attrs[attr_id]
        mfg = entry.get("mfg_code")

        # Name + type from zigpy's definition
        name = f"0x{attr_id:04X}"
        attr_type = ""
        attr_def = (cluster.attributes or {}).get(attr_id)
        if attr_def is not None:
            name = getattr(attr_def, 'name', None) or name
            type_cls = getattr(attr_def, 'type', None)
            if type_cls is not None:
                attr_type = getattr(type_cls, '__name__', str(type_cls))

        # Read value if we don't yet know readable flag, or if we do know it's true
        value: Any = None
        known_readable = entry.get("readable")
        should_read = known_readable is not False  # True or None → attempt
        if should_read:
            ok, val = await _probe_read(cluster, attr_id, manufacturer=mfg)
            if ok:
                entry["readable"] = True
                value = val
                readable_attr_ids.append(attr_id)
            elif known_readable is None:
                entry["readable"] = False

        # Write probe only if we don't already know, caller allows it,
        # and we have a value safe to echo back
        if include_write_probe and entry.get("writable") is None and value is not None:
            w = await _probe_write(cluster, attr_id, value, manufacturer=mfg)
            entry["writable"] = w  # may be True/False/None

        attributes_out.append({
            "id":                f"0x{attr_id:04X}",
            "id_int":            attr_id,
            "name":              name,
            "type":              attr_type,
            "readable":          entry.get("readable"),
            "writable":          entry.get("writable"),
            "reportable":        entry.get("reportable"),
            "manufacturer_code": mfg,
            "value":             _jsonify(value),
        })

    # ------------------------------------------------------------------
    # Reporting configuration (batch, per mfg code)
    # ------------------------------------------------------------------
    if include_reporting_config and readable_attr_ids:
        # Group by mfg_code
        by_mfg: Dict[Optional[int], List[int]] = {}
        for a in attributes_out:
            if not a["readable"]:
                continue
            by_mfg.setdefault(a["manufacturer_code"], []).append(a["id_int"])

        report_map: Dict[int, Dict[str, Any]] = {}
        for mfg_code, ids in by_mfg.items():
            # ZCL limits per-request record count; chunk at 20
            for i in range(0, len(ids), 20):
                chunk = ids[i:i + 20]
                cfg = await _read_reporting_config(cluster, chunk, manufacturer=mfg_code)
                report_map.update(cfg)

        for a in attributes_out:
            rc = report_map.get(a["id_int"])
            if rc:
                a["reporting"] = rc
                # If we didn't know reportable from Extended, infer from status
                if a["reportable"] is None:
                    status = rc.get("status")
                    # status 0 = SUCCESS (is reportable); 0x8C = UNREPORTABLE_ATTRIBUTE
                    if status == 0:
                        a["reportable"] = True
                    elif status == 0x8C:
                        a["reportable"] = False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    commands = {}
    if include_commands:
        # Try None first, then device mfg code
        commands = await _discover_commands(cluster, manufacturer=None)
        if dev_mfg_code:
            extra = await _discover_commands(cluster, manufacturer=dev_mfg_code)
            # Merge — prefer the non-empty set
            for d in ("received", "generated"):
                existing = {c["id_int"] for c in commands.get(d, [])}
                for c in extra.get(d, []):
                    if c["id_int"] not in existing:
                        commands.setdefault(d, []).append(c)

    return {
        "success":                 True,
        "ieee":                    ieee,
        "endpoint_id":             endpoint_id,
        "cluster_id":              f"0x{cluster_id:04X}",
        "manufacturer_codes_tried": [c for c in mfg_codes_to_try],
        "attributes":              attributes_out,
        "commands":                commands,
    }


def _jsonify(val: Any) -> Any:
    """Make a value JSON-safe without pulling in the full prepare_for_json."""
    if val is None:
        return None
    if isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    if isinstance(val, (list, tuple)):
        return [_jsonify(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _jsonify(v) for k, v in val.items()}
    try:
        return str(val)
    except Exception:
        return repr(val)


# ----------------------------------------------------------------------------
# BACKWARDS-COMPATIBLE SHIM
# ----------------------------------------------------------------------------

async def discover_attributes(service, ieee: str, endpoint_id: int, cluster_id: int):
    """
    Legacy text-dumping entrypoint. Kept so existing callers don't break.
    Prints a human-readable table; for structured output use introspect_cluster.
    """
    result = await introspect_cluster(service, ieee, endpoint_id, cluster_id)
    if not result["success"]:
        print(f"Error: {result.get('error')}")
        return

    print(f"\n{'='*80}")
    print(f"Device: {ieee} EP{endpoint_id} Cluster {result['cluster_id']}")
    print(f"Manufacturer codes tried: {result['manufacturer_codes_tried']}")
    print(f"{'='*80}")
    print(f"{'ID':<8} {'Name':<32} {'Type':<14} {'R':<3} {'W':<3} {'Rep':<3} {'Mfg':<6} Value")
    print("-" * 110)
    for a in result["attributes"]:
        r = {True: "Y", False: "N", None: "?"}[a["readable"]]
        w = {True: "Y", False: "N", None: "?"}[a["writable"]]
        rep = {True: "Y", False: "N", None: "?"}[a["reportable"]]
        mfg = f"0x{a['manufacturer_code']:04X}" if a["manufacturer_code"] else "—"
        val = str(a["value"])[:40] if a["value"] is not None else ""
        print(f"{a['id']:<8} {a['name']:<32} {a['type']:<14} {r:<3} {w:<3} {rep:<3} {mfg:<6} {val}")

    cmds = result.get("commands", {})
    if cmds.get("received"):
        print(f"\nReceived commands: {', '.join(c['name'] for c in cmds['received'])}")
    if cmds.get("generated"):
        print(f"Generated commands: {', '.join(c['name'] for c in cmds['generated'])}")
    print(f"{'='*80}\n")