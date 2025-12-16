import json
import os
import shutil
import argparse
import sys

# Defaults
DEFAULT_DEVICE_REGISTRY = ".storage/core.device_registry"
DEFAULT_DEVICE_BACKUP = ".storage/core.device_registry.bak"
DEFAULT_ENTITY_REGISTRY = ".storage/core.entity_registry"
DEFAULT_ENTITY_BACKUP = ".storage/core.entity_registry.bak"
DEFAULT_LIST = "zombies.txt"

def normalize_ieee(ieee_string):
    """
    Normalizes IEEE addresses to ensure matching works regardless of format.
    Removes colons, dashes, and converts to lowercase.
    Example: '00:15:8D:...' -> '00158d...'
    """
    if not isinstance(ieee_string, str):
        return str(ieee_string)
    # Strip common separators and whitespace
    return ieee_string.replace(":", "").replace("-", "").lower().strip()

def load_zombie_list(file_path):
    """
    Reads the external list of IEEE addresses.
    ROBUST MODE: Handles files that might be a single line separated by
    commas, literal '\n', or user-specified '/n'.
    """
    if not os.path.exists(file_path):
        print(f"Error: Zombie list file '{file_path}' not found.")
        sys.exit(1)

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Sanitize the content
    content = content.replace('\\n', '\n')  # Replace literal backslash-n
    content = content.replace('/n', '\n')   # Replace forward slash-n (common typo/format)
    content = content.replace(',', '\n')    # Replace commas (CSV style)

    # 2. Split into lines and normalise
    # We use a set comprehension to automatically deduplicate
    zombies = {normalize_ieee(line) for line in content.splitlines() if line.strip()}

    print(f"Loaded {len(zombies)} unique IEEE addresses to target.")
    return zombies

def clean_registries(device_reg_path, device_backup_path, entity_reg_path, entity_backup_path, zombie_list_path, dry_run):
    print(f"--- HA Registry Cleaner ---")
    print(f"Device Registry: {device_reg_path}")
    print(f"Entity Registry: {entity_reg_path}")
    print(f"Reading List:    {zombie_list_path}")
    print(f"Mode:            {'DRY RUN (No changes will be saved)' if dry_run else 'LIVE (Changes will be applied)'}")
    print("-" * 30)

    # 1. Validation
    if not os.path.exists(device_reg_path):
        print(f"Error: Device registry '{device_reg_path}' not found!")
        return
    if not os.path.exists(entity_reg_path):
        print(f"Error: Entity registry '{entity_reg_path}' not found!")
        return

    # 2. Load Data
    try:
        with open(device_reg_path, 'r') as f:
            device_data = json.load(f)
        with open(entity_reg_path, 'r') as f:
            entity_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
        return

    devices = device_data['data']['devices']
    entities = entity_data['data']['entities']

    target_ieees = load_zombie_list(zombie_list_path)
    if not target_ieees:
        print("Warning: No valid IEEE addresses found in the list file. Exiting.")
        return

    # 3. Identify Zombie Devices & Collect IDs
    zombie_device_ids = set()
    devices_to_keep = []

    print(f"\nScanning {len(devices)} devices...")
    for d in devices:
        is_zombie = False
        device_name = d.get('name_by_user') or d.get('name') or "Unknown Device"

        # Check all identifiers for this device (e.g., [['zha', '00:11:22...'], ['mqtt', '...']])
        for identifier in d.get('identifiers', []):
            if len(identifier) > 1:
                id_value = normalize_ieee(identifier[1])
                if id_value in target_ieees:
                    is_zombie = True
                    # IMPORTANT: We store the HA internal 'id' to find entities later
                    zombie_device_ids.add(d['id'])
                    print(f" [DEVICE DELETE] {device_name} (ID: {identifier[1]})")
                    break

        if not is_zombie:
            devices_to_keep.append(d)

    # 4. Identify Orphaned Entities (attached to zombie devices)
    entities_to_keep = []
    removed_entities_count = 0

    if zombie_device_ids:
        print(f"\nScanning {len(entities)} entities for association with deleted devices...")
        for e in entities:
            # Check if this entity's parent device_id is in our deletion list
            if e.get('device_id') in zombie_device_ids:
                print(f" [ENTITY DELETE] {e.get('entity_id')} (linked to zombie device)")
                removed_entities_count += 1
            else:
                entities_to_keep.append(e)
    else:
        entities_to_keep = entities

    # 5. Summary and Execution
    removed_devices_count = len(devices) - len(devices_to_keep)

    print("-" * 30)
    print(f"Devices to remove:  {removed_devices_count}")
    print(f"Entities to remove: {removed_entities_count}")

    if removed_devices_count == 0:
        print("No matching devices found. Nothing to do.")
        return

    if dry_run:
        print("\n[DRY RUN COMPLETE] No files were modified.")
        print("To actually delete files, run with: --wet-run")
    else:
        # Backup and Save Device Registry
        shutil.copy(device_reg_path, device_backup_path)
        print(f"\nDevice Backup created at {device_backup_path}")

        device_data['data']['devices'] = devices_to_keep
        with open(device_reg_path, 'w') as f:
            json.dump(device_data, f, indent=4)
        print("Device Registry updated.")

        # Backup and Save Entity Registry
        shutil.copy(entity_reg_path, entity_backup_path)
        print(f"Entity Backup created at {entity_backup_path}")

        entity_data['data']['entities'] = entities_to_keep
        with open(entity_reg_path, 'w') as f:
            json.dump(entity_data, f, indent=4)
        print("Entity Registry updated.")

        print("\nSUCCESS! Restart Home Assistant now.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean Home Assistant Device & Entity Registry based on IEEE list.")
    parser.add_argument("--registry", default=DEFAULT_DEVICE_REGISTRY, help="Path to HA device registry")
    parser.add_argument("--entity-registry", default=DEFAULT_ENTITY_REGISTRY, help="Path to HA entity registry")
    parser.add_argument("--backup", default=DEFAULT_DEVICE_BACKUP, help="Path for device backup")
    parser.add_argument("--entity-backup", default=DEFAULT_ENTITY_BACKUP, help="Path for entity backup")
    parser.add_argument("--list", default=DEFAULT_LIST, help="Path to text file with IEEE addresses")
    parser.add_argument("--wet-run", action="store_true", help="Actually execute the deletion (Default is Dry Run)")

    args = parser.parse_args()

    clean_registries(
        args.registry,
        args.backup,
        args.entity_registry,
        args.entity_backup,
        args.list,
        not args.wet_run
    )