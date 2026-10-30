#!/usr/bin/env python3
"""同题身份规范化：生产参考与质量运行共用的单一生成器。"""
from __future__ import annotations

import json


def canonical(*, input_value: object, constraints: object, expected_user_outcome: object) -> dict:
    if isinstance(constraints, str):
        try:
            parsed = json.loads(constraints)
            constraints = parsed
        except json.JSONDecodeError:
            constraints = constraints.strip()
    if isinstance(constraints, (list, dict)):
        normalized_constraints = json.dumps(constraints, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":"))
    else:
        normalized_constraints = str(constraints or "").strip()
    return {"input": str(input_value or "").strip(),
            "constraints": normalized_constraints,
            "expected_user_outcome": str(expected_user_outcome or "").strip()}
