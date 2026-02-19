#!/bin/bash
# MCP Web Bridge Startup Script
# Enterprise C SPC Dashboard - Flask Bridge Server

set -e

echo "═══════════════════════════════════════════════════════════"
echo "MCP Web Bridge for Enterprise C Dashboard"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Configuration
VENV_PATH="/Users/mdavidson58/Documents/AnyLog/Prove-IT/proxy/myenv"
MCP_PROXY="${VENV_PATH}/bin/mcp-proxy"
BRIDGE_SCRIPT="mcp_web_bridge.py"
PORT=8080

# Check if virtual environment exists
if [ ! -d "$VENV_PATH" ]; then
    echo "❌ Error: Virtual environment not found at $VENV_PATH"
    echo ""
    echo "Create it with:"
    echo "  cd /Users/mdavidson58/Documents/AnyLog/Prove-IT/proxy"
    echo "  python3 -m venv myenv"
    exit 1
fi

# Check if mcp_proxy exists
if [ ! -f "$MCP_PROXY" ]; then
    echo "❌ Error: mcp_proxy not found at $MCP_PROXY"
    echo ""
    echo "Install it with:"
    echo "  source ${VENV_PATH}/bin/activate"
    echo "  pip install mcp-proxy"
    exit 1
fi

# Check if bridge script exists
if [ ! -f "$BRIDGE_SCRIPT" ]; then
    echo "❌ Error: Bridge script not found: $BRIDGE_SCRIPT"
    echo ""
    echo "Make sure mcp_web_bridge.py is in the current directory"
    exit 1
fi

# Activate virtual environment
echo "📦 Activating virtual environment..."
source "${VENV_PATH}/bin/activate"

# Check if Flask is installed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "⚠️  Flask not installed. Installing dependencies..."
    pip install -q flask flask-cors
    echo "✅ Dependencies installed"
fi

echo ""
echo "Configuration:"
echo "  • MCP Proxy: $MCP_PROXY"
echo "  • AnyLog Server: http://50.116.13.109:32049/mcp/sse"
echo "  • Bridge Port: $PORT"
echo ""

# Check if port is already in use
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "⚠️  Warning: Port $PORT is already in use"
    echo ""
    read -p "Kill existing process and continue? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        PID=$(lsof -ti:$PORT)
        kill -9 $PID 2>/dev/null || true
        sleep 1
        echo "✅ Killed process $PID"
    else
        echo "Exiting..."
        exit 1
    fi
fi

echo "🚀 Starting MCP Web Bridge..."
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Dashboard URL: http://localhost:$PORT"
echo "  Press Ctrl+C to stop"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Start the bridge
python3 "$BRIDGE_SCRIPT"
