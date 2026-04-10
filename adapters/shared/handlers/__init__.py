"""Shared handler classes — extracted from AWS and local servers.

Each handler class receives dependencies via constructor injection (agent/ interfaces only).
The server-specific wiring (auth, tenant resolution, adapter lookup) stays in each server.
"""
