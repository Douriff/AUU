"""Active paper book for a request. Unset means the system journal.

The autopaper loop never sets this, so Go/No-Go and shadow compare stay on
the system journal.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.paper.ledger import PaperTradeJournal, get_paper_journal

_BOOK: ContextVar[Optional[PaperTradeJournal]] = ContextVar("auu_user_book", default=None)
_SHANGHAI = timezone(timedelta(hours=8))


def using_user_book() -> bool:
    return _BOOK.get() is not None


def current_book() -> PaperTradeJournal:
    book = _BOOK.get()
    return book if book is not None else get_paper_journal()


def push_book(book: PaperTradeJournal) -> Token:
    return _BOOK.set(book)


def pop_book(token: Token) -> None:
    _BOOK.reset(token)


def user_day_pnl(book: PaperTradeJournal) -> float:
    start = datetime.now(_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    total = 0.0
    for trade in book.closed:
        if int(getattr(trade, "exit_ts", 0) or 0) >= start_ms:
            total += float(getattr(trade, "pnl", 0.0) or 0.0)
    return total
