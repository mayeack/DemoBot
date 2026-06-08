#!/bin/bash

# MedAdvice v4 - Quick Start Script
# Supports dual-environment deployment: Anthropic API (local) or AWS Bedrock (production)

echo "╔════════════════════════════════════════════════╗"
echo "║         MedAdvice v4 - Starting...            ║"
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
    echo "Please copy .env.example to .env and configure your settings"
    echo ""
    echo "Run: cp .env.example .env"
    echo "Then edit .env to add your API key"
    exit 1
fi

# Check AI provider configuration
AI_PROVIDER=$(grep "^AI_PROVIDER=" .env 2>/dev/null | cut -d'=' -f2)
if [ "$AI_PROVIDER" = "bedrock" ]; then
    echo "🔧 AI Provider: AWS Bedrock"
    # Check for AWS credentials
    if [ -z "$AWS_ACCESS_KEY_ID" ] && [ ! -f ~/.aws/credentials ]; then
        echo "⚠️  WARNING: AWS credentials not found"
        echo "Configure AWS CLI: aws configure"
        echo ""
    fi
elif [ "$AI_PROVIDER" = "anthropic" ] || [ -z "$AI_PROVIDER" ]; then
    echo "🔧 AI Provider: Anthropic API"
    # Check for Anthropic API key
    if ! grep -q "ANTHROPIC_API_KEY=sk-" .env 2>/dev/null; then
        echo "⚠️  WARNING: ANTHROPIC_API_KEY not configured in .env"
        echo "The application will not work without a valid API key"
        echo ""
    fi
elif [ "$AI_PROVIDER" = "openai" ]; then
    echo "🔧 AI Provider: OpenAI-compatible API"
    # Check for OpenAI-compatible API key
    if ! grep -q "^OPENAI_API_KEY=." .env 2>/dev/null || grep -q "OPENAI_API_KEY=your_" .env 2>/dev/null; then
        echo "⚠️  WARNING: OPENAI_API_KEY not configured in .env"
        echo "The application will not work without a valid API key"
        echo ""
    fi
else
    echo "⚠️  WARNING: Unknown AI_PROVIDER: $AI_PROVIDER"
    echo "Valid options: anthropic, bedrock, openai"
    echo ""
fi

# Create logs directory
mkdir -p logs

echo ""
echo "Starting MedAdvice v4..."
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
