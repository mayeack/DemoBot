# MedAdvice v3

A macOS-compatible medical guidance web application built with Python, FastAPI, and configurable AI providers. Provides general medical guidance with strict safety guardrails and comprehensive AI governance logging.

## Features

### Core Functionality
- **Natural Language Medical Queries**: Chat interface for submitting medical questions
- **Clarifying Questions**: AI asks up to 3 targeted questions to gather necessary information
- **Medical Recommendations**: General, non-prescriptive health guidance including:
  - Preliminary assessments
  - Lifestyle adjustments
  - OTC medication suggestions
  - When to seek professional care
  - Severity classification (LOW, MEDIUM, HIGH, EMERGENCY)

### Safety & Compliance
- **Mandatory Medical Disclaimer**: Users must accept before starting consultation
- **Automatic Escalation**: Flags consultations for human review when:
  - Emergency symptoms detected
  - Vulnerable populations (infants <2 years, pregnant, elderly with complex conditions)
  - Potential drug interactions identified
  - Self-harm ideation expressed
  - Low AI confidence
  - User explicitly requests professional review
- **Safety Guardrails**: Never provides prescription dosages or pediatric medication advice
- **Natural PII/PHI Integration**: Realistic synthetic patient data naturally woven into responses for governance testing (5% rate, configurable)

### AI Governance Logging
Comprehensive logging following OpenTelemetry semantic conventions:

- **Core Operation Tracking**: Model identity, request/response IDs, operation names
- **Input/Output Logging**: Full message history, system instructions, tool definitions
- **Performance Metrics**: Token usage, latency, time-to-first-token
- **Safety Monitoring**: PII detection, guardrail triggers, policy violations
- **Evaluation/TEVV**: Confidence scores, drift metrics, evaluation results
- **Multi-Destination Logging**: File, database, and console output with rotation

## Architecture

### Backend Stack
- **Python 3.11+**
- **FastAPI**: REST API framework
- **LangChain**: AI orchestration
- **Configurable AI Providers**: Anthropic, AWS Bedrock, or OpenAI-compatible APIs
- **SQLAlchemy**: ORM for database management
- **SQLite**: Embedded database

### Frontend Stack
- **HTML5 + TailwindCSS**: Responsive UI
- **Vanilla JavaScript**: Real-time chat interface with streaming
- **Chart.js**: Metrics visualization

### Database Schema
- **conversations**: Session and message storage
- **ai_governance_logs**: Comprehensive AI interaction logs
- **escalation_queue**: Cases requiring human review
- **audit_logs**: System audit trail

## Installation

### Prerequisites
- Python 3.11 or higher
- macOS (primary target, but works on Linux/Windows)
- Credentials for your configured AI provider

### Setup

1. **Clone/Download the project**
```bash
cd ~/medadvice_v3
```

2. **Create virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate  # On macOS/Linux
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Configure environment**
```bash
cp .env.example .env
# Edit .env and add credentials for your configured AI provider
```

5. **Initialize database**
```bash
# Database will be created automatically on first run
```

## Running the Application

### Development Mode
```bash
# From project root
python -m backend.main

# Or using uvicorn directly
uvicorn backend.main:app --reload --port 8001
```

### Production Mode
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8001 --workers 4
```

### Access the Application
- **Chat Interface**: http://localhost:8001/app
- **Admin Dashboard**: http://localhost:8001/admin-ui
- **Governance Logs**: http://localhost:8001/governance-ui
- **API Docs**: http://localhost:8001/docs
- **Health Check**: http://localhost:8001/health

## API Endpoints

### Chat API
- `POST /api/chat/session/new` - Create new session
- `POST /api/chat/message` - Send message
- `GET /api/chat/session/{session_id}` - Get session history
- `GET /api/chat/disclaimer` - Get medical disclaimer

### Admin API
- `GET /admin/logs/interactions` - Get AI interaction logs
- `GET /admin/logs/escalations` - Get escalation queue
- `GET /admin/logs/metrics` - Get system metrics
- `GET /admin/logs/export` - Export logs (JSON/CSV)
- `GET /admin/governance/session/{id}` - Get complete session governance data
- `PUT /admin/escalations/{id}/review` - Update escalation review status

## Governance Logging

### Log Files
All logs are stored in the `logs/` directory:

- `ai_governance.json`: All AI interactions with full telemetry
- `escalations.json`: Escalated cases requiring review
- `audit_trail.json`: System audit events
- `errors.json`: Error logs
- `application.log`: Application-level logs

### Log Rotation
- **Max File Size**: 10MB (configurable)
- **Retention**: 90 days (configurable)
- **Format**: JSON lines for easy parsing

### Database Logging
All governance logs are also stored in SQLite tables with indexes for:
- Timestamp queries
- Session lookups
- Safety flag filtering
- Performance analysis

## Configuration

Edit `.env` file to customize:

```env
# AI Provider Configuration
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929

# OpenAI-compatible example:
# AI_PROVIDER=openai
# OPENAI_API_KEY=your_key_here
# OPENAI_MODEL=gpt-4o
# OPENAI_BASE_URL=https://api.openai.com/v1
#
# DeepSeek example:
# OPENAI_MODEL=deepseek-chat
# OPENAI_BASE_URL=https://api.deepseek.com

# Server
PORT=8001
DEBUG=True

# Logging
LOG_LEVEL=INFO
LOG_TO_FILE=True
LOG_TO_CONSOLE=True
LOG_TO_DATABASE=True
LOG_ROTATION_SIZE=10485760  # 10MB
LOG_RETENTION_DAYS=90

# Safety
PII_INJECTION_RATE=0.05  # 5% for testing
MAX_CLARIFYING_QUESTIONS=3

