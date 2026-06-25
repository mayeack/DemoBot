"""
AI Client Abstraction Layer for MedAdvice v4

Supports multi-provider deployment:
- Anthropic: Direct API access for local development
- Bedrock: AWS Bedrock for production deployment
- OpenAI-compatible APIs: OpenAI, DeepSeek, and compatible endpoints

Usage:
    from backend.services.ai_client import get_ai_client
    from backend.config import settings
    
    client = get_ai_client(settings)
    response = client.create_message(
        messages=[{"role": "user", "content": "Hello"}],
        system="You are a helpful assistant",
        max_tokens=1024
    )
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import json
import time
import random
import logging

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Base exception for AI client errors"""
    pass


class AIClientResponse:
    """Standardized response object from AI clients"""
    
    def __init__(
        self,
        id: str,
        content: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str
    ):
        self.id = id
        self.content = content
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason
    
    @property
    def usage(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens
        }
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "model": self.model,
            "usage": self.usage,
            "stop_reason": self.stop_reason
        }


class AIClient(ABC):
    """Abstract base class for AI providers"""
    
    @abstractmethod
    def create_message(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> AIClientResponse:
        """
        Send messages and get a response from the AI model.
        
        Args:
            messages: List of message dicts with "role" and "content" keys
            system: System prompt
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0 - 1.0)
            
        Returns:
            AIClientResponse with standardized response data
        """
        pass
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name for logging"""
        pass


class AnthropicClient(AIClient):
    """Client for direct Anthropic API access (local development)"""
    
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise AIClientError("Anthropic API key is required")
        
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model = model
        logger.info(f"Initialized Anthropic client with model: {model}")
    
    @property
    def provider_name(self) -> str:
        return "anthropic"
    
    def create_message(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> AIClientResponse:
        """Create a message using Anthropic's API with retry logic"""
        from anthropic import APIStatusError
        
        max_retries = 3
        retry_delay = 1.0
        last_error = None
        
        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=messages
                )
                
                return AIClientResponse(
                    id=response.id,
                    content=response.content[0].text,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    stop_reason=response.stop_reason
                )
                
            except APIStatusError as e:
                last_error = e
                # Retry on transient errors (529, 503, 500)
                if e.status_code in (529, 503, 500):
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            f"Anthropic API error {e.status_code}, "
                            f"retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait_time)
                        continue
                raise AIClientError(f"Anthropic API error: {e}") from e
        
        raise AIClientError(f"Anthropic API error after {max_retries} retries: {last_error}")


class OpenAIClient(AIClient):
    """Client for OpenAI-compatible API access"""

    def __init__(self, api_key: str, model: str, base_url: str):
        if not api_key:
            raise AIClientError("OpenAI-compatible API key is required")

        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.base_url = base_url
        logger.info(f"Initialized OpenAI-compatible client with model: {model} and base URL: {base_url}")

    @property
    def provider_name(self) -> str:
        return "openai"

    def create_message(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> AIClientResponse:
        """Create a message using an OpenAI-compatible chat completions API with retry logic"""
        from openai import APIStatusError

        max_retries = 3
        retry_delay = 1.0
        last_error = None

        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        openai_messages.extend(messages)

        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=openai_messages,
                    max_tokens=max_tokens,
                    temperature=temperature
                )

                usage = response.usage
                input_tokens = usage.prompt_tokens if usage else 0
                output_tokens = usage.completion_tokens if usage else 0
                choice = response.choices[0]

                return AIClientResponse(
                    id=response.id,
                    content=choice.message.content or "",
                    model=response.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    stop_reason=choice.finish_reason or "stop"
                )

            except APIStatusError as e:
                last_error = e
                if e.status_code in (529, 503, 500):
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            f"OpenAI-compatible API error {e.status_code}, "
                            f"retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait_time)
                        continue
                raise AIClientError(f"OpenAI-compatible API error: {e}") from e

        raise AIClientError(f"OpenAI-compatible API error after {max_retries} retries: {last_error}")


