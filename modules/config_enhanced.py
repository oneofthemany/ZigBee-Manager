"""
Enhanced EZSP Configuration
============================
Optimal configuration settings based on ZHA best practices and community experience.

This module provides:
- Production-ready EZSP configuration
- Dynamic adjustment based on network size
- Proper handling of firmware-specific limitations
- Validation and error handling
"""
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("config_enhanced")


class EZSPConfig:
    """
    Enhanced EZSP configuration manager.

    Based on extensive testing and ZHA community experience:
    - Maximizes stability for large networks
    - Balances memory usage
    - Handles firmware variations
    """

    # Base configuration - safe for all devices
    BASE_CONFIG = {
        'CONFIG_SOURCE_ROUTE_TABLE_SIZE': 200,  # Essential for mesh routing
        'CONFIG_MAX_HOPS': 30,  # Network depth
        'CONFIG_INDIRECT_TRANSMISSION_TIMEOUT': 7680,  # For sleeping devices
    }

    # Standard configuration - works with most coordinators
    STANDARD_CONFIG = {
        **BASE_CONFIG,
        'CONFIG_PACKET_BUFFER_COUNT': 254,  # Max safe value (255 fails on some firmware)
        'CONFIG_NEIGHBOR_TABLE_SIZE': 26,  # Standard max neighbors
        'CONFIG_ADDRESS_TABLE_SIZE': 16,  # Device address tracking
        'CONFIG_MULTICAST_TABLE_SIZE': 16,  # Group messaging
        'CONFIG_BINDING_TABLE_SIZE': 32,  # Binding support
        'CONFIG_APS_UNICAST_MESSAGE_COUNT': 20,  # Conservative for stability
        'CONFIG_ROUTE_TABLE_SIZE': 16,  # Routing table size
    }

    # Pro configuration - for high-end coordinators with more RAM
    PRO_CONFIG = {
        **BASE_CONFIG,
        'CONFIG_PACKET_BUFFER_COUNT': 254,
        'CONFIG_NEIGHBOR_TABLE_SIZE': 26,
        'CONFIG_ADDRESS_TABLE_SIZE': 32,  # More addresses
        'CONFIG_MULTICAST_TABLE_SIZE': 16,
        'CONFIG_BINDING_TABLE_SIZE': 32,
        'CONFIG_APS_UNICAST_MESSAGE_COUNT': 30,  # More concurrent messages
        'CONFIG_ROUTE_TABLE_SIZE': 16,
        'CONFIG_MAX_END_DEVICE_CHILDREN': 32,  # Support more end devices
    }

    # Large network configuration - for 50+ devices
    LARGE_NETWORK_CONFIG = {
        **BASE_CONFIG,
        'CONFIG_PACKET_BUFFER_COUNT': 254,
        'CONFIG_NEIGHBOR_TABLE_SIZE': 26,
        'CONFIG_ADDRESS_TABLE_SIZE': 32,
        'CONFIG_MULTICAST_TABLE_SIZE': 16,
        'CONFIG_BINDING_TABLE_SIZE': 32,
        'CONFIG_APS_UNICAST_MESSAGE_COUNT': 32,  # Higher for busy networks
        'CONFIG_ROUTE_TABLE_SIZE': 32,  # More routes
        'CONFIG_SOURCE_ROUTE_TABLE_SIZE': 255,  # Maximum source routes
    }

    # EZSPConfig class
    SENSOR_OPTIMIZED_CONFIG = {
        **BASE_CONFIG,
        'CONFIG_PACKET_BUFFER_COUNT': 254,
        'CONFIG_NEIGHBOR_TABLE_SIZE': 26,
        'CONFIG_ADDRESS_TABLE_SIZE': 32,
        'CONFIG_MULTICAST_TABLE_SIZE': 16,
        'CONFIG_APS_UNICAST_MESSAGE_COUNT': 32,
        'CONFIG_ROUTE_TABLE_SIZE': 32,
        'CONFIG_SOURCE_ROUTE_TABLE_SIZE': 255,

        # Sensor-specific optimizations
        'CONFIG_FRAGMENT_WINDOW_SIZE': 8,
        'CONFIG_FRAGMENT_DELAY_MS': 50,
        'CONFIG_BROADCAST_TABLE_SIZE': 15,
        'CONFIG_RETRY_QUEUE_SIZE': 16,
    }

    @staticmethod
    def get_config(profile: str = "standard", device_count: Optional[int] = None) -> Dict[str, int]:
        """
        Get configuration based on profile and network size.

        Args:
            profile: Configuration profile ("standard", "pro", "large")
            device_count: Number of devices in network (for auto-selection)

        Returns:
            Configuration dictionary
        """
        # Auto-select based on device count
        if device_count is not None:
            if device_count > 50:
                profile = "large"
            elif device_count > 30:
                profile = "pro"

        if profile == "pro":
            return EZSPConfig.PRO_CONFIG.copy()
        elif profile == "large":
            return EZSPConfig.LARGE_NETWORK_CONFIG.copy()
        else:
            return EZSPConfig.STANDARD_CONFIG.copy()

    @staticmethod
    def validate_config(config: Dict[str, int]) -> Dict[str, int]:
        """
        Validate and adjust configuration values.

        Some values have dependencies or firmware limitations:
        - CONFIG_PACKET_BUFFER_COUNT: Max 254 (255 fails on many)
        - CONFIG_SOURCE_ROUTE_TABLE_SIZE: Must be >= 20
        - CONFIG_APS_UNICAST_MESSAGE_COUNT: Affects memory usage

        Args:
            config: Configuration to validate

        Returns:
            Validated configuration
        """
        validated = config.copy()

        # Packet buffer count: Must be <= 254
        if validated.get('CONFIG_PACKET_BUFFER_COUNT', 0) > 254:
            logger.warning("CONFIG_PACKET_BUFFER_COUNT > 254, setting to 254")
            validated['CONFIG_PACKET_BUFFER_COUNT'] = 254

        # Source route table: Must be >= 20 for validation
        if validated.get('CONFIG_SOURCE_ROUTE_TABLE_SIZE', 0) < 20:
            logger.warning("CONFIG_SOURCE_ROUTE_TABLE_SIZE < 20, setting to 32")
            validated['CONFIG_SOURCE_ROUTE_TABLE_SIZE'] = 32

        # APS unicast message count: Reasonable limits
        aps_count = validated.get('CONFIG_APS_UNICAST_MESSAGE_COUNT', 20)
        if aps_count < 10:
            logger.warning("CONFIG_APS_UNICAST_MESSAGE_COUNT too low, setting to 10")
            validated['CONFIG_APS_UNICAST_MESSAGE_COUNT'] = 10
        elif aps_count > 64:
            logger.warning("CONFIG_APS_UNICAST_MESSAGE_COUNT too high, setting to 64")
            validated['CONFIG_APS_UNICAST_MESSAGE_COUNT'] = 64

        return validated

    @staticmethod
    def merge_with_user_config(base_config: Dict[str, int], user_config: Dict[str, Any]) -> Dict[str, int]:
        """
        Merge user configuration with base configuration.

        Args:
            base_config: Base configuration
            user_config: User overrides (can be nested in 'ezsp_config' key)

        Returns:
            Merged configuration
        """
        # Handle nested structure (config.yaml format)
        if 'ezsp_config' in user_config:
            user_ezsp = user_config['ezsp_config']
        else:
            user_ezsp = user_config

        merged = base_config.copy()

        # Apply user overrides
        for key, value in user_ezsp.items():
            if key.startswith('CONFIG_') or key.startswith('EMBER_'):
                try:
                    merged[key] = int(value)
                    logger.info(f"User override: {key} = {value}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid value for {key}: {value}")

        return merged


