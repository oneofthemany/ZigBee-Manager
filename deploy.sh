#!/bin/bash
# Zigbee Matter Manager Deployment Script
# This script sets up the Zigbee Matter Manager for production use

set -e

echo "=========================================="
echo "Zigbee Matter Manager Deployment Script"
echo "=========================================="
echo

# Configuration
INSTALL_DIR="/opt/zigbee_matter_manager"
SERVICE_NAME="zigbee-matter-manager"
SERVICE_USER="zigbee"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="$INSTALL_DIR/logs"
SUDOERS_FILE="/etc/sudoers.d/zigbee-matter-manager"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root"
    echo "Usage: sudo bash deploy.sh"
    exit 1
fi

echo "Step 1: Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false -d "$INSTALL_DIR" "$SERVICE_USER"
    echo "✓ User '$SERVICE_USER' created"
else
    echo "✓ User '$SERVICE_USER' already exists"
fi

echo
echo "Step 2: Adding user to dialout group for USB access..."
usermod -a -G dialout "$SERVICE_USER"
echo "✓ User added to dialout group"

echo
echo "Step 3: Creating directory structure..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
mkdir -p "$INSTALL_DIR/backups"
mkdir -p "$INSTALL_DIR/config"
mkdir -p "$LOG_DIR"
echo "✓ Directories created"

echo
echo "Step 4: Installing system dependencies..."
apt-get update
apt-get install -y python3 python3-venv python3-pip logrotate
echo "✓ System dependencies installed"

echo
echo "Step 5: Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi

echo
echo "Step 6: Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r requirements.txt
echo "✓ Python dependencies installed"

echo
echo "Step 7: Installing Matter server (optional)..."
echo "  Matter enables WiFi-based Matter device support."
read -p "  Install Matter server support? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  Installing python-matter-server..."
    "$VENV_DIR/bin/pip" install "python-matter-server[server]"
    echo "  ✓ python-matter-server installed"

    # CHIP SDK requires /data for its config files
    echo "  Creating /data directory for CHIP SDK..."
    mkdir -p /data
    SUSER_UID=$(id -u "$SERVICE_USER" 2>/dev/null || echo "1000")
    SUSER_GID=$(id -g "$SERVICE_USER" 2>/dev/null || echo "1000")
    chown "$SUSER_UID:$SUSER_GID" /data
    echo "  ✓ /data directory created (owner: $SERVICE_USER)"

    mkdir -p "$INSTALL_DIR/data/matter"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data/matter"
    echo "  ✓ Matter storage directory created"

    echo "  ✓ Matter server support installed"
    echo
    echo "  To enable Matter, add to config.yaml:"
    echo "    matter:"
    echo "      enabled: true"
    echo "      port: 5580"
    echo "      storage_path: ./data/matter"
else
    echo "  Skipped. You can install later with:"
    echo "    $VENV_DIR/bin/pip install 'python-matter-server[server]'"
    echo "    sudo mkdir -p /data && sudo chown $SERVICE_USER:$SERVICE_USER /data"
fi

echo
echo "Step 8: Copying application files..."
# Assumes script is run from the project directory
cp -r *.py "$INSTALL_DIR/"
cp -r handlers "$INSTALL_DIR/"
cp -r modules "$INSTALL_DIR/"
cp -r routes "$INSTALL_DIR/"
cp -r static "$INSTALL_DIR/"
[ -f config.yaml ] && cp config.yaml "$INSTALL_DIR/config/"
echo "✓ Application files copied"

echo
echo "Step 9: Setting permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 644 "$INSTALL_DIR/config/config.yaml" 2>/dev/null || true
chmod 755 "$LOG_DIR"
echo "✓ Permissions set"

echo
echo "Step 10: Installing systemd service..."
if [ -f "zigbee-matter-manager.service" ]; then
    sed -i "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|g" zigbee-matter-manager.service
    sed -i "s|ExecStartPre=.*|ExecStartPre=$VENV_DIR/bin/python3 boot_guard.py|g" zigbee-matter-manager.service
    sed -i "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 main.py|g" zigbee-matter-manager.service
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=$LOG_DIR $INSTALL_DIR /data|g" zigbee-matter-manager.service

    cp zigbee-matter-manager.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    echo "✓ Systemd service installed and enabled"
else
    echo "⚠ Warning: zigbee-matter-manager.service file not found"
fi

