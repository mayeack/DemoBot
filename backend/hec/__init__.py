"""Splunk HEC forwarding for DemoBot governance events."""
from backend.hec.config import HECConfig
from backend.hec.runtime import hec_runtime

__all__ = ["HECConfig", "hec_runtime"]
