#!/bin/bash

# MedAdvice v3 - Quick Start Script

echo "╔════════════════════════════════════════════════╗"
echo "║         MedAdvice v3 - Starting...            ║"
echo "╚════════════════════════════════════════════════╝"
echo ""

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "❌ Virtual environment not found!"
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "✅ Virtual environment created"
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Check if dependencies are installed
if ! python -c "import fastapi" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    echo "✅ Dependencies installed"
fi

# Check for .env file
if [ ! -f ".env" ]; then
    echo "❌ .env file not found!"
    echo "Please copy .env.example to .env and configure your ANTHROPIC_API_KEY"
    echo ""
    echo "Run: cp .env.example .env"
    echo "Then edit .env to add your API key"
    exit 1
fi

# Check for API key
if ! grep -q "ANTHROPIC_API_KEY=sk-" .env 2>/dev/null; then
    echo "⚠️  WARNING: ANTHROPIC_API_KEY not configured in .env"
    echo "The application will not work without a valid API key"
    echo ""
fi

# Create logs directory
mkdir -p logs

echo ""
echo "Starting MedAdvice v3..."
echo ""
echo "Access the application at:"
echo "  📱 Chat Interface:    http://localhost:8001/app"
echo "  📊 Admin Dashboard:   http://localhost:8001/admin-ui"
echo "  📋 Governance Logs:   http://localhost:8001/governance-ui"
echo "  📚 API Docs:          http://localhost:8001/docs"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Run the application
python -m backend.main
