import asyncio
import logging
import traceback
from bellows.zigbee.application import ControllerApplication
import zigpy.config

# Enable debug logging for bellows to see UART traffic
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


async def wipe_radio():
    config = {
        "device": {
            "path": "/dev/ttyACM0",
            "baudrate": 460800,
            "flow_control": "hardware"
        },
        "database_path": "zigbee.db",
        # Minimal buffer config to allow startup
        "ezsp_config": {
            "CONFIG_PACKET_BUFFER_COUNT": 255,
        }
    }

    print("------------------------------------------------")
    print("1. INITIALIZING...")

    app = None
    try:
        # Create the application object but don't start the radio logic yet
        app = await ControllerApplication.new(
            config=config,
            auto_form=False,
            start_radio=False,  # We start radio manually to catch early errors
        )

        print("2. CONNECTING TO HARDWARE...")
        await app.connect()
        print("   -> Hardware Connected.")

        print("3. PERFORMING FACTORY RESET (ERASING NVM)...")
        # Explicitly reset the generic network info
        await app.reset_network_info()

        # If specific EZSP reset is needed (optional, but good for ZBT-2)
        # await app._ezsp.reset()

        print("4. SUCCESS: STICK WIPED.")
        print("------------------------------------------------")

    except Exception:
        print("\n!!!!!!!!!!!!!!! FAILURE !!!!!!!!!!!!!!!")
        print(traceback.format_exc())
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

    finally:
        if app:
            print("Closing connection...")
            app.close_radio()


if __name__ == "__main__":
    asyncio.run(wipe_radio())