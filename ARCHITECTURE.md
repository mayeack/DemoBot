# MedAdvice v3 - System Architecture

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENT LAYER                             │
├─────────────────────────────────────────────────────────────────┤
│  Chat UI          Admin Dashboard       Governance Viewer        │
│  (index.html)     (admin.html)          (governance.html)        │
└────────────┬──────────────┬──────────────────┬──────────────────┘
             │              │                  │
             └──────────────┴──────────────────┘
                            │
                    ┌───────▼────────┐
                    │   NGINX/LB     │ (Optional)
                    └───────┬────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                     API GATEWAY LAYER                            │
├─────────────────────────────────────────────────────────────────┤
│                         FastAPI                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Chat Router │  │ Admin Router │  │ Health Check │          │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘          │
│         │                  │                                      │
│    ┌────▼──────────────────▼────┐                               │
│    │  Request Logging Middleware │                               │
│    └────────────┬────────────────┘                               │
└─────────────────┼──────────────────────────────────────────────┘
                  │
┌─────────────────▼─────────────────────────────────────────────┐
│                    SERVICE LAYER                               │
├───────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────┐     │
│  │         Recommendation Engine                         │     │
│  │  • AI Provider Integration                           │     │
│  │  • Prompt Management                                 │     │
│  │  • Response Formatting                               │     │
│  │  • PII Injection (Testing)                           │     │
│  └────────┬─────────────────────────────────────────────┘     │
│           │                                                     │
│  ┌────────▼──────────────┐  ┌─────────────────────────┐      │
│  │ Clarifying Questions   │  │  Escalation Rules       │      │
│  │ Service                │  │  Engine                 │      │
│  │ • Question Selection   │  │  • Emergency Detection  │      │
│  │ • Context Analysis     │  │  • Vulnerability Check  │      │
│  │ • Info Extraction      │  │  • Risk Assessment      │      │
│  └────────────────────────┘  └─────────────────────────┘      │
└───────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│                   GOVERNANCE LAYER                             │
├───────────────────────────────────────────────────────────────┤
│                    Governance Logger                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐           │
│  │ Log Request │  │ Log Response│  │Log Escalation│          │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘           │
│         │                 │                 │                  │
│         └─────────────────┴─────────────────┘                  │
│                           │                                     │
│              ┌────────────▼───────────────┐                    │
│              │   Multi-Destination Writer │                    │
│              └────┬───────┬───────┬───────┘                    │
│                   │       │       │                            │
└───────────────────┼───────┼───────┼────────────────────────────┘
                    │       │       │
        ┌───────────▼──┐ ┌──▼───┐ ┌▼─────────┐
        │  File Logs   │ │  DB  │ │ Console  │
        │              │ │      │ │          │
        │ • governance │ │Tables│ │  stdout  │
        │ • escalation │ │      │ │          │
        │ • audit      │ │      │ │          │
        │ • errors     │ │      │ │          │
        └──────────────┘ └──────┘ └──────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│                     DATA LAYER                                 │
├───────────────────────────────────────────────────────────────┤
│                      SQLite Database                           │
│  ┌────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │ conversations  │  │ ai_governance_   │  │ escalation_  │  │
│  │                │  │ logs             │  │ queue        │  │
│  └────────────────┘  └──────────────────┘  └──────────────┘  │
│  ┌────────────────┐                                           │
│  │ audit_logs     │                                           │
│  └────────────────┘                                           │
└───────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│                  EXTERNAL SERVICES                             │
├───────────────────────────────────────────────────────────────┤
│        Anthropic API / AWS Bedrock / OpenAI-Compatible APIs    │
│        Claude, Bedrock-hosted models, OpenAI, DeepSeek, etc.   │
└───────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Client Layer

#### Chat Interface (`index.html`)
- Single-page application
- Real-time message streaming
- Color-coded message types
- Session management
- Disclaimer acceptance

#### Admin Dashboard (`admin.html`)
- Real-time metrics visualization
- Escalation queue management
- Interaction log viewer
- Chart.js integration
- CSV/JSON export

#### Governance Viewer (`governance.html`)
- Session-specific governance data
- Complete audit trail
- PII/safety flag visualization
- Escalation details
- Performance metrics

### 2. API Gateway Layer

#### FastAPI Application
- RESTful API endpoints
- Automatic OpenAPI documentation
- Request validation (Pydantic)
- CORS middleware
- Static file serving

