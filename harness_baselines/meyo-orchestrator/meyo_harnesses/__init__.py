"""meyo_harnesses · omnigent community harness 插件包 (2026-08-25)

ADR-001:fork omnigent 作归一层,本包非侵入注册 9 家 harness。
只提供客观事实 + 工具,**不做任何路由/能力判断**(那由 captain 模型自主)。
"""
from .catalog import (MEYO_HARNESSES, MEYO_CAPABILITIES, MeyoHarness,
                      get_harness, get_capability, list_keys, list_capability_keys)

__all__ = ["MEYO_HARNESSES", "MEYO_CAPABILITIES", "MeyoHarness",
           "get_harness", "get_capability", "list_keys", "list_capability_keys"]