# Session
SESSION_TIMEOUT_MINUTES=30
```

## Safety Features

### Emergency Detection
Automatically detects and escalates:
- Chest pain
- Difficulty breathing
- Loss of consciousness
- Severe bleeding
- Stroke symptoms
- Self-harm ideation
- Anaphylaxis
- And more...

### Vulnerable Population Protection
Special handling for:
- Infants and toddlers (<2 years)
- Pregnant individuals
- Elderly with complex conditions

### Medication Safety
- Detects potential drug interactions
- Never provides prescription dosages
- Escalates for pediatric medication questions

## Monitoring & Analytics

### Metrics Dashboard
The admin interface provides:
- Total interactions count
- Escalation rate
- Average response latency
- Token usage statistics
- PII detection count
- Guardrail trigger frequency
- Severity distribution
- Recent interaction logs

### Governance Tracking
Every AI interaction logs:
- Complete input/output
- Token usage and costs
- Performance metrics
- Safety violations
- PII detection
- Confidence scores
- Escalation triggers

## Development

### Project Structure
```
medadvice_v3/
├── backend/
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration management
│   ├── routers/
│   │   ├── chat.py          # Chat endpoints
│   │   └── admin.py         # Admin endpoints
│   ├── services/
│   │   ├── recommendation_engine.py
│   │   ├── clarifying_questions.py
│   │   └── escalation_rules.py
│   ├── models/
│   │   ├── schemas.py       # Pydantic models
│   │   └── db_models.py     # SQLAlchemy models
│   ├── database/
│   │   └── db.py            # Database initialization
│   ├── logging/
│   │   ├── governance_logger.py
│   │   ├── log_handlers.py
│   │   └── log_schemas.py
│   └── middleware/
│       └── request_logging.py
├── frontend/
│   ├── index.html           # Chat interface
│   ├── admin.html           # Admin dashboard
│   ├── governance.html      # Governance viewer
│   └── js/
│       ├── chat.js
│       └── admin.js
├── logs/                    # Log files (auto-created)
├── requirements.txt
├── .env.example
└── README.md
```

### Adding Custom Escalation Rules
Edit `backend/services/escalation_rules.py`:

```python
# Add to EMERGENCY_SYMPTOMS list
EMERGENCY_SYMPTOMS = [
    "your_new_symptom",
    # ...
]
```

### Customizing Clarifying Questions
Edit `backend/services/clarifying_questions.py`:

```python
CRITICAL_QUESTIONS = [
    {
        "category": "custom_question",
        "condition": lambda text: "keyword" in text,
        "question": "Your question here?"
    }
]
```

## Testing

### Manual Testing
1. Start the application
2. Navigate to http://localhost:8001/app
3. Accept disclaimer
4. Test various scenarios:
   - Simple symptom query
   - Emergency symptoms (should escalate)
   - Medication questions
   - Unclear queries (should ask clarifying questions)

### Check Logs
```bash
# View governance logs
cat logs/ai_governance.json | jq .

# View escalations
cat logs/escalations.json | jq .

# Monitor real-time
tail -f logs/application.log
```

### API Testing
Use the interactive API docs at http://localhost:8001/docs

## Security Considerations

### Production Deployment
- Use HTTPS (TLS/SSL certificates)
- Set `DEBUG=False` in production
- Use environment variables for secrets
- Implement authentication for admin endpoints
- Configure CORS appropriately
- Use production database (PostgreSQL recommended)
- Enable rate limiting
- Regular security audits

### Data Privacy
- All conversations are logged for safety monitoring
- Implement data retention policies
- HIPAA compliance requires additional safeguards
- Ensure proper PII handling and anonymization
- Regular audits of escalated cases

## Troubleshooting

### Database Issues
```bash
# Delete and recreate database
rm medadvice.db
python -m backend.main
```

### Import Errors
```bash
# Ensure you're in the virtual environment
source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

### API Key Issues
```bash
# Verify API key is set for your selected provider
echo $ANTHROPIC_API_KEY
echo $OPENAI_API_KEY

# Or check .env file
cat .env | grep -E 'AI_PROVIDER|ANTHROPIC_API_KEY|OPENAI_API_KEY'
```

## License

This is a demonstration application. Not licensed for medical use without proper regulatory approval.

## Disclaimer

**THIS SOFTWARE IS FOR DEMONSTRATION PURPOSES ONLY.**

This application is NOT:
- A replacement for professional medical care
- FDA approved
- HIPAA compliant out-of-the-box
- Licensed for clinical use
- Intended for diagnosis or treatment

Always consult qualified healthcare professionals for medical advice.

## Support

For issues or questions:
1. Check the logs in `logs/` directory
2. Review the admin dashboard metrics
3. Check governance logs for specific sessions
4. Review this README

## Version History

**v3.0.0** (2026-01-15)
- Initial release
- Comprehensive AI governance logging
- Escalation system with human review queue
- Multi-destination logging (file, DB, console)
- OpenTelemetry-compliant log schema
- Admin dashboard with metrics
- Governance log viewer
- Safety guardrails and natural PII/PHI integration for testing

## Additional Documentation

- **[PII Integration Guide](PII_INTEGRATION.md)** - Detailed documentation on natural PII/PHI integration feature
- **[Quick Reference](QUICK_REFERENCE_PII.md)** - Quick guide for developers on PII integration
- **[Implementation Summary](IMPLEMENTATION_SUMMARY.md)** - Technical implementation details and changes
- **[Testing Guide](TESTING_GUIDE.md)** - Comprehensive testing procedures
- **[Architecture](ARCHITECTURE.md)** - System architecture documentation