echo
echo "Step 11: Installing logrotate configuration..."
if [ -f "zigbee-logrotate.conf" ]; then
    sed -i "s|/path/to/your/project/logs|$LOG_DIR|g" zigbee-logrotate.conf

    cp zigbee-logrotate.conf /etc/logrotate.d/$SERVICE_NAME
    chmod 644 /etc/logrotate.d/$SERVICE_NAME
    echo "✓ Logrotate configuration installed"

    echo "  Testing logrotate configuration..."
    if logrotate -d /etc/logrotate.d/$SERVICE_NAME >/dev/null 2>&1; then
        echo "  ✓ Logrotate configuration is valid"
    else
        echo "  ⚠ Warning: Logrotate configuration test failed"
    fi
else
    echo "⚠ Warning: zigbee-logrotate.conf file not found"
fi

echo
echo "Step 12: Configuring firewall (if UFW is active)..."
if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 8000/tcp comment "Zigbee Matter Manager Web Interface"
    ufw allow 5580/tcp comment "Matter Server WebSocket"
    echo "✓ Firewall rules added for ports 8000 and 5580"
else
    echo "  UFW not active, skipping firewall configuration"
fi

echo
echo "Step 13: Safe Deploy — Sudoers Configuration"
echo
echo "  ┌──────────────────────────────────────────────────────────────────┐"
echo "  │  SAFE DEPLOY enables the web UI to restart the service after     │"
echo "  │  code updates, with automatic backup and rollback if the new     │"
echo "  │  code fails to start.                                            │"
echo "  │                                                                  │"
echo "  │  This requires the service user ($SERVICE_USER) to have          │"
echo "  │  passwordless sudo access to ONLY these specific commands:       │"
echo "  │                                                                  │"
echo "  │    systemctl restart $SERVICE_NAME                               │"
echo "  │    systemctl status  $SERVICE_NAME                               │"
echo "  │    systemctl stop    $SERVICE_NAME                               │"
echo "  │    systemctl start   $SERVICE_NAME                               │"
echo "  │                                                                  │"
echo "  │  No other sudo access is granted. The file is installed at:      │"
echo "  │    $SUDOERS_FILE                                                 │"
echo "  │                                                                  │"
echo "  │  Without this, code updates from the web UI will require a       │"
echo "  │  manual SSH restart: sudo systemctl restart $SERVICE_NAME        │"
echo "  └──────────────────────────────────────────────────────────────────┘"
echo
read -p "  Install safe deploy sudoers rule for user '$SERVICE_USER'? [Y/n] " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    # Build the sudoers content
    SUDOERS_CONTENT="# Zigbee Matter Manager — Safe Deploy
# Allows the service user to restart/stop/start the service via the web UI.
# This is required for the safe deploy feature (backup, validate, restart, rollback).
# Scope: ONLY systemctl commands for the $SERVICE_NAME service. No other access.
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart $SERVICE_NAME
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl status $SERVICE_NAME
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop $SERVICE_NAME
$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start $SERVICE_NAME"

    # Write to temp file first and validate with visudo -c
    TEMP_SUDOERS=$(mktemp)
    echo "$SUDOERS_CONTENT" > "$TEMP_SUDOERS"

    if visudo -c -f "$TEMP_SUDOERS" >/dev/null 2>&1; then
        cp "$TEMP_SUDOERS" "$SUDOERS_FILE"
        chmod 440 "$SUDOERS_FILE"
        rm -f "$TEMP_SUDOERS"
        echo "  ✓ Sudoers file installed and validated at $SUDOERS_FILE"

        # Verify it works
        if sudo -u "$SERVICE_USER" sudo -n systemctl status "$SERVICE_NAME" >/dev/null 2>&1; then
            echo "  ✓ Verified: $SERVICE_USER can run systemctl without password"
        else
            echo "  ⚠ Verification skipped (service may not be running yet)"
        fi
    else
        rm -f "$TEMP_SUDOERS"
        echo "  ✗ Sudoers validation failed — file not installed"
        echo "  You can manually create $SUDOERS_FILE with:"
        echo "    sudo visudo -f $SUDOERS_FILE"
        echo "  And add:"
        echo "    $SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart $SERVICE_NAME"
        echo "    $SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl status $SERVICE_NAME"
        echo "    $SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop $SERVICE_NAME"
        echo "    $SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start $SERVICE_NAME"
    fi
else
    echo "  Skipped. You can install later with:"
    echo "    sudo visudo -f $SUDOERS_FILE"
fi