#### Middleware
- Request logging
- Error handling
- Request ID injection
- Performance monitoring

### 3. Service Layer

#### Recommendation Engine
**Responsibilities:**
- Orchestrates AI interactions
- Manages conversation context
- Calls configured AI provider
- Formats responses
- Coordinates with other services

**Key Functions:**
- `process_message()` - Main entry point
- `_generate_recommendation()` - AI provider call
- `_format_recommendation()` - Response formatting

#### Clarifying Questions Service
**Responsibilities:**
- Determines when to ask questions
- Selects appropriate questions
- Tracks questions asked
- Extracts user information

**Logic:**
- Maximum 3 questions per session
- Priority: Critical > Important
- Context-aware question selection
- Redundancy prevention

#### Escalation Rules Engine
**Responsibilities:**
- Evaluates escalation criteria
- Detects emergency symptoms
- Identifies vulnerable populations
- Assesses risk factors

**Triggers:**
- Emergency symptoms (chest pain, etc.)
- Age risk (infants, elderly)
- Pregnancy
- Multiple medications
- Self-harm ideation
- Low AI confidence
- User request

### 4. Governance Layer

#### Governance Logger
**Responsibilities:**
- Comprehensive AI interaction logging
- OpenTelemetry-compliant schemas
- Multi-destination writing
- Real-time monitoring

**Log Types:**
1. **Request Logs**
   - Input messages
   - Request parameters
   - Timestamp

2. **Response Logs**
   - Output messages
   - Token usage
   - Performance metrics
   - Safety flags

3. **Decision Logs**
   - Clarifying question decisions
   - Escalation triggers
   - Severity assessments

4. **Error Logs**
   - Exception details
   - Stack traces
   - Context information

5. **Escalation Logs**
   - Escalation reason
   - Conversation history
   - User demographics
   - Symptoms
   - Review status

6. **Audit Logs**
   - User actions
   - System events
   - Admin activities

#### Log Handlers
- **File Handler**: Rotated JSON logs
- **Database Handler**: Structured storage
- **Console Handler**: Real-time monitoring

### 5. Data Layer

#### Database Schema

**conversations**
```sql
- id (PK)
- session_id (UNIQUE, INDEXED)
- created_at (INDEXED)
- updated_at
- disclaimer_accepted
- final_severity
- escalated (INDEXED)
- messages (JSON)
- metadata (JSON)
```

**ai_governance_logs**
```sql
- id (PK)
- session_id (INDEXED)
- request_id (INDEXED)
- trace_id (INDEXED)
- timestamp (INDEXED)
- operation_name
- request_model
- response_model
- input_messages (JSON)
- output_messages (JSON)
- usage_input_tokens
- usage_output_tokens
- usage_total_tokens
- pii_detected (INDEXED)
- safety_violated (INDEXED)
- guardrail_triggered (INDEXED)
- [50+ fields following OTel spec]
```

**escalation_queue**
```sql
- id (PK)
- escalation_id (UNIQUE, INDEXED)
- session_id (INDEXED)
- timestamp (INDEXED)
- severity (INDEXED)
- reason
- conversation_history (JSON)
- symptoms (JSON)
- review_status (INDEXED)
- reviewer_id
- review_notes
```

**audit_logs**
```sql
- id (PK)
- audit_id (UNIQUE, INDEXED)
- session_id (INDEXED)
- timestamp (INDEXED)
- action (INDEXED)
- actor
- details (JSON)
- ip_address
```

### 6. External Services

#### AI Provider APIs
- Providers: Anthropic, AWS Bedrock, or OpenAI-compatible APIs
- Example models: `claude-sonnet-4-5-20250929`, `anthropic.claude-3-sonnet-20240229-v1:0`, `gpt-4o`, `deepseek-chat`
- Max tokens: 2048
- Temperature: 0.7
- System prompt: Medical guidance rules
- Streaming: Supported (not implemented)

## Data Flow

### Normal Query Flow