class BedrockClient(AIClient):
    """Client for AWS Bedrock (production deployment)
    
    Supports multiple model families:
    - Anthropic Claude (anthropic.claude-*)
    - Amazon Nova (amazon.nova-*)
    - Amazon Titan (amazon.titan-*) [legacy]
    """
    
    def __init__(self, region: str, model_id: str):
        import boto3
        from botocore.config import Config
        
        # Configure retry behavior
        config = Config(
            retries={
                'max_attempts': 3,
                'mode': 'adaptive'
            }
        )
        
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=config
        )
        self.model_id = model_id
        self.region = region
        
        # Detect model family
        if model_id.startswith("anthropic."):
            self.model_family = "claude"
        elif model_id.startswith("amazon.nova"):
            self.model_family = "nova"
        elif model_id.startswith("amazon.titan"):
            self.model_family = "titan"
        else:
            self.model_family = "unknown"
            
        logger.info(f"Initialized Bedrock client with model: {model_id} (family: {self.model_family}) in region: {region}")
    
    @property
    def provider_name(self) -> str:
        return "bedrock"
    
    def _build_claude_request(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int,
        temperature: float
    ) -> str:
        """Build request body for Anthropic Claude models"""
        return json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages
        })
    
    def _parse_claude_response(self, result: Dict[str, Any]) -> AIClientResponse:
        """Parse response from Anthropic Claude models"""
        return AIClientResponse(
            id=result.get("id", f"bedrock-{int(time.time())}"),
            content=result["content"][0]["text"],
            model=self.model_id,
            input_tokens=result["usage"]["input_tokens"],
            output_tokens=result["usage"]["output_tokens"],
            stop_reason=result.get("stop_reason", "end_turn")
        )
    
    def _build_titan_request(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int,
        temperature: float
    ) -> str:
        """Build request body for Amazon Titan models"""
        # Combine system prompt and messages into a single input text
        # Titan uses a simple text-in/text-out format
        prompt_parts = []
        
        if system:
            prompt_parts.append(f"System: {system}\n")
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                prompt_parts.append(f"Human: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
        
        # Add assistant prompt to encourage response
        prompt_parts.append("Assistant:")
        
        input_text = "\n".join(prompt_parts)
        
        return json.dumps({
            "inputText": input_text,
            "textGenerationConfig": {
                "maxTokenCount": max_tokens,
                "temperature": temperature,
                "topP": 0.9,
                "stopSequences": ["Human:"]
            }
        })
    
    def _parse_titan_response(self, result: Dict[str, Any], input_text: str) -> AIClientResponse:
        """Parse response from Amazon Titan models"""
        # Titan response format
        output_text = result["results"][0]["outputText"]
        token_count = result["results"][0].get("tokenCount", 0)
        input_token_count = result.get("inputTextTokenCount", 0)
        completion_reason = result["results"][0].get("completionReason", "FINISH")
        
        # Map Titan completion reasons to standard format
        stop_reason_map = {
            "FINISH": "end_turn",
            "LENGTH": "max_tokens",
            "STOP_SEQUENCE": "stop_sequence"
        }
        
        return AIClientResponse(
            id=f"titan-{int(time.time())}",
            content=output_text.strip(),
            model=self.model_id,
            input_tokens=input_token_count,
            output_tokens=token_count,
            stop_reason=stop_reason_map.get(completion_reason, "end_turn")
        )
    
    def _create_nova_message(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int,
        temperature: float
    ) -> AIClientResponse:
        """Create message using Amazon Nova via Converse API"""
        from botocore.exceptions import ClientError
        
        # Build messages for Converse API
        converse_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            converse_messages.append({
                "role": role,
                "content": [{"text": content}]
            })
        
        # Build system prompt
        system_prompts = []
        if system:
            system_prompts.append({"text": system})
        
        try:
            response = self.client.converse(
                modelId=self.model_id,
                messages=converse_messages,
                system=system_prompts,
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                    "topP": 0.9
                }
            )
            
            # Parse Nova/Converse response
            output_message = response.get("output", {}).get("message", {})
            content_blocks = output_message.get("content", [])
            output_text = content_blocks[0].get("text", "") if content_blocks else ""
            
            usage = response.get("usage", {})
            stop_reason = response.get("stopReason", "end_turn")
            
            return AIClientResponse(
                id=f"nova-{int(time.time())}",
                content=output_text,
                model=self.model_id,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                stop_reason=stop_reason
            )
            
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            raise AIClientError(
                f"Bedrock API error ({error_code}): {error_message}"
            ) from e
    
    def create_message(
        self,
        messages: List[Dict[str, str]],
        system: str,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> AIClientResponse:
        """Create a message using AWS Bedrock's API"""
        from botocore.exceptions import ClientError
        
        # Nova uses the Converse API
        if self.model_family == "nova":
            return self._create_nova_message(messages, system, max_tokens, temperature)
        
        # Build request based on model family for invoke_model API
        if self.model_family == "claude":
            body = self._build_claude_request(messages, system, max_tokens, temperature)
        elif self.model_family == "titan":
            body = self._build_titan_request(messages, system, max_tokens, temperature)
        else:
            raise AIClientError(f"Unsupported model family for model: {self.model_id}")
        
        try:
            response = self.client.invoke_model(
                modelId=self.model_id,
                body=body,
                contentType="application/json",
                accept="application/json"
            )
            
            result = json.loads(response["body"].read())
            
            # Parse response based on model family
            if self.model_family == "claude":
                return self._parse_claude_response(result)
            elif self.model_family == "titan":
                return self._parse_titan_response(result, body)
            
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            raise AIClientError(
                f"Bedrock API error ({error_code}): {error_message}"
            ) from e
        except json.JSONDecodeError as e:
            raise AIClientError(f"Failed to parse Bedrock response: {e}") from e
        except KeyError as e:
            raise AIClientError(f"Unexpected Bedrock response format: missing {e}") from e


def get_ai_client(settings) -> AIClient:
    """
    Factory function to get the appropriate AI client based on settings.
    
    Args:
        settings: Settings object with ai_provider and related configuration
        
    Returns:
        AIClient instance (AnthropicClient, BedrockClient, or OpenAIClient)
        
    Raises:
        AIClientError: If provider is unknown or configuration is invalid
    """
    provider = settings.ai_provider.lower()
    
    if provider == "bedrock":
        logger.info(f"Creating Bedrock client for region: {settings.aws_region}")
        return BedrockClient(
            region=settings.aws_region,
            model_id=settings.bedrock_model_id
        )
    elif provider == "openai":
        logger.info(f"Creating OpenAI-compatible client for base URL: {settings.openai_base_url}")
        return OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url
        )
    elif provider == "ollama":
        # Legacy-engine path. The agentic engine talks to Ollama natively via
        # langchain-ollama; here we reuse the OpenAI-compatible client because
        # Ollama exposes an OpenAI-compatible API at <base_url>/v1. The api_key is
        # ignored by Ollama but the OpenAI SDK requires a non-empty value. This
        # also prevents an import-time crash: RecommendationEngine is constructed
        # at module import (backend/routers/chat.py) and builds this client eagerly.
        ollama_base = settings.ollama_base_url.rstrip("/") + "/v1"
        logger.info(f"Creating Ollama (OpenAI-compatible) client for base URL: {ollama_base}")
        return OpenAIClient(
            api_key="ollama",
            model=settings.ollama_model,
            base_url=ollama_base
        )
    elif provider == "anthropic":
        logger.info("Creating Anthropic client")
        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model
        )
    else:
        raise AIClientError(
            f"Unknown AI provider: {provider}. "
            f"Valid options are 'anthropic', 'bedrock', 'openai', or 'ollama'"
        )
