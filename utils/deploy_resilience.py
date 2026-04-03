#!/usr/bin/env python3
"""
Zigbee Gateway Health Check and Deployment Tool
================================================

This script provides:
1. Configuration validation
2. System health checks
3. Deployment assistance
4. Runtime monitoring

Usage:
    python deploy_resilience.py --check       # Health check
    python deploy_resilience.py --validate    # Validate config
    python deploy_resilience.py --deploy      # Deploy resilience modules
    python deploy_resilience.py --monitor     # Monitor runtime stats
"""
import argparse
import sys
import os
import json
import time
import asyncio
import yaml
import requests
from pathlib import Path
from typing import Dict, Any, Optional


class Colors:
    """ANSI color codes for pretty output."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    """Print a header."""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text:^70}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.END}\n")


def print_success(text: str):
    """Print success message."""
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")


def print_error(text: str):
    """Print error message."""
    print(f"{Colors.RED}✗ {text}{Colors.END}")


def print_warning(text: str):
    """Print warning message."""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")


def print_info(text: str):
    """Print info message."""
    print(f"{Colors.CYAN}ℹ {text}{Colors.END}")


def load_config(config_path: str = "config.yaml") -> Optional[Dict[str, Any]]:
    """Load configuration file."""
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print_error(f"Configuration file not found: {config_path}")
        return None
    except yaml.YAMLError as e:
        print_error(f"Invalid YAML in configuration: {e}")
        return None


def validate_config(config: Dict[str, Any]) -> bool:
    """Validate configuration."""
    print_header("Configuration Validation")

    all_valid = True

    # Check required sections
    required_sections = ['zigbee', 'mqtt', 'web']
    for section in required_sections:
        if section not in config:
            print_error(f"Missing required section: {section}")
            all_valid = False
        else:
            print_success(f"Section '{section}' present")

    if 'zigbee' in config:
        zigbee_config = config['zigbee']

        # Validate port
        if 'port' not in zigbee_config:
            print_error("Missing 'port' in zigbee configuration")
            all_valid = False
        else:
            port = zigbee_config['port']
            if not os.path.exists(port):
                print_warning(f"Serial port not found: {port}")
            else:
                print_success(f"Serial port exists: {port}")

        # Validate channel
        channel = zigbee_config.get('channel', 25)
        if not (11 <= channel <= 26):
            print_error(f"Invalid channel: {channel} (must be 11-26)")
            all_valid = False
        else:
            if channel == 25:
                print_success(f"Using recommended channel: {channel}")
            else:
                print_info(f"Using channel: {channel}")

        # Validate EZSP configuration
        if 'ezsp_config' in zigbee_config:
            ezsp = zigbee_config['ezsp_config']

            # Check critical settings
            packet_buffers = ezsp.get('CONFIG_PACKET_BUFFER_COUNT', 0)
            if packet_buffers > 254:
                print_error(f"CONFIG_PACKET_BUFFER_COUNT too high: {packet_buffers} (max 254)")
                all_valid = False
            elif packet_buffers == 254:
                print_success(f"CONFIG_PACKET_BUFFER_COUNT optimal: {packet_buffers}")
            else:
                print_warning(f"CONFIG_PACKET_BUFFER_COUNT: {packet_buffers} (consider 254)")

            # Check APS messages
            aps_count = ezsp.get('CONFIG_APS_UNICAST_MESSAGE_COUNT', 0)
            if aps_count > 32:
                print_error(f"CONFIG_APS_UNICAST_MESSAGE_COUNT too high: {aps_count}")
                all_valid = False
            elif 10 <= aps_count <= 20:
                print_success(f"CONFIG_APS_UNICAST_MESSAGE_COUNT optimal: {aps_count}")
            else:
                print_info(f"CONFIG_APS_UNICAST_MESSAGE_COUNT: {aps_count}")

            # Check source route table
            source_routes = ezsp.get('CONFIG_SOURCE_ROUTE_TABLE_SIZE', 0)
            if source_routes < 20:
                print_error(f"CONFIG_SOURCE_ROUTE_TABLE_SIZE too low: {source_routes} (min 20)")
                all_valid = False
            else:
                print_success(f"CONFIG_SOURCE_ROUTE_TABLE_SIZE valid: {source_routes}")
        else:
            print_warning("No ezsp_config section - using defaults")

    if all_valid:
        print_success("\nConfiguration validation passed!")
    else:
        print_error("\nConfiguration validation failed!")

    return all_valid


def check_dependencies() -> bool:
    """Check Python dependencies."""
    print_header("Dependency Check")

    required_packages = {
        'fastapi': 'FastAPI web framework',
        'uvicorn': 'ASGI server',
        'zigpy': 'Zigbee protocol implementation',
        'bellows': 'EZSP protocol library',
        'paho.mqtt.client': 'MQTT client',
        'yaml': 'YAML configuration',
    }

    all_present = True

    for package, description in required_packages.items():
        try:
            __import__(package.replace('.', '/'))
            print_success(f"{package:20} - {description}")
        except ImportError:
            print_error(f"{package:20} - NOT INSTALLED ({description})")
            all_present = False

    if all_present:
        print_success("\nAll dependencies installed!")
    else:
        print_error("\nMissing dependencies!")
        print_info("Install with: pip install -r requirements.txt")

    return all_present


def check_service_running(host: str = "localhost", port: int = 8000) -> bool:
    """Check if service is running."""
    try:
        response = requests.get(f"http://{host}:{port}/api/devices", timeout=2)
        return response.status_code == 200
    except:
        return False


async def monitor_stats(host: str = "localhost", port: int = 8000, interval: int = 5):
    """Monitor runtime statistics."""
    print_header("Runtime Monitoring")
    print_info(f"Monitoring {host}:{port} every {interval}s (Ctrl+C to stop)\n")

    try:
        while True:
            try:
                # Get resilience stats
                response = requests.get(
                    f"http://{host}:{port}/api/resilience/stats",
                    timeout=2
                )

                if response.status_code == 200:
                    stats = response.json()

                    # Clear screen (optional)
                    # os.system('clear' if os.name == 'posix' else 'cls')

                    print(f"\n{Colors.BOLD}[{time.strftime('%H:%M:%S')}] System Statistics{Colors.END}")
                    print("-" * 50)

                    # Uptime
                    uptime_hours = stats.get('uptime_hours', 0)
                    print(f"Uptime: {Colors.GREEN}{uptime_hours:.1f}h{Colors.END}")

                    # Connection state
                    state = stats.get('current_state', 'unknown')
                    state_color = Colors.GREEN if state == 'connected' else Colors.RED
                    print(f"State: {state_color}{state}{Colors.END}")

                    # Errors
                    ncp_failures = stats.get('ncp_failures', 0)
                    watchdog_failures = stats.get('watchdog_failures', 0)
                    print(f"NCP Failures: {ncp_failures}")
                    print(f"Watchdog Failures: {watchdog_failures}")

                    # Recoveries
                    recoveries_attempted = stats.get('recoveries_attempted', 0)
                    recoveries_successful = stats.get('recoveries_successful', 0)
                    if recoveries_attempted > 0:
                        success_rate = (recoveries_successful / recoveries_attempted) * 100
                        print(f"Recovery Rate: {success_rate:.1f}% ({recoveries_successful}/{recoveries_attempted})")

                    # Watchdog age
                    watchdog_age = stats.get('watchdog_age_seconds', 0)
                    watchdog_color = Colors.GREEN if watchdog_age < 60 else Colors.YELLOW
                    print(f"Watchdog Age: {watchdog_color}{watchdog_age:.1f}s{Colors.END}")

                else:
                    print_error(f"Failed to get stats: HTTP {response.status_code}")

                # Get error stats
                response = requests.get(
                    f"http://{host}:{port}/api/error_stats",
                    timeout=2
                )

                if response.status_code == 200:
                    error_stats = response.json()

                    total = error_stats.get('total_attempts', 0)
                    successes = error_stats.get('total_successes', 0)
                    retries = error_stats.get('total_retries', 0)

                    if total > 0:
                        success_rate = (successes / total) * 100
                        retry_rate = (retries / total) * 100

                        print(f"\nCommand Statistics:")
                        print(f"  Total: {total}")
                        print(f"  Success Rate: {Colors.GREEN}{success_rate:.1f}%{Colors.END}")
                        print(f"  Retry Rate: {retry_rate:.1f}%")

            except requests.RequestException as e:
                print_error(f"Connection error: {e}")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        print_info("\n\nMonitoring stopped")


def deploy_modules():
    """Deploy resilience modules."""
    print_header("Deploying Resilience Modules")

    # Check if files exist
    files = [
        'resilience.py',
        'config_enhanced.py',
        'error_handler.py',
        'INTEGRATION_GUIDE.md',
        'config.yaml.production'
    ]

    all_present = True
    for file in files:
        if os.path.exists(file):
            print_success(f"Found: {file}")
        else:
            print_error(f"Missing: {file}")
            all_present = False

    if not all_present:
        print_error("\nDeployment failed: missing files")
        return False

    # Check if core.py exists
    if not os.path.exists('core.py'):
        print_error("core.py not found - cannot deploy")
        return False

    print_info("\nTo complete deployment:")
    print_info("1. Copy config.yaml.production to config.yaml")
    print_info("2. Review and customize config.yaml")
    print_info("3. Follow INTEGRATION_GUIDE.md to update core.py")
    print_info("4. Restart the service")

    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Zigbee Gateway Health Check and Deployment Tool"
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='Run system health check'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate configuration'
    )
    parser.add_argument(
        '--deploy',
        action='store_true',
        help='Deploy resilience modules'
    )
    parser.add_argument(
        '--monitor',
        action='store_true',
        help='Monitor runtime statistics'
    )
    parser.add_argument(
        '--config',
        default='config.yaml',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--host',
        default='localhost',
        help='Service host (for monitoring)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8000,
        help='Service port (for monitoring)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=5,
        help='Monitoring interval (seconds)'
    )

    args = parser.parse_args()

    # Run health check
    if args.check:
        print_header("System Health Check")

        # Check dependencies
        deps_ok = check_dependencies()

        # Check configuration
        config = load_config(args.config)
        config_ok = validate_config(config) if config else False

        # Check if service is running
        print_header("Service Status")
        if check_service_running(args.host, args.port):
            print_success(f"Service is running on {args.host}:{args.port}")
        else:
            print_warning(f"Service is not running on {args.host}:{args.port}")

        # Summary
        print_header("Health Check Summary")
        if deps_ok and config_ok:
            print_success("System is healthy!")
            return 0
        else:
            print_error("System has issues!")
            return 1

    # Validate configuration only
    elif args.validate:
        config = load_config(args.config)
        if config and validate_config(config):
            return 0
        return 1

    # Deploy modules
    elif args.deploy:
        if deploy_modules():
            return 0
        return 1

    # Monitor statistics
    elif args.monitor:
        try:
            asyncio.run(monitor_stats(args.host, args.port, args.interval))
            return 0
        except Exception as e:
            print_error(f"Monitoring error: {e}")
            return 1

    # No arguments - show help
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
