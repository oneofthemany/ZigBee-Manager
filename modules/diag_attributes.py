"""
Diagnostic: Discover attribute access control on a Zigbee device cluster.
Run inside the container: python3 -c "import asyncio; asyncio.run(main())"
Or add as a temporary API endpoint.

Usage from within the running app (e.g., via editor or shell):
    from diag_attributes import discover_attributes
    await discover_attributes(service, "00:1e:5e:09:02:a4:49:4b", 5, 0x0201)
    await discover_attributes(service, "00:1e:5e:09:02:a3:e4:c1", 9, 0x0201)
"""
import logging

logger = logging.getLogger("diag.attributes")


async def discover_attributes(service, ieee: str, endpoint_id: int, cluster_id: int):
    """
    Query a device's cluster for all supported attributes and their access control.
    Uses ZCL Discover Attributes Extended (cmd 0x0E) if supported,
    falls back to Discover Attributes (cmd 0x0C) + individual write tests.
    """
    if ieee not in service.devices:
        print(f"Device {ieee} not found")
        return

    dev = service.devices[ieee]
    zigpy_dev = dev.zigpy_dev

    ep = zigpy_dev.endpoints.get(endpoint_id)
    if not ep:
        print(f"Endpoint {endpoint_id} not found")
        return

    cluster = ep.in_clusters.get(cluster_id) or ep.out_clusters.get(cluster_id)
    if not cluster:
        print(f"Cluster 0x{cluster_id:04X} not found on EP{endpoint_id}")
        return

    print(f"\n{'='*70}")
    print(f"Device: {ieee} EP{endpoint_id} Cluster 0x{cluster_id:04X}")
    print(f"{'='*70}")

    # Method 1: Try Discover Attributes Extended (returns access control)
    try:
        import asyncio
        async with asyncio.timeout(10.0):
            result = await cluster.discover_attributes_extended(0, 255)
        print(f"\n--- Discover Attributes Extended ---")
        print(f"{'ID':<10} {'Name':<35} {'Access'}")
        print("-" * 60)
        for attr_info in result:
            attr_id = attr_info.attrid if hasattr(attr_info, 'attrid') else attr_info
            access = attr_info.acl if hasattr(attr_info, 'acl') else '?'

            # Look up name from cluster definition
            name = "unknown"
            if attr_id in cluster.attributes:
                attr_def = cluster.attributes[attr_id]
                name = attr_def.name if hasattr(attr_def, 'name') else str(attr_def)

            print(f"0x{attr_id:04X}    {name:<35} {access}")
        return
    except Exception as e:
        print(f"Discover Attributes Extended not supported: {e}")

    # Method 2: Discover Attributes (basic - just IDs)
    try:
        import asyncio
        async with asyncio.timeout(10.0):
            result = await cluster.discover_attributes(0, 255)

        print(f"\n--- Discover Attributes (basic) ---")
        print(f"Testing read/write on each attribute...\n")
        print(f"{'ID':<10} {'Name':<35} {'Read':<8} {'Write':<8} {'Value'}")
        print("-" * 80)

        for attr_id in sorted(result):
            name = "unknown"
            if attr_id in cluster.attributes:
                attr_def = cluster.attributes[attr_id]
                name = attr_def.name if hasattr(attr_def, 'name') else str(attr_def)

            # Test read
            readable = False
            value = None
            try:
                async with asyncio.timeout(5.0):
                    read_result = await cluster.read_attributes([attr_id])
                if read_result and attr_id in read_result[0]:
                    val = read_result[0][attr_id]
                    if hasattr(val, 'value'):
                        val = val.value
                    value = val
                    readable = True
            except:
                pass

            # Test write (write current value back - non-destructive)
            writable = "?"
            if readable and value is not None:
                try:
                    async with asyncio.timeout(5.0):
                        write_result = await cluster.write_attributes(
                            {attr_id: value}
                        )
                    # Check response
                    if write_result and len(write_result) > 0:
                        status = write_result[0]
                        if hasattr(status, '__iter__'):
                            # List of WriteAttributesStatusRecord
                            writable = "YES" if all(
                                getattr(s, 'status', s) == 0 for s in status
                            ) else "NO"
                        elif status == 0 or (hasattr(status, 'status') and status.status == 0):
                            writable = "YES"
                        else:
                            writable = "NO"
                except Exception as e:
                    writable = f"ERR:{e}"

            r = "YES" if readable else "NO"
            print(f"0x{attr_id:04X}    {name:<35} {r:<8} {writable:<8} {value}")

    except Exception as e:
        print(f"Discover Attributes failed: {e}")

    print(f"\n{'='*70}\n")