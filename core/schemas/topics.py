"""Canonical inter-agent topic names (CLAUDE.md §6.1)."""
from __future__ import annotations

from enum import Enum


class Topic(str, Enum):
    SCREENING_CANDIDATES = "screening.candidates"
    MARKET_STATE = "market.state"
    SIGNAL_ENTRY = "signal.entry"
    SIGNAL_EXIT = "signal.exit"
    RISK_APPROVED = "risk.decision.approved"
    RISK_REJECTED = "risk.decision.rejected"
    ORDER_EVENT = "order.event"
    ORDER_FAILED = "order.failed"
    CEO_COMMAND = "ceo.command"
    LEARNING_PROPOSAL = "learning.proposal"
