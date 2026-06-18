"""Splunk HEC forwarding for MedAdvice governance events."""
from backend.hec.config import HECConfig
from backend.hec.runtime import hec_runtime

__all__ = ["HECConfig", "hec_runtime"]