class NetworkOptimizer:
    """
    Dynamic network optimization based on observed behavior.

    Adjusts configuration based on:
    - Network size
    - Device types
    - Traffic patterns
    - Error rates
    """

    def __init__(self):
        self.recommendations = {}

    def analyse_network(self, device_count: int, router_count: int,
                        end_device_count: int, bulb_count: int = 0) -> Dict[str, Any]:
        """
        analyse network and provide recommendations.

        Args:
            device_count: Total device count
            router_count: Number of router devices
            end_device_count: Number of end devices
            bulb_count: Number of bulbs (high traffic)

        Returns:
            Recommendations dictionary
        """
        recommendations = {
            'profile': 'standard',
            'adjustments': {},
            'warnings': []
        }

        # Select profile based on size
        if device_count > 50:
            recommendations['profile'] = 'large'
        elif device_count > 30:
            recommendations['profile'] = 'pro'

        # Bulbs need more packet buffers and multicast support
        if bulb_count > 10:
            recommendations['adjustments']['CONFIG_PACKET_BUFFER_COUNT'] = 254
            recommendations['adjustments']['CONFIG_MULTICAST_TABLE_SIZE'] = 16
            recommendations['warnings'].append(
                f"{bulb_count} bulbs detected - using enhanced buffer configuration"
            )

        # Many end devices need higher child limit
        if end_device_count > 20:
            recommendations['adjustments']['CONFIG_MAX_END_DEVICE_CHILDREN'] = 32
            recommendations['warnings'].append(
                f"{end_device_count} end devices - increased child limit"
            )

        # Sparse router network needs more hops
        if device_count > 20 and router_count < 5:
            recommendations['adjustments']['CONFIG_MAX_HOPS'] = 30
            recommendations['warnings'].append(
                "Few routers detected - using higher hop count"
            )

        return recommendations

    def get_optimal_config(self, device_stats: Dict[str, int],
                           user_config: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        """
        Get optimal configuration for current network.

        Args:
            device_stats: Device statistics (device_count, router_count, etc.)
            user_config: Optional user configuration overrides

        Returns:
            Optimal configuration
        """
        # analyse network
        recommendations = self.analyse_network(
            device_stats.get('total', 0),
            device_stats.get('routers', 0),
            device_stats.get('end_devices', 0),
            device_stats.get('bulbs', 0)
        )

        # Get base config for profile
        config = EZSPConfig.get_config(recommendations['profile'])

        # Apply recommended adjustments
        config.update(recommendations['adjustments'])

        # Apply user overrides if provided
        if user_config:
            config = EZSPConfig.merge_with_user_config(config, user_config)

        # Validate final configuration
        config = EZSPConfig.validate_config(config)

        # Log recommendations
        for warning in recommendations['warnings']:
            logger.info(warning)

        return config


def get_production_config(user_config: Optional[Dict[str, Any]] = None,
                          device_count: Optional[int] = None) -> Dict[str, int]:
    """
    Get production-ready EZSP configuration.

    This is the main entry point for getting configuration.

    Args:
        user_config: Optional user configuration from config.yaml
        device_count: Optional device count for auto-tuning

    Returns:
        Production configuration dictionary
    """
    # Determine profile
    profile = "standard"
    if device_count:
        if device_count > 50:
            profile = "large"
        elif device_count > 30:
            profile = "pro"

    # Get base configuration
    config = EZSPConfig.get_config(profile, device_count)

    # Merge with user config if provided
    if user_config:
        config = EZSPConfig.merge_with_user_config(config, user_config)

    # Validate
    config = EZSPConfig.validate_config(config)

    logger.info(f"Using {profile} configuration profile")
    logger.info(f"EZSP Configuration: {config}")

    return config


# Quick reference for common scenarios
SCENARIO_CONFIGS = {
    'small_home': {
        'description': '< 20 devices, mostly sensors',
        'config': EZSPConfig.STANDARD_CONFIG
    },
    'medium_home': {
        'description': '20-40 devices, mixed types',
        'config': EZSPConfig.PRO_CONFIG
    },
    'large_home': {
        'description': '> 40 devices, many bulbs',
        'config': EZSPConfig.LARGE_NETWORK_CONFIG
    },
    'bulb_heavy': {
        'description': 'Many Philips/Ikea bulbs',
        'config': {
            **EZSPConfig.PRO_CONFIG,
            'CONFIG_PACKET_BUFFER_COUNT': 254,
            'CONFIG_MULTICAST_TABLE_SIZE': 16,
            'CONFIG_APS_UNICAST_MESSAGE_COUNT': 30
        }
    }
}


def print_config_comparison():
    """Print comparison of different configuration profiles."""
    print("\n" + "=" * 80)
    print("EZSP Configuration Profiles Comparison")
    print("=" * 80)

    profiles = {
        'Standard': EZSPConfig.STANDARD_CONFIG,
        'Pro': EZSPConfig.PRO_CONFIG,
        'Large Network': EZSPConfig.LARGE_NETWORK_CONFIG
    }

    # Get all keys
    all_keys = set()
    for config in profiles.values():
        all_keys.update(config.keys())

    # Print header
    print(f"\n{'Setting':<40} {'Standard':>10} {'Pro':>10} {'Large':>10}")
    print("-" * 80)

    # Print each setting
    for key in sorted(all_keys):
        values = [str(profiles[p].get(key, '-')) for p in profiles.keys()]
        print(f"{key:<40} {values[0]:>10} {values[1]:>10} {values[2]:>10}")

    print("=" * 80 + "\n")


if __name__ == "__main__":
    # Demo/testing
    logging.basicConfig(level=logging.INFO)

    print_config_comparison()

    # Test configuration generation
    print("\nTesting configuration generation:")
    print("-" * 40)

    # Small network
    config = get_production_config(device_count=15)
    print(f"15 devices: Using config with {config['CONFIG_PACKET_BUFFER_COUNT']} buffers")

    # Medium network
    config = get_production_config(device_count=35)
    print(f"35 devices: Using config with {config['CONFIG_APS_UNICAST_MESSAGE_COUNT']} APS messages")

    # Large network
    config = get_production_config(device_count=60)
    print(f"60 devices: Using config with {config['CONFIG_SOURCE_ROUTE_TABLE_SIZE']} source routes")