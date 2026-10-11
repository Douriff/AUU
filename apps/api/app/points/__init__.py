"""Platform points (积分): a per-account balance spent only inside the platform.

Completely separate from paper trading: its own sqlite file (points.sqlite), never read or
written by any paper / strategy / H2 code, and paper P&L never touches it. No withdrawal and
no user-to-user transfer exist anywhere in this package (there is deliberately no API for them).
"""