echo
echo "Step 14: AI Assistant — Hardware Assessment & Ollama Setup"
echo
echo "  ┌──────────────────────────────────────────────────────────────────┐"
echo "  │  The AI Assistant enables natural language automation creation.  │"
echo "  │  It uses a local LLM (via Ollama) to convert plain English       │"
echo "  │  into automation rules — no cloud API keys required.             │"
echo "  │                                                                  │"
echo "  │  This step will assess your hardware and recommend the best      │"
echo "  │  model for your system, then optionally install Ollama in a      │"
echo "  │  container (Docker or Podman).                                   │"
echo "  └──────────────────────────────────────────────────────────────────┘"
echo
read -p "  Set up local AI assistant? [Y/n] " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Nn]$ ]]; then

    # ── Hardware Detection ──────────────────────────────────────────────
    echo
    echo "  ── Hardware Assessment ──────────────────────────────────────────"
    echo

    # Architecture
    ARCH=$(uname -m)
    echo "  Architecture:    $ARCH"

    # CPU
    CPU_MODEL=$(grep -m1 "model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs)
    if [ -z "$CPU_MODEL" ]; then
        # ARM doesn't always have model name, try Hardware field
        CPU_MODEL=$(grep -m1 "Hardware" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs)
    fi
    if [ -z "$CPU_MODEL" ]; then
        CPU_MODEL=$(lscpu 2>/dev/null | grep "Model name" | cut -d: -f2 | xargs)
    fi
    CPU_CORES=$(nproc 2>/dev/null || echo "?")
    echo "  CPU:             ${CPU_MODEL:-Unknown} (${CPU_CORES} cores)"

    # Memory
    TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    TOTAL_RAM_GB=$(awk "BEGIN {printf \"%.1f\", $TOTAL_RAM_KB / 1048576}")
    AVAIL_RAM_KB=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
    AVAIL_RAM_GB=$(awk "BEGIN {printf \"%.1f\", $AVAIL_RAM_KB / 1048576}")
    echo "  Memory:          ${TOTAL_RAM_GB} GB total, ${AVAIL_RAM_GB} GB available"

    # Swap
    SWAP_KB=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
    SWAP_GB=$(awk "BEGIN {printf \"%.1f\", $SWAP_KB / 1048576}")
    echo "  Swap:            ${SWAP_GB} GB"

    # GPU Detection
    GPU_TYPE="none"
    GPU_NAME=""
    GPU_VRAM=""

    # NVIDIA
    if command -v nvidia-smi &>/dev/null; then
        GPU_TYPE="nvidia"
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
        GPU_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
        echo "  GPU:             NVIDIA ${GPU_NAME} (${GPU_VRAM} MB VRAM)"
    # AMD ROCm
    elif [ -d /dev/kfd ] && [ -d /dev/dri ]; then
        GPU_TYPE="amd"
        GPU_NAME=$(lspci 2>/dev/null | grep -i "VGA\|3D" | grep -i "AMD\|ATI" | head -1 | sed 's/.*: //')
        if [ -n "$GPU_NAME" ]; then
            echo "  GPU:             AMD ${GPU_NAME} (ROCm capable)"
        fi
    fi

    # Mali GPU (common on ARM SBCs like Rock 5B)
    if [ -d /sys/class/misc/mali0 ] || [ -d /sys/devices/platform/*gpu* ] 2>/dev/null; then
        MALI_INFO=$(cat /sys/class/misc/mali0/device/gpuinfo 2>/dev/null || echo "")
        if [ -z "$GPU_NAME" ]; then
            GPU_NAME="Mali (integrated)"
            echo "  GPU:             ${GPU_NAME} — not usable by Ollama (no OpenCL/Vulkan driver)"
        fi
    fi

    if [ "$GPU_TYPE" = "none" ] && [ -z "$GPU_NAME" ]; then
        echo "  GPU:             None detected — CPU inference only"
    fi

    # NPU Detection
    NPU_INFO=""
    # Rockchip NPU (RK3588 = 6 TOPS)
    if [ -d /sys/class/misc/rknpu ] || [ -e /dev/rknpu ]; then
        NPU_INFO="Rockchip NPU detected (not usable by Ollama)"
        echo "  NPU:             ${NPU_INFO}"
    # Intel NPU
    elif [ -d /sys/class/accel ] 2>/dev/null; then
        NPU_INFO="Intel NPU detected"
        echo "  NPU:             ${NPU_INFO}"
    # Coral TPU
    elif lsusb 2>/dev/null | grep -qi "google.*coral\|global unichip" || [ -e /dev/apex_0 ]; then
        NPU_INFO="Google Coral TPU detected (not usable by Ollama)"
        echo "  NPU:             ${NPU_INFO}"
    else
        echo "  NPU/TPU:         None detected"
    fi

    # Disk space
    DISK_AVAIL=$(df -BG "$INSTALL_DIR" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
    echo "  Disk available:  ${DISK_AVAIL:-?} GB (at $INSTALL_DIR)"

    echo

    # ── Model Recommendation ────────────────────────────────────────────
    # RAM thresholds (in KB) — model needs RAM + ~2-3GB for OS/services
    RAM_4GB=4194304
    RAM_8GB=8388608
    RAM_16GB=16777216
    RAM_32GB=33554432

    RECOMMENDED_MODEL=""
    RECOMMENDED_LABEL=""
    MODEL_SIZE_GB=""
    OLLAMA_MEM_LIMIT=""
    OLLAMA_CPU_LIMIT=""
    CAN_RUN_LOCAL=true

    # Reserve cores for the main application
    if [ "$CPU_CORES" -gt 4 ]; then
        OLLAMA_CPU_LIMIT=$(( CPU_CORES - 2 ))
    else
        OLLAMA_CPU_LIMIT=$(( CPU_CORES - 1 ))
    fi
    [ "$OLLAMA_CPU_LIMIT" -lt 1 ] && OLLAMA_CPU_LIMIT=1

    if [ "$TOTAL_RAM_KB" -lt "$RAM_4GB" ]; then
        CAN_RUN_LOCAL=false
        echo "  ⚠ Insufficient RAM for local LLM inference (${TOTAL_RAM_GB} GB)"
        echo "    Minimum 4 GB required. Use a cloud API provider instead."
        echo "    Set provider to 'openai' or 'anthropic' in the AI settings."
    elif [ "$TOTAL_RAM_KB" -lt "$RAM_8GB" ]; then
        RECOMMENDED_MODEL="llama3.2:3b-instruct-q4_K_M"
        RECOMMENDED_LABEL="Llama 3.2 3B (quantised Q4)"
        MODEL_SIZE_GB="2.0"
        OLLAMA_MEM_LIMIT="4g"
    elif [ "$TOTAL_RAM_KB" -lt "$RAM_16GB" ]; then
        RECOMMENDED_MODEL="llama3.1:8b-instruct-q4_K_M"
        RECOMMENDED_LABEL="Llama 3.1 8B (quantised Q4)"
        MODEL_SIZE_GB="4.9"
        OLLAMA_MEM_LIMIT="7g"
    elif [ "$TOTAL_RAM_KB" -lt "$RAM_32GB" ]; then
        RECOMMENDED_MODEL="llama3.1:8b-instruct-q4_K_M"
        RECOMMENDED_LABEL="Llama 3.1 8B (quantised Q4)"
        MODEL_SIZE_GB="4.9"
        OLLAMA_MEM_LIMIT="10g"
    else
        RECOMMENDED_MODEL="llama3.1:8b-instruct-q4_K_M"
        RECOMMENDED_LABEL="Llama 3.1 8B (quantised Q4)"
        MODEL_SIZE_GB="4.9"
        OLLAMA_MEM_LIMIT="12g"
    fi

    # NVIDIA GPU — can use more VRAM, potentially larger model
    if [ "$GPU_TYPE" = "nvidia" ] && [ -n "$GPU_VRAM" ] && [ "$GPU_VRAM" -ge 6000 ] 2>/dev/null; then
        RECOMMENDED_MODEL="llama3.1:8b-instruct-q4_K_M"
        RECOMMENDED_LABEL="Llama 3.1 8B (quantised Q4) — GPU accelerated"
        MODEL_SIZE_GB="4.9"
    fi

    if [ "$CAN_RUN_LOCAL" = true ]; then
        echo "  ── Recommendation ────────────────────────────────────────────"
        echo
        echo "  Model:           $RECOMMENDED_LABEL"
        echo "  Ollama tag:      $RECOMMENDED_MODEL"
        echo "  Model size:      ~${MODEL_SIZE_GB} GB download"
        echo "  Container RAM:   $OLLAMA_MEM_LIMIT"
        echo "  Container CPUs:  $OLLAMA_CPU_LIMIT (of $CPU_CORES)"
        if [ "$GPU_TYPE" = "nvidia" ]; then
            echo "  GPU offload:     Yes (NVIDIA CUDA)"
        elif [ "$GPU_TYPE" = "amd" ]; then
            echo "  GPU offload:     Possible (AMD ROCm — use ollama/ollama:rocm image)"
        else
            echo "  GPU offload:     No — CPU inference (~5-15 seconds per automation)"
        fi
        echo

        # ── Container Runtime Selection ─────────────────────────────────
        CONTAINER_CMD=""
        CONTAINER_LABEL=""

        if command -v podman &>/dev/null && command -v docker &>/dev/null; then
            echo "  Both Docker and Podman are available."
            echo
            echo "    1) Podman  — rootless, daemonless, no Docker socket needed"
            echo "    2) Docker  — traditional, wider ecosystem support"
            echo
            read -p "  Which container runtime? [1/2] " -n 1 -r RUNTIME_CHOICE
            echo
            if [ "$RUNTIME_CHOICE" = "2" ]; then
                CONTAINER_CMD="docker"
                CONTAINER_LABEL="Docker"
            else
                CONTAINER_CMD="podman"
                CONTAINER_LABEL="Podman"
            fi
        elif command -v podman &>/dev/null; then
            CONTAINER_CMD="podman"
            CONTAINER_LABEL="Podman"
            echo "  Container runtime: Podman"
        elif command -v docker &>/dev/null; then
            CONTAINER_CMD="docker"
            CONTAINER_LABEL="Docker"
            echo "  Container runtime: Docker"
        else
            echo "  ⚠ Neither Docker nor Podman found."
            echo
            echo "  Install one first:"
            echo "    Podman: sudo apt install podman"
            echo "    Docker: curl -fsSL https://get.docker.com | sudo sh"
            echo
            echo "  Then re-run this script, or manually run:"
            echo "    podman run -d --name ollama --restart always \\"
            echo "      -v ollama_models:/root/.ollama -p 11434:11434 \\"
            echo "      --memory ${OLLAMA_MEM_LIMIT} --cpus ${OLLAMA_CPU_LIMIT} \\"
            echo "      docker.io/ollama/ollama"
            echo
            CAN_RUN_LOCAL=false
        fi

        if [ "$CAN_RUN_LOCAL" = true ] && [ -n "$CONTAINER_CMD" ]; then
            echo
            echo "  Ready to install Ollama via $CONTAINER_LABEL."
            echo "  This will:"
            echo "    • Pull the Ollama container image (~1 GB)"
            echo "    • Start it with ${OLLAMA_MEM_LIMIT} RAM / ${OLLAMA_CPU_LIMIT} CPUs"
            echo "    • Download the ${RECOMMENDED_LABEL} model (~${MODEL_SIZE_GB} GB)"
            echo "    • Configure auto-restart so it survives reboots"
            echo
            read -p "  Proceed with Ollama installation? [Y/n] " -n 1 -r
            echo

            if [[ ! $REPLY =~ ^[Nn]$ ]]; then

                # Select image tag
                OLLAMA_IMAGE="docker.io/ollama/ollama"
                if [ "$GPU_TYPE" = "amd" ]; then
                    OLLAMA_IMAGE="docker.io/ollama/ollama:rocm"
                fi

                # GPU device flags
                GPU_FLAGS=""
                if [ "$GPU_TYPE" = "nvidia" ]; then
                    if [ "$CONTAINER_CMD" = "docker" ]; then
                        GPU_FLAGS="--gpus all"
                    else
                        # Podman uses CDI
                        if [ -f /etc/cdi/nvidia.yaml ]; then
                            GPU_FLAGS="--device nvidia.com/gpu=all"
                        else
                            echo "  ⚠ NVIDIA CDI not configured for Podman."
                            echo "    Run: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
                            echo "    Continuing with CPU only."
                        fi
                    fi
                elif [ "$GPU_TYPE" = "amd" ]; then
                    GPU_FLAGS="--device /dev/kfd --device /dev/dri"
                fi

                # Stop and remove existing container if present
                $CONTAINER_CMD stop ollama 2>/dev/null || true
                $CONTAINER_CMD rm ollama 2>/dev/null || true

                echo "  Pulling Ollama image..."
                $CONTAINER_CMD pull "$OLLAMA_IMAGE"

                echo "  Starting Ollama container..."
                # shellcheck disable=SC2086
                $CONTAINER_CMD run -d \
                    --name ollama \
                    --restart always \
                    -v ollama_models:/root/.ollama \
                    -p 11434:11434 \
                    --memory "$OLLAMA_MEM_LIMIT" \
                    --cpus "$OLLAMA_CPU_LIMIT" \
                    -e OLLAMA_MAX_LOADED_MODELS=1 \
                    -e OLLAMA_NUM_PARALLEL=1 \
                    -e OLLAMA_KEEP_ALIVE=10m \
                    $GPU_FLAGS \
                    "$OLLAMA_IMAGE"

                echo "  ✓ Ollama container started"

                # Wait for Ollama to be ready
                echo "  Waiting for Ollama to initialise..."
                for i in $(seq 1 30); do
                    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
                        break
                    fi
                    sleep 2
                done

                if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
                    echo "  ✓ Ollama is responding"

                    # Pull the recommended model
                    echo
                    echo "  Downloading model: $RECOMMENDED_MODEL (~${MODEL_SIZE_GB} GB)"
                    echo "  This may take several minutes depending on your connection..."
                    echo
                    $CONTAINER_CMD exec ollama ollama pull "$RECOMMENDED_MODEL"
                    echo
                    echo "  ✓ Model downloaded: $RECOMMENDED_MODEL"

                    # Quick sanity test
                    echo "  Running quick inference test..."
                    TEST_RESULT=$($CONTAINER_CMD exec ollama ollama run "$RECOMMENDED_MODEL" "Respond with only: OK" 2>/dev/null | head -1)
                    if [ -n "$TEST_RESULT" ]; then
                        echo "  ✓ Inference test passed"
                    else
                        echo "  ⚠ Inference test returned empty — model may need more time to load"
                    fi

                    # Write AI config to config.yaml
                    echo
                    echo "  Configuring AI assistant in config.yaml..."
                    CONFIG_FILE="$INSTALL_DIR/config/config.yaml"
                    if [ -f "$CONFIG_FILE" ]; then
                        # Remove existing ai: section if present
                        sed -i '/^ai:/,/^[a-z]/{ /^ai:/d; /^  /d; }' "$CONFIG_FILE"
                    fi
                    cat >> "$CONFIG_FILE" << AIEOF

ai:
  provider: ollama
  model: $RECOMMENDED_MODEL
  base_url: http://localhost:11434/v1
  temperature: 0.3
  max_tokens: 2000
AIEOF
                    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
                    echo "  ✓ AI config written to config.yaml"

                    echo
                    echo "  ┌──────────────────────────────────────────────────────────────┐"
                    echo "  │  ✓ AI Assistant setup complete                               │"
                    echo "  │                                                              │"
                    echo "  │  Container:  ollama ($CONTAINER_LABEL)                       │"
                    echo "  │  Model:      $RECOMMENDED_MODEL                              │"
                    echo "  │  Endpoint:   http://localhost:11434                          │"
                    echo "  │  RAM limit:  $OLLAMA_MEM_LIMIT                               │"
                    echo "  │  CPU limit:  $OLLAMA_CPU_LIMIT cores                         │"
                    echo "  │                                                              │"
                    echo "  │  Manage with:                                                │"
                    echo "  │    $CONTAINER_CMD exec ollama ollama list                    │"
                    echo "  │    $CONTAINER_CMD logs ollama                                │"
                    echo "  │    $CONTAINER_CMD restart ollama                             │"
                    echo "  └──────────────────────────────────────────────────────────────┘"

                else
                    echo "  ✗ Ollama not responding after 60 seconds"
                    echo "    Check logs: $CONTAINER_CMD logs ollama"
                fi

            else
                echo "  Skipped Ollama installation."
                echo "  You can install manually later or use a cloud API provider."
            fi
        fi
    fi
else
    echo "  Skipped. Configure AI via the web UI Settings → AI tab."
fi

echo
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo
echo "Next steps:"
echo "1. Edit configuration: sudo nano $INSTALL_DIR/config/config.yaml"
echo "2. Update MQTT settings, Zigbee USB port, etc."
echo "3. Start the service: sudo systemctl start $SERVICE_NAME"
echo "4. Check status: sudo systemctl status $SERVICE_NAME"
echo "5. View logs: sudo journalctl -u $SERVICE_NAME -f"
echo "6. Access web interface: http://YOUR_IP:8000"
echo
echo "Useful commands:"
echo "- Restart service: sudo systemctl restart $SERVICE_NAME"
echo "- Stop service: sudo systemctl stop $SERVICE_NAME"
echo "- View application logs: sudo tail -f $LOG_DIR/zigbee.log"
echo "- View debug logs: sudo tail -f $LOG_DIR/zigbee_debug.log"
echo "- Test logrotate: sudo logrotate -f /etc/logrotate.d/$SERVICE_NAME"
echo
echo "For debugging guide, see: DEBUGGING_GUIDE.md"
echo