```
1. User enters message
   ↓
2. Frontend sends POST /api/chat/message
   ↓
3. Request Logging Middleware logs request
   ↓
4. Chat Router validates input
   ↓
5. Recommendation Engine checks if clarifying questions needed
   ├─ Yes → Return clarifying question
   └─ No → Continue
   ↓
6. Build context from conversation history
   ↓
7. Call configured AI provider with system prompt + messages
   ↓
8. Governance Logger logs request (file, DB, console)
   ↓
9. AI provider returns response
   ↓
10. Parse response (assessment, guidance, severity)
    ↓
11. Escalation Rules check if escalation needed
    ├─ Yes → Log escalation, flag case
    └─ No → Continue
    ↓
12. Random PII injection (5% chance) for testing
    ↓
13. Governance Logger logs response with all metrics
    ↓
14. Update conversation in database
    ↓
15. Return response to frontend
    ↓
16. Frontend displays message with color coding
```

### Escalation Flow

```
1. Escalation triggered by rules
   ↓
2. Create escalation record
   ↓
3. Extract symptoms from conversation
   ↓
4. Extract user demographics
   ↓
5. Governance Logger logs escalation
   ↓
6. Write to escalation_queue table
   ↓
7. Write to escalations.json file
   ↓
8. Add escalation badge to response
   ↓
9. Flag visible in Admin Dashboard
   ↓
10. Human reviewer can view and update status
```

## Security Architecture

### Authentication (Not Implemented)
For production, add:
- JWT tokens for API access
- OAuth2 for admin endpoints
- Role-based access control
- API rate limiting

### Data Protection
- Environment variables for secrets
- No credentials in code
- Secure session management
- Input validation (Pydantic)

### Compliance Considerations
- HIPAA: Additional safeguards needed
- GDPR: Data retention policies
- FDA: Not approved for medical use
- Regular security audits

## Scalability Considerations

### Current Limitations
- SQLite (single file database)
- In-memory session storage
- Single process

### Production Scaling

**Horizontal Scaling:**
```
Load Balancer
     │
     ├─ App Instance 1
     ├─ App Instance 2
     └─ App Instance N
         │
    Shared Database
    (PostgreSQL)
         │
    Redis Cache
    (Sessions)
```

**Improvements Needed:**
1. PostgreSQL instead of SQLite
2. Redis for session storage
3. Message queue for async processing
4. CDN for static assets
5. Separate logging service
6. Database connection pooling

## Monitoring & Observability

### Metrics Collected
- Total interactions
- Escalation rate
- Average latency
- Token usage
- PII detection count
- Guardrail trigger count
- Error rate
- Response time percentiles

### Log Aggregation
For production:
- Splunk/ELK stack for log aggregation
- Prometheus for metrics
- Grafana for visualization
- OpenTelemetry for distributed tracing

### Alerting
Suggested alerts:
- High escalation rate (>20%)
- High PII detection (>10%)
- High latency (>5s)
- Error rate spike
- Database connection failures

## Performance Characteristics

### Expected Latency
- Database query: <10ms
- AI provider call: 1-3s
- Total request: 1-5s
- File logging: <5ms

### Throughput
- Current: ~100 requests/minute
- With scaling: 1000+ requests/minute

### Resource Usage
- Memory: ~200MB base
- CPU: Low (mostly I/O bound)
- Disk: Grows with logs/database

## Technology Stack Summary

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | HTML/CSS/JS | User interface |
| CSS Framework | TailwindCSS | Styling |
| Charts | Chart.js | Visualization |
| API Framework | FastAPI | REST API |
| ORM | SQLAlchemy | Database access |
| Database | SQLite | Data persistence |
| AI | Anthropic, Bedrock, OpenAI-compatible APIs | LLM responses |
| Validation | Pydantic | Data validation |
| Server | Uvicorn | ASGI server |
| Python | 3.11+ | Runtime |

## Deployment Architecture

### Development
```
Local Machine
  └─ Python venv
     └─ Uvicorn (single worker)
        └─ SQLite database
```

### Production (Recommended)
```
Docker Container
  └─ Gunicorn + Uvicorn workers
     ├─ PostgreSQL (external)
     ├─ Redis (sessions)
     └─ S3 (log storage)
```

## Future Enhancements

1. **Real-time Streaming**: WebSocket support for streaming responses
2. **Multi-modal**: Image upload for symptom photos
3. **Voice Input**: Speech-to-text integration
4. **Multi-language**: i18n support
5. **Mobile Apps**: React Native apps
6. **Advanced Analytics**: ML-based drift detection
7. **A/B Testing**: Prompt optimization
8. **Integration**: EHR system integration
9. **Advanced Auth**: SSO, MFA
10. **Caching**: Response caching for common queries
