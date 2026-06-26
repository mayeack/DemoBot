#!/bin/bash

# DemoBot v3 - Installation Verification Script

echo "╔════════════════════════════════════════════════╗"
echo "║   DemoBot v3 - Installation Verification    ║"
echo "╚════════════════════════════════════════════════╝"
echo ""

ERRORS=0
WARNINGS=0

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check functions
check_file() {
    if [ -f "$1" ]; then
        echo -e "${GREEN}✓${NC} Found: $1"
        return 0
    else
        echo -e "${RED}✗${NC} Missing: $1"
        ((ERRORS++))
        return 1
    fi
}

check_dir() {
    if [ -d "$1" ]; then
        echo -e "${GREEN}✓${NC} Directory: $1"
        return 0
    else
        echo -e "${RED}✗${NC} Missing directory: $1"
        ((ERRORS++))
        return 1
    fi
}

check_python() {
    if command -v python3 &> /dev/null; then
        VERSION=$(python3 --version | cut -d' ' -f2)
        echo -e "${GREEN}✓${NC} Python installed: $VERSION"

        # Check version is 3.11+
        MAJOR=$(echo $VERSION | cut -d. -f1)
        MINOR=$(echo $VERSION | cut -d. -f2)
        if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 11 ]; then
            echo -e "${GREEN}✓${NC} Python version is 3.11 or higher"
        else
            echo -e "${YELLOW}⚠${NC}  Python version should be 3.11 or higher"
            ((WARNINGS++))
        fi
        return 0
    else
        echo -e "${RED}✗${NC} Python 3 not found"
        ((ERRORS++))
        return 1
    fi
}

echo "Checking Python installation..."
check_python
echo ""

echo "Checking project structure..."
check_dir "backend"
check_dir "backend/routers"
check_dir "backend/services"
check_dir "backend/models"
check_dir "backend/database"
check_dir "backend/logging"
check_dir "backend/middleware"
check_dir "frontend"
check_dir "frontend/js"
check_dir "logs"
echo ""

echo "Checking backend files..."
check_file "backend/main.py"
check_file "backend/config.py"
check_file "backend/routers/chat.py"
check_file "backend/routers/admin.py"
check_file "backend/services/recommendation_engine.py"
check_file "backend/services/clarifying_questions.py"
check_file "backend/services/escalation_rules.py"
check_file "backend/models/schemas.py"
check_file "backend/models/db_models.py"
check_file "backend/database/db.py"
check_file "backend/logging/governance_logger.py"
check_file "backend/logging/log_handlers.py"
check_file "backend/logging/log_schemas.py"
check_file "backend/middleware/request_logging.py"
echo ""

echo "Checking frontend files..."
check_file "frontend/index.html"
check_file "frontend/admin.html"
check_file "frontend/governance.html"
check_file "frontend/js/chat.js"
check_file "frontend/js/admin.js"
echo ""

echo "Checking configuration files..."
check_file "requirements.txt"
check_file ".env.example"
check_file "run.sh"
echo ""

echo "Checking documentation..."
check_file "README.md"
check_file "QUICKSTART.md"
check_file "ARCHITECTURE.md"
check_file "TESTING_GUIDE.md"
check_file "PROJECT_SUMMARY.md"
echo ""

# Check .env file
echo "Checking environment configuration..."
if [ -f ".env" ]; then
    echo -e "${GREEN}✓${NC} .env file exists"

    if grep -q "ANTHROPIC_API_KEY=sk-" .env 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Anthropic API key appears to be configured"
    else
        echo -e "${YELLOW}⚠${NC}  Anthropic API key may not be configured"
        echo "   Please edit .env and add your API key"
        ((WARNINGS++))
    fi
else
    echo -e "${YELLOW}⚠${NC}  .env file not found"
    echo "   Run: cp .env.example .env"
    echo "   Then edit .env to add your ANTHROPIC_API_KEY"
    ((WARNINGS++))
fi
echo ""

# Count files
echo "Project statistics..."
PY_FILES=$(find . -name "*.py" ! -path "./venv/*" | wc -l | tr -d ' ')
HTML_FILES=$(find . -name "*.html" | wc -l | tr -d ' ')
JS_FILES=$(find . -name "*.js" | wc -l | tr -d ' ')
MD_FILES=$(find . -name "*.md" | wc -l | tr -d ' ')

echo "  Python files: $PY_FILES"
echo "  HTML files: $HTML_FILES"
echo "  JavaScript files: $JS_FILES"
echo "  Documentation files: $MD_FILES"
echo ""

# Check if virtual environment exists
echo "Checking virtual environment..."
if [ -d "venv" ]; then
    echo -e "${GREEN}✓${NC} Virtual environment exists"
else
    echo -e "${YELLOW}⚠${NC}  Virtual environment not found"
    echo "   Will be created when you run ./run.sh"
    ((WARNINGS++))
fi
echo ""

# Final summary
echo "════════════════════════════════════════════════"
echo "Verification Summary:"
echo ""
if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed!${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Copy .env.example to .env (if not done)"
    echo "2. Edit .env and add your ANTHROPIC_API_KEY"
    echo "3. Run: ./run.sh"
    echo "4. Open: http://localhost:8001/app"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}⚠ Warnings: $WARNINGS${NC}"
    echo -e "${GREEN}✓ No critical errors${NC}"
    echo ""
    echo "Installation is mostly complete."
    echo "Address warnings above before running."
else
    echo -e "${RED}✗ Errors: $ERRORS${NC}"
    echo -e "${YELLOW}⚠ Warnings: $WARNINGS${NC}"
    echo ""
    echo "Installation is incomplete."
    echo "Please fix errors above."
fi
echo "════════════════════════════════════════════════"

exit $ERRORS
