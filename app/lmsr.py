from __future__ import annotations

import math
import logging
from typing import List

logger = logging.getLogger(__name__)

def lmsr_cost(qs: List[float], b: float) -> float:
    """
    LMSR cost function. Works even when all quantities are zero.
    
    The cost to move from state qs to qs' is:
      cost = b * (max(qs') / b + log(sum(exp((q / b)))))
    """
    if not qs or len(qs) == 0:
        return 0.0
    
    try:
        # Coerce all inputs to float explicitly
        qs_float = []
        for q in qs:
            try:
                qf = float(q)
                qs_float.append(qf)
            except (TypeError, ValueError) as e:
                logger.error(f"lmsr_cost: Failed to convert q={q} (type={type(q).__name__}) to float: {e}")
                return float("inf")
        
        b_float = float(b)
        if b_float <= 0:
            logger.error(f"lmsr_cost: b <= 0, b_float={b_float}")
            return float("inf")
        
        max_q = max(qs_float)
        m = max_q / b_float
        exps = [math.exp((qf / b_float) - m) for qf in qs_float]
        s = sum(exps)
        
        if s <= 0:
            logger.error(f"lmsr_cost: sum of exps <= 0, s={s}")
            return float("inf")
        
        result = b_float * (m + math.log(s))
        
        if math.isinf(result) or math.isnan(result):
            logger.error(f"lmsr_cost: result is inf/nan, result={result}, max_q={max_q}, m={m}, s={s}, exps={exps}")
            return float("inf")
        
        return result
    except Exception as e:
        logger.error(f"lmsr_cost exception: {type(e).__name__}: {e}, qs={qs}, b={b}, qs_float={qs_float if 'qs_float' in locals() else 'undefined'}")
        return float("inf")


def lmsr_prices(qs: List[float], b: float) -> List[float]:
    """
    LMSR implied probabilities (normalized exponentials).
    
    Returns a list of probabilities (0-1) for each outcome, summing to 1.
    """
    if not qs or len(qs) == 0:
        return []
    
    try:
        # Coerce all inputs to float explicitly
        qs_float = []
        for q in qs:
            try:
                qs_float.append(float(q))
            except (TypeError, ValueError):
                logger.error(f"Failed to convert q={q} (type={type(q)}) to float")
                return [1.0 / len(qs)] * len(qs)
        
        b_float = float(b)
        if b_float <= 0:
            return [1.0 / len(qs)] * len(qs)
        
        max_q = max(qs_float)
        m = max_q / b_float
        exps = [math.exp((q / b_float) - m) for q in qs_float]
        s = sum(exps)
        if s <= 0:
            return [1.0 / len(qs)] * len(qs)
        return [e / s for e in exps]
    except Exception as e:
        logger.error(f"lmsr_prices exception: {e}, qs={qs}, b={b}")
        return [1.0 / len(qs)] * len(qs) if qs else []


def buy_cost(qs: List[float], b: float, idx: int, dq: float) -> float:
    """Cost to buy dq shares of outcome idx. Returns inf if invalid."""
    if dq <= 0 or b <= 0 or not qs or len(qs) == 0 or idx < 0 or idx >= len(qs):
        return float("inf")
    try:
        qs_float = [float(q) for q in qs]
        b_float = float(b)
        dq_float = float(dq)
        
        qs2 = list(qs_float)
        qs2[idx] += dq_float
        
        cost1 = lmsr_cost(qs_float, b_float)
        cost2 = lmsr_cost(qs2, b_float)
        cost = cost2 - cost1
        
        if cost < 0 or math.isnan(cost) or math.isinf(cost):
            logger.warning(f"buy_cost({qs_float}, b={b_float}, idx={idx}, dq={dq_float}): cost1={cost1}, cost2={cost2}, cost={cost}")
            return float("inf")
        return cost
    except Exception as e:
        logger.error(f"buy_cost exception: {type(e).__name__}: {e}")
        return float("inf")


def sell_refund(qs: List[float], b: float, idx: int, dq: float) -> float:
    """Refund from selling dq shares of outcome idx."""
    if dq <= 0 or qs[idx] < dq:
        return 0.0
    qs2 = list(qs)
    qs2[idx] -= dq
    refund = lmsr_cost(qs, b) - lmsr_cost(qs2, b)
    return max(0.0, refund)
