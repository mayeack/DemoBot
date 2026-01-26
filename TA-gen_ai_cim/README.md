# TA-gen_ai_cim: Gen AI Common Information Model Technology Add-on

## Overview

TA-gen_ai_cim provides search-time normalization of generative AI inference, safety, governance, and evaluation telemetry into a unified Common Information Model (CIM) aligned with OpenTelemetry Gen AI Semantic Conventions.

## Scope

- **Index**: `index=gen_ai_cim` (all normalization applies only to this index)
- **Providers Supported**: Anthropic, OpenAI, AWS Bedrock, Google Gen AI, Azure OpenAI, Local/Custom Models
- **Normalization Type**: Search-time only (no index-time transformations)
- **Namespace**: All normalized fields use the `gen_ai.*` prefix

## Normalized Schema

### Core Operation & Identity
- `gen_ai.operation.name`: Logical operation (chat, generate_content, embeddings, etc.)
- `gen_ai.provider.name`: Provider identifier (anthropic, openai, aws.bedrock, etc.)
- `gen_ai.request.model`: Model requested
- `gen_ai.response.model`: Model that served the response
- `gen_ai.response.id`: Unique completion/response ID
- `gen_ai.conversation.id`: Session/thread ID for correlation
- `gen_ai.deployment.id`: Deployment or endpoint identifier
- `gen_ai.request.id`: Application-level request ID
- `trace_id`: Distributed trace ID

### Input/Output Payload
- `gen_ai.input.messages`: Structured input chat history (JSON)
- `gen_ai.output.messages`: Structured model responses (JSON)
- `gen_ai.system_instructions`: System/instruction messages (JSON)
- `gen_ai.tool.definitions`: Tool/function definitions (JSON)
- `gen_ai.output.type`: Output modality (text, json, image, speech)

### Request Parameters
- `gen_ai.request.max_tokens`: Maximum output tokens
- `gen_ai.request.temperature`: Sampling temperature
- `gen_ai.request.top_p`: Nucleus sampling parameter
- `gen_ai.request.frequency_penalty`: Frequency penalty
- `gen_ai.request.presence_penalty`: Presence penalty
- `gen_ai.request.stop_sequences`: Stop sequences (multi-value)
- `gen_ai.response.finish_reasons`: Finish reasons (multi-value)
- `gen_ai.request.choice.count`: Number of completions requested
- `gen_ai.request.seed`: Random seed for deterministic generation

### Usage, Performance & Cost
- `gen_ai.usage.input_tokens`: Input token count
- `gen_ai.usage.output_tokens`: Output token count
- `gen_ai.usage.total_tokens`: Total tokens (input + output)
- `gen_ai.client.operation.duration`: Client-side operation duration (seconds)
- `gen_ai.server.request.duration`: Server-side request duration
- `gen_ai.server.time_per_output_token`: Time per output token
- `gen_ai.server.time_to_first_token`: Time to first token (TTFT)

### Safety, Guardrails & Policy
- `gen_ai.safety.violated`: Boolean flag for safety violations
- `gen_ai.safety.categories`: Safety violation categories (multi-value)
- `gen_ai.guardrail.triggered`: Boolean flag for guardrail triggers
- `gen_ai.guardrail.ids`: Guardrail IDs that triggered (multi-value)
- `gen_ai.pii.detected`: Boolean flag for PII detection
- `gen_ai.pii.types`: Types of PII detected (multi-value)
- `gen_ai.pii.risk_score`: MLTK-generated PII risk score (0-1)
- `gen_ai.policy.blocked`: Boolean flag for policy blocks
- `gen_ai.prompt_injection.detected`: Boolean flag for prompt injection
- `gen_ai.prompt_injection.risk_score`: MLTK-generated injection risk score (0-1)

### Evaluation & Drift (TEVV)
- `gen_ai.evaluation.name`: Evaluation metric name
- `gen_ai.evaluation.score.value`: Numeric evaluation score
- `gen_ai.evaluation.score.label`: Label/category for score
- `gen_ai.evaluation.explanation`: Human-readable explanation
- `gen_ai.drift.metric.name`: Drift metric name
- `gen_ai.drift.metric.value`: Drift metric value
- `gen_ai.drift.status`: Drift status (stable, warning, critical)

### Error & Infrastructure
- `error.type`: Error type/code
- `server.address`: Server address
- `server.port`: Server port

### Actor & Application Context
- `enduser.id`: End user identifier
- `service.name`: Application/service name
- `client.address`: Client IP or hostname

## Installation

1. Copy the TA to your Splunk instance:
   ```bash
   cp -r TA-gen_ai_cim $SPLUNK_HOME/etc/apps/
   ```

2. Restart Splunk or reload apps:
   ```bash
   $SPLUNK_HOME/bin/splunk restart
   # OR
   $SPLUNK_HOME/bin/splunk reload apps
   ```

3. Verify installation in Splunk Web → Settings → Data → Lookups & Field Definitions

## Usage

Once installed, all events in `index=gen_ai_cim` will automatically have normalized fields available at search time:

```spl
index=gen_ai_cim 
| table gen_ai.provider.name gen_ai.request.model gen_ai.usage.total_tokens gen_ai.safety.violated
```

## Governance Components

### Pre-Built Alerts
- Safety Violation Detection
- PII/PHI Detection
- Model Drift Critical
- Latency Outliers
- High-Cost Operations
- Prompt Injection Detection

### MLTK Models
- `pii_response_model`: Detects PII/PHI in responses
- `prompt_injection_model`: Detects adversarial prompt patterns

### Dashboard Panels
- Safety Events Timeline
- PII Categories by Volume
- Drift Status by Deployment
- Cost Trend Analysis
- Latency Comparison Across Providers
- MLTK Risk Scores Distribution

## Provider-Specific Mappings

See `default/props.conf` for detailed provider-specific field mappings.

## Support

For issues or questions, refer to the inline documentation in configuration files.

## Version

1.0.0 - Initial release

## License

Internal Use Only
